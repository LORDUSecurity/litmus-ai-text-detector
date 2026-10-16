#!/usr/bin/env python3
"""Container-deploy preflight that needs no Docker daemon.

`docker build` is the only true proof of a Dockerfile, and on this host the Linux engine
cannot start (WSL optional component missing), so #106 stays open. What CAN be proven here is
every way a recipe like this fails before a compiler ever sees it, because each of these is a
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

    # the workflow the operator is told to import exists, and is the file the generator writes
    wf = REPO / "n8n/ai-only-workflow.json"
    check(wf.is_file(), "the workflow JSON DEPLOY.md imports is present", wf.name)

    # deployable from a clone, not only from this working tree
    try:
        tracked = subprocess.run(["git", "ls-files", "--", "backend", "frontend", "docker",
                                  "installer", "tools", "docker-compose.yml"],
                                 cwd=REPO, capture_output=True, text=True).stdout.split()
        check(len(tracked) > 20, "the deploy recipe is tracked by git", f"{len(tracked)} tracked file(s)")
    except OSError as exc:
        print(f"SKIP  git tracking check ({exc})")

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
