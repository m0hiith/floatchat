"""Stage 2a: the demo float set -- ten WMOs chosen from the 254 candidates.

The *pick* is editorial: a human read the Stage 1 candidate tables and chose
these ten.  The *check* is not.  Re-running this script re-reads
float_candidates.csv and proves the set still covers everything the project
promised a judge it would cover:

  - both data modes, and a float that changes mode mid-life (D-only, R-only, mixed)
  - both named demo regions, plus the equatorial band between them
  - the Indian DAC (incois), not only foreign ones
  - more than one profiler type, so the ETL is not tuned to one instrument

Each float carries the reason it is in the set.  If a WMO ever stops
satisfying its reason -- the candidate table is regenerated from a newer GDAC
index and the float's mode mix changed -- this script fails loudly instead of
the demo quietly losing coverage.

Output: data/index/demo_floats.csv  (the set, joined with its Stage 1 stats)
"""

import sys
from pathlib import Path

import pandas as pd

DATA = Path(__file__).resolve().parent.parent / "data" / "index"
CANDIDATES = DATA / "float_candidates.csv"
OUT = DATA / "demo_floats.csv"

# wmo, dac, and the single reason this float earns a slot.  Ten floats, 939
# profiles.  Ordered by region so the table reads like the demo will.
DEMO_FLOATS = [
    # --- Arabian Sea ---
    ("6903139", "coriolis", "richest trajectory in the box (289 profiles) and both modes in one float"),
    ("2902203", "incois",   "Indian DAC, fully delayed-mode"),
    ("2902201", "incois",   "Indian DAC caught mid-transition: 39 D, 34 R"),
    ("2902273", "incois",   "Indian DAC, real-time only -- the uncalibrated counterpart to 2902203"),
    ("7901136", "incois",   "profiler type 836, not the 846 the other incois floats use"),
    # --- Bay of Bengal ---
    ("2902766", "csio",     "solidly Bay of Bengal at 15.2N 88.0E, delayed-mode"),
    ("6990608", "incois",   "Bay of Bengal AND Indian DAC -- the two coverage axes at once"),
    ("2902770", "csio",     "second Bay of Bengal track (12.3N 89.1E) for a region query with >1 float"),
    # --- equatorial band ---
    ("2902397", "aoml",     "equatorial band, delayed-mode, a third DAC"),
    ("2903143", "aoml",     "equatorial, real-time only, profiler type 876 -- the rarest instrument in the set"),
]

STAT_COLS = [
    "wmo", "dac", "n_prof", "n_delayed", "n_realtime", "dm_status",
    "first_seen", "last_seen", "lat_mean", "lon_mean", "approx_area",
    "profiler_type", "institution",
]


def load() -> pd.DataFrame:
    """Join the editorial pick onto the Stage 1 candidate stats."""
    if not CANDIDATES.exists():
        sys.exit(f"missing {CANDIDATES} -- run etl/filter_index.py first")

    cands = pd.read_csv(CANDIDATES, dtype={"wmo": str})
    picked = pd.DataFrame(DEMO_FLOATS, columns=["wmo", "dac", "reason"])

    merged = picked.merge(cands, on=["wmo", "dac"], how="left", indicator=True)
    missing = merged[merged["_merge"] != "both"]
    if not missing.empty:
        for r in missing.itertuples():
            print(f"NOT IN CANDIDATES: {r.wmo} ({r.dac})", file=sys.stderr)
        sys.exit("a demo float is not in the candidate table -- the pick is stale")
    return merged.drop(columns="_merge")


def check(df: pd.DataFrame) -> list[str]:
    """Return a list of failed coverage promises.  Empty list means all held."""
    failures = []

    modes = set(df.dm_status)
    for want in ("D-only", "R-only", "mixed"):
        if want not in modes:
            failures.append(f"no {want} float in the set")

    areas = set(df.approx_area)
    for want in ("Arabian Sea~", "Bay of Bengal~", "equatorial"):
        if want not in areas:
            failures.append(f"no float in {want}")

    if "incois" not in set(df.dac):
        failures.append("no incois (Indian DAC) float")

    n_types = df.profiler_type.nunique()
    if n_types < 3:
        failures.append(f"only {n_types} profiler type(s); want >= 3")

    # Each float must still be worth ETL'ing on its own.
    thin = df[df.n_prof < 20]
    for r in thin.itertuples():
        failures.append(f"{r.wmo} has only {r.n_prof} profiles in the window")

    return failures


def main():
    df = load()

    print(f"demo float set  : {len(df)} floats, {df.n_prof.sum():,} profiles "
          f"({df.n_delayed.sum():,} D / {df.n_realtime.sum():,} R)\n")

    print(f"  {'WMO':<10}{'DAC':<9}{'prof':>5}{'D':>5}{'R':>5}  {'status':<7}"
          f"{'type':>5}  {'lat':>6}{'lon':>7}  {'area':<15}reason")
    for r in df.itertuples():
        print(f"  {r.wmo:<10}{r.dac:<9}{r.n_prof:>5}{r.n_delayed:>5}{r.n_realtime:>5}  "
              f"{r.dm_status:<7}{r.profiler_type:>5}  {r.lat_mean:>6.1f}{r.lon_mean:>7.1f}  "
              f"{r.approx_area:<15}{r.reason}")

    def tally(col):
        return {str(k): int(v) for k, v in df[col].value_counts().items()}

    print("\ncoverage")
    print(f"  data modes     : {tally('dm_status')}")
    print(f"  areas          : {tally('approx_area')}")
    print(f"  DACs           : {tally('dac')}")
    print(f"  profiler types : {tally('profiler_type')}")
    print(f"  window covered : {df.first_seen.min()[:10]} .. {df.last_seen.max()[:10]}")

    failures = check(df)
    if failures:
        print("\nCOVERAGE FAILURES")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nall coverage promises hold.")

    df[["wmo", "dac", "reason"] + [c for c in STAT_COLS if c not in ("wmo", "dac")]] \
        .to_csv(OUT, index=False)
    print(f"wrote {OUT}")
    print("\nNext: etl/fetch_profiles.py downloads <wmo>_prof.nc for each of these.")


if __name__ == "__main__":
    main()
