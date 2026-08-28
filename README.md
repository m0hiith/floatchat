# FloatChat

Ask questions in English about ARGO ocean float data, and get answers that can
be traced back to the exact query that produced them.

The pipeline pulls the global ARGO index from the GDAC, narrows 3.4 million
profile records down to a defensible demo set, parses the NetCDF files with the
QC and calibration rules the data actually requires, loads them into Postgres
with real IHO region boundaries, and puts a Claude-driven query layer on top
that **cannot write SQL**.

Every non-obvious choice, its alternatives, and why — including the ones that
turned out to be wrong — is in [DECISIONS.md](DECISIONS.md).

---

## Quickstart

Needs Python 3.13 and a running PostgreSQL (14 or later). No Docker, no
PostGIS, no GDAL.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run_pipeline.py
```

That downloads ~155 MB, builds the database, and runs 184 checks. First run is
a few minutes, mostly transfer; afterwards it re-runs from cache in about 9
seconds.

Then apply the read-only role the query layer uses, once:

```bash
psql -h localhost -d floatchat -f db/roles.sql
```

To ask questions in English you need a key for **either** provider — the same
tool loop runs on Anthropic or Gemini behind one transport seam. **Everything
else works without either key**, including the dashboard and all five test
suites:

```bash
export ANTHROPIC_API_KEY=...          # or: export GEMINI_API_KEY=...
.venv/bin/python api/chat.py "how salty is the Bay of Bengal compared to the Arabian Sea?"
.venv/bin/python api/chat.py --gemini "which float went deepest?"
.venv/bin/python api/chat.py --models  # what a Gemini key can actually reach
```

Whichever key is present is used; `--anthropic` / `--gemini` force the choice
and `--model=NAME` overrides the model.

**The dashboard needs no key at all.** Eleven queries, dropdowns built from the
database, and an audit trail — the platform with the AI switched off:

```bash
.venv/bin/uvicorn api.server:app --port 8000     # one terminal
cd ui && npm install && npm run dev              # another; opens :5173
```

---

## What you get

| | |
|---|---:|
| floats | 10 |
| profiles | 928 |
| measured levels | 481,181 |
| date range | 2023-01-02 .. 2024-12-31 |
| depth range | 0 .. 2052 dbar |
| named regions | 9 (IHO S-23, MRGID recorded) |
| database size | 95 MB |
| parameterised queries | 11 |
| automated checks | 184 |

The ten floats are deliberately mixed: 6 delayed-mode, 2 real-time-only, 2 that
change mode mid-life; 4 data centres of which 5 floats are Indian (`incois`);
6 profiler types; the Arabian Sea, the Bay of Bengal and the equatorial band.
`etl/demo_floats.py` re-checks every one of those claims on each run and fails
if one stops holding.

---

## How it fits together

```
GDAC index (3,397,664 rows)
  └─ etl/fetch_index.py    download + cache the 58 MB gzipped index
  └─ etl/filter_index.py   → 12,372 profiles, 254 candidate floats
       └─ etl/demo_floats.py    → the 10-float demo set, with reasons
            └─ etl/fetch_profiles.py   → 10 NetCDF files, verified against the index
                 └─ etl/parse_profiles.py  → profiles.csv + levels.csv
                      └─ etl/fetch_regions.py  → IHO polygons, simplification proven lossless
                           └─ etl/load_db.py   → Postgres, 21 verification checks
                                └─ api/catalog.py  11 parameterised queries (read-only role)
                                     ├─ api/chat.py     the model picks a query and fills its parameters
                                     │    ├─ Anthropic (claude-opus-5)
                                     │    └─ api/gemini.py  the same loop on Gemini
                                     └─ api/server.py   GET /meta, POST /query
                                          └─ ui/        the dashboard, no model in the loop
```

`api/catalog.py` feeds two consumers from the same objects: the model's tool
schemas and the dashboard's dropdowns. The choices a human can pick and the
enums a model is offered are the same list by construction, and a test compares
them element by element.

`db/schema.sql` holds six tables. Four hold data; two exist because the project
forbids silent dropping — `dropped_profiles` records every profile the pipeline
refused and why, and a one-row `ingest_run` states the database's own
provenance.

---

## Why the query layer is built this way

The language model never writes SQL. It is given 11 hand-written parameterised
queries as tools and chooses one, filling typed parameters. Four consequences,
none of which depend on the model behaving:

- **No injection surface.** No string the model produces reaches the SQL.
- **No destructive statement to emit.** None exists in the catalogue.
- **A hallucinated region is unrepresentable.** The tool schema's enums are read
  from the database at startup, and a value outside them is rejected with the
  valid list — which the model then uses to correct itself.
- **The connection cannot write.** `floatchat_ro` holds `SELECT` and nothing
  else. A `DELETE` is refused by Postgres, not by a prompt.

Every answer returns its audit trail: which queries ran, with which bound
parameters, and how many rows each returned.

---

## What is verified

```bash
.venv/bin/python run_pipeline.py --check
```

- **21 database checks** — row counts against the CSVs, orphan levels, profiles
  with no levels, level counts disagreeing with their profile, pressures out of
  range, profiles outside the declared window, and the region assignment
  cross-checked against an independent Python implementation.
- **28 query-layer checks** — every query against its documented example, plus
  eight hostile inputs (an injection string, an unknown region, `"last tuesday"`
  as a date, a limit of ten million) that must be refused *with the valid values
  named*, plus four write statements Postgres must reject.
- **28 tool-loop checks** — the whole Claude loop with no API key and no
  network: request shape, parallel tool results returned in one message, a
  refused parameter round-tripping and recovering, questions outside the data
  running no query at all.
- **45 Gemini-adapter checks** — the translation in both directions against
  real `google.genai.types` objects, so a schema Gemini's own models would
  reject fails in the suite rather than in the demo.
- **62 HTTP API checks** — `/meta` compared against the catalogue and the
  database directly, so hardcoding a region list into the server fails the
  suite; the dashboard's choices compared element by element against the
  model's tool enums; the eight hostile inputs again over HTTP; `numeric`
  arriving as a number rather than a string; an aggregate over nothing
  reporting `null` rather than `0.0`; and a stopped database returning 503
  with its reason instead of an empty body.
- **The funnel reconciles.** 939 profiles promised by the index − 13 dropped by
  QC + 2 the index's box filter had excluded = 928 written. Every one of those
  15 has a name and a reason in `dropped_profiles`.
- **The ocean checks out.** Mean 0–10 dbar salinity comes out Bay of Bengal
  32.87 < Arabian Sea 35.43 < Gulf of Aden 36.29 — river discharge at one end,
  Red Sea outflow at the other. Nothing told the pipeline to produce that
  ordering, and it is asserted as a test.

---

## Known limitations

- **The routing is observed on Gemini, unmeasured everywhere.** Eight real
  questions on `gemini-3.6-flash` chose the right query eight times, and the
  three that fell outside the data ran no query at all. That is a demo, not an
  evaluation: there is no fixed question set with expected query names and no
  pass rate. On Anthropic it is worse than unmeasured — there are no Anthropic
  credentials on this machine, so the loop has never made a live call there at
  all, only scripted ones.
- **What is proven is proven on a flash model.** The available Gemini key has
  no free-tier quota for any pro model (`429`, `limit: 0`), so
  `gemini-3.6-flash` is the default and the only tier the routing has been
  seen on.
- **Ten floats, two years, one ocean basin.** The scope is deliberate and
  documented, not a stub. Widening it means re-running Stage 1 with different
  constants and re-checking the funnel.
- **The dashboard has no automated tests.** The 184 checks cover the ETL, the
  catalogue, both model loops and the HTTP API. The UI was verified by driving
  a real browser and reading back the rendered chart and map objects — which
  found three bugs that built cleanly and passed every server-side check — but
  that was a session, not a suite. Nothing re-checks on every run that depth
  still plots downward.
- **Core ARGO only.** Pressure, temperature, salinity. No biogeochemical
  parameters — the ten floats do not carry them.
- **Region polygons drop island holes and are simplified** to fit a core
  Postgres `polygon`. Proven to change no profile's region in this dataset, and
  re-proven on every run, but it is a simplification and it is recorded as one.
- **Two profiles sit just outside the declared study box** (float 2903143 drifted
  to 10.6°S). They are kept and flagged `in_study_box = false` rather than
  clipped mid-trajectory.

---

## Layout

```
etl/              the pipeline, one script per stage, each prints its own report
api/catalog.py    the 11 parameterised queries + their tool schemas
api/chat.py       the tool loop, provider-agnostic
api/gemini.py     the same loop on Gemini, behind the same seam
api/server.py     GET /meta, GET /regions.geojson, POST /query
api/test_*.py     163 checks, no network, no API key
ui/               the dashboard; ui/src/displays.js maps each query to a chart
db/schema.sql     tables, constraints, indexes
db/roles.sql      the read-only role
run_pipeline.py   runs everything, skips what is already built
CLAUDE.md         the working agreement and the ARGO domain rules
DECISIONS.md      every choice and why — the long version
```
