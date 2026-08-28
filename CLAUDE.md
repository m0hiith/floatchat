# CLAUDE.md — how to work on FloatChat

Reconstructed from `DECISIONS.md` at Stage 10. It had never been written down,
which meant the rules below lived only in the author's head and in 58 decision
entries. This file is the short version; `DECISIONS.md` is the long one and is
authoritative wherever the two disagree.

**One item needs the author's confirmation:** rule 6 is reconstructed from how
it was cited in conversation ("no component library, no state management, no
data-fetching library — if you think you need one, stop and ask — rule 6"). The
numbering of the other rules is inferred from the same source and from what the
decision log consistently enforces. Correct them if the original differed.

---

## What this is

FloatChat answers English questions about ARGO ocean float data such that every
number can be traced back to the query that produced it. Ten floats, 928
profiles, 481,181 measured levels, the North Indian Ocean, 2023–2024.

The pipeline: GDAC index → filter → float selection → NetCDF → parse → IHO
regions → Postgres → an 11-query catalogue → a model that picks a query → an
HTTP API → a dashboard.

---

## The working agreement

**1. Silent dropping is forbidden.**
Every row the pipeline refuses gets a name and a reason, in a table, not a log
line. `dropped_profiles` exists for this. Every filter step prints its surviving
count. The funnel must reconcile by *keys*, not by two aggregate counters:
939 promised − 13 dropped + 2 outside the box = 928 written.

**2. Never invent data.**
No placeholder rows, no sample values, no mock responses, no fixtures that
resemble real data. If a thing is not available, the interface says it is not
available. An empty result says "no rows"; it never says 0.0. A NULL is
rendered as absent, because a level with no salinity reading is not a level
with zero salinity. This applies to the ETL, the API, the UI, and to anything
shown in a demo.

**3. The model never writes SQL.**
It chooses one of the hand-written parameterised queries in `api/catalog.py`
and fills typed parameters. There is no code path that turns model output into
SQL. Four consequences, none of which depend on the model behaving: no
injection surface; no destructive statement exists to emit; enums come from the
database so a hallucinated region is unrepresentable; and the connection uses
`floatchat_ro`, which holds SELECT and nothing else — a DELETE is refused by
Postgres, not by a prompt.

**4. Every non-obvious choice goes in `DECISIONS.md`.**
Decided / Alternative / Why, and the ones that turned out wrong stay in with
what corrected them. Numbered `D<stage>.<n>`. These are the author's defence
notes; a decision that is not written down did not happen.

**5. A claim is only true if something re-checks it on every run.**
184 checks across five suites, none of which need a network or an API key. If a
property matters — the region assignment, the funnel, the QC asymmetry, the
Bay of Bengal being fresher than the Arabian Sea — it is asserted, not
commented. `python run_pipeline.py --check`.

**6. No new dependency without asking.**
Not a component library, not a state-management library, not a data-fetching
library, not a plotting wrapper, not a utility belt. If one seems necessary,
stop and ask. Pin every version exactly — `requirements.txt` is a full freeze
and `ui/package.json` uses no carets. A different Plotly the week of the demo
is an avoidable failure.

**7. A flag that appears to do something must do it.**
`--fresh` once passed `--force` to three scripts that parse no arguments; the
stage looked forced and was not. Quiet no-ops are the failure mode this project
is organised against.

**8. Lead with what is not proven.**
The README's Known Limitations section is near the top, not in a footnote. A
judge finds those in five minutes either way; listed by the authors it reads as
control of the material.

---

## ARGO domain rules

These are properties of the data, not preferences. Getting one wrong produces
numbers that look fine.

- **Pressure, not depth.** Measurements are in **decibars (dbar)**, and dbar is
  what the schema stores and the axes say. Depth in metres is never computed.
  Profiles plot with pressure increasing **downward**.
- **Units.** Salinity is **PSU** (practical salinity — dimensionless, but always
  labelled PSU). Temperature is **°C**. Both belong on every axis.
- **QC flags: accept {1, 2, 5, 8}. Reject 3 and 4.** 3 is "probably bad" and it
  is not data. In this subset only 1, 3 and 4 appear, and the asymmetry is the
  story: 6.4% of salinity levels rejected against 0.2% of temperature.
- **`DATA_MODE` has three states, not two.** R real-time, A real-time adjusted,
  D delayed-mode. The index filename can only ever say two of them.
- **Prefer the adjusted copy, fall back to raw, and record which.** 132
  delayed-mode profiles carry a `PSAL_ADJUSTED` that exists in name only. A
  naive "if D then ADJUSTED" blanks a quarter of the delayed-mode salinity and
  calls it data. `psal_source` ∈ {adjusted, raw, raw_fallback, empty} says which
  copy every value came from.
- **A trailing `D` in a filename means descending, not delayed-mode.**
  `R1234567_045D.nc` is a descending profile. 2,057 such files appear in the
  first 400k index rows, so this is not hypothetical.
- **`N_LEVELS` is padding.** It is the longest profile in the file; every
  shorter profile is filled out to it. 37% of level cells in these files are
  padding. 771,658 cells scanned, 481,181 written.
- **Region names are not in ARGO data.** They come from IHO S-23 polygons with
  the MRGID recorded. `floats.approx_area` is an advisory longitude cut used
  only to pick candidates by eye; nothing derived from it enters the database.
- **The stored polygons drop island holes and are simplified** to fit a core
  Postgres `polygon`. Proven to change no profile's region here, and re-proven
  every run — but it is a simplification and it is recorded as one.
- **`JULD` is a float count of days since 1950,** so `days × 86400` leaves
  nanosecond float noise. Round to the second; ARGO's real resolution is coarser.
- **Two profiles sit outside the declared study box** (float 2903143 drifted to
  10.6°S). Kept and flagged `in_study_box = false`, not clipped mid-trajectory.
- **Core ARGO only.** Pressure, temperature, salinity. No biogeochemical
  parameters — these ten floats do not carry them.

---

## Layout

```
etl/              the pipeline, one script per stage, each prints its own report
db/schema.sql     six tables; two exist because rule 1 forbids silent dropping
db/roles.sql      floatchat_ro — SELECT and nothing else
api/catalog.py    the 11 parameterised queries + their tool schemas  (rule 3)
api/chat.py       the tool loop, provider-agnostic
api/gemini.py     the same loop on Gemini, behind the same seam
api/server.py     GET /meta, GET /regions.geojson, POST /query
ui/src/displays.js  the ONLY file in the UI that knows a query name
run_pipeline.py   runs everything, skips what is already built
DECISIONS.md      every choice and why — the long version
```

Two invariants worth not breaking:

- **`api/server.py` holds no knowledge.** No region name, no date, no WMO. It
  reads `catalog.QUERIES` and `LiveValues`, which read the database. Five checks
  assert this by grepping the source.
- **`/meta` and the model's tool schemas are built from the same two objects.**
  The dropdowns a human sees and the enums a model is offered cannot drift
  apart, and a check compares them element by element.

---

## Stage numbering

Stages are build order, and the numbers have collided once already: the Gemini
transport landed from a parallel session as Stage 8 while packaging was being
written as Stage 8, and packaging was renumbered to 9 (commit 20e3215).

**Before claiming a stage number, grep `DECISIONS.md` for it and check
`git log origin/master`.** Stage 10 was informally reserved for pgvector RAG;
the dashboard shipped first and took it (D10.1), so RAG is Stage 11 if it is
built. A reservation that is not written into the log is not binding on the log
— write the claim down.

---

## Commands

```bash
.venv/bin/python run_pipeline.py            # build everything, skip what exists
.venv/bin/python run_pipeline.py --check    # 184 checks, no network, no API key
.venv/bin/python api/chat.py "how salty is the Bay of Bengal?"
.venv/bin/uvicorn api.server:app --port 8000
cd ui && npm install && npm run dev         # dashboard on :5173
```

The check suites need Postgres and nothing else. `api/chat.py` needs
`ANTHROPIC_API_KEY` or `GEMINI_API_KEY`; everything else runs without either.
