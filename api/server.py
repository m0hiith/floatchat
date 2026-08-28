#!/usr/bin/env python
"""Stage 10a: the HTTP layer over the query catalogue.

Four endpoints, and deliberately no more:

    GET  /meta              everything the UI is allowed to know
    GET  /regions.geojson   the IHO outlines, for the map
    POST /query             run one named query from the catalogue
    POST /ask               a question in English -- through the model's tool
                            loop, or through Stage 12's lexical router when
                            there is no model

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

/ask is a pass-through to `chat.ask()` in exactly the same sense.  This file
does not prompt the model, does not choose a query and does not read a row: it
supplies a `run_query` that records what the loop executed, and returns the
loop's own audit trail with the rows attached.  Every number in an /ask
response therefore came out of the same catalogue query the dashboard's
dropdowns run, and the response says which one -- so the chat panel draws its
chart with the same `displays.js` mapping as the manual panel, from the same
rows, and there is no second path into the database for a model to take.

Whether /ask can work at all is reported by /meta under `ai`, so the dashboard
never offers a chat box it cannot use.  That is a credential fact, a retrieval
fact and a router fact; none of them is a fact about ARGO, so this file still
holds no knowledge of the data.

Stage 12 adds a second answering path, and the two are kept visibly apart.
`api/router.py` is a sibling of `chat.ask`, not a `Transport`: it reaches the
database through the same injected `run_query` and returns the same audit
trail, but no model is involved and the response says `provider: "lexical"` so
the dashboard can badge it as such.  There is deliberately NO automatic
fallback from a failed model call to the router -- swapping the answering
engine mid-request would produce an answer that looks like a model wrote it.
The choice is explicit, in the request, and shown in the UI.

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
import chat
import router as lexical
from catalog import QUERIES, LiveValues, Param, QueryError

# A question longer than this is not a question. Refused here rather than
# forwarded, so an accidental paste costs nothing and gets a message that says
# what the limit is (the same shape as every catalogue refusal).
MAX_QUESTION_CHARS = 2_000

PROVIDER_LEXICAL = lexical.PROVIDER

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


class ModelUnavailable(Exception):
    """There is no usable model, or the one configured refused to answer for a
    reason the user has to fix -- a rejected key, a model id the key cannot
    reach, an exhausted quota.  Like `Unavailable` it is a platform state, not
    a bad question, so it is a 503 and not a 400.  It is a *different* 503 body
    from the database one, because the two are fixed in different places."""


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


@app.exception_handler(ModelUnavailable)
def _model_unavailable(_request, exc: ModelUnavailable) -> JSONResponse:
    return JSONResponse(status_code=503,
                        content={"error": "model unavailable", "detail": str(exc)})


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


def retrieval_state() -> dict[str, Any]:
    """What the Stage 11 index is, or why there is not one.

    Reported rather than assumed: a dashboard that showed "RAG" as a static
    badge would keep showing it after someone deleted the index directory.
    """
    try:
        import retrieval
    except ImportError as exc:
        return {"available": False, "reason": f"faiss not installed ({exc})"}
    if not retrieval.exists():
        return {"available": False,
                "reason": "no index built -- run: python etl/build_index.py"}
    try:
        index = retrieval.load()
    except Exception as exc:                       # a corrupt or stale index
        return {"available": False, "reason": _db_error(exc)}
    return {
        "available": True,
        "documents": len(index.documents),
        "kinds": {k: v for k, v in __import__("corpus").by_kind(index.documents).items()},
        "embedder": index.embedder.name,
        "dimensions": index.dim,
        "built_at": index.built_at,
        "k": retrieval.DEFAULT_K,
    }


def router_state() -> dict[str, Any]:
    """The Stage 12 lexical router.  Always available: it needs no credential,
    no network and no download, which is the whole reason it exists."""
    try:
        r = lexical.shared_router()
    except Exception as exc:                       # pragma: no cover - defensive
        return {"available": False, "reason": _db_error(exc)}
    return {
        "available": True,
        "routes": len(lexical.ROUTES),
        "exemplars": len(r.texts),
        "embedder": r.embedder.name,
        "floor": r.floor,
        # Said in the payload, not only in the UI, so an API consumer cannot
        # mistake this path for a language model either (rule 9).
        "method": "lexical nearest-exemplar matching; no model, not semantic",
    }


def ai_state() -> dict[str, Any]:
    """What can answer a question, and by what means.  No ARGO knowledge here."""
    model = chat.resolve_provider()
    router_ = router_state()
    return {
        # The chat box is usable if EITHER path can answer. Since Stage 12 the
        # router always can, so the tab is always offered -- which is the point.
        "available": model is not None or router_["available"],
        "provider": model or (PROVIDER_LEXICAL if router_["available"] else None),
        "model_provider": model,
        "providers": {"anthropic": chat.have_anthropic(), "gemini": chat.have_gemini()},
        "reason": None if model else
                  "no model credentials -- set ANTHROPIC_API_KEY or GEMINI_API_KEY "
                  "for the model path; the lexical router needs neither",
        "retrieval": retrieval_state(),
        "router": router_,
    }


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
        # Not data: whether the natural-language path is switched on, and what
        # is behind it.  The dashboard uses this to decide whether to offer a
        # chat box, which is better than offering one that always errors.
        "ai": ai_state(),
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


class AskRequest(BaseModel):
    question: str = Field(description="A question in English about the ARGO data")
    provider: str | None = Field(
        default=None, description="anthropic | gemini | lexical (no model)")
    model: str | None = Field(default=None, description="Override the provider's model id")
    retrieval: bool = Field(default=True, description="Use the Stage 11 vector index")
    k: int | None = Field(default=None, description="How many documents to retrieve")


@app.post("/ask")
def ask(req: AskRequest) -> dict[str, Any]:
    """A question in English -> an answer, its audit trail, and the rows behind it.

    Two paths, one response shape.  The rows are why this returns more than a
    string: `chat.ask` and `router.answer` both record row *counts*, and the
    dashboard needs the rows themselves so either path's answer can be drawn by
    the same `displays.js` spec the manual panel uses.  Both get them the same
    way -- through the `run_query` seam each already exposes -- which also means
    this file never calls the catalogue behind either one's back.
    """
    question = req.question.strip()
    if not question:
        raise QueryError("question: expected some text, got an empty string")
    if len(question) > MAX_QUESTION_CHARS:
        raise QueryError(f"question: must be at most {MAX_QUESTION_CHARS} characters, "
                         f"got {len(question)}")

    wants_lexical = req.provider == PROVIDER_LEXICAL
    provider = None if wants_lexical else chat.resolve_provider(req.provider)
    if provider is None and not wants_lexical:
        if req.provider in ("anthropic", "gemini"):
            # An explicit model request with no credentials is an error, not an
            # invitation to answer by other means. Substituting the router here
            # would hand back something that reads like a model wrote it.
            raise ModelUnavailable(
                f"no credentials for provider {req.provider}. "
                "Set ANTHROPIC_API_KEY or GEMINI_API_KEY, or ask for "
                f"provider '{PROVIDER_LEXICAL}', which needs neither.")
        wants_lexical = True                    # auto, and no model configured

    live = _live()
    executed: list[dict] = []

    with _connect() as conn:
        def run_query(name: str, params: dict) -> dict:
            out = catalog.run(name, params, live=live, conn=conn)
            executed.append({"query": out["query"], "params": out["params"],
                             "row_count": out["row_count"],
                             "rows": jsonable_rows(out["rows"])})
            return out

        if wants_lexical:
            out = lexical.answer(question, live=live, run_query=run_query)
            return {
                "question": question,
                "answer": lexical.statement(out),
                "refusal": out.refusal.message if out.refusal else None,
                "refusal_reason": out.refusal.reason if out.refusal else None,
                "alternatives": list(out.refusal.alternatives) if out.refusal else [],
                "stop_reason": "refused" if out.refusal else "routed",
                "turns": 1,
                "provider": PROVIDER_LEXICAL,
                "retrieved": [],
                # Stage 12's honesty surface: every bound value with where it
                # came from, and a notice for any the question did not supply.
                "slots": [{"name": s.name, "value": s.value, "source": s.source,
                           "evidence": s.evidence} for s in out.slots],
                "notices": out.notices,
                "considered": [{"query": q, "score": round(v, 4)}
                               for q, v in out.considered],
                "audit": executed,
            }

        try:
            retriever = chat.open_retriever(req.k) if req.retrieval else None
            answer = chat.ask(question, transport=chat.make_transport(provider, req.model),
                              live=live, run_query=run_query, retriever=retriever)
        except Exception as exc:
            diagnosis = chat.diagnose_provider_error(provider, exc)
            if diagnosis is None:
                raise
            raise ModelUnavailable(diagnosis) from exc

    # The loop's trail is authoritative for order and for refusals; `executed`
    # carries the rows.  Zipping by position would break the moment a query is
    # refused (refusals never reach run_query), so they are matched by advancing
    # an iterator only over the entries that actually ran.
    rows_by_position = iter(executed)
    audit = []
    for entry in answer.audit:
        item = dict(entry)
        if "error" not in entry:
            ran = next(rows_by_position, None)
            item["rows"] = ran["rows"] if ran else []
        audit.append(item)

    return {
        "question": question,
        "answer": answer.text,
        "refusal": answer.refusal,
        "refusal_reason": None,
        "alternatives": [],
        "stop_reason": answer.stop_reason,
        "turns": answer.turns,
        "provider": provider,
        "retrieved": answer.retrieved,
        "slots": [],
        "notices": [],
        "considered": [],
        "audit": audit,
    }


@app.get("/health")
def health() -> dict[str, Any]:
    with _connect() as conn:
        catalog.run_raw(conn, "SELECT 1")
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
