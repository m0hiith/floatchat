"""Stage 12: answering with no model at all.

**What this is, in one sentence:** a lexical router that picks one catalogue
query from a question and fills its parameters, so the Chat tab works on a
machine with no API key, no network and no local model.

**What it is a sibling of.**  This is a sibling of `chat.ask`, not a sibling of
`gemini.GeminiTransport`.  It does not satisfy the `Transport` protocol and it
must never be registered as one: `Transport` is a seam to a *model*, and there
is no model here.  What it *does* reuse is the other seam D7.4 introduced --
the injected `run_query` executor -- so this module never touches psycopg, and
every row it returns came through `catalog.run` under `floatchat_ro` exactly as
the dropdowns' rows do.

**What it is not.**  It is not semantic (rule 9).  Routing is nearest-exemplar
cosine over a hashed n-gram embedding: the same lexical method Stage 11 ships,
under the same honest name.  It does not understand the question, it does not
infer intent, and it cannot paraphrase.  Every string this module produces about
its own behaviour says so.

Four stages, and each can refuse:

    scope_gate    is this answerable at all?      -> Refusal(no-bgc | outside-window)
    route         which of the eleven queries?    -> Refusal(unroutable) below a floor
    fill_slots    what goes in its parameters?    -> Slot(source=...) for every one
    run_query     the D7.4 executor seam           -> catalogue refusals, unchanged

The thing this module is most careful about is **slot provenance**.  A date the
question did not contain is not silently replaced with the study window: the
`Slot` records `source="window-fallback"`, the panel prints a notice above the
chart, and the audit trail shows the bound dates like any other parameter.
Stage 10 spent a whole decision (D10.5) making the catalogue's defaults visible
rather than assumed; a router that quietly invented a date range would undo it.
"""

from __future__ import annotations

import calendar
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import catalog
import embed
from catalog import QueryError

# The one constant fitted to the question set, declared here rather than buried
# because that makes it the measurement's known weakness (D12.6).
#
# There is no clean separating value: in-scope questions score 0.159..0.779 and
# out-of-scope ones that reach the router score up to 0.226, so the two
# distributions OVERLAP and any floor trades in-scope recall against
# false accepts. 0.23 sits just above the highest-scoring out-of-scope question
# in the set. It was chosen after the structural gates above removed the one
# genuinely dangerous case, so it is trading paraphrase recall only -- it is
# not the thing standing between a destructive-sounding request and a table of
# results. Lowering it to 0.20 admits a weather question and a satellite
# question; the numbers for both settings are in D12.6.
ROUTE_FLOOR = 0.23

# Parameters this database does not measure.  Hardcoding a list of absences
# looks like the knowledge D10.3 spent a stage removing, so it is *checked*
# rather than trusted: `api/test_router.py` asserts that none of these is a
# column in `levels`.  If a biogeochemical parameter is ever ingested, the
# suite fails and forces this list to be corrected instead of quietly refusing
# data the database now holds.
NOT_MEASURED = (
    "oxygen", "doxy", "chlorophyll", "chla", "nitrate", "ph", "bgc",
    "biogeochemical", "bio-geo-chemical", "backscatter", "irradiance",
    "cdom", "turbidity", "fluorescence", "alkalinity", "carbon",
)

MEASURED = ("pressure (dbar)", "temperature (degrees C)", "practical salinity (PSU)")

# Verbs asking this interface to CHANGE something or to produce an artefact it
# does not produce.  Refused structurally, before routing, rather than left to
# the similarity floor.
#
# This is not a security control -- `floatchat_ro` is, and it holds SELECT and
# nothing else (D6.3), so "delete all the profiles" could never have deleted
# anything.  It is a HONESTY control.  Without it that question scored 0.299
# against `float_inventory`, cleared the floor, and rendered a table of floats:
# a destructive request answered with data, looking for all the world like it
# had been carried out.  The verbs are deliberately unambiguous ones; "remove"
# and "send" are left out because a legitimate question can contain them.
NOT_PERFORMED = ("delete", "truncate", "drop", "insert", "update", "alter",
                 "export", "download", "email", "train")


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Refusal:
    """A question this router will not answer, and why.

    `reason` is a machine-readable kind so the evaluation can tell a *correct*
    refusal from one that declined for the wrong reason -- refusing a
    biogeochemical question as 'outside the date window' is a wrong answer that
    happens to say no, and refusal recall is the number that catches it.
    """
    reason: str                      # no-bgc | outside-window | unroutable
    message: str
    alternatives: tuple[str, ...] = ()


@dataclass(frozen=True)
class Slot:
    """One bound parameter, and where its value came from.

    `source` is the whole point of this dataclass existing:

        extracted          read out of the question, `evidence` is the substring
        window-fallback    the question had no parseable date; the study window
                           was used and the caller MUST say so
        catalogue-default  left unset; `Param` supplies its own default
        missing            required and not found -- the router refuses
    """
    name: str
    value: Any
    source: str
    evidence: str = ""


@dataclass
class Routed:
    """What the router decided, in a shape `/ask` can return unchanged."""
    question: str
    query: str | None = None
    params: dict = field(default_factory=dict)
    slots: list[Slot] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    refusal: Refusal | None = None
    considered: list[tuple[str, float]] = field(default_factory=list)

    @property
    def routed(self) -> bool:
        return self.query is not None and self.refusal is None


# --------------------------------------------------------------------------
# routing fixtures
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Route:
    """Ways of asking for one catalogue query.

    **These are fixtures, not corpus documents, and D11.4 does not apply.**
    D11.4 governs `api/corpus.py`: things that are retrieved and shown as
    evidence *about the ocean*, which therefore have to be generated from a
    query they carry.  An exemplar carries no fact.  It is a phrasing, it is
    matched *against* and never returned *as* content, and no word of it
    reaches an answer.  It is the same species as `retrieval.EVALUATION`'s
    patterns and `catalog.Query.example`: a fixture describing how to *reach*
    the data, not a claim *about* it.

    Enforced mechanically: no exemplar may contain a digit.  A digit in a
    phrasing means the phrasing is carrying a value, and values come from
    `fill_slots` or from the catalogue's own defaults -- never from here.
    """
    query: str
    exemplars: tuple[str, ...]


ROUTES: tuple[Route, ...] = (
    Route("profiles_in_region", (
        "show me the profiles in a region",
        "list the profiles for an area",
        "which profiles are in this sea",
        "salinity profiles in a region",
        "temperature profiles for an area during a period",
        "what profiles do you have there",
        "individual profiles with their positions",
        "profiles collected in a named sea",
        "map the profiles in an area",
        "every profile inside a region",
    )),
    Route("region_summary", (
        "summarise a region",
        "give me an overview of an area",
        "how many profiles and floats are in a region",
        "what does the record look like for this sea",
        "a summary of coverage for an area",
        "the totals for one region",
        "how much data is there for this sea",
        "overall figures for a region",
        "brief me on an area",
        "how deep does the record go in a region",
    )),
    Route("monthly_profile_counts", (
        "profiles per month",
        "how did coverage change over time",
        "month by month counts",
        "the time series of profile numbers",
        "how many profiles each month",
        "counts over the months",
        "trend in profiles across the period",
        "monthly totals for a region",
        "how did sampling vary through the year",
        "plot profile counts against time",
    )),
    Route("depth_profile", (
        "temperature against depth",
        "salinity against depth",
        "plot the temperature profile",
        "show me the vertical profile",
        "how does temperature change with depth",
        "how does salinity vary with pressure",
        "what does the water column look like",
        "mean temperature by pressure bin",
        "the depth profile for a region",
        "temperature at different depths",
    )),
    Route("surface_conditions", (
        "conditions near the surface",
        "surface temperature and salinity",
        "how warm is the surface",
        "compare the surface across regions",
        "near-surface values everywhere",
        "which region has the saltiest surface water",
        "the shallowest measurements by region",
        "sea surface conditions by area",
        "surface salinity across all the seas",
        "which sea is warmest at the top",
    )),
    Route("float_trajectory", (
        "where did this float go",
        "show me the trajectory",
        "the path this float took",
        "plot where a float travelled",
        "map the drift of a float",
        "every surfacing position for a float",
        "track a float over time",
        "where has this float been",
        "the route of a float",
        "show the positions of a float in order",
    )),
    Route("nearest_profiles", (
        "which floats are nearest to this location",
        "profiles near a point",
        "closest profiles to these coordinates",
        "what is near this latitude and longitude",
        "floats within a radius",
        "anything close to this position",
        "how far away is the closest float",
        "profiles around this spot",
        "nearest floats to a given position",
        "search near a coordinate",
    )),
    Route("compare_regions", (
        "compare two regions",
        "is one sea saltier than another",
        "which of these two areas is fresher",
        "put two regions side by side",
        "the difference between two seas",
        "contrast one area with another",
        "warmer or cooler than the other region",
        "compare salinity between two areas",
        "how do these two seas differ",
        "one region versus another",
    )),
    Route("float_inventory", (
        "list all the floats",
        "which floats are in this database",
        "show me the float inventory",
        "what floats do you have",
        "how many profiles does each float have",
        "which data centres operate these floats",
        "which float went deepest",
        "give me every float with its counts",
        "the full list of floats",
        "which floats are Indian",
    )),
    Route("data_provenance", (
        "how trustworthy is this float's data",
        "is this float calibrated",
        "delayed mode or real time for this float",
        "which copy of the salinity was used",
        "what is the data quality for a float",
        "how was this float's data processed",
        "is the salinity adjusted or raw",
        "the provenance of a float's measurements",
        "real time versus delayed mode breakdown",
        "how reliable are this float's readings",
    )),
    Route("missing_profiles", (
        "which profiles were dropped",
        "why are there fewer profiles than expected",
        "what did the pipeline refuse",
        "show me the rejected profiles",
        "why is the count lower than the index said",
        "what was thrown away and why",
        "the profiles that did not make it in",
        "explain the missing records",
        "which profiles failed quality control",
        "what is absent and for what reason",
    )),
)

BY_QUERY = {r.query: r for r in ROUTES}


# --------------------------------------------------------------------------
# the scope gate
# --------------------------------------------------------------------------

WORD = re.compile(r"[a-z0-9']+")
YEAR = re.compile(r"\b(19|20)\d{2}\b")
ISO = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
COORD = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*°?\s*([NnSs])\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*°?\s*([EeWw])")
COORD_PLAIN = re.compile(r"(?:lat(?:itude)?)\D{0,4}(-?\d+(?:\.\d+)?)\D{1,12}"
                         r"(?:lon(?:gitude)?)\D{0,4}(-?\d+(?:\.\d+)?)")


def words(text: str) -> list[str]:
    return WORD.findall(text.lower())


def scope_gate(question: str, live: catalog.LiveValues) -> Refusal | None:
    """Refuse what the database cannot answer, before routing gets a chance.

    Both refusals name what IS available, the way every catalogue refusal does
    (D6.2) -- a 'no' that does not say what would have worked is a dead end.
    """
    tokens = set(words(question))

    doing = [t for t in NOT_PERFORMED if t in tokens]
    if doing:
        return Refusal(
            "unroutable",
            f"This interface cannot {doing[0]} anything. It reads the database "
            f"through {len(catalog.QUERIES)} fixed queries on a connection that "
            f"holds SELECT and nothing else, and it has no export, mail or "
            f"training path. Ask it a question about the data instead.",
            tuple(q.name for q in catalog.QUERIES))

    absent = [t for t in NOT_MEASURED if t in tokens]
    if absent:
        return Refusal(
            "no-bgc",
            f"This database has no {absent[0]} measurements. These ten floats are "
            f"core ARGO and carry no biogeochemical parameters at all. What is "
            f"measured: {', '.join(MEASURED)}.",
            MEASURED)

    # A year the database cannot possibly cover. Only refuse when EVERY year
    # named is outside the window -- "compare 2019 with 2023" still has a year
    # we can answer for, and refusing it outright would be wrong.
    years = [int(y.group()) for y in YEAR.finditer(question)]
    if years:
        lo, hi = int(live.window[0][:4]), int(live.window[1][:4])
        if all(y < lo or y > hi for y in years):
            named = ", ".join(str(y) for y in sorted(set(years)))
            return Refusal(
                "outside-window",
                f"This database covers {live.window[0]} to {live.window[1]} only. "
                f"Nothing from {named} exists here.",
                (f"{live.window[0]} .. {live.window[1]}",))
    return None


# --------------------------------------------------------------------------
# routing -- nearest exemplar, and nothing cleverer
# --------------------------------------------------------------------------

def mask(question: str, live: catalog.LiveValues) -> str:
    """Replace the values in a question with neutral placeholders, for routing.

    Routing is about the SHAPE of a question -- "compare two of these" -- and
    the values are `fill_slots`'s job.  Left in, they are noise: "is the Bay of
    Bengal fresher than the Arabian Sea" shares almost no tokens with "compare
    two regions", and the concrete names actively pull the score down.  Masked,
    the two are obviously the same question.

    This is the same principle the no-digit rule enforces on the other side of
    the match: neither an exemplar nor the question being matched against it
    should carry a value.  Measured, not asserted -- a check compares routing
    accuracy with masking on and off.

    Values are REMOVED, not replaced with a phrase.  Substituting "a region"
    was tried first and was worse than doing nothing: the placeholder is itself
    a token, it matched the exemplar "the depth profile for a region", and
    "is the Bay of Bengal fresher than the Arabian Sea" routed to
    `depth_profile`.  A masked value has to contribute nothing to any route,
    not contribute equally to the wrong one.  The sentinel is punctuation,
    which `embed.features` drops on the floor.
    """
    out = question
    for name in sorted(live.regions, key=len, reverse=True):
        out = re.sub(re.escape(name), " \u00a7 ", out, flags=re.IGNORECASE)
    out = COORD.sub(" \u00a7 ", out)
    for token in re.findall(r"\b\d{5,9}\b", out):
        if token in live.wmos:
            out = out.replace(token, " \u00a7 ")
    return re.sub(r"\s+", " ", out).strip()


class Router:
    """Nearest-exemplar cosine over the routing fixtures.

    `max` over a route's exemplars rather than `mean`: a question that matches
    one phrasing well should not be diluted by the nine other ways of asking.
    """

    def __init__(self, routes: tuple[Route, ...] = ROUTES,
                 floor: float = ROUTE_FLOOR, mask_values: bool = True):
        self.routes, self.floor, self.mask_values = routes, floor, mask_values
        self.texts = [e for r in routes for e in r.exemplars]
        self.owner = [r.query for r in routes for _ in r.exemplars]
        self.embedder = embed.HashingEmbedder().fit(self.texts)
        self.matrix = self.embedder.embed_documents(self.texts)

    @property
    def name(self) -> str:
        return f"lexical-exemplar:{self.embedder.name}"

    def scores(self, question: str, live: catalog.LiveValues | None = None) -> list[tuple[str, float]]:
        q = self.embedder.embed_query(
            mask(question, live) if (live and self.mask_values) else question)
        sims = self.matrix @ q
        best: dict[str, float] = {}
        for query, s in zip(self.owner, sims):
            best[query] = max(best.get(query, -1.0), float(s))
        return sorted(best.items(), key=lambda kv: -kv[1])

    def route(self, question: str,
              live: catalog.LiveValues | None = None) -> tuple[str | None, list[tuple[str, float]]]:
        ranked = self.scores(question, live)
        if not ranked or ranked[0][1] < self.floor:
            return None, ranked
        return ranked[0][0], ranked


# --------------------------------------------------------------------------
# slot filling -- every value carries where it came from
# --------------------------------------------------------------------------

def find_regions(question: str, live: catalog.LiveValues) -> list[tuple[str, int]]:
    """Regions named in the question, in the order they appear.

    Longest name first, so a region whose name contains another's is matched
    whole. The candidate list is `live.regions`, read from the database -- no
    region name is written into this file (D10.3's rule, applied here).
    """
    found: list[tuple[str, int]] = []
    lowered = question.lower()
    for name in sorted(live.regions, key=len, reverse=True):
        at = lowered.find(name.lower())
        if at >= 0 and not any(at >= s and at < s + len(n) for n, s in found):
            found.append((name, at))
    return [(n, at) for n, at in sorted(found, key=lambda p: p[1])]


def find_wmo(question: str, live: catalog.LiveValues) -> str | None:
    for token in re.findall(r"\b\d{5,9}\b", question):
        if token in live.wmos:
            return token
    return None


def find_coords(question: str) -> tuple[float, float] | None:
    m = COORD.search(question)
    if m:
        lat = float(m.group(1)) * (-1 if m.group(2).lower() == "s" else 1)
        lon = float(m.group(3)) * (-1 if m.group(4).lower() == "w" else 1)
        return lat, lon
    m = COORD_PLAIN.search(question.lower())
    if m:
        return float(m.group(1)), float(m.group(2))
    return None


def clamp(day: date, live: catalog.LiveValues) -> date:
    lo = date.fromisoformat(live.window[0])
    hi = date.fromisoformat(live.window[1])
    return max(lo, min(hi, day))


def find_dates(question: str, live: catalog.LiveValues) -> tuple[str, str, str, str]:
    """(start, end, source, evidence).

    `source` is `extracted` when the question actually contained a date, and
    `window-fallback` when it did not.  There is no third outcome and no silent
    substitution: everything unparseable -- "last 6 months", "recently", "this
    year" -- lands in the fallback branch, which the caller is required to
    announce.
    """
    lowered = question.lower()

    iso = ISO.findall(question)
    if len(iso) >= 2:
        return iso[0], iso[1], "extracted", f"{iso[0]} .. {iso[1]}"
    if len(iso) == 1:
        return iso[0], iso[0], "extracted", iso[0]

    years = [int(y.group()) for y in YEAR.finditer(question)]
    months = [(MONTHS[w], w) for w in words(question) if w in MONTHS]

    if months and years:
        year = years[0]
        first_m, first_w = months[0]
        last_m, last_w = months[-1]
        start = date(year, first_m, 1)
        end = date(years[-1], last_m,
                   calendar.monthrange(years[-1], last_m)[1])
        evidence = f"{first_w} {year}" if first_m == last_m else \
                   f"{first_w}..{last_w} {years[-1]}"
        return (clamp(start, live).isoformat(), clamp(end, live).isoformat(),
                "extracted", evidence)

    if years:
        start = clamp(date(min(years), 1, 1), live)
        end = clamp(date(max(years), 12, 31), live)
        return start.isoformat(), end.isoformat(), "extracted", \
            ", ".join(str(y) for y in sorted(set(years)))

    return live.window[0], live.window[1], "window-fallback", lowered[:0]


NO_DATE_NOTICE = (
    "No date range in the question, so the full study window "
    "{start} to {end} was used. The audit trail shows the dates that were bound.")

NEED_COORDS = (
    "This query needs coordinates and the question has none. There is no "
    "place-name lookup in this project -- writing one would mean inventing "
    "latitudes that are nowhere in the database. Give a position instead, "
    "for example: 15N 68E.")


def fill_slots(question: str, query_name: str,
               live: catalog.LiveValues) -> tuple[dict, list[Slot], list[str], Refusal | None]:
    """Bind the parameters a query needs, recording the provenance of each.

    Nothing here decides whether a value is *acceptable* -- `Param.coerce` does
    that against database-derived enums, and refuses with the valid list, for
    the router exactly as it does for the model (D6.2).  A region this function
    mis-extracts is refused by the catalogue, not by a second validator.
    """
    query = catalog.BY_NAME[query_name]
    needed = {p.name for p in query.params}
    params: dict[str, Any] = {}
    slots: list[Slot] = []
    notices: list[str] = []

    regions = find_regions(question, live)
    if "region" in needed:
        if not regions:
            return {}, slots, notices, Refusal(
                "unroutable",
                f"'{query_name}' needs a region and the question names none.",
                tuple(live.regions))
        params["region"] = regions[0][0]
        slots.append(Slot("region", regions[0][0], "extracted", regions[0][0]))

    if "region_a" in needed:
        if len(regions) < 2:
            return {}, slots, notices, Refusal(
                "unroutable",
                f"'{query_name}' compares two regions and the question names "
                f"{len(regions)}.",
                tuple(live.regions))
        params["region_a"], params["region_b"] = regions[0][0], regions[1][0]
        slots.append(Slot("region_a", regions[0][0], "extracted", regions[0][0]))
        slots.append(Slot("region_b", regions[1][0], "extracted", regions[1][0]))

    if "wmo" in needed:
        wmo = find_wmo(question, live)
        required = next(p.required for p in query.params if p.name == "wmo")
        if wmo:
            params["wmo"] = wmo
            slots.append(Slot("wmo", wmo, "extracted", wmo))
        elif required:
            return {}, slots, notices, Refusal(
                "unroutable",
                f"'{query_name}' needs a float and the question names none of "
                f"the {len(live.wmos)} in this database.",
                tuple(live.wmos))
        else:
            slots.append(Slot("wmo", None, "catalogue-default"))

    if "lat" in needed:
        coords = find_coords(question)
        if not coords:
            return {}, slots, notices, Refusal("unroutable", NEED_COORDS, ())
        params["lat"], params["lon"] = coords
        slots.append(Slot("lat", coords[0], "extracted", str(coords[0])))
        slots.append(Slot("lon", coords[1], "extracted", str(coords[1])))

    if "start" in needed:
        start, end, source, evidence = find_dates(question, live)
        params["start"], params["end"] = start, end
        slots.append(Slot("start", start, source, evidence))
        slots.append(Slot("end", end, source, evidence))
        if source == "window-fallback":
            notices.append(NO_DATE_NOTICE.format(start=start, end=end))

    # Everything else the query accepts is left to the catalogue, which fills
    # its own documented default and reports it back in `params` -- the same
    # path the dropdowns take, so the audit panel shows it identically.
    for p in query.params:
        if p.name not in params and not p.required:
            slots.append(Slot(p.name, p.default, "catalogue-default"))

    return params, slots, notices, None


# --------------------------------------------------------------------------
# the entry point -- the sibling of chat.ask
# --------------------------------------------------------------------------

RAN = ("Ran the catalogue query `{query}`. There is no model in this path: the "
       "query was chosen by matching your wording against written examples, and "
       "the result below is the query's own rows.")

PROVIDER = "lexical"

_ROUTER: Router | None = None


def shared_router() -> Router:
    """One Router per process.  Fitting it is cheap but not free, and every
    question would otherwise re-embed 110 exemplars."""
    global _ROUTER
    if _ROUTER is None:
        _ROUTER = Router()
    return _ROUTER


def answer(question: str,
           live: catalog.LiveValues | None = None,
           run_query: Callable[[str, dict], dict] | None = None,
           router: Router | None = None,
           conn=None) -> Routed:
    """One question in, one routed query out -- or a refusal that says why.

    Deliberately the same signature shape as `chat.ask`, and deliberately NOT a
    `Transport`: there is no model to transport to.  `run_query` is the D7.4
    executor seam, injected by `api/server.py`, which is how this module reaches
    the database without knowing anything about psycopg.
    """
    live = live or catalog.LiveValues.load()
    router = router or shared_router()
    own_conn = conn is None and run_query is None
    if own_conn:
        conn = catalog.connect()

    def default_run(name: str, params: dict) -> dict:
        return catalog.run(name, params, live=live, conn=conn)

    execute = run_query or default_run
    out = Routed(question=question)

    try:
        refusal = scope_gate(question, live)
        if refusal:
            out.refusal = refusal
            return out

        name, ranked = router.route(question, live)
        out.considered = ranked[:4]
        if name is None:
            out.refusal = Refusal(
                "unroutable",
                "This does not match any of the catalogue queries closely enough "
                f"to run one (best match {ranked[0][0]} at {ranked[0][1]:.2f}, "
                f"floor {router.floor:.2f}). There is no model in this path to "
                "reason about an unfamiliar phrasing.",
                tuple(q.name for q in catalog.QUERIES))
            return out

        params, slots, notices, refusal = fill_slots(question, name, live)
        out.query, out.params, out.slots, out.notices = name, params, slots, notices
        if refusal:
            out.query, out.refusal = None, refusal
            return out

        try:
            result = execute(name, params)
        except QueryError as exc:
            # The catalogue refused a value this router extracted. Surfaced with
            # the catalogue's own message, which names the valid values (D6.2).
            out.query, out.refusal = None, Refusal("unroutable", str(exc), ())
            return out

        # `result["params"]` is post-validation, so defaults the catalogue chose
        # are visible here exactly as they are for the dropdowns (D10.5).
        out.params = result["params"]
        return out
    finally:
        if own_conn and conn is not None:
            conn.close()


def statement(out: Routed) -> str:
    """What goes in the answer bubble.

    A statement about what RAN, never a sentence about the ocean.  This path
    writes no prose about the data and must not look as though it did: the
    numbers are in the chart and the table below it, where they came from a
    query the sentence names.
    """
    if out.refusal:
        return out.refusal.message
    return RAN.format(query=out.query)


# --------------------------------------------------------------------------
# the question set
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Case:
    question: str
    expect: tuple[str, ...] | None      # acceptable query names; None = must refuse
    reason: str | None = None           # for refusals, the reason kind required
    note: str = ""


IN_SCOPE: tuple[Case, ...] = (
    Case("what were conditions like in the Bay of Bengal during 2023?",
         ("region_summary", "profiles_in_region")),
    Case("give me an overview of the Arabian Sea record", ("region_summary",)),
    Case("how deep does the data go in the Laccadive Sea?",
         ("region_summary", "depth_profile")),
    Case("show me each profile position in the Arabian Sea for March 2023",
         ("profiles_in_region",)),
    Case("I want the individual casts in the Bay of Bengal",
         ("profiles_in_region",)),
    Case("map every measurement location in the Gulf of Aden",
         ("profiles_in_region",)),
    Case("did sampling get busier or quieter through 2024 in the Arabian Sea?",
         ("monthly_profile_counts",)),
    Case("break the Bay of Bengal down by calendar month",
         ("monthly_profile_counts",)),
    Case("chart how many casts happened each month in the Arabian Sea",
         ("monthly_profile_counts",)),
    Case("what happens to warmth as you go deeper in the Arabian Sea?",
         ("depth_profile",)),
    Case("draw the salinity curve down the water column in the Bay of Bengal",
         ("depth_profile",)),
    Case("binned averages by pressure for the Gulf of Aden", ("depth_profile",)),
    Case("which sea has the saltiest water at the top?", ("surface_conditions",)),
    Case("rank the seas by how warm they are near the top",
         ("surface_conditions",)),
    Case("shallow readings across every area", ("surface_conditions",)),
    Case("what path did float 6903139 follow?", ("float_trajectory",)),
    Case("draw the drift of 2902203 over its life", ("float_trajectory",)),
    Case("I want to see where 6903139 surfaced each cycle", ("float_trajectory",)),
    Case("what is close to 15N 68E?", ("nearest_profiles",)),
    Case("find casts within 300 km of 12.5N, 72E", ("nearest_profiles",)),
    Case("anything recorded around latitude 8 longitude 77?",
         ("nearest_profiles",)),
    Case("is the Bay of Bengal fresher than the Arabian Sea?",
         ("compare_regions",)),
    Case("put the Arabian Sea and the Gulf of Aden next to each other",
         ("compare_regions",)),
    Case("which is warmer, the Bay of Bengal or the Laccadive Sea?",
         ("compare_regions",)),
    Case("what floats went into this demo set and why?", ("float_inventory",)),
    Case("give me the roster of instruments", ("float_inventory",)),
    Case("how many casts did each instrument contribute?", ("float_inventory",)),
    Case("can I trust the numbers from 2902203?", ("data_provenance",)),
    Case("was 6903139 calibrated by a person or an algorithm?",
         ("data_provenance",)),
    Case("tell me about the salinity source for float 2902203",
         ("data_provenance",)),
    Case("what did you throw out for 2902203 and why?", ("missing_profiles",)),
    Case("why is the count short of what the index promised?",
         ("missing_profiles",)),
    Case("list everything the pipeline turned away", ("missing_profiles",)),
)

OUT_OF_SCOPE: tuple[Case, ...] = (
    # -- no-bgc: the problem statement asks for these and the floats have none
    Case("show me the oxygen profiles", None, "no-bgc"),
    Case("what is the chlorophyll concentration in the Arabian Sea?", None, "no-bgc"),
    Case("compare BGC parameters in the Arabian Sea for the last six months",
         None, "no-bgc", note="the problem statement's own example question"),
    Case("nitrate levels near the coast", None, "no-bgc"),
    Case("do you have ph measurements?", None, "no-bgc"),
    Case("plot dissolved oxygen against depth in the Bay of Bengal", None, "no-bgc"),
    Case("biogeochemical readings for these floats", None, "no-bgc"),
    Case("give me backscatter and irradiance", None, "no-bgc"),
    Case("chlorophyll fluorescence over time", None, "no-bgc"),

    # -- outside-window: every year named is outside 2023..2024
    Case("show me profiles from 2019", None, "outside-window"),
    Case("what did the floats record in 2015?", None, "outside-window"),
    Case("salinity in the Bay of Bengal in 1998", None, "outside-window"),
    Case("profiles from January 2022", None, "outside-window"),
    Case("compare 2020 and 2021 in the Arabian Sea", None, "outside-window"),
    Case("temperature in the Arabian Sea in 2026", None, "outside-window"),
    Case("what will salinity be in 2027?", None, "outside-window"),
    Case("give me the 2016 to 2018 record", None, "outside-window"),

    # -- unroutable: in the domain, and no catalogue query answers it
    Case("who funded this research?", None, "unroutable"),
    Case("how does an ARGO float actually work?", None, "unroutable"),
    Case("what is the weather forecast for Mumbai?", None, "unroutable"),
    Case("export all of this to NetCDF for me", None, "unroutable",
         note="asked for by the problem statement and not built -- must not be faked"),
    Case("delete all the profiles", None, "unroutable",
         note="must refuse, and must never route to anything"),
    Case("email these results to my supervisor", None, "unroutable"),
    Case("train a neural network on this dataset", None, "unroutable"),
    Case("show me satellite imagery of the Bay of Bengal", None, "unroutable"),
)

CASES: tuple[Case, ...] = IN_SCOPE + OUT_OF_SCOPE


def normalise(text: str) -> str:
    return " ".join(words(text))


def leakage() -> list[tuple[str, str]]:
    """Every case that contains a routing fixture, verbatim.

    The measurement is worthless if a question is an exemplar: it would be
    scoring the router against its own answer key.  The substring form is the
    real guard -- exact-match disjointness alone would let "show me temperature
    against depth" through while the exemplar "temperature against depth" sits
    inside it.
    """
    exemplars = [(r.query, normalise(e)) for r in ROUTES for e in r.exemplars]
    found = []
    for case in CASES:
        q = normalise(case.question)
        for query, ex in exemplars:
            if ex in q:
                found.append((case.question, f"{query}: {ex}"))
    return found


def evaluate(router: Router | None = None,
             live: catalog.LiveValues | None = None,
             cases: tuple[Case, ...] = CASES) -> dict:
    """Three numbers, and the false-accept rate leads.

    false_accept   of out-of-scope questions, the fraction ROUTED ANYWAY.
                   The dangerous one: answering something we should not.
    refusal_recall of out-of-scope questions, the fraction refused WITH THE
                   RIGHT REASON. Deliberately not 1 - false_accept: declining a
                   biogeochemical question because of the date window is a
                   wrong answer that happens to say no, and only this catches it.
    routing        of in-scope questions, the fraction reaching an acceptable
                   query. Misses split into wrong-query and wrongly-refused.
    """
    router = router or shared_router()
    live = live or catalog.LiveValues.load()

    rows, in_hits, accepted, right_reason = [], 0, 0, 0
    n_in = n_out = 0

    # The evaluation calls `answer()` -- the same function the API calls -- and
    # does not re-implement the pipeline.  It did at first, and the copy went
    # wrong immediately: a below-floor refusal is constructed inside `answer`,
    # so the reimplementation reported those as "routed to None" and the
    # false-accept rate was inflated by every question the router had in fact
    # correctly refused.  A measurement of a reimplementation measures the
    # reimplementation.
    conn = catalog.connect()
    try:
        for case in cases:
            out = answer(case.question, live=live, router=router, conn=conn)
            name = out.query if out.routed else None
            got_reason = out.refusal.reason if out.refusal else None

            if case.expect is None:
                n_out += 1
                ok = name is None and got_reason == case.reason
                if name is not None:
                    accepted += 1
                elif got_reason == case.reason:
                    right_reason += 1
                rows.append({"question": case.question, "kind": "out",
                             "want": case.reason,
                             "got": f"ROUTED:{name}" if name else got_reason,
                             "ok": ok,
                             "score": round(out.considered[0][1], 3)
                                      if out.considered else None})
            else:
                n_in += 1
                ok = name in case.expect
                in_hits += ok
                rows.append({"question": case.question, "kind": "in",
                             "want": "|".join(case.expect),
                             "got": name or f"REFUSED:{got_reason}", "ok": ok,
                             "score": round(out.considered[0][1], 3)
                                      if out.considered else None})
    finally:
        conn.close()

    return {
        "n_in": n_in, "n_out": n_out,
        "false_accept": accepted / n_out if n_out else 0.0,
        "refusal_recall": right_reason / n_out if n_out else 0.0,
        "routing": in_hits / n_in if n_in else 0.0,
        "rows": rows,
        "leakage": leakage(),
    }


def main() -> int:
    live = catalog.LiveValues.load()
    r = shared_router()
    print(f"router     {len(ROUTES)} routes, {len(r.texts)} exemplars, "
          f"embedder {r.embedder.name}, floor {r.floor}")
    print("           lexical nearest-exemplar matching. There is no model in "
          "this path.")

    leaks = leakage()
    print(f"\nleakage    {len(leaks)} evaluation question(s) contain a routing "
          f"fixture" + (" -- THE MEASUREMENT IS INVALID" if leaks else " (clean)"))
    for q, ex in leaks:
        print(f"             {q!r} contains {ex!r}")

    result = evaluate(r, live)
    print(f"\nfalse-accept rate     {result['false_accept']:6.1%}   "
          f"({result['n_out']} out-of-scope questions; lower is better, 0 is the target)")
    print(f"refusal recall        {result['refusal_recall']:6.1%}   "
          f"(refused WITH the right reason)")
    print(f"routing accuracy      {result['routing']:6.1%}   "
          f"({result['n_in']} in-scope questions)")

    misses = [r_ for r_ in result["rows"] if not r_["ok"]]
    print(f"\n{len(misses)} miss(es), printed:")
    for m in misses or []:
        print(f"  [{m['kind']}] want {m['want']}")
        print(f"        got  {m['got']}   {m['question']}")
    if not misses:
        print("  none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
