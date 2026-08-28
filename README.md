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

That downloads ~155 MB, builds the database, and runs 122 checks. First run is
a few minutes, mostly transfer; afterwards it re-runs from cache in about 9
seconds.

Then apply the read-only role the query layer uses, once:

```bash
psql -h localhost -d floatchat -f db/roles.sql
```

To ask questions in English you need a key for **either** provider — the same
tool loop runs on Anthropic or Gemini behind one transport seam. **Everything
else works without either key**, including all four test suites:

```bash
export ANTHROPIC_API_KEY=...          # or: export GEMINI_API_KEY=...
.venv/bin/python api/chat.py "how salty is the Bay of Bengal compared to the Arabian Sea?"
.venv/bin/python api/chat.py --gemini "which float went deepest?"
.venv/bin/python api/chat.py --models  # what a Gemini key can actually reach
```

Whichever key is present is used; `--anthropic` / `--gemini` force the choice
and `--model=NAME` overrides the model.

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
                                     └─ api/chat.py     the model picks a query and fills its parameters
                                          ├─ Anthropic (claude-opus-5)
                                          └─ api/gemini.py  the same loop on Gemini
```

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
- **The funnel reconciles.** 939 profiles promised by the index − 13 dropped by
  QC + 2 the index's box filter had excluded = 928 written. Every one of those
  15 has a name and a reason in `dropped_profiles`.
- **The ocean checks out.** Mean 0–10 dbar salinity comes out Bay of Bengal
  32.87 < Arabian Sea 35.43 < Gulf of Aden 36.29 — river discharge at one end,
  Red Sea outflow at the other. Nothing told the pipeline to produce that
  ordering, and it is asserted as a test.

---

## Known limitations

- **The routing is untested against a real API, on both providers.** Everything
  between the model's answer and Postgres is covered by offline tests, but
  whether either model picks the right query for a real question has never been
  observed. There are no Anthropic credentials on the build machine, and the
  `GEMINI_API_KEY` that is set returns `400 API_KEY_INVALID`. One valid key is
  all that stands in the way.
- **Ten floats, two years, one ocean basin.** The scope is deliberate and
  documented, not a stub. Widening it means re-running Stage 1 with different
  constants and re-checking the funnel.
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
api/test_*.py     101 checks, no network, no API key
db/schema.sql     tables, constraints, indexes
db/roles.sql      the read-only role
run_pipeline.py   runs everything, skips what is already built
DECISIONS.md      every choice and why — the long version
```
