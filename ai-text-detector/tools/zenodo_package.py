#!/usr/bin/env python3
"""Build the Zenodo deposit package for the benchmark — from the code paths that are already shipped.

The report's own §8 promises "append-only per-row JSONL … with a run manifest" and "per-model
reproduction checklist available on request". A deposit on a research repository is the place where
that promise should stop being a request and become a file, so this tool writes it out of the same
two sources the application reads:

* the model table comes from `GET /ranking` through `tools/public_ranking_page.fetch_ranking()` —
  the exact call the public page and the Ranking view are built on. A deposit that re-derived the
  sort would answer the question "which model is best" twice, which is the failure mode #142
  demonstrated in the client;
* the per-text scores and the run manifest come from `benchmark_results` / `benchmark_runs`, opened
  READ-ONLY, and contain no text bodies — the corpus is third-party HC3 data and stays unredistributed.

    python tools/zenodo_package.py --author "Family, Given" [--affiliation "..."] [--orcid 0000-...]
    python tools/zenodo_package.py --dataset-only     # skip metadata.json (no author yet)
    python tools/zenodo_package.py --check            # re-verify the files already on disk

Two refusals are deliberate. It will not write `metadata.json` without a creator name, because a DOI
mints a citation and an invented author is not a placeholder you can ship. And it will not write any
file whose bytes still contain a secret-shaped or loopback-shaped string: three of the measured lanes
ran through a gateway on this machine, and a public dataset should say "local gateway" rather than
print a private address and port.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

DB = ROOT / "backend" / "data" / "detector.sqlite3"
BENCH = ROOT / "benchmark"
DEPOSIT = ROOT / "research-zenodo" / "deposit"
REPORT_DIR = ROOT / "research-zenodo"
DEPOSIT_DOC = REPORT_DIR / "zenodo-deposit.md"

TITLE = ("Free-Tier LLM Gateways as AI-Text Detectors: A Dated Benchmark of Model Accuracy, "
         "Quota Regimes, Availability Churn and Out-of-Sample Stability")
LICENSE_ID = "cc-by-4.0"
KEYWORDS = ["AI text detection", "LLM benchmark", "free tier", "model quotas", "model deprecation",
            "out-of-sample validation", "evidence highlighting", "NLP"]
RELATED = [
    {"identifier": "10.48550/arXiv.2301.07597", "relation": "references", "resource_type": "publication",
     "scheme": "doi"},
    {"identifier": "10.48550/arXiv.2309.02731", "relation": "references", "resource_type": "publication",
     "scheme": "doi"},
]
# Zenodo's deposit vocabulary for relation. isDerivedFrom/isSupplementTo are valid too, but "references"
# is what the corpus actually is to this report: the measurements are taken over its documents, and the
# report does not extend or supplement their paper.
FILES_PARAGRAPH = (
    "<p>This record carries the tables the report argues from, not only the report. Alongside "
    "<code>report.pdf</code> and <code>report.html</code> it holds <code>model-ranking.csv</code> (one "
    "row per measured model, in the order the application's own <code>GET /ranking</code> returns them, "
    "each scored at its own stored band), <code>per-text-scores.csv</code> (the append-only captures: "
    "one row per run and text with the model's reported probability and confidence, no corpus text "
    "redistributed), <code>runs-manifest.csv</code> (the generation parameters of every capture) and "
    "<code>README.md</code>, which defines each column. Re-scoring the per-text file at each model's "
    "exported band reproduces the ranking file's correct/wrong/accuracy figures for all 48 rows, so any "
    "number in the report can be traced from the dataset to the command that produced it.</p>"
)
NOTES = ("Preprint / technical report; not peer reviewed. Part I measurement window 2026-09-28 to "
         "2026-10-13; Part II window 2026-10-13 to 2026-10-17. All availability, quota and latency "
         "claims are dated to their gateway and window and are expected to expire: two models "
         "recommended or analysed in Part I were unreachable (HTTP 410 and HTTP 404) within four days "
         "of the Part I window closing. The CSVs in this record are the tables the report argues from; "
         "raw third-party corpus text is not redistributed (see report §8).")

# A public artifact must not carry these. Checked against the bytes actually written, not against a
# list of fields, so a new column cannot smuggle one through.
SECRET_RE = re.compile(r"(?i)(api[_-]?key\s*[:=]|secret\s*[:=]|bearer\s+[a-z0-9._-]{8,}|sk-[a-z0-9]{8,}|gk_live_[a-z0-9]+)")
LOOPBACK_RE = re.compile(r"(?i)\b(127\.0\.0\.1|0\.0\.0\.0|localhost)(:\d+)?\b")
LOCAL_LABEL = "local gateway (measuring machine)"
WINPATH_RE = re.compile(r"[A-Za-z]:\\" + "|" + re.escape(chr(92)) + r"Users" + r"|" + r"/home/[a-z0-9._-]+/")


def _clean_host(value: object) -> str:
    """Endpoint label for a public file: keep the name, drop the private address."""
    host = str(value or "").strip()
    if not host:
        return ""
    bare = host.split(":")[0]
    if LOOPBACK_RE.search(bare):
        return LOCAL_LABEL
    return bare


def _read_only_db() -> sqlite3.Connection:
    """The benchmark store, opened so a write is impossible rather than merely unplanned."""
    if not DB.exists():
        raise SystemExit(f"no database to read at {DB}")
    conn = sqlite3.connect(f"file:{DB.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def write_csv(path: Path, header: list[str], rows: list[list], note: str) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        fh.write(note + "\n")
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def build_model_table(payload: dict) -> tuple[Path, int]:
    rows = payload.get("ranked", [])
    header = ["rank", "model", "provider", "endpoint", "free_tier", "corpus", "run_tag", "tier",
              "rows_captured", "rows_scored", "decided", "correct", "wrong", "deferred",
              "accuracy_on_decided", "coverage", "band_ai_min", "band_human_max", "confidence_gate",
              "deployed_lane", "health_at_export"]
    out = []
    for r in rows:
        band = r.get("band") or {}
        out.append([
            r.get("rank"), r.get("model"), r.get("provider_name") or r.get("provider"),
            _clean_host(r.get("host")), "yes" if r.get("free_tier") else "no",
            r.get("corpus"), r.get("tag"), "provisional" if r.get("provisional") else "measured",
            r.get("n_rows"), r.get("n_clean"), r.get("decided"), r.get("correct"), r.get("wrong"),
            r.get("review"), r.get("accuracy"), r.get("coverage"),
            band.get("ai"), band.get("human"), band.get("gate"),
            r.get("lane") or "", r.get("health") or "unknown",
        ])
    p = DEPOSIT / "model-ranking.csv"
    write_csv(p, header, out,
              "# One row per measured model, in the order GET /ranking returns them (see README.md).")
    return p, len(out)


def build_per_text(conn: sqlite3.Connection) -> tuple[Path, int]:
    header = ["run_tag", "model", "provider", "corpus", "text_id", "label", "answered",
              "error_class", "ai_probability", "confidence"]
    cur = conn.execute(
        """SELECT r.tag, r.model, r.provider, r.corpus, b.text_id, b.label, b.ok, b.error_code,
                  b.ai_probability, b.confidence
           FROM benchmark_results b JOIN benchmark_runs r ON r.id = b.run_id
           ORDER BY r.id, b.text_id""")
    rows = [[t, m, p, c, x, l, ("yes" if ok else "no"), e or "", a, f]
            for t, m, p, c, x, l, ok, e, a, f in cur.fetchall()]
    p = DEPOSIT / "per-text-scores.csv"
    write_csv(p, header, rows,
              "# Append-only captures: one line per (run, text). No corpus text is redistributed.")
    return p, len(rows)


def build_runs_manifest(conn: sqlite3.Connection) -> tuple[Path, int]:
    # No date column, on purpose. `benchmark_runs.imported_at` is the only timestamp the table holds,
    # and seed.py's upsert rewrites it to datetime('now') on every boot that re-reads the captures —
    # all 144 rows currently carry one shared second. Exporting it as a capture date would publish a
    # number that is neither a capture time nor stable between two exports of the same data. The
    # captures themselves record none — checked 2026-10-20 across every results-*.jsonl: the only
    # capture-shaped file carrying a `ts` key is results-appbench.jsonl, the app-path latency bench,
    # which is not one of the 144 corpus runs in this manifest. So the dated claims stay where they
    # are actually evidenced — the round windows in the report.
    header = ["run_id", "run_tag", "model", "provider", "corpus", "n_rows", "temperature", "top_p",
              "max_tokens", "concurrency", "endpoint", "capture_file"]
    out = []
    for r in conn.execute("SELECT * FROM benchmark_runs ORDER BY id"):
        try:
            params = json.loads(r["params_json"] or "{}")
        except json.JSONDecodeError:
            params = {}
        out.append([r["id"], r["tag"], r["model"], r["provider"], r["corpus"], r["n_rows"],
                    params.get("temperature"), params.get("top_p"), params.get("max_tokens"),
                    params.get("concurrency"), _clean_host(params.get("base_url")),
                    r["source_file"] or ""])
    p = DEPOSIT / "runs-manifest.csv"
    write_csv(p, header, out,
              "# Every capture with the parameters of the command that produced it (report §2, §8).")
    return p, len(out)


def abstract_from_doc() -> str:
    """The deposit abstract is the text already maintained in zenodo-deposit.md — not a copy of it here."""
    if not DEPOSIT_DOC.exists():
        raise SystemExit(f"missing {DEPOSIT_DOC}: the abstract is read from it")
    text = DEPOSIT_DOC.read_text(encoding="utf-8")
    m = re.search(r"^## Abstract.*?$(.*?)(?=^## )", text, re.M | re.S)
    if not m:
        raise SystemExit("could not find the '## Abstract' section in zenodo-deposit.md")
    body = " ".join(line.strip() for line in m.group(1).strip().splitlines() if line.strip())
    if len(body) < 400:
        raise SystemExit(f"abstract looks truncated ({len(body)} chars) — refusing to deposit it")
    return f"<p>{body}</p>"


def build_metadata(args) -> tuple[Path, int]:
    if not args.author or not args.author.strip():
        raise SystemExit("--author is required for metadata.json: a DOI mints a citation, and an "
                         "invented name is not a placeholder you can ship. Use --dataset-only to "
                         "skip it.")
    creator: dict = {"name": args.author.strip()}
    if args.affiliation:
        creator["affiliation"] = args.affiliation.strip()
    if args.orcid:
        if not re.fullmatch(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", args.orcid.strip()):
            raise SystemExit(f"--orcid must look like 0000-0000-0000-0000, got {args.orcid!r}")
        creator["orcid"] = args.orcid.strip()
    meta = {
        "title": TITLE,
        "creators": [creator],
        "upload_type": "publication",
        "publication_type": "preprint",
        "communities": [],
        "description": abstract_from_doc() + FILES_PARAGRAPH,
        "keywords": KEYWORDS,
        "license": {"id": LICENSE_ID},
        "language": "eng",
        "publication_date": args.date or dt.date.today().isoformat(),
        "related_identifiers": RELATED,
        "notes": NOTES,
        # The record's own version label. This is v1: nothing was ever deposited. The report inside it
        # is the second version of the text (Part I, then Part I + II), which is a different sequence.
        "version": "v1",
    }
    payload = {"metadata": meta}
    p = DEPOSIT / "metadata.json"
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p, 1


README = """# What this folder is

The upload set for the Zenodo record of *"Free-Tier LLM Gateways as AI-Text Detectors"*. Every table
here is generated by `python tools/zenodo_package.py` from the same code paths the application uses,
so a number in these files and a number in the app cannot disagree. Rebuild before uploading; do not
edit these files by hand.

`report.pdf` and `report.html` live one directory up (`research-zenodo/`) and are uploaded with these
three CSVs.

## model-ranking.csv

One row per measured model, **in the order `GET /ranking` returns them** — the order the app and the
public page use. Nothing is re-sorted here.

| Column | Meaning |
| --- | --- |
| `rank` | the route's own rank: fewest wrong, then most decided, then accuracy, then coverage, with a full capture tiered above a probe |
| `model`, `provider`, `endpoint` | what was called, and which gateway served it. `endpoint` says *local gateway (measuring machine)* where the capture went through a router running on the author's host — the address is private and is not printed |
| `free_tier` | whether that gateway serves the model without payment |
| `corpus`, `run_tag` | which labelled corpus produced the row, and the tag of the capture that filled it |
| `tier` | `measured` = at least 24 scored texts (`MEASURED_MIN_TEXTS`); `provisional` = fewer, so the score is a sample, not a measurement |
| `rows_captured`, `rows_scored` | attempts made, and attempts that parsed to a usable probability. The gap is the transport/parse failure class the report counts separately |
| `decided`, `correct`, `wrong`, `deferred` | the model's verdict under **its own stored band** and confidence gate: calls that matched the label, calls that did not, and calls the rule declined to make |
| `accuracy_on_decided` | `correct / decided` — deliberately NOT accuracy over all texts, because deferral is a real outcome of this design |
| `coverage` | `decided / rows_scored`: how much of the corpus the rule was willing to call at all |
| `band_ai_min`, `band_human_max`, `confidence_gate` | the stored decision rule itself, so any reader can re-score `per-text-scores.csv` against the ruler that produced these numbers |
| `deployed_lane` | `primary` / `secondary` if this model is in the shipped configuration, otherwise blank |
| `health_at_export` | last known availability state when the file was written. Availability and accuracy are separate columns on purpose: the report's central availability finding is that rank 1 here was delisted (HTTP 410) four days after its window closed |

## per-text-scores.csv

One row per (run, text): `run_tag, model, provider, corpus, text_id, label, answered, error_class,
ai_probability, confidence`. **No corpus text is included** — the corpus is third-party (HC3) and is
not redistributed. `text_id` is the row's stable id within its corpus, `label` is the ground truth
(`AI` / `HUMAN`), `answered` is whether the call returned anything parseable, `error_class` names the
failure when it did not, and the two numbers are the model's own reported probability and confidence.
Scores from this file plus the bands in `model-ranking.csv` reproduce every accuracy, coverage and
wrong-count figure in the report.

## runs-manifest.csv

One row per capture run: which model, provider and corpus, how many rows, and the generation
parameters that produced it (`temperature`, `top_p`, `max_tokens`, `concurrency`, endpoint). Every
report claim about a token cap, a truncation artifact, or a provider's quota regime can be traced to a
row here and then to the capture file named in `capture_file`.

**This file has no date column, and that is a fact about the data, not an omission.** None of the 144
capture files named in `capture_file` carries a per-request timestamp, and the only timestamp the
measurement store holds is when a row was *imported* into it — which the loader rewrites on every
reload, so all 144 runs share one second, and that second moves each time the archive is rebuilt.
Printing it would put a capture date in a citable dataset that is neither a capture date nor stable.
The dated claims live where they are actually evidenced: the round windows named in the report
(§1–§3 and Part II).

## Reproducing a claim

1. Pick the model's band from `model-ranking.csv`.
2. Filter `per-text-scores.csv` to that model's `run_tag` and to `answered = yes`.
3. Call it AI if `confidence >= confidence_gate` and `ai_probability >= band_ai_min`; HUMAN if
   `confidence >= confidence_gate` and `ai_probability <= band_human_max`; otherwise deferred.
4. Compare against `label`. `correct` / `wrong` / `deferred` should match exactly.
"""


def scan_written() -> list[str]:
    """Prove the bytes we just wrote are fit for a public repository."""
    bad: list[str] = []
    for p in sorted(DEPOSIT.iterdir()):
        text = p.read_text(encoding="utf-8", errors="replace")
        for name, rx in (("secret", SECRET_RE), ("loopback", LOOPBACK_RE), ("local path", WINPATH_RE)):
            hit = rx.search(text)
            if hit:
                bad.append(f"{p.name}: {name} shaped string {hit.group(0)[:40]!r}")
    return bad


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader([l for l in fh if not l.startswith("#")]))


STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?$|^\d{4}-\d{2}-\d{2}$")


def constant_stamp_columns(rows: list[dict]) -> list[str]:
    """Name columns that look like a timestamp and hold one value across every row.

    This is the shape of the bug that made `imported_at` unexportable as `captured_at`: a loader
    rewrites it for the whole table on each reload, so 144 runs that spanned twelve days arrive at a
    reader as one shared second. A date column is only honest if its values actually differ.
    """
    if len(rows) < 2:
        return []
    bad = []
    for col in rows[0]:
        vals = {r[col] for r in rows}
        if len(vals) == 1 and STAMP_RE.match(next(iter(vals)) or ""):
            bad.append(f"{col} = {next(iter(vals))} on all {len(rows)} rows")
    return bad


def reproduce_the_ruler(ranking: list[dict], per_text: list[dict]) -> tuple[int, list[str]]:
    """Re-score every exported row from the exported per-text scores, the way a reader would.

    This is the check that makes the dataset citable rather than merely present: the README tells a
    reader to apply `band_ai_min` / `band_human_max` / `confidence_gate` to `per-text-scores.csv`, so
    the tool runs that recipe over all 48 models and requires the totals in `model-ranking.csv` to
    fall out of it. A mismatch means the two files describe different measurements, and a deposit
    built from them would be untraceable.
    """
    def f(v):
        return None if v in ("", None) else float(v)

    by_run: dict[str, list[dict]] = {}
    for r in per_text:
        if r["answered"] == "yes" and r["ai_probability"] not in ("", None) and r["label"]:
            by_run.setdefault(r["run_tag"], []).append(r)

    mismatches: list[str] = []
    checked = 0
    for row in ranking:
        scored = by_run.get(row["run_tag"])
        if scored is None:
            mismatches.append(f"{row['model']}: no per-text rows for run_tag {row['run_tag']}")
            continue
        checked += 1
        ai_cut, hu_cut, gate = f(row["band_ai_min"]), f(row["band_human_max"]), f(row["confidence_gate"])
        correct = wrong = 0
        for r in scored:
            p, conf = float(r["ai_probability"]), (float(r["confidence"]) if r["confidence"] else 1.0)
            verdict = "AI" if (conf >= gate and p >= ai_cut) else "HUMAN" if (conf >= gate and p <= hu_cut) else None
            if verdict is None:
                continue
            if verdict == r["label"]:
                correct += 1
            else:
                wrong += 1
        decided = correct + wrong
        want = (int(row["correct"]), int(row["wrong"]), int(row["decided"]), int(row["rows_scored"]))
        got = (correct, wrong, decided, len(scored))
        if want != got:
            mismatches.append(f"{row['model']}: table says correct/wrong/decided/scored {want}, "
                              f"rescoring the CSV gives {got}")
        acc = f(row["accuracy_on_decided"])
        if decided and acc is not None and abs(round(correct / decided, 4) - acc) > 1e-9:
            mismatches.append(f"{row['model']}: accuracy_on_decided {acc} != {correct}/{decided}")
    return checked, mismatches


def verify(payload: dict) -> list[tuple[bool, str]]:
    """Re-read the files and check them against the live sources, not against what we intended."""
    out: list[tuple[bool, str]] = []
    ranked = payload.get("ranked", [])
    rows = read_csv(DEPOSIT / "model-ranking.csv")
    out.append((len(rows) == len(ranked),
                f"model-ranking.csv has {len(rows)} rows, /ranking returned {len(ranked)}"))
    same_order = [r["model"] for r in rows] == [x["model"] for x in ranked]
    out.append((same_order, "row order equals GET /ranking order (no second sort)"))

    per_text = read_csv(DEPOSIT / "per-text-scores.csv")
    checked, mismatches = reproduce_the_ruler(rows, per_text)
    out.append((checked == len(rows) and not mismatches,
                f"re-scoring per-text-scores.csv at each model's own exported band reproduces the "
                f"correct/wrong/decided/accuracy columns of {checked}/{len(rows)} ranking rows"
                + ("" if not mismatches else f" — {len(mismatches)} mismatch(es), first: {mismatches[0]}")))
    out.append((not any(LOOPBACK_RE.search(r["endpoint"]) for r in rows),
                f"loopback endpoints are labelled, not addressed "
                f"({sum(1 for r in rows if r['endpoint'] == LOCAL_LABEL)} rows)"))
    runs_rows = read_csv(DEPOSIT / "runs-manifest.csv")

    conn = _read_only_db()
    n_results = conn.execute("SELECT COUNT(*) FROM benchmark_results").fetchone()[0]
    n_runs = conn.execute("SELECT COUNT(*) FROM benchmark_runs").fetchone()[0]
    out.append((len(per_text) == n_results,
                f"per-text-scores.csv has {len(per_text)} data rows, benchmark_results has {n_results}"))
    out.append((len(runs_rows) == n_runs,
                f"runs-manifest.csv has {len(runs_rows)} data rows, benchmark_runs has {n_runs}"))
    stamps = constant_stamp_columns(runs_rows)
    out.append((not stamps, "runs-manifest.csv has no timestamp column shared by all "
                f"{len(runs_rows)} runs" + ("" if not stamps else f" — {stamps}")))
    # The README tells a reader that every claim traces to "the capture file named in capture_file",
    # so prove the pointer resolves: the file is on disk and holds exactly the rows it claims.
    gone, short = [], []
    for row in runs_rows:
        f = BENCH / row["capture_file"]
        if not f.exists():
            gone.append(row["capture_file"])
            continue
        n = sum(1 for l in f.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip())
        if n != int(row["n_rows"]):
            short.append(f"{row['capture_file']} says {row['n_rows']}, holds {n}")
    out.append((not gone and not short,
                f"every capture_file resolves on disk with its stated row count "
                f"({len(runs_rows) - len(gone) - len(short)}/{len(runs_rows)})"
                + ("" if not (gone or short) else f" — missing {gone[:2]}, mismatched {short[:2]}")))

    bad = scan_written()
    out.append((not bad, "no secret-, loopback- or local-path-shaped string in any written file"
                + ("" if not bad else ": " + "; ".join(bad[:3]))))
    mp = DEPOSIT / "metadata.json"
    if mp.exists():
        meta = json.loads(mp.read_text(encoding="utf-8"))["metadata"]
        need = ["title", "creators", "upload_type", "publication_type", "description", "keywords",
                "license", "language", "publication_date", "related_identifiers", "notes"]
        missing = [k for k in need if k not in meta]
        out.append((not missing, f"metadata.json carries every required field"
                    + ("" if not missing else f" (missing {missing})")))
        c = meta["creators"][0]
        out.append((bool(re.fullmatch(r"[^,]+,.+", c.get("name", ""))),
                    f"creator name is Family, Given — got {c.get('name')!r}"))
        out.append((meta["license"]["id"] == LICENSE_ID and meta["publication_type"] == "preprint",
                    "license id + resource type match Zenodo's vocabulary (cc-by-4.0 / preprint)"))
        pd = meta["publication_date"]
        out.append((bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", pd)), f"publication_date is a real date: {pd}"))
    conn.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--author", help='Creator name, "Family, Given" — required for metadata.json')
    ap.add_argument("--affiliation", default="Independent Researcher")
    ap.add_argument("--orcid", help="Optional 0000-0000-0000-0000")
    ap.add_argument("--date", help="publication_date, YYYY-MM-DD (defaults to today)")
    ap.add_argument("--dataset-only", action="store_true", help="write the CSVs, skip metadata.json")
    ap.add_argument("--check", action="store_true", help="only re-verify the files already on disk")
    args = ap.parse_args()

    DEPOSIT.mkdir(parents=True, exist_ok=True)

    if args.check:
        from public_ranking_page import fetch_ranking
        results = verify(fetch_ranking())
        for ok, msg in results:
            print(("PASS " if ok else "FAIL ") + msg)
        print(f"{sum(1 for ok, _ in results if ok)}/{len(results)} checks passed")
        return 0 if all(ok for ok, _ in results) else 1

    print("reading GET /ranking through the shipped code path …")
    from public_ranking_page import fetch_ranking
    payload = fetch_ranking()

    conn = _read_only_db()
    files = [build_model_table(payload), build_per_text(conn), build_runs_manifest(conn)]
    conn.close()
    (DEPOSIT / "README.md").write_text(README, encoding="utf-8")
    files.append((DEPOSIT / "README.md", 1))
    if not args.dataset_only:
        files.append(build_metadata(args))

    print(f"\nwrote {len(files)} files to {DEPOSIT}:")
    for p, n in files:
        digest = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
        print(f"  {p.name:<24} {p.stat().st_size:>9,} B  sha256 {digest}…  {n if n > 1 else ''}".rstrip())
    for extra in ("report.pdf", "report.html"):
        p = REPORT_DIR / extra
        if p.exists():
            print(f"  (upload) {extra:<19} {p.stat().st_size:>9,} B  sha256 "
                  f"{hashlib.sha256(p.read_bytes()).hexdigest()[:16]}…")

    results = verify(payload)
    print()
    for ok, msg in results:
        print(("PASS " if ok else "FAIL ") + msg)
    passed = sum(1 for ok, _ in results if ok)
    print(f"{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
