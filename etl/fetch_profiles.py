"""Stage 2b: download one aggregated NetCDF per demo float, and prove it.

For each float in data/index/demo_floats.csv we fetch the GDAC's per-float
aggregate:

    dac/<dac>/<wmo>/<wmo>_prof.nc

not the ~70-290 individual per-cycle files.  One request per float instead of
939 in total, and the aggregate carries the authoritative per-profile
DATA_MODE variable -- which is what Stage 4 must read anyway (see D1.5).

Downloading is not the point of this script; PROVING the download is.  For
every float it checks, against the Stage 1 index rows we already trust:

  1. does the file open, and is it the float we asked for (PLATFORM_NUMBER)?
  2. is every (cycle, direction) the index promised in our 2023-2024 window
     actually present in the file?
  3. does the file's DATA_MODE agree with the R/D letter in the index
     filename -- allowing for mode 'A', which the filename cannot express?

Anything that fails is printed and recorded.  Nothing is dropped silently.

Outputs:
  data/profiles/<dac>/<wmo>_prof.nc
  data/profiles/manifest.json
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from netCDF4 import Dataset

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "data" / "index"
DEST_ROOT = ROOT / "data" / "profiles"
DEMO_CSV = INDEX / "demo_floats.csv"
PROFILES_CSV = INDEX / "filtered_profiles.csv"
MANIFEST = DEST_ROOT / "manifest.json"

BASE_URL = "https://data-argo.ifremer.fr/dac"
CHUNK_BYTES = 1 << 20

# The index filename can only say R or D.  The file itself has a third mode:
# 'A' = real-time data with adjusted values.  An 'A' profile ships inside an
# R-prefixed file, so this is the only honest way to compare the two.
MODE_FROM_FILENAME = {"D": {"D"}, "R": {"R", "A"}}


def chars(var) -> list[str]:
    """Decode a NetCDF char variable to a flat list of stripped strings.

    ARGO char variables come in three shapes and all three matter here:
      (N_PROF,)                     DATA_MODE, DIRECTION   -- one letter each
      (N_PROF, STRING8)             PLATFORM_NUMBER        -- one string each
      (N_PROF, N_PARAM, STRING16)   STATION_PARAMETERS     -- a grid of strings
    The last axis is always the character axis, so join along it and flatten
    whatever is left.
    """
    arr = np.ma.filled(var[:], b" ")
    if arr.ndim == 1:
        return [(x.decode(errors="replace") if isinstance(x, bytes) else str(x)).strip()
                for x in arr]

    flat = arr.reshape(-1, arr.shape[-1])
    out = []
    for row in flat:
        joined = b"".join(x if isinstance(x, bytes) else str(x).encode() for x in row)
        out.append(joined.decode(errors="replace").strip())
    return out


def download(url: str, dest: Path, force: bool) -> dict:
    """Fetch url to dest unless already cached.  Returns transfer metadata."""
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists() and not force:
        return {"cached": True, "bytes": dest.stat().st_size, "last_modified": None}

    part = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=300) as resp:
        resp.raise_for_status()
        expected = int(resp.headers.get("content-length", 0))
        last_mod = resp.headers.get("last-modified")
        written = 0
        with open(part, "wb") as out:
            for block in resp.iter_content(chunk_size=CHUNK_BYTES):
                out.write(block)
                written += len(block)
                if expected:
                    print(f"\r    {written:,} / {expected:,} bytes "
                          f"({100.0 * written / expected:5.1f}%)", end="", file=sys.stderr)
        print("", file=sys.stderr)

    if expected and written != expected:
        part.unlink(missing_ok=True)
        raise IOError(f"short read: got {written:,}, expected {expected:,}")

    part.rename(dest)
    return {"cached": False, "bytes": written, "last_modified": last_mod}


def inspect(path: Path, wmo: str, expected: pd.DataFrame) -> dict:
    """Open the file and check it against the index rows we already trust."""
    problems = []
    with Dataset(path) as ds:
        n_prof = ds.dimensions["N_PROF"].size
        n_levels = ds.dimensions["N_LEVELS"].size

        platforms = set(chars(ds.variables["PLATFORM_NUMBER"]))
        if platforms != {wmo}:
            problems.append(f"PLATFORM_NUMBER is {sorted(platforms)}, expected {{{wmo}}}")

        cycles = np.ma.filled(ds.variables["CYCLE_NUMBER"][:], -1).astype(int)
        directions = chars(ds.variables["DIRECTION"])
        modes = chars(ds.variables["DATA_MODE"])
        juld = np.ma.filled(ds.variables["JULD"][:], np.nan).astype(float)

        params = sorted({p.strip() for p in chars(ds.variables["STATION_PARAMETERS"]) if p.strip()})
        variables = sorted(ds.variables)

    # ARGO reference epoch for JULD is 1950-01-01.
    stamps = pd.Timestamp("1950-01-01") + pd.to_timedelta(juld, unit="D")

    # (cycle, direction) is the real key -- a descending and an ascending
    # profile share a cycle number, so cycle alone collides.
    in_file = {(int(c), d) for c, d in zip(cycles, directions)}
    mode_of = {(int(c), d): m for c, d, m in zip(cycles, directions, modes)}

    want = {
        (int(r.cycle), "D" if r.descending else "A"): r.mode
        for r in expected.itertuples()
    }
    missing = sorted(k for k in want if k not in in_file)
    if missing:
        problems.append(f"{len(missing)} indexed profile(s) absent from the file, "
                        f"first: {missing[:5]}")

    mode_conflicts = []
    for key, filename_mode in want.items():
        file_mode = mode_of.get(key)
        if file_mode is None:
            continue
        if file_mode not in MODE_FROM_FILENAME.get(filename_mode, set()):
            mode_conflicts.append({"cycle": key[0], "direction": key[1],
                                   "filename": filename_mode, "file": file_mode})
    if mode_conflicts:
        problems.append(f"{len(mode_conflicts)} DATA_MODE disagreement(s) with the index filename")

    mode_counts = {m: int((np.array(modes) == m).sum()) for m in sorted(set(modes))}

    return {
        "n_prof_file": int(n_prof),
        "n_levels": int(n_levels),
        "n_prof_indexed_in_window": int(len(want)),
        "file_span": [str(stamps.min())[:10], str(stamps.max())[:10]],
        "data_mode_counts": mode_counts,
        "adjusted_realtime_A": mode_counts.get("A", 0),
        "params": params,
        "has_adjusted_fields": any(v.endswith("_ADJUSTED") for v in variables),
        "missing_indexed_profiles": len(missing),
        "missing_sample": missing[:10],
        "mode_conflicts": mode_conflicts[:10],
        "n_mode_conflicts": len(mode_conflicts),
        "problems": problems,
    }


def main(force: bool = False):
    if not DEMO_CSV.exists():
        sys.exit(f"missing {DEMO_CSV} -- run etl/demo_floats.py first")

    demo = pd.read_csv(DEMO_CSV, dtype={"wmo": str})
    prof = pd.read_csv(PROFILES_CSV, dtype={"wmo": str})
    prof["descending"] = prof["descending"].astype(bool)

    records, failed = [], []
    fetched_bytes = cached_bytes = 0

    for i, f in enumerate(demo.itertuples(), 1):
        url = f"{BASE_URL}/{f.dac}/{f.wmo}/{f.wmo}_prof.nc"
        dest = DEST_ROOT / f.dac / f"{f.wmo}_prof.nc"
        print(f"[{i}/{len(demo)}] {f.dac}/{f.wmo}")

        try:
            transfer = download(url, dest, force)
        except Exception as exc:
            print(f"    DOWNLOAD FAILED: {exc}")
            failed.append({"wmo": f.wmo, "dac": f.dac, "error": str(exc)})
            continue

        if transfer["cached"]:
            cached_bytes += transfer["bytes"]
        else:
            fetched_bytes += transfer["bytes"]
        checks = inspect(dest, f.wmo, prof[prof.wmo == f.wmo])

        tag = "cached" if transfer["cached"] else "fetched"
        print(f"    {tag} {transfer['bytes']:,} bytes -> {dest.relative_to(ROOT)}")
        print(f"    N_PROF {checks['n_prof_file']:>4} (file, {checks['file_span'][0]}..{checks['file_span'][1]})"
              f"   {checks['n_prof_indexed_in_window']:>4} indexed in our window"
              f"   N_LEVELS {checks['n_levels']}")
        print(f"    DATA_MODE {checks['data_mode_counts']}   params {checks['params']}")
        for p in checks["problems"]:
            print(f"    !! {p}")

        records.append({"wmo": f.wmo, "dac": f.dac, "url": url,
                        "path": str(dest.relative_to(ROOT)), **transfer, **checks})

    # ---- summary ----------------------------------------------------------
    print(f"\n{len(records)} of {len(demo)} floats on disk"
          f"  ({fetched_bytes:,} bytes downloaded, {cached_bytes:,} already cached)")
    if records:
        m = pd.DataFrame(records)
        print(f"  profiles in files      : {m.n_prof_file.sum():,}")
        print(f"  profiles our index knew: {m.n_prof_indexed_in_window.sum():,} (2023-2024 window only)")
        modes = {}
        for d in m.data_mode_counts:
            for k, v in d.items():
                modes[k] = modes.get(k, 0) + v
        print(f"  DATA_MODE totals       : {modes}"
              + ("   <- 'A' = real-time, adjusted; the filename could not say this"
                 if modes.get("A") else ""))
        print(f"  missing indexed profs  : {int(m.missing_indexed_profiles.sum())}")
        print(f"  DATA_MODE conflicts    : {int(m.n_mode_conflicts.sum())}")
        clean = int((m.problems.apply(len) == 0).sum())
        print(f"  floats with no problems: {clean}/{len(m)}")

    if failed:
        print(f"\n{len(failed)} download(s) failed:")
        for f in failed:
            print(f"  {f['dac']}/{f['wmo']}: {f['error']}")

    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps({
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "base_url": BASE_URL,
        "floats": records,
        "failed": failed,
    }, indent=2, default=str))
    print(f"\nwrote {MANIFEST}")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="re-download even if cached")
    main(**vars(ap.parse_args()))
