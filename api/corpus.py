"""Stage 11a: the corpus -- what the vector index is an index *of*.

The problem statement asks for a vector database holding "metadata and
summaries".  This file decides what those summaries are, and it applies the
project's second rule to them: **never invent data**.

Every document below is generated from rows this database actually holds, and
every document carries the SQL that produced it.  A retrieved summary is
therefore auditable in exactly the way a query result is -- you can re-run its
`source` and get its numbers back.  Nothing here is typed in by hand as fact,
including the glossary entries: "delayed-mode" is explained with the real count
of delayed-mode profiles, and the QC flag list comes from `ingest_run`, not
from a constant in this file.

Seven kinds of document, chosen because they are the seven things a question
tends to be *about*:

    dataset        1    what this database is, in its own numbers
    region         9    one per named IHO region, including the empty ones
    region_month  92    one per region and calendar month that has profiles
    float         10    one per float: where it went, how it was calibrated
    query         11    one per catalogue query -- what it answers
    glossary       6    the domain traps, each with the data's own numbers
    dropped        2    the profiles the pipeline refused, by reason

An empty region gets a document that says it is empty.  That is deliberate and
it is rule 1: "the Red Sea has no profiles" is an answer, and deleting the
document would turn it into a silence.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import catalog


@dataclass(frozen=True)
class Document:
    """One retrievable summary.

    `text` is what gets embedded and what reaches the model.  `source` is the
    SQL that produced the facts in it -- the audit handle.  `keys` are the
    structured handles a caller can act on: a retrieved region_month document
    hands back {"region": ..., "start": ..., "end": ...}, which is exactly the
    parameter set a catalogue query wants.  That is how retrieval helps routing
    without ever becoming the answer.
    """
    doc_id: str
    kind: str
    title: str
    text: str
    source: str
    keys: dict = field(default_factory=dict)

    def embedding_text(self) -> str:
        """Title and body together.  The title carries the proper nouns -- a
        region name, a WMO -- and dropping it costs exact-name matches."""
        return f"{self.title}\n{self.text}"


# --------------------------------------------------------------------------
# small helpers -- absent is absent (rule 2)
# --------------------------------------------------------------------------

def num(value, unit: str = "", digits: int = 2) -> str:
    """Render a measurement, or say it is missing.  Never 0.0 for NULL."""
    if value is None:
        return "not available"
    return f"{float(value):.{digits}f}{(' ' + unit) if unit else ''}"


def count(value) -> str:
    return f"{int(value):,}"


SQL_DATASET = """
    SELECT loaded_at::text AS loaded_at, gdac_index_date,
           window_start::date::text AS window_start,
           window_end::date::text AS window_end,
           good_qc_flags, index_rows_total, index_rows_kept,
           profiles_in_files, profiles_written, levels_written
    FROM ingest_run"""

SQL_REGIONS = """
    SELECT r.name, r.mrgid, r.source, r.holes_dropped, r.vertices_stored,
           r.min_lon, r.min_lat, r.max_lon, r.max_lat,
           count(p.profile_id) AS profiles,
           count(DISTINCT p.wmo) AS floats,
           min(p.juld)::date::text AS first_profile,
           max(p.juld)::date::text AS last_profile,
           max(p.pres_max) AS deepest_dbar
    FROM regions r
    LEFT JOIN profile_regions pr ON pr.region = r.name
    LEFT JOIN profiles p ON p.profile_id = pr.profile_id
    GROUP BY r.name, r.mrgid, r.source, r.holes_dropped, r.vertices_stored,
             r.min_lon, r.min_lat, r.max_lon, r.max_lat
    ORDER BY r.name"""

SQL_REGION_SURFACE = """
    SELECT pr.region,
           avg(l.temp) AS mean_temp_c,
           avg(l.psal) AS mean_psal_psu,
           count(*) AS surface_levels
    FROM levels l
    JOIN profiles p USING (profile_id)
    JOIN profile_regions pr ON pr.profile_id = p.profile_id
    WHERE l.pres <= 10
    GROUP BY pr.region"""

# Two queries rather than one.  Joining 481,181 levels in to compute a
# per-month surface mean, only to de-duplicate the profile counts back out
# again with count(DISTINCT), is a slow way to get a wrong-looking plan; the
# means are computed against the level table on their own and merged by key.
SQL_REGION_MONTHS = """
    SELECT pr.region,
           date_trunc('month', p.juld)::date::text AS month_start,
           (date_trunc('month', p.juld) + interval '1 month'
                                        - interval '1 day')::date::text AS month_end,
           to_char(p.juld, 'FMMonth YYYY') AS month_label,
           count(*) AS profiles,
           count(DISTINCT p.wmo) AS floats,
           max(p.pres_max) AS deepest_dbar
    FROM profiles p
    JOIN profile_regions pr ON pr.profile_id = p.profile_id
    GROUP BY pr.region, date_trunc('month', p.juld), to_char(p.juld, 'FMMonth YYYY')
    ORDER BY pr.region, month_start"""

SQL_REGION_MONTH_SURFACE = """
    SELECT pr.region, date_trunc('month', p.juld)::date::text AS month_start,
           avg(l.temp) AS mean_temp_c, avg(l.psal) AS mean_psal_psu
    FROM levels l
    JOIN profiles p ON p.profile_id = l.profile_id
    JOIN profile_regions pr ON pr.profile_id = p.profile_id
    WHERE l.pres <= 10
    GROUP BY pr.region, date_trunc('month', p.juld)"""

SQL_FLOATS = """
    SELECT f.wmo, f.dac, f.profiler_type, f.dm_status, f.institution,
           f.selection_reason, f.n_profiles_index, f.approx_area,
           count(p.profile_id) AS profiles,
           min(p.juld)::date::text AS first_profile,
           max(p.juld)::date::text AS last_profile,
           min(p.lat) AS min_lat, max(p.lat) AS max_lat,
           min(p.lon) AS min_lon, max(p.lon) AS max_lon,
           max(p.pres_max) AS deepest_dbar,
           count(*) FILTER (WHERE p.data_mode = 'D') AS delayed,
           count(*) FILTER (WHERE p.data_mode = 'A') AS adjusted,
           count(*) FILTER (WHERE p.data_mode = 'R') AS realtime,
           count(*) FILTER (WHERE NOT p.in_study_box) AS outside_box
    FROM floats f
    LEFT JOIN profiles p USING (wmo)
    GROUP BY f.wmo, f.dac, f.profiler_type, f.dm_status, f.institution,
             f.selection_reason, f.n_profiles_index, f.approx_area
    ORDER BY f.wmo"""

SQL_FLOAT_REGIONS = """
    SELECT p.wmo, pr.region, count(*) AS profiles
    FROM profiles p
    JOIN profile_regions pr ON pr.profile_id = p.profile_id
    GROUP BY p.wmo, pr.region
    ORDER BY p.wmo, count(*) DESC"""

SQL_DATA_MODES = """
    SELECT data_mode, count(*) AS profiles
    FROM profiles GROUP BY data_mode ORDER BY data_mode"""

SQL_PSAL_SOURCES = """
    SELECT psal_source, count(*) AS profiles
    FROM profiles GROUP BY psal_source ORDER BY count(*) DESC"""

SQL_QC_PRESENT = """
    SELECT 'temp' AS parameter, temp_qc AS flag, count(*) AS levels
    FROM levels WHERE temp_qc IS NOT NULL GROUP BY temp_qc
    UNION ALL
    SELECT 'psal', psal_qc, count(*) FROM levels WHERE psal_qc IS NOT NULL GROUP BY psal_qc
    ORDER BY 1, 2"""

SQL_PRESSURE = """
    SELECT min(pres) AS min_pres, max(pres) AS max_pres, count(*) AS levels,
           count(*) FILTER (WHERE psal IS NULL) AS levels_without_salinity,
           count(*) FILTER (WHERE temp IS NULL) AS levels_without_temperature
    FROM levels"""

SQL_STUDY_BOX = """
    SELECT p.wmo, count(*) AS profiles, min(p.lat) AS min_lat, max(p.lat) AS max_lat
    FROM profiles p WHERE NOT p.in_study_box GROUP BY p.wmo ORDER BY p.wmo"""

SQL_DROPPED = """
    SELECT d.reason, count(*) AS profiles,
           count(*) FILTER (WHERE d.was_indexed) AS was_indexed,
           string_agg(DISTINCT d.wmo, ', ' ORDER BY d.wmo) AS floats,
           min(d.detail) AS example_detail
    FROM dropped_profiles d GROUP BY d.reason ORDER BY count(*) DESC"""


# --------------------------------------------------------------------------
# builders, one per kind
# --------------------------------------------------------------------------

def dataset_docs(conn) -> list[Document]:
    r = catalog.run_raw(conn, SQL_DATASET)[0]
    text = (
        f"This database holds {count(r['profiles_written'])} ARGO float profiles "
        f"containing {count(r['levels_written'])} measured levels, covering "
        f"{r['window_start']} to {r['window_end']} in the North Indian Ocean. "
        f"Nothing outside that date window exists here. "
        f"It was built from the GDAC global profile index dated {r['gdac_index_date']}, "
        f"which held {count(r['index_rows_total'])} rows; "
        f"{count(r['index_rows_kept'])} survived the study filter and "
        f"{count(r['profiles_in_files'])} profiles were present in the downloaded NetCDF "
        f"files. Measurements are pressure in decibars, temperature in degrees Celsius, "
        f"and practical salinity in PSU. There are no biogeochemical parameters: no "
        f"oxygen, no chlorophyll, no nitrate, no pH, no BGC floats of any kind. "
        f"Levels whose QC flag was outside {r['good_qc_flags']} were removed during "
        f"ingest, so every value here is good-QC data. Loaded {r['loaded_at']}."
    )
    return [Document("dataset", "dataset", "What this FloatChat database contains",
                     text, SQL_DATASET.strip(),
                     {"start": r["window_start"], "end": r["window_end"]})]


def region_docs(conn) -> list[Document]:
    surface = {r["region"]: r for r in catalog.run_raw(conn, SQL_REGION_SURFACE)}
    docs = []
    for r in catalog.run_raw(conn, SQL_REGIONS):
        name = r["name"]
        head = (f"{name} is a named ocean region with IHO S-23 MRGID {r['mrgid']} "
                f"(source: {r['source']}). Its stored outline spans longitude "
                f"{num(r['min_lon'], '', 2)} to {num(r['max_lon'], '', 2)} east and latitude "
                f"{num(r['min_lat'], '', 2)} to {num(r['max_lat'], '', 2)} north.")
        if r["profiles"]:
            s = surface.get(name)
            body = (f" It holds {count(r['profiles'])} profiles from "
                    f"{count(r['floats'])} floats, between {r['first_profile']} and "
                    f"{r['last_profile']}, reaching {num(r['deepest_dbar'], 'dbar', 1)} at "
                    f"the deepest.")
            if s:
                body += (f" Its mean near-surface (0-10 dbar) temperature is "
                         f"{num(s['mean_temp_c'], 'degrees C')} and its mean near-surface "
                         f"salinity is {num(s['mean_psal_psu'], 'PSU', 3)}, over "
                         f"{count(s['surface_levels'])} levels.")
        else:
            # Rule 1: the document exists and says it is empty.  Deleting it
            # would turn "no profiles here" into a silence.
            body = (f" No profiles in this database fall inside {name}. It is a region "
                    f"the boundary set knows about, not a region these ten floats visited. "
                    f"Any question about {name} has no data behind it.")
        return_keys = {"region": name, "profiles": int(r["profiles"])}
        docs.append(Document(f"region:{name}", "region", f"Region: {name}",
                             head + body, SQL_REGIONS.strip(), return_keys))
    return docs


def region_month_docs(conn) -> list[Document]:
    surface = {(r["region"], r["month_start"]): r
               for r in catalog.run_raw(conn, SQL_REGION_MONTH_SURFACE)}
    docs = []
    for r in catalog.run_raw(conn, SQL_REGION_MONTHS):
        label = r["month_label"]
        s = surface.get((r["region"], r["month_start"]), {})
        text = (f"In {label}, the {r['region']} has {count(r['profiles'])} profiles from "
                f"{count(r['floats'])} floats, reaching "
                f"{num(r['deepest_dbar'], 'dbar', 1)}. "
                f"Mean near-surface temperature "
                f"{num(s.get('mean_temp_c'), 'degrees C')}, mean near-surface salinity "
                f"{num(s.get('mean_psal_psu'), 'PSU', 3)}. "
                f"Date range {r['month_start']} to {r['month_end']}.")
        docs.append(Document(
            f"region_month:{r['region']}:{r['month_start']}", "region_month",
            f"{r['region']}, {label}", text, SQL_REGION_MONTHS.strip(),
            # Exactly the parameters profiles_in_region / region_summary want.
            {"region": r["region"], "start": r["month_start"], "end": r["month_end"]}))
    return docs


def float_docs(conn) -> list[Document]:
    visited: dict[str, list[str]] = {}
    for r in catalog.run_raw(conn, SQL_FLOAT_REGIONS):
        visited.setdefault(r["wmo"], []).append(f"{r['region']} ({r['profiles']})")
    docs = []
    for r in catalog.run_raw(conn, SQL_FLOATS):
        where = ", ".join(visited.get(r["wmo"], [])) or "no named region"
        text = (
            f"ARGO float WMO {r['wmo']} is operated through the {r['dac']} data centre, "
            f"profiler type {r['profiler_type']}, delayed-mode status {r['dm_status']}. "
            f"It contributed {count(r['profiles'])} profiles to this database out of "
            f"{count(r['n_profiles_index'])} the GDAC index listed for it, between "
            f"{r['first_profile']} and {r['last_profile']}. "
            f"Of those, {count(r['delayed'])} are delayed-mode (D), {count(r['adjusted'])} "
            f"are real-time adjusted (A) and {count(r['realtime'])} are raw real-time (R). "
            f"It travelled between latitude {num(r['min_lat'], 'N')} and "
            f"{num(r['max_lat'], 'N')} and longitude {num(r['min_lon'], 'E')} to "
            f"{num(r['max_lon'], 'E')}, reaching {num(r['deepest_dbar'], 'dbar', 1)}. "
            f"Regions visited: {where}. "
            f"It was chosen for this demo set because: {r['selection_reason']}."
        )
        if r["outside_box"]:
            text += (f" {count(r['outside_box'])} of its profiles sit outside the declared "
                     f"study box and are flagged in_study_box = false rather than clipped.")
        docs.append(Document(f"float:{r['wmo']}", "float", f"Float {r['wmo']} ({r['dac']})",
                             text, SQL_FLOATS.strip(), {"wmo": r["wmo"]}))
    return docs


def query_docs(live: catalog.LiveValues) -> list[Document]:
    """One document per catalogue query.

    These are the documents that make retrieval useful for *routing*: a
    question that lands near `depth_profile`'s description is a question
    `depth_profile` answers.  The text is the catalogue's own, so it cannot
    drift from the tool schema the model is offered.
    """
    docs = []
    for q in catalog.QUERIES:
        params = "; ".join(
            f"{p.name} ({p.kind}{'' if p.required else ', optional'}): {p.description}"
            for p in q.params) or "no parameters"
        example = ", ".join(f"{k}={v!r}" for k, v in q.example.items()) or "no arguments needed"
        text = (f"Catalogue query '{q.name}'. {q.description} "
                f"Parameters: {params}. Example call: {q.name}({example}). "
                f"This is one of the {len(catalog.QUERIES)} hand-written parameterised "
                f"queries; the model chooses it and fills its parameters, and never "
                f"writes SQL.")
        docs.append(Document(f"query:{q.name}", "query", f"Query: {q.name}",
                             text, "api/catalog.py QUERIES", {"query": q.name}))
    return docs


def glossary_docs(conn) -> list[Document]:
    """The domain traps -- each explained with this database's own numbers.

    The explanations are documentation, but every number in them is a query
    result, which is what keeps them inside rule 2.  A glossary entry that
    said "about a quarter" instead of a count would be an invention.
    """
    docs = []

    modes = {r["data_mode"]: r["profiles"] for r in catalog.run_raw(conn, SQL_DATA_MODES)}
    docs.append(Document(
        "glossary:data_mode", "glossary", "DATA_MODE: R, A and D are three states",
        f"Every ARGO profile has a DATA_MODE. R means real-time: raw, automatic QC only. "
        f"A means real-time with adjustments applied. D means delayed-mode: a scientist "
        f"has calibrated it, and it is the most trustworthy. This database holds "
        f"{count(modes.get('R', 0))} R profiles, {count(modes.get('A', 0))} A profiles and "
        f"{count(modes.get('D', 0))} D profiles. Three states, not two -- the GDAC index "
        f"filename can only ever distinguish two of them, so mode is read from the file.",
        SQL_DATA_MODES.strip(), {"query": "data_provenance"}))

    sources = {r["psal_source"]: r["profiles"] for r in catalog.run_raw(conn, SQL_PSAL_SOURCES)}
    docs.append(Document(
        "glossary:psal_source", "glossary", "Which copy of the salinity was used",
        f"ARGO files carry a raw parameter and an adjusted copy. This pipeline prefers the "
        f"adjusted copy, falls back to raw, and records which it used in psal_source: "
        f"adjusted {count(sources.get('adjusted', 0))} profiles, raw "
        f"{count(sources.get('raw', 0))}, raw_fallback {count(sources.get('raw_fallback', 0))}, "
        f"empty {count(sources.get('empty', 0))}. raw_fallback means DATA_MODE claimed an "
        f"adjusted copy that existed in name only and was empty; a naive 'if D then use "
        f"ADJUSTED' would have blanked those profiles and called the result data.",
        SQL_PSAL_SOURCES.strip(), {"query": "data_provenance"}))

    run = catalog.run_raw(conn, SQL_DATASET)[0]
    qc = catalog.run_raw(conn, SQL_QC_PRESENT)
    present = "; ".join(f"{r['parameter']} flag {r['flag']}: {count(r['levels'])} levels"
                        for r in qc)
    docs.append(Document(
        "glossary:qc_flags", "glossary", "QC flags, and why salinity loses more levels",
        f"ARGO quality-control flags run 1 (good) to 9 (missing). This pipeline accepts "
        f"{run['good_qc_flags']} and rejects 3 and 4. Flag 3 is 'probably bad' and it is "
        f"not data. Levels failing QC were removed at ingest, so every value stored here "
        f"is good-QC data and counts are counts of good data only. Flags surviving in the "
        f"database: {present}. Salinity loses far more levels to QC than temperature does; "
        f"that asymmetry is a property of the instrument, not of this pipeline.",
        SQL_QC_PRESENT.strip(), {}))

    p = catalog.run_raw(conn, SQL_PRESSURE)[0]
    docs.append(Document(
        "glossary:pressure", "glossary", "Pressure in decibars, not depth in metres",
        f"Measurements are recorded against pressure in decibars (dbar), which is what this "
        f"schema stores and what every axis says. Depth in metres is never computed; one "
        f"decibar is roughly one metre of seawater, but the conversion is latitude "
        f"dependent and this project does not do it. Stored pressures run "
        f"{num(p['min_pres'], 'dbar', 1)} to {num(p['max_pres'], 'dbar', 1)} across "
        f"{count(p['levels'])} levels. Profiles are plotted with pressure increasing "
        f"downward. {count(p['levels_without_salinity'])} levels have a pressure and a "
        f"temperature but no salinity reading, and "
        f"{count(p['levels_without_temperature'])} have no temperature; those are absent "
        f"values, not zeros.",
        SQL_PRESSURE.strip(), {"query": "depth_profile"}))

    box = catalog.run_raw(conn, SQL_STUDY_BOX)
    if box:
        drift = "; ".join(f"float {r['wmo']}: {count(r['profiles'])} profiles, latitude "
                          f"{num(r['min_lat'], 'N')} to {num(r['max_lat'], 'N')}" for r in box)
        text = (f"Some profiles sit outside the declared study box and are kept, flagged "
                f"in_study_box = false, rather than clipped mid-trajectory: {drift}. "
                f"A float that drifts out of the box is still the same float, and cutting "
                f"its track would misrepresent where it went.")
    else:
        text = ("Every profile in this database sits inside the declared study box; the "
                "in_study_box flag is true for all of them.")
    docs.append(Document("glossary:study_box", "glossary",
                         "Profiles outside the study box are kept and flagged",
                         text, SQL_STUDY_BOX.strip(), {"query": "float_trajectory"}))

    regions = catalog.run_raw(conn, SQL_REGIONS)
    named = ", ".join(r["name"] for r in regions)
    holes = sum(r["holes_dropped"] for r in regions)
    docs.append(Document(
        "glossary:regions", "glossary", "Where the region names come from",
        f"Region names are not in ARGO data. They come from IHO S-23 sea-area polygons, "
        f"each stored with its MRGID so the boundary is traceable. The {len(regions)} named "
        f"regions are: {named}. The stored outlines drop island holes ({holes} in total) and "
        f"are simplified to fit a core Postgres polygon; that simplification was verified to "
        f"change no profile's region assignment and is re-verified on every load. The "
        f"floats.approx_area column is an advisory longitude cut used only to pick candidate "
        f"floats by eye, and nothing derived from it enters an answer.",
        SQL_REGIONS.strip(), {}))

    return docs


def dropped_docs(conn) -> list[Document]:
    docs = []
    for r in catalog.run_raw(conn, SQL_DROPPED):
        text = (f"{count(r['profiles'])} profiles were refused by the pipeline for the "
                f"reason '{r['reason']}', affecting float(s) {r['floats']}; "
                f"{count(r['was_indexed'])} of them were promised by the GDAC index. "
                f"Example detail: {r['example_detail']}. They are recorded in the "
                f"dropped_profiles table with a name and a reason rather than dropped "
                f"silently, so a profile count that looks low can be explained instead of "
                f"argued about. The missing_profiles query lists them.")
        docs.append(Document(f"dropped:{r['reason']}", "dropped",
                             f"Refused profiles: {r['reason']}", text,
                             SQL_DROPPED.strip(), {"query": "missing_profiles"}))
    return docs


# --------------------------------------------------------------------------
# the whole corpus
# --------------------------------------------------------------------------

KINDS = ("dataset", "region", "region_month", "float", "query", "glossary", "dropped")


def build(conn=None, live: catalog.LiveValues | None = None) -> list[Document]:
    """Every document, in a stable order.

    Stable order matters: the index stores vectors by position, so a corpus
    that reordered itself between runs would silently re-point every id.
    """
    own = conn is None
    conn = conn or catalog.connect()
    try:
        live = live or catalog.LiveValues.load()
        docs = (dataset_docs(conn) + region_docs(conn) + region_month_docs(conn)
                + float_docs(conn) + query_docs(live) + glossary_docs(conn)
                + dropped_docs(conn))
    finally:
        if own:
            conn.close()

    return validate(docs)


def validate(docs: list[Document]) -> list[Document]:
    """Refuse a corpus that would index badly.

    A duplicate id is the dangerous one: ids are how a hit is addressed back to
    a document, so two documents sharing one would make retrieval return the
    wrong text with a perfectly plausible score and nothing would raise.
    """
    seen = set()
    for d in docs:
        if d.doc_id in seen:
            raise ValueError(f"duplicate doc_id {d.doc_id!r} -- ids address vectors")
        seen.add(d.doc_id)
        if d.kind not in KINDS:
            raise ValueError(f"{d.doc_id}: unknown kind {d.kind!r}")
    return docs


def by_kind(docs: list[Document]) -> dict[str, int]:
    out = {k: 0 for k in KINDS}
    for d in docs:
        out[d.kind] += 1
    return out


def main() -> int:
    docs = build()
    counts = by_kind(docs)
    print("corpus")
    for kind in KINDS:
        print(f"  {kind:<14}{counts[kind]:>5}")
    print(f"  {'total':<14}{len(docs):>5}")
    chars = sum(len(d.embedding_text()) for d in docs)
    print(f"\n{chars:,} characters, mean {chars // len(docs):,} per document")
    print(f"\nlongest: {max(docs, key=lambda d: len(d.text)).doc_id}")
    print("\nfirst document of each kind:")
    for kind in KINDS:
        first = next((d for d in docs if d.kind == kind), None)
        if first:
            print(f"\n  [{first.doc_id}] {first.title}")
            print(f"    {first.text[:200]}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
