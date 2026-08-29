#!/usr/bin/env python
"""Run the whole FloatChat pipeline, in order, and say what happened.

    python run_pipeline.py              # run every stage, skipping cached work
    python run_pipeline.py --from 3     # start at stage 3
    python run_pipeline.py --only 6     # one stage
    python run_pipeline.py --check      # just the verification suites
    python run_pipeline.py --fresh      # re-download and rebuild everything

Each stage is an ordinary script that prints its own report; this file only
decides what to run and reports how it went.  Stages are skipped when their
output is already on disk, so a re-run costs seconds rather than 155 MB.

The point of this file is D1.1's claim: clone the repo, make a venv,
`pip install -r requirements.txt`, run this, and you have the database.
"""

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable


@dataclass
class Stage:
    id: str
    title: str
    script: str
    produces: str | None = None      # if this exists, the stage is already done
    args: tuple[str, ...] = ()
    is_check: bool = False
    # Only the three downloading stages take --force.  Passing it to a stage
    # that does not parse arguments would be silently ignored, which is exactly
    # the kind of quiet no-op this project refuses to ship.
    accepts_force: bool = False


STAGES = [
    Stage("1a", "fetch the GDAC global profile index", "etl/fetch_index.py",
          "data/index/ar_index_global_prof.txt.gz", accepts_force=True),
    Stage("1b", "filter it to candidate floats", "etl/filter_index.py",
          "data/index/float_candidates.csv"),
    Stage("2a", "choose and check the demo float set", "etl/demo_floats.py",
          "data/index/demo_floats.csv"),
    Stage("2b", "download and verify their NetCDF files", "etl/fetch_profiles.py",
          "data/profiles/manifest.json", accepts_force=True),
    Stage("3", "parse NetCDF into flat tables", "etl/parse_profiles.py",
          "data/parsed/levels.csv"),
    Stage("5a", "build the IHO region polygons", "etl/fetch_regions.py",
          "data/regions/regions.csv", accepts_force=True),
    Stage("4", "load Postgres and verify", "etl/load_db.py"),
    Stage("11", "build the vector index over the summaries", "etl/build_index.py",
          "data/rag/manifest.json", accepts_force=True),
    Stage("6", "query catalogue tests", "api/test_catalog.py", is_check=True),
    Stage("7", "natural-language tool loop tests", "api/test_chat.py", is_check=True),
    Stage("8", "Gemini adapter tests", "api/test_gemini.py", is_check=True),
    Stage("10", "HTTP API tests", "api/test_server.py", is_check=True),
    Stage("11c", "retrieval and /ask tests", "api/test_retrieval.py", is_check=True),
    # Stage 12 has no build artefact -- the router fits its exemplars at import,
    # in milliseconds -- so it appears here only as its check suite. That suite
    # is the stage's central claim: it needs no network, no API key, no daemon
    # and no model download, and it runs on a fresh clone.
    Stage("12", "lexical router tests (no model, no key, no network)",
          "api/test_router.py", is_check=True),
    # Stage 13 has no build artefact either. The dashboard was the one surface
    # with nothing asserting anything about it, and it is where the silent
    # failures were: an axis colour Plotly 4 drops without a warning, two map
    # markers that 404 only in a production build, a tab badge that called a
    # configured model "no model". This suite reads the UI source as text, so
    # it needs no Postgres, no npm and no browser.
    Stage("13", "dashboard source checks (no database, no npm)",
          "ui/test_ui.py", is_check=True),
    # Stage 14 is the only stage that runs the dashboard rather than reading
    # it. It drives headless Chrome over the DevTools Protocol against the
    # PRODUCTION build -- D13.2's broken markers do not exist in the dev
    # server -- and needs Chrome plus `npm install`. Without them it exits 2
    # and is reported as skipped, never as ok.
    Stage("14", "dashboard rendered in Chrome (needs npm + Chrome)",
          "ui/test_render.py", is_check=True),
    # Stage 15 has no build artefact either -- deploying is something a person
    # does with the Vercel CLI, not something this file can do. What it leaves
    # behind is configuration, and configuration is where the quiet failures
    # live: an ignore file one line too long ships an API whose retrieval is
    # off, and a second requirements freeze drifts from the first in silence.
    # This suite reads that configuration and computes the upload set, so it
    # needs no Postgres, no network and no Vercel account.
    Stage("15", "deployment configuration checks (no database, no network)",
          "api/test_deploy.py", is_check=True),
]


def preflight() -> bool:
    print("preflight")
    ok = True

    v = sys.version_info
    print(f"  python              {v.major}.{v.minor}.{v.micro}")

    missing = []
    for mod in ("pandas", "numpy", "requests", "netCDF4", "xarray", "psycopg", "anthropic",
                "fastapi", "uvicorn", "faiss"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        print(f"  packages            MISSING: {', '.join(missing)}")
        print("                      pip install -r requirements.txt")
        ok = False
    else:
        print("  packages            all present")

    try:
        out = subprocess.run(["psql", "-h", "localhost", "-p", "5432", "-d", "postgres",
                              "-tAc", "select version()"],
                             capture_output=True, text=True, timeout=15)
        if out.returncode == 0:
            print(f"  postgres            {out.stdout.strip().split(' on ')[0]}")
        else:
            print(f"  postgres            NOT REACHABLE: {out.stderr.strip().splitlines()[0]}")
            ok = False
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"  postgres            NOT REACHABLE: {type(exc).__name__}")
        ok = False

    ro = ROOT / "db" / "roles.sql"
    print(f"  read-only role      apply once with: psql -d floatchat -f {ro.relative_to(ROOT)}")
    return ok


def run_stage(stage: Stage, fresh: bool) -> tuple[str, float, str]:
    target = ROOT / stage.produces if stage.produces else None
    if target and target.exists() and not fresh:
        return "cached", 0.0, f"{stage.produces} already on disk"

    args = list(stage.args) + (["--force"] if fresh and stage.accepts_force else [])
    started = time.time()
    out = subprocess.run([PY, str(ROOT / stage.script), *args],
                         capture_output=True, text=True, cwd=ROOT)
    took = time.time() - started

    lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    tail = lines[-1] if lines else ""
    if out.returncode == 2:
        # Stage 14 needs more than the others -- Chrome, and a built `ui/dist`
        # -- and exits 2 when it cannot find them. That gets its own status and
        # never "ok": a suite that ran nothing must not read like one that
        # passed, and the detail column names the missing piece.
        return "skipped", took, tail[:120]
    if out.returncode != 0:
        err = (out.stderr.strip().splitlines() or ["(no stderr)"])[-1]
        return "FAILED", took, f"{tail}  |  {err}"[:160]
    return "ok", took, tail[:120]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="start", metavar="ID", help="start at this stage id")
    ap.add_argument("--only", metavar="ID", help="run just this stage id")
    ap.add_argument("--check", action="store_true", help="run only the verification suites")
    ap.add_argument("--fresh", action="store_true", help="ignore caches and rebuild everything")
    args = ap.parse_args()

    if not preflight():
        print("\npreflight failed -- fix the above before running the pipeline.")
        return 1

    stages = STAGES
    if args.check:
        stages = [s for s in STAGES if s.is_check or s.id == "4"]
    elif args.only:
        stages = [s for s in STAGES if s.id == args.only]
        if not stages:
            print(f"\nno stage '{args.only}'. Ids: {', '.join(s.id for s in STAGES)}")
            return 1
    elif args.start:
        ids = [s.id for s in STAGES]
        if args.start not in ids:
            print(f"\nno stage '{args.start}'. Ids: {', '.join(ids)}")
            return 1
        stages = STAGES[ids.index(args.start):]

    print(f"\nrunning {len(stages)} stage(s)\n")
    print(f"  {'id':<4}{'stage':<44}{'status':<9}{'secs':>7}  detail")
    results = []
    for stage in stages:
        status, took, detail = run_stage(stage, args.fresh)
        results.append((stage, status, took))
        print(f"  {stage.id:<4}{stage.title:<44}{status:<9}{took:>7.1f}  {detail}")
        if status == "FAILED":
            print(f"\nstopped at stage {stage.id}. Run it directly for the full output:")
            print(f"  {PY} {stage.script}")
            return 1

    total = sum(t for _, _, t in results)
    ran = sum(1 for _, s, _ in results if s == "ok")
    cached = sum(1 for _, s, _ in results if s == "cached")
    skipped = [st.id for st, s, _ in results if s == "skipped"]
    print(f"\n{ran} stage(s) ran, {cached} cached, {total:.1f}s total")
    if skipped:
        # Said again at the bottom, because a skipped check is a claim NOT
        # made and the one line in the table is easy to read past.
        print(f"stage(s) {', '.join(skipped)} were SKIPPED, not passed -- "
              "see their detail line for what is missing.")
    print("\nthe database is ready:  psql -h localhost -d floatchat")
    print("ask it something     :  python api/chat.py \"how salty is the Bay of Bengal?\"")
    print("                        (needs ANTHROPIC_API_KEY or GEMINI_API_KEY;")
    print("                         everything above runs without either)")
    print("or open the dashboard:  .venv/bin/uvicorn api.server:app --port 8000")
    print("                        cd ui && npm install && npm run dev")
    print("                        No key needed for any of it: the Chat tab's")
    print("                        lexical router answers without a model.")
    print("routing, measured    :  python api/router.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
