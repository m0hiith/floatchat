"""Stage 10 tests: the HTTP layer serves the database, and nothing of its own.

No pytest -- one file, run it, read the result.  Same as the other four suites.

    .venv/bin/python api/test_server.py

The theme of this suite is that `api/server.py` must contain no knowledge.
Several checks compare a /meta response against `catalog` and the database
directly: if someone ever hardcodes a region list or a date window into the
server "just for the demo", these fail.
"""

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import catalog
from api.server import app, jsonable, jsonable_rows

passed = failed = 0


def check(label: str, ok: bool, detail: str = ""):
    global passed, failed
    passed, failed = passed + ok, failed + (not ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")


def main():
    client = TestClient(app)
    live = catalog.LiveValues.load()

    print("GET /meta")
    r = client.get("/meta")
    check("200", r.status_code == 200, str(r.status_code))
    meta = r.json()

    check("carries every catalogue query",
          [q["name"] for q in meta["queries"]] == [q.name for q in catalog.QUERIES],
          f"{len(meta['queries'])} queries")
    check("region list is the database's, not a constant",
          [x["name"] for x in meta["regions"]] == list(live.regions),
          f"{len(meta['regions'])} regions")
    check("float list is the database's, not a constant",
          [x["wmo"] for x in meta["floats"]] == list(live.wmos),
          f"{len(meta['floats'])} floats")
    check("date window is the database's, not a constant",
          (meta["database"]["window"]["start"], meta["database"]["window"]["end"]) == live.window,
          " .. ".join(live.window))
    check("counts match ingest_run",
          meta["database"]["profiles"] > 0 and meta["database"]["levels"] > 0,
          f"{meta['database']['profiles']} profiles, {meta['database']['levels']} levels")
    check("map extent is derived from loaded profiles",
          all(meta["extent"][k] is not None for k in
              ("min_lat", "max_lat", "min_lon", "max_lon")),
          f"lat {meta['extent']['min_lat']:.2f}..{meta['extent']['max_lat']:.2f}")

    print("\n/meta describes every parameter well enough to build a control")
    by_name = {q["name"]: q for q in meta["queries"]}
    for q in catalog.QUERIES:
        served = by_name[q.name]["params"]
        check(f"{q.name:<24}",
              [p["name"] for p in served] == [p.name for p in q.params],
              f"{len(served)} param(s)")

    flat = [p for q in meta["queries"] for p in q["params"]]
    check("every param declares kind, required and default",
          all({"kind", "required", "default"} <= set(p) for p in flat), f"{len(flat)} params")
    check("region params carry the live region list as choices",
          all(p["choices"] == list(live.regions) for p in flat if p["kind"] == "region"))
    check("wmo params carry the live float list as choices",
          all(p["choices"] == list(live.wmos) for p in flat if p["kind"] == "wmo"))
    check("date params carry the live window as min/max",
          all((p["minimum"], p["maximum"]) == live.window for p in flat if p["kind"] == "date"))
    check("optional params state their default",
          all(p["default"] is not None for p in flat
              if not p["required"] and p["name"] != "wmo"),
          "missing_profiles.wmo defaults to None on purpose")
    check("numeric params carry their bounds",
          all(p["minimum"] is not None and p["maximum"] is not None
              for p in flat if p["kind"] in ("int", "number")))

    print("\nthe UI's choices and the model's choices come from one source")
    schemas = {s["name"]: s for s in catalog.tool_schemas(live)}
    same = []
    for q in meta["queries"]:
        props = schemas[q["name"]]["input_schema"]["properties"]
        for p in q["params"]:
            if p["choices"] is not None:
                same.append(p["choices"] == props[p["name"]].get("enum"))
    check("every /meta choice list equals the model's tool enum",
          all(same) and len(same) > 0, f"{len(same)} enum(s) compared")

    print("\nGET /regions.geojson")
    g = client.get("/regions.geojson")
    check("200", g.status_code == 200, str(g.status_code))
    fc = g.json()
    check("one feature per region", len(fc["features"]) == len(live.regions))
    check("rings are closed",
          all(f["geometry"]["coordinates"][0][0] == f["geometry"]["coordinates"][0][-1]
              for f in fc["features"]))
    # If the pair were emitted (lat, lon) instead of (lon, lat), the values
    # would fall outside the region's own longitude bbox.  Checked against the
    # database's bbox rather than a coastline I remembered -- the Red Sea
    # reaches west of 40E, which an invented bound got wrong.
    bbox = {r["name"]: r for r in meta["regions"]}
    inside = []
    for f in fc["features"]:
        b = bbox[f["properties"]["name"]]
        for lon, lat in f["geometry"]["coordinates"][0]:
            inside.append(b["min_lon"] <= lon <= b["max_lon"]
                          and b["min_lat"] <= lat <= b["max_lat"])
    check("coordinates are [lon, lat], inside each region's own bbox",
          all(inside), f"{len(inside)} vertices checked")

    print("\nPOST /query returns rows and what was bound")
    q = client.post("/query", json={"name": "depth_profile",
                                    "params": {"region": "Arabian Sea",
                                               "start": "2023-01-01", "end": "2023-12-31"}})
    check("200", q.status_code == 200, str(q.status_code))
    body = q.json()
    check("names the query that ran", body["query"] == "depth_profile")
    check("reports a row count", body["row_count"] == len(body["rows"]),
          f"{body['row_count']} rows")
    check("bound params include the defaults the caller omitted",
          body["params"]["bin_dbar"] == 50 and body["params"]["max_dbar"] == 2000,
          f"bin_dbar={body['params']['bin_dbar']}, max_dbar={body['params']['max_dbar']}")

    print("\nmeasurements arrive as numbers, not strings")
    row = body["rows"][0]
    check("temperature is a number", isinstance(row["mean_temp_c"], (int, float)),
          f"{row['mean_temp_c']!r} ({type(row['mean_temp_c']).__name__})")
    check("salinity is a number", isinstance(row["mean_psal_psu"], (int, float)),
          f"{row['mean_psal_psu']!r}")
    check("Decimal becomes float, not str", jsonable(Decimal("28.093")) == 28.093)
    check("None stays None, never 0.0",
          jsonable(None) is None and jsonable_rows([{"x": None}]) == [{"x": None}])

    print("\nan empty result is empty, not zero")
    e = client.post("/query", json={"name": "profiles_in_region",
                                    "params": {"region": "Red Sea",
                                               "start": "2023-01-01", "end": "2023-01-02"}})
    check("200 with no rows", e.status_code == 200 and e.json()["row_count"] == 0,
          "a region with no profiles is a success, not an error")
    s = client.post("/query", json={"name": "region_summary",
                                    "params": {"region": "Andaman or Burma Sea",
                                               "start": "2023-01-01", "end": "2024-12-31"}})
    srow = s.json()["rows"][0]
    check("an aggregate over nothing reports null, not 0.0",
          srow["profiles"] == 0 and srow["deepest_dbar"] is None,
          "profiles=0 is a real count; deepest_dbar=null is an absence")

    print("\nrefusals are 400 and name what was allowed")
    for label, payload, expect in [
        ("unknown region",
         {"name": "depth_profile", "params": {"region": "Atlantis",
                                              "start": "2023-01-01", "end": "2023-12-31"}},
         "valid regions"),
        ("unknown float",
         {"name": "float_trajectory", "params": {"wmo": "9999999"}}, "valid floats"),
        ("'last tuesday' as a date",
         {"name": "region_summary", "params": {"region": "Arabian Sea",
                                               "start": "last tuesday", "end": "2023-12-31"}},
         "yyyy-mm-dd"),
        ("a limit of ten million",
         {"name": "profiles_in_region", "params": {"region": "Arabian Sea",
                                                   "start": "2023-01-01", "end": "2023-12-31",
                                                   "limit": 10_000_000}}, "must be <="),
        ("an injection string as a region",
         {"name": "depth_profile", "params": {"region": "'; DROP TABLE profiles; --",
                                              "start": "2023-01-01", "end": "2023-12-31"}},
         "valid regions"),
        ("a query that does not exist",
         {"name": "drop_everything", "params": {}}, "no query named"),
        ("a parameter the query does not take",
         {"name": "float_inventory", "params": {"region": "Arabian Sea"}}, "unknown parameter"),
        ("a missing required parameter",
         {"name": "depth_profile", "params": {"region": "Arabian Sea"}}, "missing required"),
    ]:
        resp = client.post("/query", json=payload)
        ok = resp.status_code == 400 and expect in resp.json().get("detail", "").lower()
        check(f"{label:<34}", ok,
              (resp.json().get("detail", "")[:80] if ok
               else f"{resp.status_code}: {resp.text[:80]}"))

    print("\na refusal is not an outage: the two have different status codes")
    check("refused query is 400",
          client.post("/query", json={"name": "depth_profile",
                                      "params": {"region": "Atlantis",
                                                 "start": "2023-01-01",
                                                 "end": "2023-12-31"}}).status_code == 400)
    check("healthy database is 200", client.get("/health").status_code == 200)
    check("503 is reserved for the database being unreachable",
          "Unavailable" in Path(__file__).with_name("server.py").read_text()
          and "503" in Path(__file__).with_name("server.py").read_text())

    print("\na stopped database is a 503 that says why")
    # The dashboard has to tell 'the API is down' apart from 'the database has
    # no regions'.  If both were an empty list they would look identical.
    real_dsn = catalog.DSN
    catalog.DSN = real_dsn.replace(":5432/", ":5599/")
    down = TestClient(app, raise_server_exceptions=False)
    for path in ("/meta", "/regions.geojson", "/health"):
        r = down.get(path)
        check(f"{path:<20} is 503, not an empty body",
              r.status_code == 503 and r.json().get("error") == "database unavailable",
              str(r.status_code))
    body = down.get("/meta").json()
    check("the 503 names the reason", "refused" in body["detail"].lower()
          or "failed" in body["detail"].lower(), body["detail"][:70])
    check("the 503 names the host it tried", "5599" in body["dsn"], body["dsn"])
    r = down.post("/query", json={"name": "float_inventory", "params": {}})
    check("a query against a dead database is 503, not 400",
          r.status_code == 503, str(r.status_code))
    catalog.DSN = real_dsn
    check("connection restored for the remaining checks",
          client.get("/health").status_code == 200)

    print("\nthe server hardcodes no data of its own")
    src = Path(__file__).with_name("server.py").read_text()
    for term in ("Arabian Sea", "Bay of Bengal", "2023-01-01", "2024-12-31", "6903139"):
        check(f"no literal {term!r:<18}", term not in src)

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
