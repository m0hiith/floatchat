#!/usr/bin/env python
"""Stage 10a: the HTTP layer over the query catalogue.

Three endpoints, and deliberately no more:

    GET  /meta              everything the UI is allowed to know
    GET  /regions.geojson   the IHO outlines, for the map
    POST /query             run one named query from the catalogue

The important property is that this file adds no knowledge.  It does not know
what a region is called, which floats exist, what the date window is, or what
parameters a query takes -- all of that is read from `api/catalog.py`, which
reads it from the database.  A dashboard built against /meta therefore cannot
drift from the data: change the database, restart nothing, reload the page.

That is the same guarantee Stage 7 relies on.  `catalog.tool_schemas()` builds
the model's tools from `QUERIES` and `LiveValues`; `/meta` builds the UI's
dropdowns from the *same two objects*.  One source, two consumers -- the model
and the human get identical, database-derived choices.

/query is a pass-through to `catalog.run()`, including its refusals.  A
`QueryError` becomes a 400 whose body is the catalogue's own message, and those
messages always name what would have been acceptable ("Valid regions: ...").
The UI renders that string; it never composes its own.

Run it:
    .venv/bin/uvicorn api.server:app --reload --port 8000
"""

from __future__ import annotations

import re
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import catalog
from catalog import QUERIES, LiveValues, Param, QueryError

# The Vite dev server.  Listed explicitly rather than "*" -- this API is a
# read-only view of a local database, but a wildcard would still be a claim we
# have not thought about.
DEV_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]

# Postgres renders `polygon` as ((x,y),(x,y),...).  x is longitude (D5.1).
POINT = re.compile(r"\(([-\d.eE+]+),([-\d.eE+]+)\)")

app = FastAPI(
    title="FloatChat API",
    description=__doc__,
    version="10.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=DEV_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


# --------------------------------------------------------------------------
# failure is a response, not a traceback
# --------------------------------------------------------------------------

class Unavailable(Exception):
    """The database did not answer.  Distinct from a query being refused: one
    means the platform is down, the other means the question was wrong.  The UI
    must not render them the same way, so they do not share a status code."""


def _db_error(exc: Exception) -> str:
    """A one-line reason a human can act on.  psycopg's messages carry the host,
    port and reason; the UI shows this verbatim rather than 'failed to load'."""
    first = str(exc).strip().splitlines()
    return first[0] if first else exc.__class__.__name__


@app.exception_handler(Unavailable)
def _unavailable(_request, exc: Unavailable) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"error": "database unavailable", "detail": str(exc),
                 "dsn": catalog.DSN.split("@")[-1]},
    )


@app.exception_handler(QueryError)
def _refused(_request, exc: QueryError) -> JSONResponse:
    # 400, not 500: the catalogue worked exactly as designed.  The message
    # already names the valid values -- see catalog.Param.coerce.
    return JSONResponse(status_code=400, content={"error": "refused", "detail": str(exc)})


def _connect() -> psycopg.Connection:
    try:
        return catalog.connect()
    except psycopg.Error as exc:
        raise Unavailable(_db_error(exc)) from exc


def _live() -> LiveValues:
    """`LiveValues.load()` opens its own connection, so its failure has to be
    translated here too -- otherwise a stopped Postgres reaches the client as a
    500 traceback instead of the 503 the UI knows how to render."""
    try:
        return LiveValues.load()
    except psycopg.Error as exc:
        raise Unavailable(_db_error(exc)) from exc


# --------------------------------------------------------------------------
# GET /meta -- the only thing the UI loads at startup
# --------------------------------------------------------------------------

def param_json(p: Param, live: LiveValues) -> dict[str, Any]:
    """One parameter, described well enough to build a control from it.

    `choices` is resolved here rather than left as a kind name: a `region`
    param and an `enum` param are the same control once the options are known,
    and the UI should not have to learn what 'region' means.
    """
    out: dict[str, Any] = {
        "name": p.name,
        "kind": p.kind,
        "description": p.description,
        "required": p.required,
        "default": p.default,
        "minimum": p.minimum,
        "maximum": p.maximum,
        "choices": None,
    }
    if p.kind == "region":
        out["choices"] = list(live.regions)
    elif p.kind == "wmo":
        out["choices"] = list(live.wmos)
    elif p.kind == "enum":
        out["choices"] = list(p.choices)
    elif p.kind == "date":
        # Not a choice list, but the same idea: the window the data covers.
        out["minimum"], out["maximum"] = live.window
    return out


@app.get("/meta")
def meta() -> dict[str, Any]:
    """Regions, floats, the date window, the row counts, and every query with
    its typed parameters.  Nothing here is a constant in this file."""
    live = _live()

    with _connect() as conn:
        run = catalog.run_raw(conn, """
            SELECT loaded_at::text, gdac_index_date,
                   window_start::date::text AS window_start,
                   window_end::date::text   AS window_end,
                   good_qc_flags, index_rows_total, index_rows_kept,
                   profiles_in_files, profiles_written, levels_written
            FROM ingest_run""")[0]

        regions = catalog.run_raw(conn, """
            SELECT r.name, r.mrgid, r.source, r.vertices_stored, r.holes_dropped,
                   r.min_lon, r.min_lat, r.max_lon, r.max_lat,
                   count(pr.profile_id) AS profiles
            FROM regions r
            LEFT JOIN profile_regions pr ON pr.region = r.name
            GROUP BY r.name, r.mrgid, r.source, r.vertices_stored, r.holes_dropped,
                     r.min_lon, r.min_lat, r.max_lon, r.max_lat
            ORDER BY r.name""")

        floats = catalog.run_raw(conn, """
            SELECT f.wmo, f.dac, f.profiler_type, f.dm_status,
                   count(p.profile_id) AS profiles,
                   min(p.juld)::date::text AS first,
                   max(p.juld)::date::text AS last
            FROM floats f LEFT JOIN profiles p USING (wmo)
            GROUP BY f.wmo, f.dac, f.profiler_type, f.dm_status
            ORDER BY f.wmo""")

        extent = catalog.run_raw(conn, """
            SELECT min(lat) AS min_lat, max(lat) AS max_lat,
                   min(lon) AS min_lon, max(lon) AS max_lon
            FROM profiles""")[0]

    return {
        "database": {
            "loaded_at": run["loaded_at"],
            "gdac_index_date": run["gdac_index_date"],
            "window": {"start": run["window_start"], "end": run["window_end"]},
            "good_qc_flags": run["good_qc_flags"],
            "floats": len(floats),
            "profiles": run["profiles_written"],
            "levels": run["levels_written"],
            "index_rows_total": run["index_rows_total"],
            "index_rows_kept": run["index_rows_kept"],
        },
        # Where the map should open.  Derived from the profiles actually
        # loaded, so a different dataset frames itself differently.
        "extent": extent,
        "regions": regions,
        "floats": floats,
        "queries": [
            {
                "name": q.name,
                "description": q.description,
                "example": q.example,
                "params": [param_json(p, live) for p in q.params],
            }
            for q in QUERIES
        ],
    }


@app.get("/regions.geojson")
def regions_geojson() -> dict[str, Any]:
    """The IHO outlines as GeoJSON, coordinates in [lon, lat] order.

    Served apart from /meta on purpose.  /meta is the critical path -- without
    it there are no dropdowns -- and it should not carry 13,500 vertices.  If
    this endpoint fails the map simply has no outlines; the dashboard works.
    """
    with _connect() as conn:
        rows = catalog.run_raw(conn, """
            SELECT name, mrgid, poly::text AS poly FROM regions ORDER BY name""")

    features = []
    for r in rows:
        ring = [[float(lon), float(lat)] for lon, lat in POINT.findall(r["poly"])]
        if ring and ring[0] != ring[-1]:
            ring.append(ring[0])          # GeoJSON rings must close
        features.append({
            "type": "Feature",
            "properties": {"name": r["name"], "mrgid": r["mrgid"]},
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        })
    return {"type": "FeatureCollection", "features": features}


# --------------------------------------------------------------------------
# POST /query
# --------------------------------------------------------------------------

def jsonable(value: Any) -> Any:
    """Coerce one database value into something a chart can use.

    Postgres `numeric` -- which every `round()` in the catalogue returns --
    arrives as a `Decimal`, and the default JSON encoding of a Decimal is a
    *string*.  Handed to Plotly, "28.093" is a category label, not a number:
    the axis silently becomes ordinal and the line is drawn in row order rather
    than in value order.  It looks like a chart, which is why it is worth a
    named function and a test.

    NULL stays None and becomes JSON null.  It must not become 0.0 -- a level
    with no salinity reading is not a level with zero salinity.
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def jsonable_rows(rows: list[dict]) -> list[dict]:
    return [{k: jsonable(v) for k, v in row.items()} for row in rows]


class QueryRequest(BaseModel):
    name: str = Field(description="A query name from /meta")
    params: dict[str, Any] = Field(default_factory=dict)


@app.post("/query")
def query(req: QueryRequest) -> dict[str, Any]:
    """Run one catalogue query and return its rows plus what was bound.

    The response's `params` is post-validation, so every default the caller
    left out is present with the value the catalogue chose.  That is what the
    dashboard's audit panel displays -- it reports what *ran*, not what was
    typed into the form.
    """
    live = _live()
    with _connect() as conn:
        out = catalog.run(req.name, req.params, live=live, conn=conn)
    out["rows"] = jsonable_rows(out["rows"])
    return out


@app.get("/health")
def health() -> dict[str, Any]:
    with _connect() as conn:
        catalog.run_raw(conn, "SELECT 1")
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
