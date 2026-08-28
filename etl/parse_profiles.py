"""Stage 3: turn ten NetCDF files into two flat tables Postgres can COPY.

Input : data/profiles/<dac>/<wmo>_prof.nc          (Stage 2)
Output: data/parsed/profiles.csv   one row per profile
        data/parsed/levels.csv     one row per measured level
        data/parsed/parse_report.json

Three rules do the real work here, and each is a decision logged in
DECISIONS.md rather than a line of code someone has to reverse-engineer:

  1. SCOPE.  Only profiles inside the declared 2023-2024 window are ingested,
     even though the files carry each float's whole life (D2.5).  Every count
     in this project then reconciles with the Stage 1 funnel.

  2. WHICH VALUE.  ARGO ships raw and adjusted copies of every parameter.
     DATA_MODE 'D' and 'A' mean an adjusted copy should exist, so we prefer
     <PARAM>_ADJUSTED for those and <PARAM> for 'R'.  But "should exist" is
     not "does exist": a delayed-mode profile can carry an entirely empty
     PSAL_ADJUSTED.  We fall back to the raw value, RECORD the fallback per
     profile, and count it -- rather than emit a column of silent NaN.

  3. WHICH LEVELS.  A QC flag of 3 (probably bad) or 4 (bad) is not data.
     Levels are kept only where pressure is present and good and at least one
     of temperature or salinity survives its own flag.  Every flag seen is
     counted and reported; nothing is dropped without a number attached.
"""

import json
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
from netCDF4 import Dataset

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "data" / "index"
PROFILES_DIR = ROOT / "data" / "profiles"
OUT = ROOT / "data" / "parsed"

# D2.5: the declared scope, not the whole file.
WINDOW_START = pd.Timestamp("2023-01-01")
WINDOW_END = pd.Timestamp("2024-12-31 23:59:59")

# ARGO reference table 2.  1 good, 2 probably good, 5 changed, 8 interpolated
# are usable; 3 probably bad, 4 bad, 9 missing are not.  We write the full
# accepted set even though this ten-float subset only ever uses 1 -- the
# report says which flags actually turned up.
GOOD_QC = {"1", "2", "5", "8"}

# D1.4's study box.  Stage 1 filtered the INDEX by it; the per-float aggregate
# has no such filter, so a float that drifted out of the box mid-window brings
# those cycles back.  We keep them (D3.4) and count them here rather than
# discovering the mismatch later as an unexplained row-count difference.
BOX_LAT = (-10.0, 30.0)
BOX_LON = (40.0, 100.0)
ARGO_EPOCH = pd.Timestamp("1950-01-01")
PARAMS = ("PRES", "TEMP", "PSAL")


def chars(var) -> np.ndarray:
    """Decode a char variable to a numpy array of python strings, same shape."""
    arr = np.ma.filled(var[:], b" ")
    flat = np.array([(x.decode(errors="replace") if isinstance(x, bytes) else str(x))
                     for x in arr.ravel()])
    return flat.reshape(arr.shape)


def joined(var) -> list[str]:
    """Decode a (N, STRING_n) char variable to one string per row."""
    arr = chars(var)
    return ["".join(row).strip() for row in arr]


def pick_source(mode: str, raw, adj):
    """Return (values, qc, source_label) for one profile and one parameter.

    Rule 2 in the module docstring.  'raw_fallback' is the interesting case:
    the file claims an adjusted copy exists and it does not.
    """
    if mode in ("D", "A"):
        if np.ma.count(adj["val"]) > 0:
            return adj["val"], adj["qc"], "adjusted"
        if np.ma.count(raw["val"]) > 0:
            return raw["val"], raw["qc"], "raw_fallback"
        return adj["val"], adj["qc"], "empty"
    return raw["val"], raw["qc"], "raw"


def parse_float(path: Path, wmo: str, dac: str, counts: Counter,
                qc_seen: Counter, sources: Counter, dropped: list):
    """Parse one file into (profile rows, level rows)."""
    with Dataset(path) as ds:
        n_prof = ds.dimensions["N_PROF"].size

        cycles = np.ma.filled(ds["CYCLE_NUMBER"][:], -1).astype(int)
        direction = chars(ds["DIRECTION"])
        data_mode = chars(ds["DATA_MODE"])
        juld_days = np.ma.filled(ds["JULD"][:], np.nan).astype(float)
        juld_qc = chars(ds["JULD_QC"])
        lat = np.ma.filled(ds["LATITUDE"][:], np.nan).astype(float)
        lon = np.ma.filled(ds["LONGITUDE"][:], np.nan).astype(float)
        pos_qc = chars(ds["POSITION_QC"])
        platform = joined(ds["PLATFORM_NUMBER"])
        prof_qc = {p: chars(ds[f"PROFILE_{p}_QC"]) for p in PARAMS}

        raw = {p: {"val": ds[p][:], "qc": chars(ds[f"{p}_QC"])} for p in PARAMS}
        adj = {p: {"val": ds[f"{p}_ADJUSTED"][:], "qc": chars(ds[f"{p}_ADJUSTED_QC"])}
               for p in PARAMS}

    # JULD is a float day count, so days*86400 leaves nanosecond float noise
    # (2023-01-02 04:00:12.000000081).  ARGO's real time resolution is coarser
    # than a second; round to it rather than shipping the artefact to Postgres.
    stamps = (ARGO_EPOCH + pd.to_timedelta(juld_days, unit="D")).round("s")
    counts["profiles_in_files"] += n_prof

    in_window = (stamps >= WINDOW_START) & (stamps <= WINDOW_END)
    counts["profiles_in_window"] += int(in_window.sum())

    prof_rows, level_frames = [], []

    for i in range(n_prof):
        if not in_window[i]:
            continue

        key = f"{wmo}_{cycles[i]:04d}{direction[i]}"

        if platform[i] != wmo:
            counts["profiles_wrong_platform"] += 1
            dropped.append({"profile": key, "reason": "wrong PLATFORM_NUMBER",
                            "detail": platform[i]})
            continue

        # A profile with a bad time or a bad position cannot be placed on a map
        # or a timeline, which is the entire demo.  Drop it, loudly.
        if juld_qc[i] not in GOOD_QC or pos_qc[i] not in GOOD_QC:
            counts["profiles_bad_time_or_position"] += 1
            dropped.append({"profile": key, "reason": "bad time/position QC",
                            "detail": f"JULD_QC={str(juld_qc[i])} POSITION_QC={str(pos_qc[i])}"})
            continue
        if not (np.isfinite(lat[i]) and np.isfinite(lon[i])):
            counts["profiles_no_position"] += 1
            dropped.append({"profile": key, "reason": "no position", "detail": ""})
            continue
        counts["profiles_locatable"] += 1

        in_box = (BOX_LAT[0] <= lat[i] <= BOX_LAT[1]) and (BOX_LON[0] <= lon[i] <= BOX_LON[1])
        if not in_box:
            counts["profiles_outside_study_box"] += 1

        mode = data_mode[i]
        vals, qcs, srcs = {}, {}, {}
        for p in PARAMS:
            v, q, src = pick_source(
                mode,
                {"val": raw[p]["val"][i], "qc": raw[p]["qc"][i]},
                {"val": adj[p]["val"][i], "qc": adj[p]["qc"][i]},
            )
            vals[p], qcs[p], srcs[p] = v, q, src
            sources[(p, mode, src)] += 1

        for p in PARAMS:
            for flag in qcs[p]:
                qc_seen[(p, flag)] += 1

        n_levels = vals["PRES"].shape[0]
        counts["level_cells"] += n_levels

        present = {p: ~np.ma.getmaskarray(vals[p]) & np.isfinite(np.ma.filled(vals[p], np.nan))
                   for p in PARAMS}
        good = {p: present[p] & np.isin(qcs[p], list(GOOD_QC)) for p in PARAMS}

        counts["levels_pres_present"] += int(present["PRES"].sum())
        counts["levels_pres_good"] += int(good["PRES"].sum())
        counts["levels_temp_good"] += int(good["TEMP"].sum())
        counts["levels_psal_good"] += int(good["PSAL"].sum())
        counts["levels_temp_rejected_qc"] += int((present["TEMP"] & ~good["TEMP"]).sum())
        counts["levels_psal_rejected_qc"] += int((present["PSAL"] & ~good["PSAL"]).sum())

        keep = good["PRES"] & (good["TEMP"] | good["PSAL"])
        counts["levels_kept"] += int(keep.sum())
        if not keep.any():
            counts["profiles_no_usable_level"] += 1
            dropped.append({"profile": key, "reason": "no level survived QC",
                            "detail": f"data_mode={mode} n_levels={n_levels}"})
            continue
        counts["profiles_kept"] += 1

        idx = np.nonzero(keep)[0]
        prof_id = key

        frame = pd.DataFrame({
            "profile_id": prof_id,
            "wmo": wmo,
            "cycle": cycles[i],
            "level_index": idx,
            "pres": np.ma.filled(vals["PRES"], np.nan)[idx],
            "pres_qc": qcs["PRES"][idx],
        })
        for p in ("TEMP", "PSAL"):
            col = p.lower()
            v = np.where(good[p], np.ma.filled(vals[p], np.nan), np.nan)[idx]
            frame[col] = v
            frame[f"{col}_qc"] = np.where(good[p][idx], qcs[p][idx], "")
        level_frames.append(frame)

        prof_rows.append({
            "profile_id": prof_id,
            "wmo": wmo,
            "dac": dac,
            "cycle": int(cycles[i]),
            "direction": direction[i],
            "data_mode": mode,
            "juld": stamps[i],
            "juld_qc": juld_qc[i],
            "lat": float(lat[i]),
            "lon": float(lon[i]),
            "position_qc": pos_qc[i],
            "in_study_box": in_box,
            "n_levels_kept": int(keep.sum()),
            "pres_max": float(np.ma.filled(vals["PRES"], np.nan)[idx].max()),
            "pres_source": srcs["PRES"],
            "temp_source": srcs["TEMP"],
            "psal_source": srcs["PSAL"],
            "profile_pres_qc": prof_qc["PRES"][i],
            "profile_temp_qc": prof_qc["TEMP"][i],
            "profile_psal_qc": prof_qc["PSAL"][i],
        })

    levels = pd.concat(level_frames, ignore_index=True) if level_frames else pd.DataFrame()
    return pd.DataFrame(prof_rows), levels


def main():
    demo = pd.read_csv(INDEX / "demo_floats.csv", dtype={"wmo": str})
    counts, qc_seen, sources = Counter(), Counter(), Counter()
    dropped: list = []
    all_prof, all_lev = [], []

    for f in demo.itertuples():
        path = PROFILES_DIR / f.dac / f"{f.wmo}_prof.nc"
        p, l = parse_float(path, f.wmo, f.dac, counts, qc_seen, sources, dropped)
        print(f"  {f.dac}/{f.wmo:<9} {len(p):>4} profiles  {len(l):>8,} levels")
        all_prof.append(p)
        all_lev.append(l)

    profiles = pd.concat(all_prof, ignore_index=True).sort_values(["wmo", "juld"])
    levels = pd.concat(all_lev, ignore_index=True)

    # ---- the funnels ------------------------------------------------------
    print("\nprofile funnel")
    for label, key in [
        ("in the ten files", "profiles_in_files"),
        ("inside 2023-2024 window", "profiles_in_window"),
        ("wrong PLATFORM_NUMBER", "profiles_wrong_platform"),
        ("dropped: bad time/position QC", "profiles_bad_time_or_position"),
        ("dropped: no position", "profiles_no_position"),
        ("locatable", "profiles_locatable"),
        ("  of which outside D1.4 box", "profiles_outside_study_box"),
        ("dropped: no usable level", "profiles_no_usable_level"),
        ("KEPT", "profiles_kept"),
    ]:
        print(f"  {label:<32}{counts[key]:>10,}")

    # ---- reconciliation against Stage 1 ----------------------------------
    # Compare the actual profile keys, not two aggregate counters.  Every
    # profile in one set and not the other must come with a name and a reason.
    idx = pd.read_csv(INDEX / "filtered_profiles.csv", dtype={"wmo": str})
    idx = idx[idx.wmo.isin(set(demo.wmo))]
    idx_keys = {f"{w}_{int(c):04d}{'D' if d else 'A'}"
                for w, c, d in zip(idx.wmo, idx.cycle, idx.descending.astype(bool))}
    written_keys = set(profiles.profile_id)
    drop_reason = {d["profile"]: d["reason"] for d in dropped}

    missing = sorted(idx_keys - written_keys)      # promised by Stage 1, not written
    extra = sorted(written_keys - idx_keys)        # in the file, never in the index
    unexplained = [k for k in missing if k not in drop_reason]

    print("\nreconciliation with the Stage 1 index")
    print(f"  {'profiles the index promised':<38}{len(idx_keys):>10,}")
    print(f"  {'- dropped here, each named below':<38}{-len(missing):>+10,}")
    print(f"  {'+ in-window cycles the index excluded':<38}{len(extra):>+10,}"
          "   (the aggregate has no box filter)")
    print(f"  {'= profiles written':<38}{len(profiles):>10,}"
          f"   {'OK' if len(idx_keys) - len(missing) + len(extra) == len(profiles) else 'MISMATCH'}")
    if unexplained:
        print(f"  !! {len(unexplained)} indexed profile(s) vanished with no recorded reason: "
              f"{unexplained[:5]}")

    if extra:
        print("\nprofiles the files have and the index did not")
        for k in extra:
            r = profiles[profiles.profile_id == k].iloc[0]
            print(f"  {k:<16}{r.juld:%Y-%m-%d}  lat {r.lat:8.3f}  lon {r.lon:8.3f}  "
                  f"in D1.4 box: {r.in_study_box}")

    if dropped:
        print("\nevery dropped profile, by name")
        for d in dropped:
            seen = "was indexed" if d["profile"] in idx_keys else "not in index either"
            print(f"  {d['profile']:<16}{d['reason']:<24}{d['detail']:<40}{seen}")

    cells = counts["level_cells"]
    print("\nlevel funnel")
    for label, key in [
        ("level cells scanned", "level_cells"),
        ("pressure present", "levels_pres_present"),
        ("pressure QC good", "levels_pres_good"),
        ("temperature QC good", "levels_temp_good"),
        ("salinity QC good", "levels_psal_good"),
        ("temperature rejected by QC", "levels_temp_rejected_qc"),
        ("salinity rejected by QC", "levels_psal_rejected_qc"),
        ("KEPT (pres + temp or psal)", "levels_kept"),
    ]:
        pct = 100.0 * counts[key] / cells if cells else 0.0
        print(f"  {label:<32}{counts[key]:>10,}{pct:>10.2f}%")

    print("\nQC flags seen (kept profiles only)")
    for (p, flag), n in sorted(qc_seen.items()):
        shown = "fill" if flag.strip() == "" else flag
        note = "accepted" if flag in GOOD_QC else ("padding" if not flag.strip() else "REJECTED")
        print(f"  {p:<6}{shown:<5}{n:>12,}  {note}")

    print("\nvalue source by DATA_MODE (profiles)")
    for (p, mode, src), n in sorted(sources.items()):
        flag = "   <-- adjusted copy was empty" if src == "raw_fallback" else ""
        print(f"  {p:<6}mode {mode}  {src:<13}{n:>6}{flag}")

    # ---- outputs ----------------------------------------------------------
    OUT.mkdir(parents=True, exist_ok=True)
    profiles.to_csv(OUT / "profiles.csv", index=False)
    levels.to_csv(OUT / "levels.csv", index=False, float_format="%.4f")

    report = {
        "window": {"start": str(WINDOW_START), "end": str(WINDOW_END)},
        "good_qc_flags": sorted(GOOD_QC),
        "counts": dict(counts),
        "dropped_profiles": dropped,
        "indexed_profiles_stage1": int(len(idx_keys)),
        "indexed_but_not_written": missing,
        "written_but_not_indexed": extra,
        "unexplained": unexplained,
        "profiles_outside_study_box": int(counts["profiles_outside_study_box"]),
        "qc_flags_seen": {f"{p}:{flag.strip() or 'fill'}": n for (p, flag), n in qc_seen.items()},
        "value_sources": {f"{p}:{mode}:{src}": n for (p, mode, src), n in sources.items()},
        "profiles_written": int(len(profiles)),
        "levels_written": int(len(levels)),
        "floats": int(profiles.wmo.nunique()),
        "date_range": [str(profiles.juld.min()), str(profiles.juld.max())],
        "depth_range_dbar": [float(levels.pres.min()), float(levels.pres.max())],
    }
    (OUT / "parse_report.json").write_text(json.dumps(report, indent=2, default=str))

    print(f"\nwrote {OUT/'profiles.csv'}  ({len(profiles):,} rows)")
    print(f"wrote {OUT/'levels.csv'}    ({len(levels):,} rows)")
    print(f"wrote {OUT/'parse_report.json'}")
    print(f"\n{len(profiles):,} profiles from {profiles.wmo.nunique()} floats, "
          f"{profiles.juld.min():%Y-%m-%d}..{profiles.juld.max():%Y-%m-%d}, "
          f"{levels.pres.min():.1f}..{levels.pres.max():.1f} dbar")


if __name__ == "__main__":
    main()
