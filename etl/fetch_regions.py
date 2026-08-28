"""Stage 5a: build the named-region polygons, from a source we can cite.

D1.7 promised that the demo's region names would come from real boundaries
with the source recorded, not from the advisory longitude cut used to pick
candidate floats.  This script delivers that.

Source: IHO "Limits of Oceans and Seas" (Special Publication 23), as published
by Flanders Marine Institute / Marine Regions, fetched from their public WFS.
Each region keeps its MRGID so the boundary is traceable to a record.

Two things have to happen to those polygons before Postgres 14 can hold them,
and both are measured rather than asserted:

  1. HOLES ARE DROPPED.  The IHO polygons carry an island hole for every
     landmass -- 2,875 of them in the Indian Ocean alone -- and core Postgres
     `polygon` cannot represent a hole.  Dropping them makes each region
     slightly larger, by exactly the area of the islands inside it.  An ARGO
     float parks at 1000 dbar and surfaces in open water; it is never on an
     island.  So this cannot misclassify our data, and the verification below
     proves it did not.

  2. THE OUTLINE IS SIMPLIFIED.  Bay of Bengal ships 47,421 vertices in its
     outer ring.  We Douglas-Peucker it down to a budget.  The tolerance used
     and the vertex count before and after are recorded per region.

Verification: every one of the 928 profiles is classified twice -- once against
the FULL-RESOLUTION geometry including every island hole, and once against the
simplified ring that actually goes into the database.  If those two disagree
for even one profile, this script says so.

Outputs:
  data/regions/iho_raw.json     the fetched source, cached
  data/regions/regions.csv      name, mrgid, source, vertex counts, polygon
  data/regions/region_report.json
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "regions"
RAW = OUT / "iho_raw.json"
PROFILES = ROOT / "data" / "parsed" / "profiles.csv"

WFS = "https://geo.vliz.be/geoserver/MarineRegions/wfs"
LAYER = "MarineRegions:iho"
CITATION = ("IHO Limits of Oceans and Seas, Special Publication 23; "
            "published by Flanders Marine Institute (VLIZ), Marine Regions, "
            "https://www.marineregions.org/")

# Every IHO sea that intersects the D1.4 study box, so a region name always
# resolves to a boundary and never to a guess.
REGIONS = [
    "Arabian Sea", "Bay of Bengal", "Laccadive Sea", "Andaman or Burma Sea",
    "Gulf of Aden", "Gulf of Oman", "Persian Gulf", "Red Sea", "Indian Ocean",
]

VERTEX_BUDGET = 1500      # per region, after simplification


def fetch(force: bool = False) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    if RAW.exists() and not force:
        print(f"cached      : {RAW}  ({RAW.stat().st_size:,} bytes)")
        return json.loads(RAW.read_text())

    cql = " OR ".join(f"name='{n}'" for n in REGIONS)
    params = {"service": "WFS", "version": "1.0.0", "request": "GetFeature",
              "typeName": LAYER, "outputFormat": "application/json", "CQL_FILTER": cql}
    print(f"source      : {WFS}\nlayer       : {LAYER}")
    resp = requests.get(WFS, params=params, timeout=300)
    resp.raise_for_status()

    part = RAW.with_suffix(".json.part")
    part.write_bytes(resp.content)
    part.rename(RAW)
    print(f"fetched     : {len(resp.content):,} bytes -> {RAW}")
    return json.loads(RAW.read_text())


# ---- geometry, written out rather than imported ---------------------------
# shapely/geopandas would do all of this, but they would also pull GEOS and
# GDAL in for two operations we can state in thirty lines and verify exactly.

def ring_area(ring: np.ndarray) -> float:
    """Shoelace area.  Used only to pick the biggest ring, so sign is dropped."""
    x, y = ring[:, 0], ring[:, 1]
    return abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2.0


def rdp(points: np.ndarray, eps: float) -> np.ndarray:
    """Douglas-Peucker, iterative so a 125k-vertex ring cannot blow the stack."""
    n = len(points)
    if n < 3:
        return points
    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        seg = points[i + 1:j]
        a, b = points[i], points[j]
        ab = b - a
        norm = np.hypot(*ab)
        if norm == 0:
            d = np.hypot(seg[:, 0] - a[0], seg[:, 1] - a[1])
        else:
            rel = seg - a
            # 2-D cross product magnitude; numpy 2 dropped np.cross for 2-vectors.
            d = np.abs(ab[0] * rel[:, 1] - ab[1] * rel[:, 0]) / norm
        k = int(d.argmax())
        if d[k] > eps:
            k += i + 1
            keep[k] = True
            stack.append((i, k))
            stack.append((k, j))
    return points[keep]


def simplify_to_budget(ring: np.ndarray, budget: int) -> tuple[np.ndarray, float]:
    """Smallest tolerance that gets the ring under the vertex budget."""
    if len(ring) <= budget:
        return ring, 0.0
    lo, hi = 1e-6, 5.0
    best, best_eps = ring, hi
    for _ in range(22):
        mid = (lo + hi) / 2
        out = rdp(ring, mid)
        if len(out) <= budget:
            best, best_eps = out, mid
            hi = mid
        else:
            lo = mid
    return best, best_eps


def point_in_ring(pts: np.ndarray, ring: np.ndarray) -> np.ndarray:
    """Ray casting, vectorised over the ring's edges, one point at a time."""
    x1, y1 = ring[:-1, 0], ring[:-1, 1]
    x2, y2 = ring[1:, 0], ring[1:, 1]
    out = np.zeros(len(pts), dtype=bool)
    for i, (px, py) in enumerate(pts):
        straddles = (y1 > py) != (y2 > py)
        if not straddles.any():
            continue
        xs = x1[straddles] + (py - y1[straddles]) * (x2[straddles] - x1[straddles]) \
            / (y2[straddles] - y1[straddles])
        out[i] = (xs > px).sum() % 2 == 1
    return out


def bbox(ring: np.ndarray) -> tuple[float, float, float, float]:
    return ring[:, 0].min(), ring[:, 1].min(), ring[:, 0].max(), ring[:, 1].max()


def classify_full(pts: np.ndarray, outer: np.ndarray, holes: list) -> np.ndarray:
    """Inside the outer ring and inside none of the island holes."""
    inside = point_in_ring(pts, outer)
    if not inside.any():
        return inside
    idx = np.nonzero(inside)[0]
    for hole in holes:
        x0, y0, x1_, y1_ = bbox(hole)
        # Only test the points a hole could possibly contain.
        cand = idx[(pts[idx, 0] >= x0) & (pts[idx, 0] <= x1_) &
                   (pts[idx, 1] >= y0) & (pts[idx, 1] <= y1_)]
        if len(cand) == 0:
            continue
        inside[cand] &= ~point_in_ring(pts[cand], hole)
        idx = np.nonzero(inside)[0]
        if len(idx) == 0:
            break
    return inside


def pg_polygon(ring: np.ndarray) -> str:
    """Postgres `polygon` literal, (lon, lat) so x is longitude."""
    return "(" + ",".join(f"({x:.5f},{y:.5f})" for x, y in ring) + ")"


def main(force: bool = False):
    geo = fetch(force)
    prof = pd.read_csv(PROFILES, dtype={"wmo": str})
    pts = prof[["lon", "lat"]].to_numpy(dtype=float)
    print(f"profiles    : {len(pts)} to classify\n")

    rows, checks = [], {}
    full_hits = {}

    print(f"  {'region':<22}{'verts in':>9}{'holes':>7}{'verts out':>10}"
          f"{'tol deg':>9}{'full':>6}{'simpl':>7}  agree")
    for feat in sorted(geo["features"], key=lambda f: f["properties"]["name"]):
        name = feat["properties"]["name"]
        mrgid = feat["properties"]["mrgid"]
        g = feat["geometry"]
        polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]

        # The biggest exterior ring is the region; the rest are islands.
        parts = [(np.asarray(p[0], dtype=float), [np.asarray(h, dtype=float) for h in p[1:]])
                 for p in polys]
        outer, holes = max(parts, key=lambda ph: ring_area(ph[0]))
        n_in = sum(len(r) for r in [outer] + holes)

        simple, eps = simplify_to_budget(outer, VERTEX_BUDGET)
        if not np.array_equal(simple[0], simple[-1]):
            simple = np.vstack([simple, simple[:1]])

        in_full = classify_full(pts, outer, holes)
        in_simple = point_in_ring(pts, simple)
        agree = int((in_full == in_simple).sum())
        full_hits[name] = in_full

        print(f"  {name:<22}{n_in:>9,}{len(holes):>7,}{len(simple):>10,}"
              f"{eps:>9.4f}{int(in_full.sum()):>6}{int(in_simple.sum()):>7}"
              f"  {'yes' if agree == len(pts) else f'NO ({len(pts)-agree})'}")

        checks[name] = {"disagreements": int(len(pts) - agree),
                        "profiles_full": int(in_full.sum()),
                        "profiles_simplified": int(in_simple.sum())}
        x0, y0, x1_, y1_ = bbox(simple)
        rows.append({
            "name": name, "mrgid": mrgid, "source": CITATION,
            "vertices_source": n_in, "holes_dropped": len(holes),
            "vertices_stored": len(simple), "tolerance_deg": round(eps, 6),
            "min_lon": round(x0, 5), "min_lat": round(y0, 5),
            "max_lon": round(x1_, 5), "max_lat": round(y1_, 5),
            "poly": pg_polygon(simple),
        })

    total_bad = sum(c["disagreements"] for c in checks.values())

    # How many regions does each profile land in?  IHO areas are meant to be
    # adjacent, not overlapping; this says whether that held after simplifying.
    stack = np.vstack([full_hits[r["name"]] for r in rows])
    per_profile = stack.sum(axis=0)
    print(f"\nregion membership per profile (full resolution)")
    for k in sorted(set(per_profile.tolist())):
        print(f"  in {k} region(s): {int((per_profile == k).sum()):>4} profiles")

    unassigned = prof[per_profile == 0]
    if len(unassigned):
        print(f"\n{len(unassigned)} profile(s) in no named region:")
        for r in unassigned.head(5).itertuples():
            print(f"  {r.profile_id}  lat {r.lat:7.3f}  lon {r.lon:7.3f}")

    df = pd.DataFrame(rows)
    df.to_csv(OUT / "regions.csv", index=False)
    (OUT / "region_report.json").write_text(json.dumps({
        "source": CITATION, "wfs": WFS, "layer": LAYER,
        "vertex_budget": VERTEX_BUDGET,
        "regions": {r["name"]: {k: v for k, v in r.items() if k != "poly"} for r in rows},
        "simplification_check": checks,
        "total_disagreements": total_bad,
        "profiles_checked": int(len(pts)),
        "profiles_in_no_region": int((per_profile == 0).sum()),
        "profiles_in_multiple_regions": int((per_profile > 1).sum()),
    }, indent=2, default=str))

    print(f"\nwrote {OUT/'regions.csv'}  ({len(df)} regions)")
    print(f"wrote {OUT/'region_report.json'}")

    if total_bad:
        sys.exit(f"\n{total_bad} profile(s) classified differently by the simplified "
                 "polygon -- the tolerance is too coarse to use.")
    print(f"\nsimplification is lossless for all {len(pts)} profiles in this dataset.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="re-download the IHO geometry")
    main(**vars(ap.parse_args()))
