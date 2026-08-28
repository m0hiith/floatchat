"""Stage 1b: filter the global profile index down to a candidate float list.

Reads the cached gzipped index (~3 million rows), narrows it to the study
scope, and reports the surviving row count after EVERY step.  Nothing is
dropped silently.

Scope (decisions logged in DECISIONS.md):
  ocean code   'I'  (Indian Ocean, as tagged by the GDAC)
  dates        2023-01-01 .. 2024-12-31 inclusive
  box          lat -10..30 N, lon 40..100 E  (North Indian Ocean: Arabian Sea,
               Bay of Bengal, equatorial band -- the waters our demo regions name)

Key trick: the data mode is in the FILE NAME.  A profile file is called
'<dac>/<wmo>/profiles/<R|D><wmo>_<cycle>.nc' -- the leading letter is R
(real-time) or D (delayed-mode).  So we can tell which floats are
scientist-calibrated and which are not WITHOUT downloading a single NetCDF
file.  That is how we deliberately pick a mix of both.

Outputs (data/index/):
  filtered_profiles.csv  one row per surviving profile, with dac/wmo/mode/cycle
  float_candidates.csv   one row per float, aggregated
  filter_report.json     the funnel counts, for the record
"""

import gzip
import json
import re
from pathlib import Path

import pandas as pd

DATA = Path(__file__).resolve().parent.parent / "data" / "index"
INDEX_GZ = DATA / "ar_index_global_prof.txt.gz"

OCEAN_CODE = "I"
DATE_START = pd.Timestamp("2023-01-01")
DATE_END = pd.Timestamp("2024-12-31 23:59:59")
LAT_MIN, LAT_MAX = -10.0, 30.0
LON_MIN, LON_MAX = 40.0, 100.0

CHUNK_ROWS = 500_000
MIN_PROFILES = 20          # a float needs a real time series to be a candidate
SHOW_TOP = 15

# <dac>/<wmo>/profiles/<prefix><wmo>_<cycle>[D].nc
# The trailing optional 'D' after the cycle number means a DESCENDING profile
# and has nothing to do with delayed mode -- the mode is the last letter of
# <prefix>.  Getting these two confused is a classic ARGO mistake.
FILE_RE = re.compile(
    r"^(?P<dac>[^/]+)/(?P<wmo>\d+)/profiles/"
    r"(?P<prefix>[A-Z]+)(?P=wmo)_(?P<cycle>\d+)(?P<descending>D?)\.nc$"
)


def read_header(path: Path):
    """Return (number of '#' lines, GDAC metadata dict)."""
    meta, n_comment = {}, 0
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.startswith("#"):
                break
            n_comment += 1
            if ":" in line:
                key, val = line[1:].split(":", 1)
                meta[key.strip()] = val.strip()
    return n_comment, meta


def main():
    n_comment, gdac_meta = read_header(INDEX_GZ)
    print(f"index file  : {INDEX_GZ.name}")
    print(f"gdac update : {gdac_meta.get('Date of update', '?')}")
    print(f"format ver  : {gdac_meta.get('Format version', '?')}\n")

    counts = {
        "rows_total": 0,
        "ocean_I": 0,
        "date_parsed": 0,
        "date_in_window": 0,
        "position_present": 0,
        "position_in_box": 0,
        "filename_parsed": 0,
    }
    kept = []

    reader = pd.read_csv(
        INDEX_GZ,
        skiprows=n_comment,          # the 8 '#' lines; row 9 is the real header
        header=0,
        dtype=str,                   # parse nothing implicitly; we cast on purpose
        chunksize=CHUNK_ROWS,
        compression="gzip",
    )

    for chunk in reader:
        counts["rows_total"] += len(chunk)

        chunk = chunk[chunk["ocean"] == OCEAN_CODE]
        counts["ocean_I"] += len(chunk)
        if chunk.empty:
            continue

        # JULD is not in the index; the index carries an ISO-ish 14-digit stamp.
        ts = pd.to_datetime(chunk["date"], format="%Y%m%d%H%M%S", errors="coerce")
        chunk = chunk.assign(timestamp=ts)
        chunk = chunk[chunk["timestamp"].notna()]
        counts["date_parsed"] += len(chunk)

        chunk = chunk[(chunk["timestamp"] >= DATE_START) & (chunk["timestamp"] <= DATE_END)]
        counts["date_in_window"] += len(chunk)
        if chunk.empty:
            continue

        lat = pd.to_numeric(chunk["latitude"], errors="coerce")
        lon = pd.to_numeric(chunk["longitude"], errors="coerce")
        chunk = chunk.assign(lat=lat, lon=lon)
        chunk = chunk[chunk["lat"].notna() & chunk["lon"].notna()]
        counts["position_present"] += len(chunk)

        chunk = chunk[
            chunk["lat"].between(LAT_MIN, LAT_MAX) & chunk["lon"].between(LON_MIN, LON_MAX)
        ]
        counts["position_in_box"] += len(chunk)
        if chunk.empty:
            continue

        parts = chunk["file"].str.extract(FILE_RE)
        chunk = chunk.join(parts)
        chunk = chunk[chunk["wmo"].notna()]
        counts["filename_parsed"] += len(chunk)

        chunk["mode"] = chunk["prefix"].str[-1]          # 'R' or 'D'
        chunk["cycle"] = chunk["cycle"].astype(int)
        chunk["descending"] = chunk["descending"] == "D"

        kept.append(chunk[[
            "file", "dac", "wmo", "cycle", "mode", "descending",
            "timestamp", "lat", "lon", "profiler_type", "institution",
        ]])

    prof = pd.concat(kept, ignore_index=True) if kept else pd.DataFrame()

    # ---- the funnel -------------------------------------------------------
    total = counts["rows_total"]
    print("filtering funnel (rows surviving each step)")
    print(f"  {'step':<28}{'rows':>12}{'dropped':>12}{'% of total':>12}")
    prev = total
    for label, key in [
        ("index rows read", "rows_total"),
        (f"ocean == '{OCEAN_CODE}'", "ocean_I"),
        ("date parsed", "date_parsed"),
        ("date in 2023-2024", "date_in_window"),
        ("position present", "position_present"),
        ("position in study box", "position_in_box"),
        ("filename parsed", "filename_parsed"),
    ]:
        n = counts[key]
        print(f"  {label:<28}{n:>12,}{prev - n:>12,}{100.0 * n / total:>11.4f}%")
        prev = n

    if prof.empty:
        print("\nNo profiles survived. Widen the scope in the constants at the top.")
        return

    # ---- per-float aggregation -------------------------------------------
    grp = prof.groupby(["wmo", "dac"], as_index=False)
    floats = grp.agg(
        n_prof=("cycle", "size"),
        n_delayed=("mode", lambda s: (s == "D").sum()),
        first_seen=("timestamp", "min"),
        last_seen=("timestamp", "max"),
        lat_min=("lat", "min"), lat_max=("lat", "max"), lat_mean=("lat", "mean"),
        lon_min=("lon", "min"), lon_max=("lon", "max"), lon_mean=("lon", "mean"),
        profiler_type=("profiler_type", "first"),
        institution=("institution", "first"),
    )
    floats["n_realtime"] = floats["n_prof"] - floats["n_delayed"]
    floats["dm_status"] = floats.apply(
        lambda r: "D-only" if r.n_realtime == 0
        else ("R-only" if r.n_delayed == 0 else "mixed"), axis=1
    )

    # Advisory label only, from the float's MEAN position. This is a convenience
    # for choosing candidates by eye. It is NOT the regions table -- the real
    # named-region polygons arrive at Stage 5 in PostGIS.
    def approx_area(r):
        if r.lat_mean < 5:
            return "equatorial"
        if r.lon_mean < 78:
            return "Arabian Sea~"
        if r.lon_mean < 95:
            return "Bay of Bengal~"
        return "Andaman~"
    floats["approx_area"] = floats.apply(approx_area, axis=1)

    floats = floats.sort_values("n_prof", ascending=False)

    print(f"\nsurviving profiles : {len(prof):,}")
    print(f"distinct floats    : {len(floats):,}")
    print(f"  with any D file  : {(floats.n_delayed > 0).sum():,}")
    print(f"  real-time only   : {(floats.n_delayed == 0).sum():,}")
    print(f"  incois DAC       : {(floats.dac == 'incois').sum():,}")
    print(f"date range covered : {prof.timestamp.min():%Y-%m-%d} .. {prof.timestamp.max():%Y-%m-%d}")

    # ---- candidate tables -------------------------------------------------
    cands = floats[floats.n_prof >= MIN_PROFILES]
    hdr = (f"  {'WMO':<10}{'DAC':<9}{'prof':>5}{'D':>5}{'R':>5}  {'status':<7}"
           f"{'first':<12}{'last':<12}{'lat':>7}{'lon':>7}  approx area")

    def show(df, title):
        print(f"\n{title}  (>= {MIN_PROFILES} profiles, top {SHOW_TOP} by count)")
        print(hdr)
        for r in df.head(SHOW_TOP).itertuples():
            print(f"  {r.wmo:<10}{r.dac:<9}{r.n_prof:>5}{r.n_delayed:>5}{r.n_realtime:>5}  "
                  f"{r.dm_status:<7}{r.first_seen:%Y-%m-%d}  {r.last_seen:%Y-%m-%d}  "
                  f"{r.lat_mean:>6.1f}{r.lon_mean:>7.1f}  {r.approx_area}")

    show(cands[cands.n_delayed > 0], "A. floats WITH delayed-mode (D) profiles")
    show(cands[cands.n_delayed == 0], "B. real-time-only (R) floats")

    # ---- outputs ----------------------------------------------------------
    prof.to_csv(DATA / "filtered_profiles.csv", index=False)
    floats.to_csv(DATA / "float_candidates.csv", index=False)
    report = {
        "gdac_date_of_update": gdac_meta.get("Date of update"),
        "gdac_format_version": gdac_meta.get("Format version"),
        "scope": {
            "ocean_code": OCEAN_CODE,
            "date_start": str(DATE_START), "date_end": str(DATE_END),
            "lat_min": LAT_MIN, "lat_max": LAT_MAX,
            "lon_min": LON_MIN, "lon_max": LON_MAX,
        },
        "funnel": counts,
        "distinct_floats": int(len(floats)),
        "floats_with_delayed_mode": int((floats.n_delayed > 0).sum()),
        "floats_realtime_only": int((floats.n_delayed == 0).sum()),
    }
    (DATA / "filter_report.json").write_text(json.dumps(report, indent=2))

    print(f"\nwrote {DATA/'filtered_profiles.csv'}  ({len(prof):,} rows)")
    print(f"wrote {DATA/'float_candidates.csv'}   ({len(floats):,} rows)")
    print(f"wrote {DATA/'filter_report.json'}")
    print("\nNext: pick 8-10 WMOs from the tables above -- take some from A and")
    print("some from B so the ETL is exercised on both delayed and real-time data.")


if __name__ == "__main__":
    main()
