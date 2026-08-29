# CLAUDE.md — how to work on FloatChat

Reconstructed from `DECISIONS.md` at Stage 10, extended at Stage 11. It had
never been written down, which meant the rules below lived only in the author's
head and in the decision entries. This file is the short version;
`DECISIONS.md` is the long one and is authoritative wherever the two disagree.

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
regions → Postgres → an 11-query catalogue → a FAISS index over summaries the
database writes about itself → a model *or* a model-free lexical router that
picks a query → an HTTP API → a dashboard that opens in the chat tab, with the
catalogue one click away and one audit trail shared between them.

**The dashboard asks the lexical router and nothing else (D16.8).** The model
path is real, tested and reachable — through `/ask` with an explicit
`provider`, and through `api/chat.py` on the command line — but no button in
the UI takes it, and the UI names `lexical` in every request rather than
letting the server's environment decide. Put the selector back only with a
decision entry saying why.

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
544 checks across ten suites, none of which need a network or an API key. If a
property matters — the region assignment, the funnel, the QC asymmetry, the
Bay of Bengal being fresher than the Arabian Sea — it is asserted, not
commented. `python run_pipeline.py --check`.
**Assert the property, never the spelling.** A check pinned to a literal source
string tests how something is written, so it passes while the property is false
and fails when the property is fixed — which is what D13.3's badge check did.
And a new check is not finished until it has been made to fail: break the thing
on purpose, watch the check catch it, put it back.
Where a property is a *degree* rather than a fact — how good retrieval is —
**measure it and print the misses**, do not assert a vibe. A test whose target
cannot be hit is an error, not a pass.

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

**9. Never call a thing more than it is.**
The keyless embedder is a hashed n-gram bag with IDF. It is called lexical
everywhere it is named — in the code, the build report, the README and the UI —
and never "semantic search". The same rule retired every other flattering word
in this project: the region polygons are "simplified" and the routing is
"observed". The dashboard was "untested" until Stage 14 and is now "rendered
and read back, but not looked at" — it is checked for structure and computed
style, never for appearance. If the honest word is worse, use the honest word.

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
- **There is no place-name lookup, deliberately.** "Near the equator" cannot be
  resolved, because writing `Arabian Sea = 15N 65E` would put a coordinate in
  an answer that is nowhere in the database. Region centroids are derivable
  from `regions.poly` and would be legitimate; nothing else is.
- **The stored polygons drop island holes and are simplified** to fit a core
  Postgres `polygon`. Proven to change no profile's region here, and re-proven
  every run — but it is a simplification and it is recorded as one.
- **`JULD` is a float count of days since 1950,** so `days × 86400` leaves
  nanosecond float noise. Round to the second; ARGO's real resolution is coarser.
- **Two profiles sit outside the declared study box** (float 2903143 drifted to
  10.6°S). Kept and flagged `in_study_box = false`, not clipped mid-trajectory.
- **Core ARGO only.** Pressure, temperature, salinity. No biogeochemical
  parameters — these ten floats do not carry them. The dashboard puts the BGC
  question on a suggestion button on purpose, so the honest "there are none" is
  demonstrated rather than avoided.

---

## Layout

```
etl/              the pipeline, one script per stage, each prints its own report
etl/build_index.py  Stage 11: build the vector index, then measure its recall
db/schema.sql     six tables; two exist because rule 1 forbids silent dropping
db/roles.sql      floatchat_ro — SELECT and nothing else
api/catalog.py    the 11 parameterised queries + their tool schemas  (rule 3)
api/corpus.py     131 summaries, each generated by a query it carries
api/embed.py      three embedders behind one seam (Gemini · keyless · scripted)
api/retrieval.py  the FAISS index, and the measurement of whether it works
api/router.py     the lexical router — picks a query with NO model (Stage 12)
api/chat.py       the tool loop, provider-agnostic, retriever optional
api/gemini.py     the same loop on Gemini, behind the same seam
api/server.py     GET /meta, GET /regions.geojson, POST /query, POST /ask
ui/src/displays.js  the ONLY file in the UI that knows a query name
ui/test_ui.py     Stage 13: the dashboard's couplings, checked from Python
ui/test_render.py Stage 14: the dashboard RENDERED in Chrome and read back
api/index.py      Stage 15: the one module a Vercel function imports
api/requirements.txt  the API's closure alone — the root freeze does not fit
api/test_deploy.py    Stage 15+17: the deployment config, both upload sets,
                      the git tree as a deploy path, and .env.example
vercel.json       one build, one route — zero-config would publish the suites
.vercelignore     REPLACES .gitignore as the upload filter (D15.4)
ui/vercel.json    the dashboard's own project: vite, static, no builds array
ui/.gitignore     IS the dashboard's upload filter — no ui/.vercelignore (D17.7)
.env.example      every variable the code reads; checked both ways (D17.4)
DEPLOYMENT.md     the three tiers, step by step, and what each failure looks like
run_pipeline.py   runs everything, skips what is already built
DECISIONS.md      every choice and why — the long version
```

Five invariants worth not breaking:

- **`api/server.py` holds no knowledge.** No region name, no date, no WMO. It
  reads `catalog.QUERIES` and `LiveValues`, which read the database. Checks
  assert this by grepping the source — and now grep `api/corpus.py` too, which
  writes English about the data and still names nothing.
- **`/meta` and the model's tool schemas are built from the same two objects.**
  The dropdowns a human sees and the enums a model is offered cannot drift
  apart, and a check compares them element by element.
- **Retrieval orients; it never answers.** Retrieved notes choose the query and
  fill its parameters. Every number in an answer comes from a tool result in
  that same conversation. Do not add a path that lets a summary be quoted as a
  figure — that breaks rules 2 and 3 in one move.
- **The notes go in the user turn.** The system prompt and the tool list are the
  byte-stable cache prefix; putting a per-question block in them invalidates the
  cache on every call, and a check compares two questions' `system` arrays byte
  for byte.
- **Every retrievable document carries the SQL that made it.** No hand-written
  facts in the corpus. A glossary sentence's numbers are asserted against the
  table they came from.
- **`api/router.py` is a sibling of `chat.ask`, not of `Transport`.** There is
  no model in it. Never register it on the transport seam: that seam's
  guarantees belong to `chat.ask`, and a model-free router claiming them would
  be claiming a tool loop and a turn bound it does not have. It reuses the
  *other* D7.4 seam — the injected `run_query` — which is why there is no
  second path into the database.
- **The path that answered is named everywhere it is shown.** Composer before
  you send, badge on the reply, chip in the audit trail, `provider` in the API
  response. There is no automatic fallback between paths: an answer that
  silently changed engine would read as though a model wrote it. Since D16.8
  the dashboard has one path and says so in all four places — and sends
  `provider: "lexical"` explicitly, so a key appearing in the API's environment
  cannot change what answered underneath a badge that still says `no model`.
- **Every preset question on the landing screen is measured, every run.**
  `ui/test_render.py` reads the chips off the rendered page, asks `/ask` each
  one, and prints the misses: nine chips, each routing to a real query with
  rows — except the BGC question, whose correct answer is a refusal (D16.7).
  A chip is not a piece of copy; it is a claim that a question works.

---

## Stage numbering

Stages are build order, and the numbers have collided once already: the Gemini
transport landed from a parallel session as Stage 8 while packaging was being
written as Stage 8, and packaging was renumbered to 9 (commit 20e3215).

**Before claiming a stage number, grep `DECISIONS.md` for it and check
`git log origin/master`.** Stage 10 was informally reserved for pgvector RAG;
the dashboard shipped first and took it (D10.1), so RAG became Stage 11 — and
because D10.1 *wrote that down*, Stage 11 was binding and retrieval took it
(D11.1). A reservation that is not written into the log is not binding on the
log; one that is, is. Stage 12 took the lexical router (D12.1), and Stage 13
took the dashboard's own check suite (D13.6), Stage 14 the dashboard driven
in Chrome (D14.1), Stage 15 the Vercel deployment configuration (D15.1), and
Stage 16 the chat-first dashboard, its curated preset questions and its
single answering path (D16.1, D16.7, D16.8), and Stage 17 the finished
deployment — the hosted database, the dashboard as its own project, and the
checks for the failure modes two deploy paths create (D17.1).
This section said "Stage 14 is unclaimed" for two stages after Stage 14 had
shipped — a reconstruction that was never corrected, which is the D10.1 failure
arriving from the other direction. **Stage 18 is unclaimed**; the deferred
items with a written home are the Ollama transport (D12.1), region centroids
(D12.10), the Playwright suite over the rendering (D13.6) and a real connection
pool in `catalog.py` (D17.5).

---

## Commands

```bash
.venv/bin/python run_pipeline.py            # build everything, skip what exists
.venv/bin/python run_pipeline.py --check    # 544 checks, no network, no API key
.venv/bin/python ui/test_ui.py              # the dashboard's couplings, no npm needed
.venv/bin/python ui/test_render.py          # the dashboard in Chrome (needs npm + Chrome)
.venv/bin/python etl/build_index.py         # Stage 11 index + its recall figures
.venv/bin/python api/retrieval.py           # just the measurement
.venv/bin/python api/corpus.py              # what is in the index, by kind
.venv/bin/python api/chat.py "how salty is the Bay of Bengal?"
.venv/bin/python api/chat.py --no-rag "..."  # the same loop, retrieval off
.venv/bin/python api/router.py              # routing, measured: 3 rates + misses
.venv/bin/uvicorn api.server:app --port 8000
cd ui && npm install && npm run dev         # dashboard on :5173
.venv/bin/python api/test_deploy.py         # Stage 15+17: the deployment config
```

**Deploying is `DEPLOYMENT.md`, and every platform step in it is marked
you.** Three tiers: Supabase Postgres, the API as a Vercel project rooted at
`.`, the dashboard as a second Vercel project rooted at `ui`, both imported
from GitHub so a push to master deploys both (D17.1).

The API is live at `argo-rose.vercel.app` from Stage 15; it boots, routes and
refuses honestly, and **it has no database behind it**, so it answers no
question about ARGO until Step 1 of `DEPLOYMENT.md` is done.

`FLOATCHAT_DSN`, `FLOATCHAT_ORIGINS` and `VITE_API_BASE` are the three
variables, and `.env.example` is the list. Four traps, all logged:

- `FLOATCHAT_DSN` must be the **pooled** connection string — a connection per
  query against a direct endpoint exhausts it under serverless (D17.5).
- Never `vercel deploy --prebuilt` from macOS: the Python builder resolves
  darwin wheels for numpy and faiss and the function dies on Linux (D15.8).
- Never `cd ui && vercel link`: D15.11's repo-level link resolves it to the
  **API** project, and `vercel --prod` there replaces the API with the
  dashboard at the API's URL. Import the second project in the web console.
- `data/rag/` is committed on purpose (D17.2). A push deploys from the git
  tree, and a gitignored index deploys an API whose retrieval is silently off.

The check suites need Postgres and nothing else. `api/chat.py` needs
`ANTHROPIC_API_KEY` or `GEMINI_API_KEY`; everything else runs without either,
**including the vector index** — with no key the embedder is the local lexical
one, which is why it is not called semantic.

**The key in this environment is the placeholder `YOUR_REAL_KEY` and the API
rejects it.** No live model call and no live embedding call has been made here.
If you are about to claim either works, check first.
