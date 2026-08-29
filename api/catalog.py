"""Stage 6: the query layer -- a fixed catalogue of parameterised queries.

This is the whole safety argument of FloatChat, so it is worth stating plainly.

The language model does NOT write SQL.  It chooses one of the named queries
below and fills in typed parameters.  Every query is written by hand, reviewed,
and shipped in this file; the model's entire influence is which one runs and
what values go into the placeholders.  That means:

  * No generated SQL means no SQL injection surface and no destructive
    statement, however the model is prompted.
  * Every answer the demo gives is traceable to a named query someone wrote.
  * The queries are testable without a model, an API key, or a network.

Three further defences, none of which rely on the model behaving:

  1. The connection uses the `floatchat_ro` role, which has SELECT and nothing
     else.  A DELETE is refused by Postgres, not by a prompt (db/roles.sql).
  2. Parameters are validated here before binding -- enums come from the
     DATABASE, so a region name that does not exist cannot be passed, and a
     hallucinated one is rejected with the list of real ones.
  3. Every statement runs under a statement_timeout and a row cap.

`to_tool_schema()` renders each query as an Anthropic tool definition with
`strict: true`, which is how Stage 7 hands this catalogue to Claude.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import psycopg
from psycopg.rows import dict_row

# The local development database, and the default.  `FLOATCHAT_DSN` overrides it
# so a deployment can point at a hosted Postgres without a source edit -- the
# credentials of a deployed database do not belong in a committed file.
#
# The default names `floatchat_ro` because defence 1 above assumes a role with
# SELECT and nothing else, and a deployment that supplies an owner DSN has
# quietly removed it.  Nothing here can inspect what an environment variable
# contains; what CAN check it is `api/test_catalog.py`, which tries a DELETE on
# whatever DSN is in force and fails if the connection executes it.  Run it
# against the deployment before believing the deployment is read-only (D15.2).
DSN = os.environ.get("FLOATCHAT_DSN") or \
    "postgresql://floatchat_ro:floatchat_ro@localhost:5432/floatchat"
STATEMENT_TIMEOUT_MS = 10_000
MAX_ROWS = 5_000

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class QueryError(ValueError):
    """A parameter the catalogue refuses.  The message is meant to be shown to
    the model, so it always says what WOULD have been acceptable."""


# --------------------------------------------------------------------------
# parameters
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Param:
    name: str
    kind: str                      # region | wmo | date | int | number | enum
    description: str
    required: bool = True
    default: Any = None
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()

    def coerce(self, value: Any, live: "LiveValues") -> Any:
        if self.kind == "region":
            if value not in live.regions:
                raise QueryError(
                    f"{self.name}: '{value}' is not a region in this database. "
                    f"Valid regions: {', '.join(live.regions)}")
            return value
        if self.kind == "wmo":
            v = str(value)
            if v not in live.wmos:
                raise QueryError(
                    f"{self.name}: float '{v}' is not in this database. "
                    f"Valid floats: {', '.join(live.wmos)}")
            return v
        if self.kind == "date":
            if not (isinstance(value, str) and ISO_DATE.match(value)):
                raise QueryError(f"{self.name}: expected a YYYY-MM-DD date, got {value!r}")
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise QueryError(f"{self.name}: {exc}") from exc
            return value
        if self.kind == "enum":
            if value not in self.choices:
                raise QueryError(f"{self.name}: expected one of {', '.join(self.choices)}, "
                                 f"got {value!r}")
            return value
        if self.kind in ("int", "number"):
            try:
                v = int(value) if self.kind == "int" else float(value)
            except (TypeError, ValueError) as exc:
                raise QueryError(f"{self.name}: expected a number, got {value!r}") from exc
            if self.minimum is not None and v < self.minimum:
                raise QueryError(f"{self.name}: must be >= {self.minimum}, got {v}")
            if self.maximum is not None and v > self.maximum:
                raise QueryError(f"{self.name}: must be <= {self.maximum}, got {v}")
            return v
        raise QueryError(f"{self.name}: unknown parameter kind {self.kind!r}")

    def json_schema(self, live: "LiveValues") -> dict:
        if self.kind == "region":
            return {"type": "string", "enum": list(live.regions), "description": self.description}
        if self.kind == "wmo":
            return {"type": "string", "enum": list(live.wmos), "description": self.description}
        if self.kind == "date":
            return {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$",
                    "description": f"{self.description} (YYYY-MM-DD)"}
        if self.kind == "enum":
            return {"type": "string", "enum": list(self.choices), "description": self.description}
        schema: dict[str, Any] = {"type": "integer" if self.kind == "int" else "number",
                                  "description": self.description}
        if self.minimum is not None:
            schema["minimum"] = self.minimum
        if self.maximum is not None:
            schema["maximum"] = self.maximum
        return schema


@dataclass(frozen=True)
class Query:
    name: str
    description: str
    sql: str
    params: tuple[Param, ...] = ()
    example: dict = field(default_factory=dict)

    def validate(self, given: dict, live: "LiveValues") -> dict:
        known = {p.name for p in self.params}
        unknown = set(given) - known
        if unknown:
            raise QueryError(f"{self.name}: unknown parameter(s) {', '.join(sorted(unknown))}. "
                             f"Accepted: {', '.join(sorted(known))}")
        out = {}
        for p in self.params:
            if p.name in given and given[p.name] is not None:
                out[p.name] = p.coerce(given[p.name], live)
            elif p.required:
                raise QueryError(f"{self.name}: missing required parameter '{p.name}'")
            else:
                out[p.name] = p.default
        return out

    def to_tool_schema(self, live: "LiveValues") -> dict:
        """An Anthropic tool definition.  strict=True plus additionalProperties
        false means the model cannot invent a parameter that we then ignore."""
        return {
            "name": self.name,
            "description": self.description,
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": {p.name: p.json_schema(live) for p in self.params},
                "required": [p.name for p in self.params if p.required],
                "additionalProperties": False,
            },
        }


# --------------------------------------------------------------------------
# live values -- enums come from the database, not from a constant
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class LiveValues:
    regions: tuple[str, ...]
    wmos: tuple[str, ...]
    window: tuple[str, str]

    @classmethod
    def load(cls) -> "LiveValues":
        with connect() as conn:
            regions = [r["name"] for r in run_raw(conn,
                       "SELECT name FROM regions ORDER BY name")]
            wmos = [r["wmo"] for r in run_raw(conn,
                    "SELECT wmo FROM floats ORDER BY wmo")]
            w = run_raw(conn, "SELECT window_start::date::text AS s, "
                              "window_end::date::text AS e FROM ingest_run")[0]
        return cls(tuple(regions), tuple(wmos), (w["s"], w["e"]))


# --------------------------------------------------------------------------
# the catalogue
# --------------------------------------------------------------------------

P_REGION = Param("region", "region", "Named ocean region (IHO boundary)")
P_START = Param("start", "date", "Inclusive start of the date range")
P_END = Param("end", "date", "Inclusive end of the date range")
P_WMO = Param("wmo", "wmo", "ARGO float WMO identifier")
P_LIMIT = Param("limit", "int", "Maximum rows to return", required=False,
                default=200, minimum=1, maximum=MAX_ROWS)

QUERIES: tuple[Query, ...] = (
    Query(
        name="profiles_in_region",
        description=("List individual ARGO profiles inside a named region and date "
                     "range, newest first. Use for 'show me profiles in X'."),
        params=(P_REGION, P_START, P_END, P_LIMIT),
        example={"region": "Bay of Bengal", "start": "2023-03-01", "end": "2023-03-31"},
        sql="""
            SELECT p.profile_id, p.wmo, f.dac, p.cycle, p.juld::date AS date,
                   round(p.lat::numeric, 3) AS lat, round(p.lon::numeric, 3) AS lon,
                   p.data_mode, p.n_levels, round(p.pres_max::numeric, 1) AS deepest_dbar
            FROM profiles p
            JOIN floats f USING (wmo)
            JOIN profile_regions pr ON pr.profile_id = p.profile_id
            WHERE pr.region = %(region)s
              AND p.juld >= %(start)s::date AND p.juld < (%(end)s::date + 1)
            ORDER BY p.juld DESC
            LIMIT %(limit)s""",
    ),
    Query(
        name="region_summary",
        description=("One-row summary of a region and date range: how many profiles and "
                     "floats, the dates actually covered, and the depth reached."),
        params=(P_REGION, P_START, P_END),
        example={"region": "Arabian Sea", "start": "2023-01-01", "end": "2024-12-31"},
        sql="""
            SELECT %(region)s AS region,
                   count(*) AS profiles, count(DISTINCT p.wmo) AS floats,
                   min(p.juld)::date AS first_profile, max(p.juld)::date AS last_profile,
                   round(max(p.pres_max)::numeric, 1) AS deepest_dbar,
                   count(*) FILTER (WHERE p.data_mode = 'D') AS delayed_mode,
                   count(*) FILTER (WHERE p.data_mode = 'A') AS realtime_adjusted,
                   count(*) FILTER (WHERE p.data_mode = 'R') AS realtime
            FROM profiles p
            JOIN profile_regions pr ON pr.profile_id = p.profile_id
            WHERE pr.region = %(region)s
              AND p.juld >= %(start)s::date AND p.juld < (%(end)s::date + 1)""",
    ),
    Query(
        name="monthly_profile_counts",
        description="Profiles per calendar month in a region. Use for 'how did X change over time'.",
        params=(P_REGION, P_START, P_END),
        example={"region": "Bay of Bengal", "start": "2023-01-01", "end": "2024-12-31"},
        sql="""
            SELECT date_trunc('month', p.juld)::date AS month,
                   count(*) AS profiles, count(DISTINCT p.wmo) AS floats
            FROM profiles p
            JOIN profile_regions pr ON pr.profile_id = p.profile_id
            WHERE pr.region = %(region)s
              AND p.juld >= %(start)s::date AND p.juld < (%(end)s::date + 1)
            GROUP BY 1 ORDER BY 1""",
    ),
    Query(
        name="depth_profile",
        description=("Mean temperature and salinity against depth for a region and date "
                     "range, averaged into fixed-width pressure bins. This is the query "
                     "behind 'show me the temperature profile in X'."),
        params=(P_REGION, P_START, P_END,
                Param("bin_dbar", "int", "Pressure bin width in decibars", required=False,
                      default=50, minimum=5, maximum=500),
                Param("max_dbar", "number", "Deepest pressure to include", required=False,
                      default=2000, minimum=10, maximum=6000)),
        example={"region": "Arabian Sea", "start": "2023-01-01", "end": "2023-12-31"},
        sql="""
            SELECT (floor(l.pres / %(bin_dbar)s) * %(bin_dbar)s)::int AS depth_bin_dbar,
                   count(*) AS levels,
                   round(avg(l.temp)::numeric, 3) AS mean_temp_c,
                   round(avg(l.psal)::numeric, 3) AS mean_psal_psu,
                   round(stddev_samp(l.temp)::numeric, 3) AS sd_temp_c
            FROM levels l
            JOIN profiles p USING (profile_id)
            JOIN profile_regions pr ON pr.profile_id = p.profile_id
            WHERE pr.region = %(region)s
              AND p.juld >= %(start)s::date AND p.juld < (%(end)s::date + 1)
              AND l.pres <= %(max_dbar)s
            GROUP BY 1 ORDER BY 1""",
    ),
    Query(
        name="surface_conditions",
        description=("Mean near-surface (0-10 dbar by default) temperature and salinity "
                     "per region over a date range. Use to compare regions."),
        params=(P_START, P_END,
                Param("max_dbar", "number", "Deepest pressure counted as 'surface'",
                      required=False, default=10, minimum=1, maximum=200)),
        example={"start": "2023-01-01", "end": "2024-12-31"},
        sql="""
            SELECT pr.region, count(DISTINCT p.profile_id) AS profiles,
                   round(avg(l.temp)::numeric, 2) AS mean_temp_c,
                   round(avg(l.psal)::numeric, 3) AS mean_psal_psu
            FROM levels l
            JOIN profiles p USING (profile_id)
            JOIN profile_regions pr ON pr.profile_id = p.profile_id
            WHERE l.pres <= %(max_dbar)s
              AND p.juld >= %(start)s::date AND p.juld < (%(end)s::date + 1)
            GROUP BY pr.region ORDER BY profiles DESC""",
    ),
    Query(
        name="float_trajectory",
        description="Every surfacing position of one float, in time order. Use for 'where did float X go'.",
        params=(P_WMO, P_LIMIT),
        example={"wmo": "6903139"},
        sql="""
            SELECT p.cycle, p.juld::date AS date,
                   round(p.lat::numeric, 3) AS lat, round(p.lon::numeric, 3) AS lon,
                   pr.region, p.data_mode, p.in_study_box
            FROM profiles p
            LEFT JOIN profile_regions pr ON pr.profile_id = p.profile_id
            WHERE p.wmo = %(wmo)s
            ORDER BY p.juld
            LIMIT %(limit)s""",
    ),
    Query(
        name="nearest_profiles",
        description=("Profiles within a great-circle radius of a point, nearest first. "
                     "Use for 'profiles near <place>' once the place has coordinates."),
        params=(Param("lat", "number", "Latitude in degrees north", minimum=-90, maximum=90),
                Param("lon", "number", "Longitude in degrees east", minimum=-180, maximum=180),
                Param("radius_km", "number", "Search radius in kilometres", required=False,
                      default=200, minimum=1, maximum=5000),
                P_LIMIT),
        example={"lat": 15.0, "lon": 68.0, "radius_km": 300},
        sql="""
            SELECT p.profile_id, p.wmo, p.juld::date AS date, pr.region,
                   round(p.lat::numeric, 3) AS lat, round(p.lon::numeric, 3) AS lon,
                   round((earth_distance(ll_to_earth(%(lat)s, %(lon)s),
                                         ll_to_earth(p.lat, p.lon)) / 1000)::numeric, 1) AS km
            FROM profiles p
            LEFT JOIN profile_regions pr ON pr.profile_id = p.profile_id
            WHERE earth_box(ll_to_earth(%(lat)s, %(lon)s), %(radius_km)s * 1000) @> ll_to_earth(p.lat, p.lon)
              AND earth_distance(ll_to_earth(%(lat)s, %(lon)s),
                                 ll_to_earth(p.lat, p.lon)) <= %(radius_km)s * 1000
            ORDER BY km
            LIMIT %(limit)s""",
    ),
    Query(
        name="compare_regions",
        description=("Side-by-side mean temperature and salinity for two regions in one "
                     "depth band. Use for 'is X saltier than Y'."),
        params=(Param("region_a", "region", "First region"),
                Param("region_b", "region", "Second region"),
                P_START, P_END,
                Param("min_dbar", "number", "Shallowest pressure in the band",
                      required=False, default=0, minimum=0, maximum=6000),
                Param("max_dbar", "number", "Deepest pressure in the band",
                      required=False, default=10, minimum=1, maximum=6000)),
        example={"region_a": "Bay of Bengal", "region_b": "Arabian Sea",
                 "start": "2023-01-01", "end": "2024-12-31"},
        sql="""
            SELECT pr.region, count(DISTINCT p.profile_id) AS profiles, count(*) AS levels,
                   round(avg(l.temp)::numeric, 3) AS mean_temp_c,
                   round(avg(l.psal)::numeric, 3) AS mean_psal_psu
            FROM levels l
            JOIN profiles p USING (profile_id)
            JOIN profile_regions pr ON pr.profile_id = p.profile_id
            WHERE pr.region IN (%(region_a)s, %(region_b)s)
              AND l.pres BETWEEN %(min_dbar)s AND %(max_dbar)s
              AND p.juld >= %(start)s::date AND p.juld < (%(end)s::date + 1)
            GROUP BY pr.region ORDER BY pr.region""",
    ),
    Query(
        name="float_inventory",
        description="Every float in the database with its DAC, why it was selected, and its profile counts.",
        params=(),
        example={},
        sql="""
            SELECT f.wmo, f.dac, f.profiler_type, f.dm_status,
                   count(p.*) AS profiles_loaded, f.n_profiles_index AS profiles_indexed,
                   min(p.juld)::date AS first, max(p.juld)::date AS last,
                   f.selection_reason
            FROM floats f LEFT JOIN profiles p USING (wmo)
            GROUP BY f.wmo, f.dac, f.profiler_type, f.dm_status,
                     f.n_profiles_index, f.selection_reason
            ORDER BY profiles_loaded DESC""",
    ),
    Query(
        name="data_provenance",
        description=("For one float: how many profiles are delayed-mode vs real-time, "
                     "which copy of each value was used, and how deep it went. Use when "
                     "asked how trustworthy or how calibrated the data is."),
        params=(P_WMO,),
        example={"wmo": "2902203"},
        sql="""
            SELECT p.data_mode, p.psal_source, count(*) AS profiles,
                   min(p.juld)::date AS first, max(p.juld)::date AS last,
                   round(avg(p.n_levels)::numeric, 0) AS mean_levels
            FROM profiles p
            WHERE p.wmo = %(wmo)s
            GROUP BY p.data_mode, p.psal_source
            ORDER BY profiles DESC""",
    ),
    Query(
        name="missing_profiles",
        description=("Why profiles are absent: the profiles this pipeline refused, with "
                     "the reason. Use when a count looks lower than expected."),
        params=(Param("wmo", "wmo", "ARGO float WMO identifier", required=False, default=None),),
        example={"wmo": "2902203"},
        sql="""
            SELECT d.profile_id, d.wmo, d.reason, d.detail, d.was_indexed
            FROM dropped_profiles d
            WHERE (%(wmo)s::text IS NULL OR d.wmo = %(wmo)s)
            ORDER BY d.profile_id""",
    ),
)

BY_NAME = {q.name: q for q in QUERIES}


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------

def connect() -> psycopg.Connection:
    conn = psycopg.connect(DSN, row_factory=dict_row, autocommit=True)
    conn.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
    return conn


def run_raw(conn: psycopg.Connection, sql: str, params: dict | None = None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params or {})
        return cur.fetchall()


def run(name: str, params: dict, live: LiveValues | None = None,
        conn: psycopg.Connection | None = None) -> dict:
    """Validate, execute, and return rows plus what was actually run.

    The returned `query`/`params` are what makes an answer auditable: the demo
    can always show which named query produced a number.
    """
    if name not in BY_NAME:
        raise QueryError(f"no query named '{name}'. Available: {', '.join(BY_NAME)}")
    query = BY_NAME[name]
    live = live or LiveValues.load()
    bound = query.validate(params, live)

    own = conn is None
    conn = conn or connect()
    try:
        rows = run_raw(conn, query.sql, bound)
    finally:
        if own:
            conn.close()

    return {"query": name, "params": bound, "row_count": len(rows), "rows": rows}


def tool_schemas(live: LiveValues | None = None) -> list[dict]:
    """The catalogue as Anthropic tool definitions -- the Stage 7 hand-off."""
    live = live or LiveValues.load()
    return [q.to_tool_schema(live) for q in QUERIES]
