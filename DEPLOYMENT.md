# Deploying FloatChat

Stage 15 produced the configuration and deployed the API with no database
behind it. Stage 17 is the rest: a hosted Postgres, the dashboard as its own
project, and the two of them naming each other.

Everything below has been checked against the repository by
`api/test_deploy.py` (50 checks, no network, no account). **What none of it can
check is the platform side** — no Supabase project was created from here, no
`vercel env` was set, no push was made. The steps marked **you** are the ones
nothing in this repository can do for you, and the last section says exactly
what remains unproven after you have done them.

---

## The architecture

```
        GitHub  m0hiith/floatchat  ──  push to master triggers both
                       │
        ┌──────────────┴───────────────┐
        │                              │
  Vercel project "argo"          Vercel project "floatchat-ui"
  root directory: .              root directory: ui
  @vercel/python → api/index.py  vite → ui/dist  (static)
  FLOATCHAT_DSN ────┐            VITE_API_BASE ──┐
  FLOATCHAT_ORIGINS ┼───────────────── names ────┘
        │           │                    (and is named by
        │           │                     FLOATCHAT_ORIGINS)
        │           └──── pooled, read-only ────┐
        │                                       ▼
        │                          Supabase Postgres
        │                          floatchat_ro, SELECT only
        ▼                          transaction pooler :6543
  bundled: data/rag/*  (536 KB FAISS index + manifest)
```

Three services, one repository, one push. The AI path needs no third service
and no GPU: the dashboard's answering engine is `api/router.py`, a lexical
router that runs inside the API process with no model, no key and no network
call. The FAISS index is a 536 KB file that ships inside the function bundle.

### Why this and not the other three

| | Vercel + Supabase (**chosen**) | Render + Supabase | Railway | Fly.io / a VM |
|---|---|---|---|---|
| cost for this project | ₹0 | ₹0 until the free instance sleeps, then ~$7/mo | ~$5/mo, no free tier | ~$5/mo + your time |
| already configured here | **yes — Stage 15, deployed and booting** | no | no | no |
| cold start | measured 0.63 s on this project (D15.11) | free tier sleeps after 15 min, ~50 s to wake | no sleep | no sleep |
| what a demo-day failure looks like | a function cold start | **a 50-second blank page** | a card expiring | a machine you have to nurse |
| checks that already assert it | 50 (`api/test_deploy.py`) | 0 | 0 | 0 |
| Docker needed | no | no | no | yes |

**Chosen: Vercel for both projects, Supabase for Postgres.** The deciding
argument is not cost — three of the four are near enough free. It is that this
repository already contains a *checked* Vercel configuration that has been seen
to boot on Linux, and the one thing that would justify moving (an AI workload
that does not fit a serverless function) does not exist here: the dashboard's
router is arithmetic over 110 written exemplars.

The free-tier sleep is what rules out Render for an SIH demo. A judge clicking
a link and getting fifty seconds of blank page is the failure this whole
project is organised against, and it is the one platform difference that is
visible from the audience.

**Render stays the documented fallback**, and the repository is ready for it —
`api/server.py` reads `PORT` and `HOST`, so the start command below works
unchanged. Use it if the function bundle ever stops fitting the 250 MB limit;
that is the one failure mode that forces the move.

```bash
# Render / Railway / any container host, if you ever need it:
#   build:  pip install -r api/requirements.txt
#   start:  uvicorn api.server:app --host 0.0.0.0 --port $PORT
```

**No Dockerfile is included, on purpose.** Neither chosen platform builds one
— Vercel's Python builder installs `api/requirements.txt` itself — so a
Dockerfile here would be a fourth description of the dependencies that nothing
runs and nothing checks, which is exactly the drift rule 6 pins versions to
prevent. If you move to a container host, that is when to add one.

---

## Before you start

| | |
|---|---|
| a GitHub account with this repo pushed | `git push origin master` |
| a Vercel account | free Hobby plan |
| a Supabase account | free tier: 500 MB, and the database is 96 MB |
| `psql` and `pg_dump` locally | already present — PostgreSQL 14 |
| the Vercel CLI | optional; the whole deployment can be done from the web console |

A local database that passes `run_pipeline.py --check`. **The hosted database is
a copy of the one this repository builds, not a second build of it** — the
pipeline needs 155 MB of GDAC downloads and 300 MB of Python that no build step
should assume.

---

## Step 1 — the database

**you** Create a Supabase project. Region `ap-south-1` (Mumbai) — the data is
the North Indian Ocean and so, on demo day, are you. Save the database password
Supabase generates; it appears once.

From **Project Settings → Database → Connection string** you need two of the
three forms it offers, and they are not interchangeable:

| form | port | used for |
|---|---|---|
| session pooler (or direct) | 5432 | the restore and the role, below — full SQL, one session |
| **transaction pooler** | **6543** | **`FLOATCHAT_DSN` on the API** — see below |

The API needs the *transaction pooler*. `catalog.connect()` opens a connection
per query, which was correct for one local uvicorn process and is a liability
under serverless, where every invocation is its own process (D15.10). A direct
5432 endpoint runs out of connections under concurrency; the pooler is what
makes the unchanged code safe. This is the single most important line in this
document.

Dump the local database and restore it:

```bash
pg_dump -h localhost -d floatchat --no-owner --no-privileges -Fc -f /tmp/floatchat.dump
pg_restore -d "$OWNER_DSN" --no-owner --no-privileges --clean --if-exists /tmp/floatchat.dump
```

`$OWNER_DSN` is the **session pooler / direct** string with the owner role.
`--no-owner --no-privileges` because the local owner role does not exist there.
No PostGIS, no extensions: the schema uses core `point` and `polygon` with a
GiST index, which is why this restore is one command (D4.4).

Then the read-only role, **with a password that is not in this repository**:

```bash
psql "$OWNER_DSN" -v ro_password="$(openssl rand -hex 24)" -f db/roles.sql
```

`db/roles.sql` defaults to `floatchat_ro/floatchat_ro`, which is fine for a
database listening only on localhost and is a published credential for one that
is not. The file takes `-v ro_password=...` for exactly this, and it names no
database — `current_database()` is correct both locally (`floatchat`) and on
Supabase (`postgres`), where a hard-coded name would fail (D17.3).

**Prove it is read-only before believing it.** Nothing in the code can inspect
what a DSN contains; this can:

```bash
FLOATCHAT_DSN="postgresql://floatchat_ro:PASSWORD@HOST:6543/postgres?sslmode=require" \
  .venv/bin/python api/test_catalog.py
```

28 checks against the hosted database, four of which are write statements
Postgres must refuse. If a DELETE succeeds, you restored with an owner DSN and
the entire safety argument in `api/catalog.py` is void (D15.2).

> The username through Supavisor carries the project ref — `floatchat_ro.abcdefgh`
> rather than `floatchat_ro`. Copy the exact form from the connection-string
> panel and swap the role name; do not assemble it by hand.

---

## Step 2 — the API

**you** In Vercel: **Add New → Project → Import** `m0hiith/floatchat`.

| setting | value |
|---|---|
| Root Directory | `.` |
| Framework Preset | Other |
| Build/Output/Install | leave empty — `vercel.json` decides |

`vercel.json` names one build (`api/index.py`) and one route. That `builds`
array is load-bearing: without it Vercel's zero-config Python runtime publishes
every `.py` under `api/` as its own function, including all seven test suites
(D15.7).

Environment variables, **Production** scope:

```
FLOATCHAT_DSN      postgresql://floatchat_ro.<ref>:<password>@<host>:6543/postgres?sslmode=require
FLOATCHAT_ORIGINS  https://floatchat-ui.vercel.app
```

You do not know the dashboard's URL yet. Set `FLOATCHAT_ORIGINS` after Step 3
and redeploy — it is read at import, so it takes effect on the next deployment
and not before.

**Check the value actually saved.** `vercel env add` prints `! Value is empty`,
offers "Leave as is", and saves nothing; `FLOATCHAT_DSN` existed in this
project's production environment as `""` for a whole stage (D15.11). An empty
DSN is falsy, so the API silently falls back to `localhost` and every question
gets a 503 naming an address 8,000 km away.

Deploy, then:

```bash
curl -s https://<api>.vercel.app/health              # {"ok":true}
curl -s https://<api>.vercel.app/meta | head -c 400  # the database's own numbers
```

---

## Step 3 — the dashboard

**you** **Add New → Project → Import the same repository again.**

| setting | value |
|---|---|
| Root Directory | **`ui`** |
| Framework Preset | Vite (auto-detected) |
| Environment variable | `VITE_API_BASE = https://<api>.vercel.app` |

No trailing slash: `ui/src/api.js` concatenates `${API_BASE}${path}`.

**Vite inlines `VITE_*` at build time.** Changing it later does nothing until
the next deployment. Verified here on the real build: with the variable set,
the string appears in the bundle and the `http://localhost:8000` fallback is
constant-folded out of it entirely.

> **Do not use `cd ui && vercel link` for this.** Accepting the GitHub
> connection wrote a *repo-level* link (`.vercel/repo.json`, one project,
> `"directory": "."`), so linking inside `ui/` resolves to the **API** project
> and `vercel --prod` from there would replace the API with the dashboard at
> the API's URL (D15.11). Importing twice in the web console has no such trap.

Now go back to Step 2 and set `FLOATCHAT_ORIGINS` to this project's production
URL, and redeploy the API.

---

## Connecting them

Two variables, pointing at each other. Both are needed; one alone fails in a
browser console rather than in a log.

```
dashboard  VITE_API_BASE      = https://<api>.vercel.app
API        FLOATCHAT_ORIGINS  = https://<dashboard>.vercel.app
```

`*` is refused at import rather than honoured, and so is an origin without a
scheme — CORS matches by exact string, so `floatchat.vercel.app` would match
nothing and fail silently in front of an audience (D15.3).

**Preview deployments are not in the list.** Every Vercel preview gets its own
hostname, so a preview dashboard cannot call the production API. Demo from the
production alias.

---

## Testing the deployment

In order. Each one fails differently, which is the point.

```bash
API=https://<api>.vercel.app
UI=https://<dashboard>.vercel.app

# 1. the function boots and Postgres answers
curl -s $API/health                       # {"ok":true}

# 2. the database is the right database
curl -s $API/meta | .venv/bin/python -c \
  'import json,sys; d=json.load(sys.stdin)["database"]; print(d["floats"], d["profiles"], d["levels"])'
#    10 928 481181

# 3. retrieval actually shipped -- this is the one that is silently off if
#    data/rag did not make it into the bundle
curl -s $API/meta | .venv/bin/python -c \
  'import json,sys; print(json.load(sys.stdin)["ai"]["retrieval"])'
#    {'available': True, 'documents': 131, 'embedder': 'hashing', ...}
#    "no index built" here means includeFiles or the git tree is wrong (D15.5)

# 4. the answering path the dashboard uses
curl -s $API/ask -H 'Content-Type: application/json' \
  -d '{"question":"how salty is the Bay of Bengal?","provider":"lexical"}' \
  | head -c 300

# 5. the suites the test suite exists to keep private
curl -s -o /dev/null -w '%{http_code}\n' $API/test_router   # 404
curl -s -o /dev/null -w '%{http_code}\n' $API/catalog       # 404

# 6. the safety argument, against the real database
FLOATCHAT_DSN="<the production DSN>" .venv/bin/python api/test_catalog.py
#    28 passed -- including four writes Postgres must refuse

# 7. the browser path, which is the only one that exercises CORS
open $UI    # ask a preset question; the reply is badged "lexical router · no model"
```

---

## When it goes wrong

| what you see | what it is | the fix |
|---|---|---|
| `503 {"error":"database unavailable","dsn":"localhost:5432/floatchat"}` | `FLOATCHAT_DSN` is unset or empty — `vercel env add` saved `""` | set it, redeploy, re-check with `vercel env ls` |
| `503` naming the Supabase host | the DSN is right and the database refused | wrong password, wrong port, or the project is paused — open the Supabase console |
| the dashboard says "Cannot reach the FloatChat API at http://localhost:8000" | `VITE_API_BASE` was not set **at build time** | set it, then **redeploy** — a saved variable does not rebuild anything |
| the page loads, every request fails, the console says CORS | `FLOATCHAT_ORIGINS` does not contain this exact origin | add it with the scheme, no trailing slash, redeploy the API |
| the API fails at import with `FLOATCHAT_ORIGINS may not contain '*'` | someone set a wildcard | list the origins; this refusal is deliberate (D15.3) |
| `/meta` says `retrieval: {"available": false, "reason": "no index built"}` | `data/rag` is not in the bundle | it must be in the git tree *and* in `vercel.json`'s `includeFiles`; `api/test_deploy.py` checks both |
| `ImportError` on numpy or faiss at cold start | someone ran `vercel deploy --prebuilt` from macOS | plain `vercel --prod`; darwin wheels do not run on Linux (D15.8) |
| the build exceeds 250 MB | a dependency was added to `api/requirements.txt` | it is a deliberate subset of the root freeze — check what pulled the weight in |
| a `/query` times out | 10 s `statement_timeout` in `api/catalog.py`, not the platform | that is the catalogue refusing, correctly |
| too many connections | the DSN is the direct endpoint, not the pooler | use port 6543 (D15.10) |
| the dashboard deploy replaced the API | `cd ui && vercel link` resolved to the repo-level link | import the second project in the web console with Root Directory `ui` (D15.11) |
| you set `GEMINI_API_KEY` and retrieval did not change | it cannot — the index is searched with the embedder recorded in its own manifest (D17.10) | rebuild the index with the key set, then redeploy |
| `/meta` reports `ai.provider: "gemini"` after setting a key | correct, and harmless — the dashboard sends `provider: "lexical"` on every request, so the badge stays true (D16.8) | nothing, unless you did not mean to set the key |

---

## Demonstrating it

The demo is the audit trail, not the answer. Anyone can show a chat box; what
this project has is that every number on screen names the query that produced
it, and the engine that chose it.

1. **Open the dashboard cold.** It lands in **Chat** with nine preset
   questions. Do not type — click. Every one of those chips is measured on
   every run (`ui/test_render.py` asks `/ask` each one and prints the misses),
   so none of them is a hopeful piece of copy.
2. **Click "how salty is the Bay of Bengal?"** Point at the badge: `lexical
   router · no model`. Then say the sentence that wins this: *there is no API
   key behind this dashboard, and there is no code path that turns model output
   into SQL.* The router picked one of eleven hand-written parameterised
   queries.
3. **Open the audit trail beside it.** The query name, the parameters the
   catalogue bound, the row count. Then the chart, drawn from those rows by the
   same `displays.js` spec the Catalogue tab uses.
4. **Click the BGC question** — the one about biogeochemical parameters. It
   refuses, and names what it does have. That refusal is on the landing screen
   on purpose (D16.7): the honest "there are none" is demonstrated rather than
   avoided.
5. **Switch to Catalogue** to show the same eleven queries with dropdowns built
   from the database, then back. One audit trail, two front doors.
6. **If asked "what if it hallucinates a region?"** — open the browser console
   or `curl` the API with a made-up region name. The catalogue refuses with the
   list of real ones, because the enums are read from the database.
7. **If asked about the model** — `api/chat.py` on your laptop, and be straight
   that the deployed dashboard does not use it (D16.8) and that no live model
   call has been made from this repository.

Two things to do before you present: open the dashboard the day before so
Supabase is not paused, and click one question so the function is warm.

## What deploying this still does not prove

Written here rather than discovered on demo day:

- **Cold start is unmeasured for `/ask`.** `GET /openapi.json` was 0.63 s
  (D15.11); the router path additionally loads FAISS and fits 110 exemplars.
  Click one question before the judges arrive.
- **`catalog.connect()` still opens a connection per query.** The pooler makes
  that survivable; it does not make it a connection pool.
- **The model path has never made a live call from this repository.** The key
  here is the placeholder `YOUR_REAL_KEY`. The deployed dashboard does not offer
  it (D16.8), so this is a limitation of the claim, not of the demo.
- **Nothing checks the production environment's values.** Not whether the DSN
  is read-only (Step 1's manual check is the only instrument), not whether it
  is the pooled endpoint, not whether it is empty.
- **Supabase free-tier projects pause after inactivity.** Open the dashboard the
  day before, not the hour of.
