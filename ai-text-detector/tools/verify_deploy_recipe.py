#!/usr/bin/env python3
"""Container-deploy preflight that needs no Docker daemon.

`python tools/verify_container_deploy.py` is the live half of the same story -- it drives the running
stack through nginx and is what caught the HEALTHCHECK-on-a-401 and root-owned-SQLite failures. This
file is the half that runs with no engine at all, which matters because a clone does not have this
host's volumes or keys: `docker build` is the only true proof of a Dockerfile, so what CAN be proven
here is every way a recipe like this fails before a compiler ever sees it, because each of these is a
file the build reads as a name rather than as code:

  * a COPY source that does not exist in the declared build context (build fails at that step,
    halfway through an image that looked fine);
  * a COPY source that exists on disk but is excluded by .dockerignore -- invisible to the
    daemon, so the error is "not found" even though the operator can see the file;
  * CRLF in a file a Linux container executes. The shebang becomes `#!/bin/sh\\r` and the
    container dies with `no such file or directory`, pointing at an interpreter that is there.
    backend/Dockerfile neutralises this with `sed -i 's/\\r$//'` before chmod; a config or
    host-run script COPY'd or `bash`'d raw gets no such rescue;
  * an ENTRYPOINT/HEALTHCHECK naming a path the image never receives;
  * a compose service whose build context or dockerfile is not where the file says it is;
  * a timeout chain that no longer nests -- lane ceiling + grace above gunicorn above nginx, because
    an outer tier shorter than the wait inside it silently converts a slow detection into a 502;
  * an nginx config that does not parse (a missing `;` in a header line is an `nginx -t` failure on
    the deploy host and nothing else sees it), and a request-hardening policy that covers `/api/*`
    but not the static document nginx serves itself -- see the `the hardened deploy` block;
  * a served copy of the technical report that has drifted from the copy the deposit package cites,
    or that the built bundle does not carry (the link then lands on the SPA fallback, which answers
    200 with index.html instead of the report -- a working page with a citation that is not one);
  * and, since "ready to deploy" means *someone else can clone and run it*, whether the deploy
    artifacts are actually tracked by git rather than only present in this working tree.

Usage:  python tools/verify_deploy_recipe.py
Exits non-zero with the failing checks listed; prints one line per check.
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMPOSE = REPO / "docker-compose.yml"
FAILS: list[str] = []
CHECKS = 0


def check(ok: bool, label: str, detail: str = "") -> bool:
    global CHECKS
    CHECKS += 1
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(label)
    return ok


# ---------------------------------------------------------------- .dockerignore
def ignore_matcher(path: str) -> "callable":
    """Return accepts(path) honouring Docker's last-matching-rule semantics.

    Docker patterns are matched against the context-relative path, `**` spans separators, and a
    leading `!` negates. Directory patterns (a bare name like `backend/data`) also exclude
    everything beneath them, which is the part a hand-rolled grep always gets wrong.
    """
    rules = []
    f = REPO / ".dockerignore"
    if f.is_file():
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            neg = line.startswith("!")
            pat = line[1:].strip() if neg else line
            pat = pat.lstrip("/")
            rules.append((neg, pat.rstrip("/")))

    def accepts(p: str) -> bool:
        p = p.replace("\\", "/").lstrip("./")
        allowed = True
        for neg, pat in rules:
            # match the path itself or any of its parent directories against a directory rule
            parts = p.split("/")
            hit = (fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(p, pat + "/*")
                   or any(fnmatch.fnmatch("/".join(parts[:i]), pat) for i in range(1, len(parts))))
            if hit:
                allowed = neg
        return allowed

    return accepts


# ---------------------------------------------------------------- compose
def services() -> list[dict]:
    """Cheap block scan: the file is written one service per top-level indent, and pulling in
    PyYAML here would make the preflight depend on a package the deploy host need not have."""
    text = COMPOSE.read_text(encoding="utf-8")
    out, cur = [], None
    in_services = False
    for line in text.splitlines():
        if re.match(r"^services:\s*$", line):
            in_services = True
            continue
        if in_services and re.match(r"^[a-z_]+:", line) and not line.startswith(" "):
            break
        if not in_services:
            continue
        m = re.match(r"^  ([a-zA-Z0-9_.-]+):\s*$", line)
        if m:
            cur = {"name": m.group(1), "lines": []}
            out.append(cur)
            continue
        if cur is not None:
            cur["lines"].append(line)
    return out


def val(svc: dict, key: str) -> str | None:
    m = re.search(r"^\s+" + key + r":\s*(.+?)\s*$", "\n".join(svc["lines"]), re.M)
    return m.group(1).strip('"\'') if m else None


# ---------------------------------------------------------------- Dockerfile
def copy_sources(dockerfile: Path) -> list[tuple[str, list[str]]]:
    """COPY/ADD sources, skipping --from= (a prior stage, not the context) and --flags."""
    out = []
    text = dockerfile.read_text(encoding="utf-8")
    text = re.sub(r"\\\n\s*", " ", text)          # join line continuations
    for line in text.splitlines():
        m = re.match(r"^\s*(?:COPY|ADD)\s+(.*)$", line, re.I)
        if not m:
            continue
        args, srcs, dest = m.group(1).split(), [], None
        for a in args:
            if a.startswith("--"):
                if a.startswith("--from"):
                    srcs = []                      # stage copy: nothing comes from the context
                    dest = "--from"
                continue
            srcs.append(a)
        if dest == "--from" or not srcs:
            out.append((dockerfile.name, []))
            continue
        out.append((dockerfile.name, srcs[:-1] if len(srcs) > 1 else []))
    return out


# ---------------------------------------------------------------- nginx structure
class NginxSyntax(Exception):
    pass


def nginx_statements(text: str) -> list[tuple[tuple[str, ...], str, str]]:
    """Split an nginx config into (block-stack, directive, args).

    Not a full lexer -- nginx's real one is a generated parser -- but it enforces the three things
    that actually break a deploy, none of which any other file in this repo can see: an unbalanced
    brace, an unterminated quote, and a directive that forgot its `;`. Comments (`#` to end of line,
    outside quotes) and quoted strings ('...' and "...", which may contain `;` and `#` -- the CSP and
    the JSON error body both do) are handled, because a header value that trips the scanner is
    exactly the case worth covering.
    """
    out: list[tuple[tuple[str, ...], str, str]] = []
    stack: list[str] = []
    buf, quote = "", ""
    line = 1
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\n":
            line += 1
        if quote:
            buf += c
            if c == quote:
                quote = ""
            i += 1
            continue
        if c == "#" and not buf.strip():
            i = text.find("\n", i)
            if i == -1:
                break
            continue
        if c in "'\"":
            quote = c
            buf += c
            i += 1
            continue
        if c in ";{}":
            stmt = buf.strip()
            buf = ""
            if c == "{":
                if not stmt:
                    raise NginxSyntax(f"line {line}: `{{` with no block name")
                stack.append(re.sub(r"\s+", " ", stmt))
                i += 1
                continue
            if c == "}":
                if not stack:
                    raise NginxSyntax(f"line {line}: `}}` closes nothing")
                stack.pop()
                if stmt:
                    raise NginxSyntax(f"line {line}: `{stmt[:40]}` needs `;`, not `{{`/`}}`")
                i += 1
                continue
            if not stmt:
                raise NginxSyntax(f"line {line}: stray `;`")
            name, _, args = stmt.partition(" ")
            out.append((tuple(stack), name, re.sub(r"\s+", " ", args).strip()))
            i += 1
            continue
        buf += c
        i += 1
    if quote:
        raise NginxSyntax("an unterminated quote")
    if stack:
        raise NginxSyntax(f"`{stack[-1]}` is never closed")
    return out


def unquote(nginx_value: str) -> str:
    """The value nginx would actually send: the trailing `always` flag off, one layer of quotes off.

    Order matters twice over. `"nosniff" always` has its quote at the far end, so a helper that
    strips quotes first finds nothing to strip and reports a header that is present as absent. And
    the quote layer is one matching pair, not a character class -- `strip("'\"")` also eats the
    closing quote of the last CSP directive, `... frame-ancestors 'self'"`, which then "differs" from
    the app's own policy by a single character.
    """
    nginx_value = nginx_value.removesuffix(" always").strip()
    if len(nginx_value) > 1 and nginx_value[0] == nginx_value[-1] and nginx_value[0] in "'\"":
        # One matching pair and nothing else: `.strip("\"'")` eats the closing quote of the last
        # CSP directive (`... frame-ancestors 'self'"`), so it reports a policy that differs by one
        # character from the one the app sends.
        return nginx_value[1:-1]
    return nginx_value


def in_block(statements, label: str, directive: str | None = None):
    return [(name, args) for (stack, name, args) in statements
            if stack and stack[-1] == label and (directive is None or name == directive)]


def main() -> int:
    if os.name != "nt":                           # paths below are written repo-relative anyway
        pass

    svcs = {s["name"]: s for s in services()}
    check({"backend", "web", "n8n"} <= set(svcs), "compose declares backend, web and n8n",
          ",".join(sorted(svcs)))

    for name, svc in svcs.items():
        ctx, df = val(svc, "context"), val(svc, "dockerfile")
        if not df:
            continue
        ctx_dir = (REPO / ctx).resolve()
        check(ctx_dir.is_dir(), f"{name}: build context exists", str(ctx_dir.relative_to(REPO)))
        dfile = ctx_dir / df
        check(dfile.is_file(), f"{name}: dockerfile present at {df}")
        if not dfile.is_file():
            continue

        accepts = ignore_matcher(str(dfile))
        for fname, srcs in copy_sources(dfile):
            for src in srcs:
                if "$" in src:                     # runtime interpolation, not a context path
                    continue
                has_magic = bool(re.search(r"[\*\?\[]", src))
                target = ctx_dir / src
                exists = bool(list(target.parent.glob(target.name))) if has_magic else target.exists()
                check(exists, f"{name}: COPY source {src} exists in the context",
                      "matched by glob" if has_magic and exists else str(target.relative_to(REPO)))
                if exists and not accepts(src):
                    check(False, f"{name}: COPY source {src} is excluded by .dockerignore",
                          "the daemon would not see it")

        # files this image executes, and the line endings they must not have
        copied = set()
        for line in dfile.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*(?:COPY|ADD)\s+.*\s+(/[^\s\"']+)\s*$", line)
            if m:
                copied.add(m.group(1))
        for sh in [p for p in copied if p.endswith(".sh")]:
            check(True, f"{name}: {sh} lands in the image")

    # CRLF: the two things a Linux container runs or reads raw
    for rel in ("docker/backend-entrypoint.sh", "docker/n8n-import.sh", "docker/nginx.conf"):
        p = REPO / rel
        if not p.is_file():
            check(False, f"{rel} exists")
            continue
        crlf = p.read_bytes().count(b"\r\n")
        if "backend-entrypoint" in rel:
            check(True, f"{rel}: Dockerfile strips CRLF before chmod (crlf={crlf})",
                  "sed -i 's/\\r$//' in the image build")
        else:
            check(crlf == 0, f"{rel} has no CRLF", f"{crlf} CRLF line(s)")

    # line endings are now guarded by .gitattributes, because core.autocrlf=true on this host
    # would otherwise hand every checkout to a Linux process with CRLF
    ga = REPO / ".gitattributes"
    if check(ga.is_file(), ".gitattributes pins line endings for the deploy files"):
        g = ga.read_text(encoding="utf-8")
        for pat in ("*.sh", "*.conf"):
            m = re.search(r"^\s*" + re.escape(pat) + r"\s+text\s+eol=lf", g, re.M)
            check(bool(m), f".gitattributes forces eol=lf for {pat}")
        r = subprocess.run(["git", "check-attr", "text", "eol", "--", "docker/n8n-import.sh"],
                           cwd=REPO, capture_output=True, text=True)
        check("eol: lf" in r.stdout, "git resolves the n8n import script to eol=lf",
              r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "")

    # nginx.conf must actually proxy /api and fall back to the SPA, or the SPA 404s on refresh
    ngx = (REPO / "docker/nginx.conf").read_text(encoding="utf-8", errors="replace")
    check("proxy_pass" in ngx and "api" in ngx.lower(), "nginx.conf reverse-proxies the API")
    check(re.search(r"try_files.*index\.html", ngx, re.S) is not None,
          "nginx.conf falls back to index.html for SPA routes")

    # ---------------------------------------------------------------- the hardened deploy
    #
    # In this stack nginx, not Flask, answers the document that IS the application: index.html, the
    # hashed bundles and /research/report.html are files it serves straight off disk. So the request
    # hardening has two halves that have to agree, and neither one is compiled by anything -- a
    # missing `;` in a header line is a `nginx -t` failure on the deploy host, and a CSP edited on
    # one side only is a policy that quietly stops covering the page. Both are checked here, because
    # this is the half that runs with no daemon.
    sys.path.insert(0, str(REPO))
    try:
        blocks = nginx_statements(ngx)
        check(True, "nginx.conf parses: balanced braces, closed quotes, every directive ends in `;`")
    except NginxSyntax as exc:
        blocks = []
        check(False, "nginx.conf parses", str(exc))

    # The guard needs teeth: a parser that accepts anything protects nothing, and the header checks
    # below would read an empty statement list as "absent from `location /`" forever.
    broken = {"a directive that ends in `}` not `;`": 'server { listen 80 }',
              "an unbalanced brace": 'server { location / { try_files $uri /index.html; }',
              "an unterminated quote": 'server { add_header CSP "default-src; }',
              "a stray semicolon": 'server { ; listen 80; }'}
    for label, snippet in broken.items():
        try:
            nginx_statements(snippet)
            check(False, f"the nginx parser rejects {label}")
        except NginxSyntax:
            check(True, f"the nginx parser rejects {label}")
    plain = "default-src 'self'; frame-ancestors 'self'"
    check(unquote('"nosniff" always') == "nosniff" and unquote(plain) == plain,
          "…and reads a quoted value without eating the directive's own quotes")

    static_loc = in_block(blocks, "location /")
    served_headers = {}
    for name, args in static_loc:
        if name == "add_header":
            head, _, rest = args.partition(" ")
            served_headers[head] = rest
    check(bool(static_loc), "nginx.conf keeps a `location /` block for static traffic",
          f"{len(static_loc)} directive(s)")

    from backend import security as security_mod   # the Python half of the same policy

    csp_served = served_headers.get("Content-Security-Policy", "")
    check(csp_served.endswith(" always"),
          "…with `always`, so a 404 or a 413 carries the policy too")
    check(unquote(csp_served) == security_mod.CSP,
          "…and serves the SAME Content-Security-Policy the Flask layer sets",
          f"nginx: {csp_served[:55]}… / flask: {security_mod.CSP[:55]}…")
    for header, value in security_mod.RESPONSE_HEADERS.items():
        got = served_headers.get(header, "")
        check(unquote(got) == value,
              f"…and the {header} the app sets is on static responses too",
              got[:60] or "absent from `location /`")
    check(len(served_headers) == len(security_mod.RESPONSE_HEADERS),
          "nginx adds exactly the app header set, no stale extras",
          ", ".join(sorted(served_headers)))

    api_loc = in_block(blocks, "location /api/", "limit_req")
    check(bool(api_loc), "nginx.conf rate-limits the API location",
          "; ".join(a for _, a in api_loc) or "absent")
    zone = re.search(r"limit_req_zone\s+\$(\w+)\s+zone=auth", ngx)
    mapped = re.findall(r"^\s+(/api/auth/\w+)\s+\$binary_remote_addr;", ngx, re.M)
    check(bool(zone) and sorted(mapped) == ["/api/auth/login", "/api/auth/register"],
          "…and only the two sign-in routes have a non-empty key, so a detection never queues "
          "behind someone else's login attempts",
          f"zone=${zone.group(1) if zone else '?'} mapped={mapped}")
    check(re.search(r"^\s*server_tokens off;", ngx, re.M) is not None,
          "nginx.conf does not advertise its version")

    # The timeout chain: lane -> backend wait -> gunicorn -> nginx. Every tier has to clear the one
    # inside it, or the outer layer kills a request the inner layer was still legitimately waiting
    # for. That is how a working 214 s detection came back as a 502 on 2026-10-17: the lane ceiling
    # moved twice (120 s -> 180 s -> 240 s) while the tiers above it were never re-derived from it.
    # The ceiling is read from `seed`, not from a number typed into this file, because the boot
    # re-seed rewrites `models.timeout_ms` from LANE_TIMEOUT_MS on every start.
    sys.path.insert(0, str(REPO))
    from backend import seed as seed_mod
    lane_ceiling_s = max(
        seed_mod.LANE_TIMEOUT_MS.get(m[0], seed_mod.DEFAULT_LLM["LLM_TIMEOUT_MS"]) / 1000.0
        for m in seed_mod.DEFAULT_LANES.values())
    backend_wait_s = lane_ceiling_s + 30.0            # app.py adds its own 30 s of grace
    compose_gunicorn = re.search(r'GUNICORN_TIMEOUT:\s*"\$\{GUNICORN_TIMEOUT:-(\d+)\}"',
                                 COMPOSE.read_text(encoding="utf-8", errors="replace"))
    entrypoint_gunicorn = re.search(r'TIMEOUT="\$\{GUNICORN_TIMEOUT:-(\d+)\}"',
                                    (REPO / "docker/backend-entrypoint.sh")
                                    .read_text(encoding="utf-8", errors="replace"))
    nginx_read = re.search(r"proxy_read_timeout\s+(\d+)s", ngx)
    check(bool(compose_gunicorn and entrypoint_gunicorn
                and compose_gunicorn.group(1) == entrypoint_gunicorn.group(1)),
          "compose and the container entrypoint agree on gunicorn --timeout",
          f"compose={compose_gunicorn.group(1) if compose_gunicorn else '?'} "
          f"entrypoint={entrypoint_gunicorn.group(1) if entrypoint_gunicorn else '?'}")
    if compose_gunicorn and nginx_read:
        gunicorn_s = int(compose_gunicorn.group(1))
        nginx_s = int(nginx_read.group(1))
        check(gunicorn_s > backend_wait_s, "gunicorn --timeout clears the backend's longest wait",
              f"lane {lane_ceiling_s:.0f}s + 30 = {backend_wait_s:.0f}s wait, gunicorn {gunicorn_s}s")
        check(nginx_s > gunicorn_s, "nginx proxy_read_timeout clears gunicorn",
              f"nginx {nginx_s}s > gunicorn {gunicorn_s}s")

        # One tier further down the same chain, and the nesting bug again, wearing a different
        # number. A failover request is more than one lane call, and the total is set in app.py
        # (REQUEST_BUDGET_S), not derived from the lane ceiling -- so nothing above it enforced that
        # the two actually fit together. On 2026-10-17 the request deadline was computed from the
        # pool's FIRST row (90 s + 30), which starved the 240 s replacement and turned a correct
        # switch into two non-answers.
        from backend.app import REQUEST_BUDGET_S
        check(REQUEST_BUDGET_S <= backend_wait_s,
              "a two-attempt request fits inside the backend's own wait",
              f"REQUEST_BUDGET_S {REQUEST_BUDGET_S:.0f}s vs lane+30 = {backend_wait_s:.0f}s wait")

        # The outermost tier is the browser, and it is the one no server-side file carries, so it
        # is the one nobody re-derives when a ceiling moves. It was 150 s for /detect while the
        # chain below it grew to 270 s, and 60 s for a key probe while `probe_timeout_s` grew to
        # 250 s: the inner tier gave up first, so the user saw "failed" for a request the server
        # was still honouring -- and the verdict landed in History behind the error message.
        budget = (REPO / "frontend" / "src" / "app" / "core" / "http-budget.ts") \
            .read_text(encoding="utf-8", errors="replace")
        patience = re.search(r"HTTP_PATIENCE_MS\s*=\s*([\d_]+)", budget)
        check(patience is not None and int(patience.group(1).replace("_", "")) > nginx_s * 1000,
              "the browser waits longer than the gateway it is waiting on",
              f"HTTP_PATIENCE_MS="
              f"{patience.group(1) if patience else 'missing'} vs nginx {nginx_s}s")

        # DEPLOY.md is the text an operator follows, and it quotes every one of these numbers. A doc
        # that still says the old ceiling sends someone to change a tier that is already correct.
        deploy = (REPO / "docker/DEPLOY.md").read_text(encoding="utf-8", errors="replace")
        stated = (re.search(r"lane timeout_ms` \((\d+) s on the shipped primary\)", deploy),
                  re.search(r"gunicorn `--timeout` (\d+) s", deploy),
                  re.search(r"nginx `proxy_read_timeout` (\d+) s", deploy))
        check(all(stated) and int(stated[0].group(1)) == int(lane_ceiling_s)
              and int(stated[1].group(1)) == gunicorn_s and int(stated[2].group(1)) == nginx_s,
              "DEPLOY.md quotes the chain the files actually carry",
              " ".join(m.group(1) if m else "missing" for m in stated))

    # the workflow the operator is told to import exists, and is the file the generator writes
    wf = REPO / "n8n/ai-only-workflow.json"
    check(wf.is_file(), "the workflow JSON DEPLOY.md imports is present", wf.name)

    # The technical report is served by the site itself, not only kept in a deposit folder, so the
    # bundle has a second copy of a 50 KB document. Two failure modes are worth a check each: the
    # copies drifting apart (the deposit package is what gets cited, the served copy is what users
    # read -- a silent difference is a citation to text that does not exist), and the Ranking view
    # linking a path the asset tree does not have. Both are file-name typos waiting to happen.
    site = REPO / "frontend" / "public" / "research"
    deposit = REPO / "research-zenodo"
    for fname in ("report.html", "report.pdf"):
        src, dst = deposit / fname, site / fname
        if not (src.is_file() and dst.is_file()):
            check(False, f"the served {fname} exists next to the deposit copy",
                  f"deposit={src.is_file()} served={dst.is_file()}")
            continue
        same = src.read_bytes() == dst.read_bytes()
        check(same, f"the site serves the same {fname} the deposit package holds",
              "" if same else f"drifted -- copy it: cp {src.relative_to(REPO).as_posix()} "
                              f"{dst.relative_to(REPO).as_posix()}")
    view = (REPO / "frontend" / "src" / "app" / "ui" / "ranking-view" / "ranking-view.ts") \
        .read_text(encoding="utf-8", errors="replace")
    check("/research/report.html" in view,
          "the Ranking view links the path the asset tree actually serves",
          "public/research/report.html -> /research/report.html")
    try:
        t = subprocess.run(["git", "ls-files", "--", "research-zenodo", "frontend/public/research"],
                           cwd=REPO, capture_output=True, text=True).stdout.split()
        check(sum(1 for x in t if x.endswith(("report.html", "report.pdf"))) >= 4,
              "both report copies are tracked, so a clone can build the site with the citation",
              ", ".join(sorted(x.split("/")[-2] + "/" + x.split("/")[-1] for x in t)))
    except OSError as exc:
        print(f"SKIP  git tracking of the report ({exc})")

    # a built bundle must carry the report, or the link lands on the SPA fallback instead
    dist = REPO / "frontend" / "dist" / "frontend" / "browser" / "research" / "report.html"
    if dist.is_file() and (site / "report.html").is_file():
        check(dist.read_bytes() == (site / "report.html").read_bytes(),
              "the built bundle carries the current report (rebuild after editing it)",
              str(dist.relative_to(REPO)))
    else:
        print("SKIP  built-bundle report freshness (no build output yet -- run `ng build`)")

    # deployable from a clone, not only from this working tree
    try:
        tracked = subprocess.run(["git", "ls-files", "--", "backend", "frontend", "docker",
                                  "installer", "tools", "docker-compose.yml"],
                                 cwd=REPO, capture_output=True, text=True).stdout.split()
        check(len(tracked) > 20, "the deploy recipe is tracked by git", f"{len(tracked)} tracked file(s)")
    except OSError as exc:
        print(f"SKIP  git tracking check ({exc})")

    # The container's own liveness probe: a HEALTHCHECK pointed at a route the auth guard protects
    # never passes, `depends_on: {backend: {condition: service_healthy}}` then blocks the web
    # service, and `docker compose up -d` dies with "dependency failed to start". No YAML check can
    # see that -- it needs the guard's public set and the route table. Found on the first run with a
    # real daemon (2026-10-17): the probe targeted /gateway-health, which answers 401.
    df = (REPO / "backend" / "Dockerfile").read_text(encoding="utf-8", errors="replace")
    m = re.search(r"urlopen\(['\"]http://[^/]+(/[^'\"]+)", df)
    probe = m.group(1) if m else None
    pub = set(re.findall(r'"(/[a-z0-9_-]*)"',
                         (REPO / "backend" / "auth.py").read_text(encoding="utf-8", errors="replace")
                         .split("PUBLIC_PATHS = {")[-1].split("}")[0]))
    routes = set(re.findall(r'@app\.route\("(/[a-z0-9_-]*)"',
                            (REPO / "backend" / "app.py").read_text(encoding="utf-8", errors="replace")))
    check(bool(probe), "backend Dockerfile declares a liveness URL", str(probe))
    check(bool(probe) and probe in pub,
          "…and its path is in the auth guard's PUBLIC_PATHS",
          f"probe={probe} public={sorted(pub)}")
    check(bool(probe) and probe in routes,
          "…and a route actually answers it (a public path with no route is a 404)",
          f"probe={probe} routes_with_no_auth={sorted(pub & routes)}")

    # and what a daemon would say, if one is listening
    r = subprocess.run(["docker", "compose", "-f", str(COMPOSE), "config", "--quiet"],
                       capture_output=True, text=True, cwd=REPO)
    check(r.returncode == 0, "docker compose config validates without a daemon",
          (r.stderr or "").strip()[:120] or "exit 0")

    print(f"\n{CHECKS - len(FAILS)}/{CHECKS} checks passed")
    if FAILS:
        print("failed: " + "; ".join(FAILS))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
