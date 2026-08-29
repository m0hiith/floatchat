# FloatChat — decision log

Every non-obvious choice, its alternatives, and why. These are my presentation
notes for the SIH defence.

---

## Stage 1 — fetch and filter the global profile index

### D1.1 — Dedicated venv, not the conda base environment
**Decided:** `/Users/mohith/argo/.venv` with a pinned `requirements.txt`.
**Alternative:** use the existing miniconda base env, which already had pandas,
numpy, fastapi and pydantic — zero installs needed for Stage 1.
**Why:** reproducibility. "Clone the repo, create a venv, `pip install -r
requirements.txt`, it runs" is a claim a judge can verify in a minute. If the
dependency list is "whatever happens to be in my global conda env", it isn't.
**Note:** pip resolved pandas **3.0.5** / numpy 2.5.2 on Python 3.13. pandas 3
is a major release; xarray/netCDF4 compatibility must be re-checked at Stage 3.
**Resolved at Stage 2:** xarray 2026.7.0 (declares `pandas>=2.2`), netCDF4 1.7.4
and cftime 1.6.5 all install as wheels on this interpreter -- no source builds,
no system HDF5, no numpy conflict. All ten Stage 2 files open and read
correctly through netCDF4, and an xarray smoke test on 6990608 decodes JULD to
`datetime64[ns]` and round-trips through `.to_dataframe()` under pandas 3.
The risk flagged here did not materialise.

### D1.2 — Download the gzipped index, not the plain text
**Decided:** `ar_index_global_prof.txt.gz` (58 MB).
**Alternative:** `ar_index_global_prof.txt` (315 MB).
**Why:** identical content, 5.4x smaller transfer. pandas reads gzip natively,
so nothing downstream is more complicated. Verified: the file decompresses to
315,732,336 bytes, exactly the plain file's advertised length.

### D1.3 — Fetch and filter are two separate scripts
**Decided:** `etl/fetch_index.py` downloads and caches; `etl/filter_index.py`
reads the cache and filters.
**Alternative:** one script that does both.
**Why:** the filter constants (date window, bounding box) are the things we
will re-tune. Splitting them means re-tuning never re-downloads 58 MB. The
download also writes to a `.part` file and renames only on success, so an
interrupted run cannot leave a truncated file that looks like a valid cache.

### D1.4 — Study box: North Indian Ocean, not the whole Indian Ocean
**Decided:** after the GDAC `ocean == 'I'` filter, restrict to
**lat −10..30 N, lon 40..100 E**.
**Alternative:** accept all of ocean code `I`, which reaches to the Southern
Ocean (lat −70).
**Why:** the demo's named regions are the Arabian Sea and the Bay of Bengal.
Floats drifting at 45°S are real ARGO data but would never be returned by a
region query, so they add ingest cost and no demo value.
**Cost:** 53,573 profiles in the date window drop to 12,372 — but from 2,100+
floats down to a workable 254 candidates, all in the waters we care about.

### D1.5 — The data mode is read from the FILE NAME, not from the file
**Decided:** determine delayed-mode (`D`) vs real-time (`R`) coverage per float
from the index filename prefix — `dac/wmo/profiles/**D**1234567_045.nc`.
**Alternative:** download candidate floats and read the `DATA_MODE` variable.
**Why:** the requirement is to *deliberately* include both delayed-mode and
real-time-only floats. The filename prefix answers that for all 254 candidates
without downloading a single NetCDF file. The authoritative per-profile
`DATA_MODE` is still read from the file itself at ETL time (Stage 4); the
filename is used only for *selection*.
**Trap avoided:** a trailing `D` after the cycle number
(`R1234567_045**D**.nc`) means a **descending** profile and has nothing to do
with delayed mode. The regex separates the two explicitly. 2,057 such files
appear in the first 400k index rows, so this is not hypothetical.

### D1.6 — Log the surviving row count at every filter step
**Decided:** the filter prints a funnel table and writes `filter_report.json`.
**Why:** project rule — silent dropping is forbidden. It is also the honest
answer to "how much of the GDAC did you actually look at": 3,397,664 index rows
in, 12,372 profiles out, 0.364%.

### D1.7 — The `approx_area` column is advisory, not data
**Decided:** the candidate table tags each float "Arabian Sea~" /
"Bay of Bengal~" / "equatorial" from its mean longitude and latitude.
**Why it is flagged:** region names are **not in ARGO data**. This label is a
crude longitude cut that exists only to help pick candidates by eye. The real
named-region polygons are a `regions` table in PostGIS at Stage 5, with their
boundary source recorded here. Nothing derived from `approx_area` enters the
database.

### Funnel result (GDAC index dated 2026-08-28)
| step | rows | % of total |
|---|---:|---:|
| index rows read | 3,397,664 | 100.0000% |
| ocean == 'I' | 649,035 | 19.1024% |
| date parsed | 649,005 | 19.1015% |
| date in 2023–2024 | 53,573 | 1.5768% |
| position present | 53,573 | 1.5768% |
| position in study box | 12,372 | 0.3641% |
| filename parsed | 12,372 | 0.3641% |

254 distinct floats — 193 with delayed-mode data, 61 real-time only, 81 from
the `incois` DAC.


---

## Stage 2 — pick the demo floats and pull their NetCDF files

### D2.1 — One aggregated file per float, not 939 per-cycle files
**Decided:** download the GDAC per-float aggregate,
`dac/<dac>/<wmo>/<wmo>_prof.nc` — 10 requests, 82,317,540 bytes.
**Alternative:** the 939 individual `<R|D><wmo>_<cycle>.nc` paths that Stage 1
already resolved for us and wrote into `filtered_profiles.csv`.
**Why:** 10 HTTP requests instead of 939, and the aggregate carries the
per-profile `DATA_MODE` variable — the authoritative source D1.5 already said
we would read at ETL time. Nothing we planned to use is lost.
**Cost, stated plainly:** when a cycle is upgraded from real-time to
delayed-mode, the aggregate keeps only the delayed copy. So we cannot show the
same cycle in both its R and its D form side by side. If that becomes a demo
beat, fetch the per-cycle files for **one** float; the exact index rows needed
to do it are already on disk.

### D2.2 — Ten floats, each carrying the reason it was chosen
**Decided:** the set in `etl/demo_floats.py` — 10 floats, 939 profiles in the
window (664 D / 275 R by index filename).
Coverage: 6 D-only, 2 R-only, 2 mixed · 5 Arabian Sea, 3 Bay of Bengal,
2 equatorial · 4 DACs of which 5 floats are **incois** · 6 profiler types.
**Why the list lives in code rather than in my head:** the pick is editorial —
a human read the Stage 1 candidate tables and chose. The *check* is not.
`demo_floats.py` re-derives every coverage claim from `float_candidates.csv`
on each run and exits non-zero if one stops holding. A regenerated index that
changes a float's mode mix breaks the build instead of quietly costing the
demo its real-time example.

### D2.3 — Every download is verified against the index, keyed on (cycle, direction)
**Decided:** `fetch_profiles.py` does not just download. For each float it
checks `PLATFORM_NUMBER` is the float we asked for, that **every** profile the
index promised inside the 2023–2024 window is present in the file, and that
`DATA_MODE` agrees with the R/D letter in the index filename.
**Why keyed on (cycle, direction):** an ascending and a descending profile
**share a cycle number**. Keying the comparison on cycle alone collides
silently — the same class of trap as D1.5, one level down.
**Result:** 10/10 floats clean — 0 missing profiles, 0 mode disagreements.
That number is the point: it means the Stage 1 index and the Stage 2 files
tell the same story, and we can say so rather than hope it.

### D2.4 — `DATA_MODE` has a third value the filename cannot express
**Found:** mode **`A`** — real-time data with *adjusted* values. 370 of the
2,575 profiles on disk are `A` (floats 6903139, 2902273, 7901136, 2902766).
An `A` profile ships inside an **R**-prefixed file, so the filename says
"real-time" while the file says "real-time, adjusted".
**Consequence:** D1.5's filename trick is correct for *selection* and would be
wrong for *display*. Anything the UI claims about calibration must come from
`DATA_MODE`, never from the filename. The verification therefore compares
`D → {D}` and `R → {R, A}`, written out as `MODE_FROM_FILENAME` rather than
left implicit.
**Judge-facing version:** "delayed vs real-time" is a three-state field, and
we say which of the three each profile is.

### D2.5 — The files cover each float's whole life; our declared scope does not
**Found:** the 10 aggregates hold **2,575** profiles spanning
**2015-12-12 .. 2026-08-24**. Only 939 of them fall in the 2023–2024 window
D1.4 declared.
**Resolved at Stage 3 (D3.1): the window only.** Leaning was to ingest the
window only, so every count in the project reconciles with the Stage 1 funnel
and "0.364% of the GDAC" stays a true statement end to end. Ingesting all
2,575 is nearly free in storage and gives richer trajectories, but then the
funnel no longer describes the database and the scope claim has to be rewritten.
Whichever way it goes, it gets written here first.

### D2.6 — What the files actually contain (the Stage 3 inputs)
All ten floats are **core Argo**: `PRES`, `TEMP`, `PSAL` and nothing
biogeochemical, so one table schema covers the set. `*_ADJUSTED` fields are
present in all ten. `N_LEVELS` ranges from **59** (2902201) to **2221**
(7901136) — a 37x spread — so the ETL must treat the depth axis as ragged and
must not assume a fixed grid. `N_PROF` in a file is the float's whole life,
not our window, which is why every count above is qualified by which of the two
it refers to.

### Stage 2 result
| | |
|---|---:|
| floats downloaded | 10 / 10 |
| bytes | 82,317,540 |
| profiles in files (whole life) | 2,575 |
| profiles inside the 2023–2024 window | 939 |
| DATA_MODE D / A / R | 1,850 / 370 / 355 |
| indexed profiles missing from files | 0 |
| DATA_MODE vs filename disagreements | 0 |


---

## Stage 3 — parse the NetCDF files into flat tables

### D3.1 — Ingest the declared window, not the whole file (closes D2.5)
**Decided:** `parse_profiles.py` keeps only profiles whose `JULD` falls in
2023-01-01 .. 2024-12-31. 2,575 profiles on disk, **942** in the window.
**Alternative:** ingest all 2,575 — it is nearly free and the trajectories
would be a decade long instead of two years.
**Why the window:** every number in this project then reconciles with the
Stage 1 funnel. "3,397,664 index rows in, 0.364% survived, and here is the
database that contains exactly those" is one claim a judge can follow end to
end. Ingesting the rest would make the funnel describe something that is no
longer what we built, and the scope claim would have to be rewritten.

### D3.2 — Two CSV tables, not one wide table and not parquet
**Decided:** `profiles.csv` (one row per profile, 928 rows, 137 KB) and
`levels.csv` (one row per measured level, 481,181 rows, 27 MB).
**Alternative A:** one denormalised table. Rejected — profile metadata would
repeat 481,181 times, and the region/time queries the demo runs are all
profile-level.
**Alternative B:** parquet. Rejected — it costs a pyarrow dependency, and
Postgres `COPY` reads CSV natively, which is exactly how Stage 5 will load it.
The volume does not justify a columnar format.

### D3.3 — Prefer the adjusted value, fall back to raw, and record which
**Decided:** for `DATA_MODE` D and A use `<PARAM>_ADJUSTED`; for R use
`<PARAM>`. Where the adjusted copy exists **in name only**, fall back to the
raw value and stamp the profile row with `pres_source` / `temp_source` /
`psal_source` ∈ {adjusted, raw, raw_fallback, empty}.
**Why the fallback is not a hack:** 132 delayed-mode profiles carry a
completely empty `PSAL_ADJUSTED` (119 survive to the written table); a further
9 have no salinity in either copy. A naive "if D then ADJUSTED" rule would
have blanked a quarter of the delayed-mode salinity and called it data.
The written table says which copy every value came from, per profile.
**Written totals:** PSAL adjusted 690 · raw 110 · raw_fallback 119 · empty 9.

### D3.4 — Two out-of-box cycles are kept, and named
**Found:** float 2903143 drifted to 10.6°S and 10.1°S on cycles 61 and 62
(Jul 2024) — just outside D1.4's −10° southern edge. Stage 1 filtered the
*index* by position so it never saw them; the per-float aggregate has no such
filter, so they came back.
**Decided:** keep them, flagged `in_study_box = False`.
**Why:** D1.4's box was a device for choosing *floats*, not for clipping a
chosen float's track. Cutting two cycles out of the middle of a trajectory
would put a hole in the demo map to defend a boundary that was always
advisory. The alternative — re-clipping — was rejected for that reason, and
the flag means anyone can re-impose the box with a `WHERE` clause.

### D3.5 — QC flags 3 and 4 are not data
**Decided:** accept `{1, 2, 5, 8}`, reject 3 (probably bad) and 4 (bad). A
level is written only where pressure is present and good and at least one of
temperature or salinity survives its own flag.
**What this ten-float subset actually contains:** only flags 1, 3 and 4 ever
appear. The asymmetry is the story — **49,701 salinity levels rejected (6.4%)
against 1,538 temperature levels (0.2%)**. Salinity is ~32x more likely to be
flagged bad, which is the honest answer to "is your data clean".
**Also worth saying out loud:** 37% of the level cells in these files are
padding, not data — `N_LEVELS` is the longest profile in the file and every
shorter profile is filled out to it. 771,658 cells scanned, 481,181 written.

### D3.6 — Every dropped profile has a name and a reason
**Decided:** the parser prints, and `parse_report.json` stores, one line per
dropped profile, and it reconciles the written set against the Stage 1 index
by comparing **profile keys**, not two aggregate counters:

    939 promised by the index  −13 dropped here  +2 outside the box  = 928 written

**The 13:** float 2902203 cycles 250–262 (Jan–May 2023) are flagged QC **4 at
every level, in both the raw and the adjusted copy**, with
`PROFILE_TEMP_QC = PROFILE_PSAL_QC = 'F'` — the delayed-mode QC operator
condemned the whole block. Our ETL drops them and says so.
**The 1 extra:** 7901136 cycle 1 has `POSITION_QC = 4`. It never reached the
Stage 1 index either, because a profile with no trustworthy position fails the
"position present" step. The two stages agree without being made to.
**Why this matters more than it looks:** "your float has 74 profiles and your
database has 61" is the first question a sceptical judge asks. The answer is a
list of thirteen cycle numbers and a QC flag, not a shrug.

### D3.7 — JULD is rounded to the second
**Decided:** `stamps.round("s")`.
**Why:** `JULD` is a float count of days since 1950, so `days x 86400` leaves
nanosecond float noise — the first profile came out as
`2023-01-02 04:00:12.000000081`. ARGO's real time resolution is coarser than a
second, so that tail is an artefact of our own arithmetic, not data. Rounding
removes it before it reaches Postgres, where it would otherwise be silently
truncated to microseconds and look like precision we do not have.

### Stage 3 result
| | |
|---|---:|
| profiles written | 928 |
| levels written | 481,181 |
| floats | 10 |
| date range | 2023-01-02 .. 2024-12-31 |
| depth range | 0.0 .. 2052.2 dbar |
| levels per profile (min / median / max) | 35 / 509 / 1,153 |
| DATA_MODE D / A / R | 651 / 167 / 110 |
| salinity from a raw fallback | 119 profiles |
| profiles dropped, all named | 14 |
| index reconciliation | OK |


---

## Stage 4 — load into Postgres

### D4.1 — The Postgres already on this machine, not Docker
**Decided:** the running Homebrew **PostgreSQL 14.23** on `localhost:5432`, in
a dedicated database named `floatchat`.
**Alternative:** a Docker Compose Postgres, which is what most projects ship.
**Why:** there is no Docker on this machine — no `docker`, `podman` or
`colima` binary — and there *is* a Postgres already serving two other
databases. Adding a container runtime to run a database that is already
running would be ceremony, and the demo has to come up on this laptop.
**What we owe for that:** the setup steps are not self-documenting the way a
compose file is, so `db/schema.sql` and `etl/load_db.py` have to be runnable
from a bare `createdb`, and they are — the loader creates the database itself
if it is missing.

### D4.2 — Four tables, and the provenance is one of them
**Decided:** `floats` (10) → `profiles` (928) → `levels` (481,181), plus
`dropped_profiles` (14) and a one-row `ingest_run`.
**Why `dropped_profiles` is a table and not a log line:** project rule — silent
dropping is forbidden. Putting the refusals *in the database* means "why does
float 2902203 have 61 profiles when the index promised 74?" is a SQL query
returning thirteen cycle numbers and a QC reason, not an argument I have to
remember on stage.
**Why `ingest_run`:** the database states its own provenance — the GDAC index
date, the 2023–2024 window, the accepted QC flags, and both funnel endpoints
(3,397,664 index rows in, 928 profiles out). Nothing about where this data
came from lives only in a slide.
**Why `levels` is separate:** denormalising profile metadata onto 481,181 rows
to avoid a join is a bad trade at this size, and every region/time query the
demo runs is profile-level anyway.

### D4.3 — COPY through an UNLOGGED staging table, no ORM
**Decided:** `psql \copy` into `s_*` staging tables, then
`INSERT ... SELECT` into the typed tables, then drop the staging.
**Alternative:** psycopg2 + SQLAlchemy, or pandas `to_sql`.
**Why:** COPY loads 481,181 rows in one statement and costs **no new Python
dependency** at all. The staging hop is what makes the types honest — the real
tables carry `CHECK (data_mode IN ('R','A','D'))`, `CHECK (temp IS NOT NULL OR
psal IS NOT NULL)` and the foreign keys, so a bad row is rejected by Postgres
with a line number instead of being coerced quietly by a driver. A driver will
arrive when the API does; it is not needed to load a file.

### D4.4 — No PostGIS. Core Postgres already does the region query.
**Decided:** store position as `lat`/`lon` doubles plus a native `point`
column (`geom`), indexed with GiST. Named-region containment is
`polygon @> point`.
**Alternative:** `brew install postgis` — which pulls gdal, geos, proj,
sfcgal, protobuf-c and json-c, and whose Homebrew formula does not pin the
Postgres major version it builds against, so it may or may not land on the
running 14.
**Why core is enough:** D1.7 promised a `regions` table that answers "which
profiles are in the Bay of Bengal". Postgres 14 answers exactly that, from an
index. Proven, not assumed:

    EXPLAIN → Index Only Scan using profiles_geom_idx
              Index Cond: (geom <@ '((80,5),(95,5),(95,22),(80,22))'::polygon)
    341 profiles / 5 floats  Arabian Sea      mean 0-10 dbar temperature 28.10 degC
    209 profiles / 3 floats  Bay of Bengal    mean 0-10 dbar temperature 29.18 degC
    58 ms including the join to levels

**When this decision would flip:** PostGIS earns its install the moment we need
geodesic distance ("floats within 200 km of a point"), reprojection, or real
coastline polygons rather than convex boxes. If a demo question needs any of
those, install it and migrate `geom` to `geography(Point,4326)` — the column is
deliberately the only thing that would have to change.
**Note for Stage 5:** those polygons above are throwaways used to prove the
mechanism. The real ones need a cited boundary source, and floats drift across
region edges — 5 + 3 + 3 floats over two regions and unassigned water is 11,
not 10, because one float appears in two.

### D4.5 — The loader rebuilds, then refuses to finish quietly
**Decided:** `db/schema.sql` drops every table before creating it, and
`load_db.py` runs nine checks after loading and exits non-zero if any fails.
**Why rebuild rather than migrate:** the CSVs in `data/parsed` are the source
of truth and the database is a derived artefact. A re-run must produce the same
database, not append to whatever was there. Migrations start mattering when the
database holds something the files do not.
**The nine checks:** row counts for all four tables against the Stage 3 report,
orphan levels, profiles with no levels, `n_levels` disagreeing with the actual
level count per profile, pressures outside 0–6000 dbar, and profiles outside the
declared window. All passed on the first load.

### Stage 4 result
| | |
|---|---:|
| database | `floatchat` on PostgreSQL 14.23 |
| size on disk | 95 MB |
| floats / profiles / levels | 10 / 928 / 481,181 |
| dropped profiles recorded | 14 |
| verification checks | 9 / 9 passed |
| region query (polygon @> point) | GiST index-only scan, 58 ms |
| new Python dependencies | none |


---

## Stage 5 — the named regions

### D5.1 — Boundaries come from IHO S-23, with the MRGID stored
**Decided:** the nine IHO "Limits of Oceans and Seas" (Special Publication 23)
areas that intersect the study box — Arabian Sea, Bay of Bengal, Laccadive Sea,
Andaman or Burma Sea, Gulf of Aden, Gulf of Oman, Persian Gulf, Red Sea, Indian
Ocean — fetched from the Flanders Marine Institute (VLIZ) Marine Regions WFS.
Each row in `regions` keeps its **MRGID**, so any boundary can be traced back
to a published record.
**Alternative:** hand-drawn boxes, which is what the throwaway polygons in D4.4
were and what most hackathon projects ship.
**Why:** D1.7 committed to this in writing — "the real named-region polygons
are a `regions` table with their boundary source recorded here". A region name
in a natural-language query now resolves to a citation, not to my judgement
about where the Bay of Bengal ends.

### D5.2 — Two lossy steps, both measured rather than asserted
The IHO geometry cannot go into a core Postgres `polygon` as-is, so two things
had to happen, and neither is allowed to be a silent approximation.

**Island holes are dropped.** The source polygons carry one hole per landmass —
2,875 of them in the Indian Ocean alone — and core `polygon` cannot represent a
hole. Dropping them enlarges each region by exactly the area of the islands
inside it. An ARGO float parks at 1000 dbar and surfaces in open water; it is
never standing on an island.

**The outline is simplified.** Bay of Bengal ships 92,739 vertices. Every
region is Douglas-Peucker'd to a 1,500-vertex budget, and the tolerance that
achieved it is stored per row: 0.0009 deg (Gulf of Oman) to 0.0287 deg
(Indian Ocean).

**The measurement that makes both acceptable:** every one of the 928 profiles
is classified twice — once against the **full-resolution** geometry including
every island hole, once against the simplified ring that goes into the
database. **Zero disagreements.** The script exits non-zero if that is ever
untrue, so the claim is re-checked on every run rather than being a note from
the day it happened.
**Why no shapely/geopandas:** the two operations needed are Douglas-Peucker and
a point-in-polygon test, about thirty lines each. Importing them would pull
GEOS and GDAL — the same dependency weight D4.4 declined — to do work we can
write out and verify exactly. Still zero new Python dependencies at Stage 5.

### D5.3 — The assignment is computed twice, by two different implementations
**Decided:** `profile_regions` is populated by Postgres
(`regions.poly @> profiles.geom`), and `load_db.py` then checks each region's
count against the number produced by the independent Python ray-casting in
`fetch_regions.py`.
**Why:** the two implementations share no code. If the GiST/`@>` path and the
hand-written ray casting agree on all nine regions, the classification is very
unlikely to be wrong in the same way twice. All nine matched on the first run.
**Why a table and not a column on `profiles`:** IHO areas are meant to be
disjoint. A composite primary key `(profile_id, region)` means that if two
regions ever claim the same profile, the row appears and the "profiles in >1
region" check fails — instead of one assignment silently overwriting the other.
Confirmed disjoint here: all 928 profiles land in exactly one region.

### D5.4 — The advisory label was wrong, exactly as D1.7 predicted
**Found:** `approx_area` called all 289 profiles of float 6903139
"Arabian Sea~". The IHO boundary puts **268 of them in the Gulf of Aden**, and
only the last 21 — after the float drifted east past 51.3 E in February 2024 —
in the Arabian Sea.
**Why this is worth saying out loud:** it is the concrete vindication of the
rule in D1.7 that nothing derived from `approx_area` may enter the database.
A longitude cut chosen to help pick candidates by eye was wrong about a quarter
of the dataset. The advisory label is still in the `floats` table, next to the
real region, precisely so the difference can be shown rather than described.
**Also:** region membership is per-profile, not per-float. One float spans two
IHO areas, which is why "how many floats are in the Arabian Sea" and "how many
profiles" are different questions with different answers (5 and 299).

### D5.5 — The result is checked against physical oceanography
Mean 0–10 dbar values, straight out of the database:

| region | temperature | salinity |
|---|---:|---:|
| Bay of Bengal | 29.18 degC | **32.87** |
| Indian Ocean (equatorial) | 29.13 degC | 34.35 |
| Arabian Sea | 28.28 degC | 35.43 |
| Gulf of Aden | 29.22 degC | **36.29** |

**Why this matters as a check and not just a chart:** the Bay of Bengal is the
freshest surface water in the Indian Ocean because the Ganges and Brahmaputra
empty into it, and the Gulf of Aden is the saltiest because Red Sea outflow
passes through it. The pipeline reproduces both without being told to. A
region/QC/adjusted-value bug anywhere upstream would have blurred that ordering.

### Stage 5 result
| | |
|---|---:|
| regions loaded | 9 (IHO S-23, MRGID recorded) |
| source vertices / stored | 537,487 / 13,496 |
| island holes dropped | 8,827 |
| profiles classified | 928, each in exactly one region |
| simplification disagreements | 0 / 928 |
| Postgres vs Python cross-check | 9 / 9 regions agree |
| verification checks | 21 / 21 passed |
| region + depth + average query | 32 ms |
| new Python dependencies | none |


---

## Stage 6 — the query layer

### D6.1 — The model fills parameters. It never writes SQL.
**Decided:** a fixed catalogue of **11 hand-written parameterised queries**
(`api/catalog.py`). The language model's entire influence is *which* query runs
and *what values* go into the placeholders.
**Alternative:** natural-language to generated SQL, which is what most
text-to-SQL demos do and what demos better on stage.
**Why templates:** three properties fall out for free and none of them depend
on the model behaving. There is no SQL-injection surface, because no string the
model produces ever reaches the SQL. There is no destructive statement to emit,
because none exists in the catalogue. And every answer is traceable to a named
query a human wrote and reviewed — `run()` returns the query name and the bound
parameters alongside the rows, so the demo can always show *which* query
produced a number.
**What it costs:** a question nobody anticipated gets "I can't answer that"
instead of a wrong SQL query confidently executed. For a defence, that is the
better failure.

### D6.2 — The enums come from the database, not from a constant
**Decided:** `to_tool_schema()` fills each region and float parameter's `enum`
by querying `regions` and `floats` at load time, and every tool is declared
`strict: true` with `additionalProperties: false`.
**Why:** a hallucinated region name is not something to detect and apologise
for — it is something to make unrepresentable. The model is handed the nine
real region names, and if it somehow proposes a tenth, `Param.coerce` rejects
it with the list of valid ones so the next attempt succeeds. The same holds for
the ten WMOs. Add a float to the demo set and the tool schema grows by itself.

### D6.3 — A read-only role, because "read-only by prompt" is not read-only
**Decided:** the query layer connects as `floatchat_ro`, which holds SELECT and
nothing else (`db/roles.sql`).
**Why:** the defence has to survive the prompt being wrong. Even a model talked
into requesting a DELETE gets `permission denied for table profiles` from
Postgres.
**What the test caught:** the first version of the role was **not actually
read-only**. On PostgreSQL 14 the `public` schema grants CREATE to `PUBLIC`,
so `REVOKE CREATE ... FROM floatchat_ro` changed nothing and the role happily
created a table. The test asserted it couldn't, and failed. The fix is
`REVOKE CREATE ON SCHEMA public FROM PUBLIC` — which PostgreSQL 15 made the
default, and which we now do explicitly so the hardening does not depend on
the server version. This is the argument for writing the hostile test even when
you are sure of the answer.

### D6.4 — Claude Opus 5, adaptive thinking, strict tools
**Decided:** model `claude-opus-5`, `thinking: {"type": "adaptive"}`, catalogue
exposed as tool definitions with `strict: true`.
**Why Opus rather than a cheaper tier:** the model's job is intent routing over
eleven similar queries plus date arithmetic from vague phrasing ("last monsoon
season"). Getting the wrong query is a wrong answer with a citation attached,
which is worse than no answer. Cost is negligible at demo volume — one call per
question.
**Note:** `budget_tokens` is not used; it is rejected on this model family.
Effort stays at the default rather than being tuned before there is anything to
measure.

### D6.5 — The query layer is tested without a model, an API key, or a network
**Decided:** `api/test_catalog.py`, one file, no pytest, run it and read the
output — the same rule the ETL scripts follow. **28 checks, all passing.**
Every query runs against its documented example; eight hostile inputs are
refused *and* checked for a message that names the valid values (a bad region,
an injection string, an unknown float, `"last tuesday"` as a date, a limit of
ten million, an invented parameter); four write statements are refused by
Postgres itself.
**The check worth stealing:** one assertion is physics — Bay of Bengal surface
water must come out fresher than the Arabian Sea (32.872 against 35.428). It
fails if anything from the QC rules to the region polygons breaks, and it is
the only test in the suite that cannot be satisfied by a bug that is merely
self-consistent.

### D6.6 — Geodesic distance without PostGIS (closes D4.4's open clause)
**Found:** the `cube` and `earthdistance` extensions ship with this Postgres and
give great-circle distance in metres — `earth_distance(ll_to_earth(...), ...)`
— plus `earth_box` for an indexable radius search. Verified against a known
pair: 15N 68E to 12N 88E returns 2,189.6 km.
**Consequence:** D4.4 named "floats within 200 km of a point" as the thing that
would force a PostGIS install. It does not. `nearest_profiles` does exactly
that in core Postgres. The remaining reasons to install PostGIS are
reprojection and true multipolygon geometry, and the demo needs neither.

### D6.7 — The repository exists now
`git init`, one commit, 14 files. Data stays out (every stage regenerates it
from the GDAC), so the repository is 212 KB. D1.1's claim — clone, make a venv,
`pip install -r requirements.txt`, run it — is finally something a judge can
actually do.

### Stage 6 result
| | |
|---|---:|
| queries in the catalogue | 11 |
| tests | 28 / 28 passing |
| generated SQL | none, by design |
| database role | `floatchat_ro`, SELECT only |
| statement timeout / row cap | 10 s / 5,000 |
| new Python dependency | `psycopg` (the driver D4.3 deferred) |
| repository | initialised, 14 files, 212 KB |


---

## Stage 7 — the natural-language layer

### D7.1 — A manual tool loop, not the SDK's tool runner
**Decided:** `api/chat.py` drives the request → tool_use → execute → loop cycle
itself against `client.messages.create`.
**Alternative:** the SDK's `client.beta.messages.tool_runner`, which writes the
loop for you.
**Why:** the runner expects tools declared as decorated Python functions. Ours
are **generated at runtime** from the catalogue, with enums read out of the
database (D6.2) — there is no fixed function to decorate. More importantly,
every tool call has to pass through `Param.coerce` and land in the audit trail
before it reaches Postgres, and owning the loop is the simplest way to
guarantee that rather than hope a helper preserves it.

### D7.2 — The system prompt is generated from the database
**Decided:** `build_system()` fills the scope paragraph — float count, profile
count, level count, the date window, the nine region names — by querying at
startup.
**Why:** a hand-written prompt saying "about 900 profiles" starts drifting the
moment the demo set changes, and a prompt that misstates its own scope is how a
model ends up confidently answering outside it. Adding a float updates the
prompt, the tool enums, and the tests together or not at all.
**What the prompt is actually for:** three refusals the tools cannot enforce on
their own — answer only from returned rows, say plainly when the data does not
cover the question, and never silently substitute an easier question. The
tools stop the model reaching bad data; the prompt stops it filling silence
with plausible prose.

### D7.3 — A refused parameter goes back to the model, not up to the user
**Decided:** a `QueryError` becomes a `tool_result` with `is_error: true`
carrying the message, and the loop continues.
**Why:** the catalogue's error messages were written to name the valid values
(D6.2) — "'Atlantic Ocean' is not a region in this database. Valid regions:
…". Handed back, that is a correction the model can act on in one more turn.
Raised to the user, it is a stack trace. The trail records the failed attempt
*and* the corrected one, so the correction is visible rather than hidden.

### D7.4 — Two seams, so the whole loop is tested with no API key
**Decided:** the model is reached through a `Transport` protocol
(`AnthropicTransport` for real, `ScriptedTransport` for tests) and the executor
is injectable.
**Result:** `api/test_chat.py` — **28 checks, no network, no credentials** —
asserts what we own rather than whether Claude is clever: the request shape
(model, adaptive thinking, every tool `strict` with `additionalProperties:
false`, system prompt cached and stating the real scope), that parallel tool
results go back in **one** user message with matching `tool_use_id`s, that a
refused parameter round-trips and recovers, that a question outside the data
runs no query at all, and that the loop is bounded, refusals are surfaced, and
an over-large limit is stopped before Postgres sees it.
**Total across Stages 6 and 7: 56 checks, both suites runnable offline.**

### D7.5 — The test double had to snapshot requests
**Found:** `ScriptedTransport` originally stored the `kwargs` it was handed.
`ask` mutates one `messages` list in place, so every recorded turn showed the
conversation's *final* state. One assertion failed because of it — and a second
one **passed for the wrong reason**, matching "Arabian Sea" inside a success
payload it should never have been looking at.
**Fixed:** the transport deep-copies each request.
**Why it is in this log:** a test that passes for the wrong reason is worse than
one that fails, because nothing ever asks it again. The failing neighbour is
the only reason it was caught.

### D7.6 — Every answer carries its audit trail
**Decided:** `ask()` returns an `Answer` holding the text *and* an ordered list
of every query that ran, with the **bound** parameters — defaults included —
and the row count or the error. Printing an `Answer` shows both.
**Why:** this is the thing that separates the project from a text-to-SQL demo.
Any number on screen can be pointed at a named query, its parameters, and the
row count behind it, live, during questions.

### D7.7 — Written, tested, not yet run against the API
**State:** there are no Anthropic credentials on this machine — no
`ANTHROPIC_API_KEY`, no `ANTHROPIC_AUTH_TOKEN`, no `ant` CLI, no stored
profile. `python api/chat.py "<question>"` says so and points at the two
offline suites instead of failing obscurely.
**What is therefore still unproven:** whether Claude routes real questions to
the right query. Everything between the model's answer and Postgres is tested;
the routing itself needs one key and an afternoon of real questions.
**Model:** `claude-opus-5`, adaptive thinking, default effort (D6.4).

### Stage 7 result
| | |
|---|---:|
| tool loop | manual, bounded at 8 turns |
| tools exposed | 11, all `strict` |
| tests | 28 offline (56 with Stage 6) |
| model | `claude-opus-5`, adaptive thinking |
| new dependency | `anthropic` 1.2.0 |
| live API calls made | none — no credentials on this machine |

---

## Stage 8 — running the same loop on Gemini

### D8.1 — An adapter behind the existing seam, not a second loop
**Decided:** `api/gemini.py` provides `GeminiTransport`, which satisfies the
`Transport` protocol from D7.4. `chat.ask`, the catalogue, the audit trail and
the system prompt are untouched.
**Alternative:** a parallel `ask_gemini()`, or refactoring the request into a
provider-neutral intermediate format that both transports render.
**Why:** the loop is the part that carries the guarantees — bounded turns,
every call through `Param.coerce`, every call in the trail. Two copies of it
means two things to keep correct, and the second copy is the one nobody
re-reads. A neutral format is the same work plus a third representation to
maintain, for a project with exactly two providers.
**What this cost:** the request is still written in Anthropic's shape, and
`api/gemini.py` translates it. That is a wire format, not an allegiance; the
docstring now says so, so the next reader does not mistake it for coupling.
**Evidence the seam was real:** Stages 6 and 7 needed no edit at all. All 56
existing checks pass unmodified, and `main()` — provider selection — is the
only line of Stage 7 that changed.

### D8.2 — Gemini has no `strict`, and it does not matter
**Found:** Anthropic tools take `strict: true` and `additionalProperties:
false` and enforce them. Gemini's `FunctionDeclaration` has no equivalent, and
rejects `additionalProperties` outright, so `clean_schema()` strips it.
**Why the safety argument survives:** the schema was never what made this
safe. An invented parameter is refused by `Query.validate`, a hallucinated
region by `Param.coerce`, and a write by the `floatchat_ro` role — all of them
in our process or in Postgres, none of them in the model's API (D6.1, D6.2).
`strict` made the model's job easier; it was not a defence.
**Demonstrated, not asserted:** `api/test_gemini.py` runs the same refusal and
row-cap cases through the Gemini path. `'Atlantic Ocean'` and `limit=99999999`
are refused there exactly as they are on Anthropic, with the valid values
named.

### D8.3 — Gemini matches tool results by NAME, so we send ids as well
**Found:** Anthropic pairs a result to its call by `tool_use_id`. Gemini's
`FunctionResponse` is matched by function *name* — which is ambiguous the
moment the model calls one query twice in parallel, which is exactly what a
region comparison does.
**Decided:** every `FunctionResponse` carries both `name` and `id`, and
`call_names()` rebuilds the id → name map by reading the transcript, since
Anthropic's `tool_result` block carries only the id.
**Test:** two parallel `region_summary` calls, one Arabian Sea and one Bay of
Bengal, come back distinguishable. Without the id this test passes by luck of
ordering, which is not a property worth having.

### D8.4 — Echo the original `Part`, never rebuild it
**Found:** Gemini attaches an opaque `thought_signature` to function-call
parts and rejects the next turn if it does not come back. A part reconstructed
from `name` + `args` loses it and looks well-formed.
**Decided:** every response block keeps the `types.Part` it was parsed from,
and `to_gemini_contents` returns that same object rather than building a new
one.
**The test nearly repeated D7.5.** The first draft of the identity assertion
had a stray `and False or` in it, so it collapsed to checking the signature
value and would have passed with a rebuilt part. It is now three checks: the
echoed part `is` the original object, its signature is byte-identical, and a
deliberately rebuilt part has `thought_signature is None` — the last one
existing only to prove the first two could fail.

### D8.5 — Reasoning is a distinct block type, so it cannot become the answer
**Decided:** a part Gemini marks `thought=True` becomes a `ThinkingBlock` with
`type="thinking"`, not `type="text"`.
**Why:** `chat.ask` builds the answer with `"".join(b.text for b in content if
b.type == "text")`. Had thoughts arrived as text, the model's private
reasoning — including numbers it was still working out — would have been
concatenated into the user-facing answer, in a project whose whole claim is
that every number on screen is traceable to a query. Asserted directly: a
response containing a thought part and an answer part yields only the answer.

### D8.6 — `cache_control` is dropped, not translated
**Decided:** the Anthropic cache breakpoint has no Gemini equivalent and is
simply not passed through.
**Why it changes nothing:** Gemini caches long stable prefixes implicitly, and
the system prompt is still the stable prefix (D7.2) with the volatile question
after it. The layout that earns the cache is the same; only the instruction
saying so is Anthropic-specific.

### D8.7 — The provider is whichever key exists; the transport owns its model id
**Decided:** `python api/chat.py "..."` picks Anthropic if Anthropic
credentials are present, else Gemini if `GEMINI_API_KEY` is. `--gemini` /
`--anthropic` force it, `--model=NAME` overrides the model, `--models` lists
what the key can actually reach.
**Why the transport ignores the model id it is handed:** `ask` sends
`model="claude-opus-5"` because that is what Stage 7 hardcodes. Honouring it
would be nonsense; silently substituting *and* logging it would put a model
name in the trail that was never called. `GeminiTransport` uses its own
`model` field, and that is the one recorded.
**Failures are diagnosed, not raised:** a rejected key and a renamed model id
each print one line and the command that fixes them, on D7.7's rule.

### D8.8 — `models.list` is not the same as "this key can call it"
**Found, with a working key:** the key lists 39 models, and three separate
things go wrong under it.
  * `gemini-3.1-pro-preview` and `gemini-pro-latest` return **429 with
    `limit: 0`** — not a rate limit but a tier with no pro quota at all, which
    no amount of waiting fixes.
  * `gemini-2.5-flash` and `gemini-2.5-pro` are in the listing and return
    **404 NOT_FOUND** when called.
  * `gemini-3.7-flash` and `gemini-flash-latest` answered `"say ok"` fine and
    then returned **503 UNAVAILABLE** twice under the real request, which
    carries the system prompt and eleven tool declarations.
**Decided:** default to **`gemini-3.6-flash`** — the newest model that
answered the actual request reliably — overridable by `$GEMINI_MODEL` or
`--model=`. My first guess, `gemini-3-pro-preview`, does not exist on this
key; guessing a model id from memory is not a thing to do when `--models`
takes a second.
**Why it is logged:** those three failures look identical in a traceback and
mean completely different things. `report_provider_error` now separates them:
`limit: 0` says retrying will never help and points at `--models`, a plain 429
says wait, a 503 says transient, a 404 says the id is wrong. A judge on a
fresh key hits at least one of these.

### D8.9 — Routing works. Stage 7's open item is closed.
**D7.7 and the first draft of D8.8 both ended "whether the model routes real
questions to the right query is unproven."** Seven live questions on
`gemini-3.6-flash`, against the real database:

| Question | Query chosen | Turns |
|---|---|---:|
| dissolved oxygen at 500 m | *none* — refused | 1 |
| how temperature changes with depth in the Arabian Sea | `depth_profile` | 2 |
| how much data for the Atlantic Ocean | *none* — refused | 1 |
| which floats, and which centres run them | `float_inventory` | 2 |
| why is float 2902203 missing profiles | `missing_profiles` + `data_provenance` | 3 |
| profiles per month in the Bay of Bengal | `monthly_profile_counts` | 2 |
| what did the monsoon look like in 1998 | *none* — refused | 1 |
| surface salinity, Bay of Bengal vs Arabian Sea | `compare_regions` | 2 |

**Seven of seven correct**, and every number in every answer traced to a named
query in the trail.
**The two results worth reading twice:**
  * **The three refusals ran no query at all.** Oxygen, the Atlantic and 1998
    were each declined from the system prompt and the tool enums, before any
    tool call — so the scope statement in D7.2 is doing exactly the work it
    was written for, and a wrong question costs nothing.
  * **The missing-profiles question called a second query nobody asked for.**
    It answered from `missing_profiles`, then pulled `data_provenance` to
    check the float's ingest history before committing to "no level survived
    QC". That is the behaviour the catalogue was shaped to allow, and the
    trail shows it happened rather than leaving it to be inferred.
**Still worth saying plainly:** eight questions is a demo, not an evaluation.
It proves the loop routes; it does not measure how often it routes right.

### Stage 8 result
| | |
|---|---:|
| files changed in Stages 6–7 | 0 (plus `main()`) |
| new module | `api/gemini.py` |
| tests | 45 offline (**101 with Stages 6 and 7**) |
| default model | `gemini-3.6-flash` |
| new dependency | `google-genai` 2.20.0 |
| live questions answered | 8 of 8, all traced to a named query |

## Stage 9 — packaging

### D9.1 — One runner, and it skips what is already built
**Decided:** `run_pipeline.py` runs the eight stages in order, skips any whose
output is already on disk, times each, and prints one line per stage.
`--check` runs only the verification suites; `--from` / `--only` / `--fresh`
do the obvious things.
**Why not a Makefile:** the conventional choice, and it would express the DAG
more honestly. But every stage in this project is a script that prints its own
report, and a runner in the same language can do the preflight — Python
version, the seven imports, whether Postgres answers — and say what is missing
instead of failing three stages later with a traceback.
**Caching is the point:** a first run moves 155 MB; a re-run finishes in about
8 seconds. Nobody re-verifies a pipeline that costs a coffee break to check.

### D9.2 — `--fresh` only forces the stages that can be forced
**Found while writing it:** the first version passed `--force` to every stage
with an output file. Three of those scripts parse no arguments at all, so the
flag was silently ignored — the stage looked forced and was not.
**Fixed:** `accepts_force` is set on the three downloading stages only; for the
rest `--fresh` just means "do not skip". A flag that appears to do something
and does nothing is the exact failure mode this log exists to prevent.

### D9.3 — The README leads with what is *not* proven
**Decided:** `README.md` carries a Known Limitations section naming the
untested routing, the deliberate ten-float scope, the absence of
biogeochemical parameters, the dropped island holes, and the two profiles
outside the study box.
**Why it goes near the top rather than in a footnote:** a judge will find every
one of these in five minutes. Finding them listed by the authors reads as
control of the material; finding them unlisted reads as an oversight. The
limitations are also all consequences of decisions logged here, so each one has
an answer ready.

### Stage 9 result
| | |
|---|---:|
| entry point | `python run_pipeline.py` |
| cold run | ~155 MB downloaded |
| warm re-run | 9.3 s, 122 checks |
| checks | 21 database + 28 catalogue + 28 tool loop + 45 Gemini adapter |
| documentation | `README.md` front door, `DECISIONS.md` long version |

---

## Stage 10 — the HTTP API and the dashboard

### D10.1 — Stage 10 is the dashboard; pgvector RAG becomes Stage 11
**Decided:** claim 10 for the API and the dashboard, and write the claim down.
**Why it needed deciding at all:** 10 was informally reserved for a pgvector
RAG layer, but nothing in this log said so — `grep 'Stage 10'` and
`grep pgvector` both returned nothing, and `origin/master` had no new commits.
The reservation existed only in conversation, which is exactly how D8/D9
collided the first time (commit 20e3215 renumbered packaging from Stage 8 to
Stage 9 after the Gemini transport landed from a parallel session).
**The rule that follows:** stage numbers track build order, and a reservation
that is not in this file is not binding on this file. The dashboard was built
first, so it is Stage 10. RAG is Stage 11 when it exists. `CLAUDE.md` now
carries "grep before you number" so the next parallel session reads it.

### D10.2 — An HTTP layer had to exist before a dashboard could
**Found:** the stage brief said "inputs: the FastAPI endpoints ... everything
the UI knows comes from GET /meta". No such endpoint existed. `fastapi` was
not in `requirements.txt`; the only mention of it in the repository was D1.1
noting that the conda env happened to have it. The closing section of this log
said so plainly — "there is no interface beyond the CLI".
**Decided:** one stage, two commits — `api/server.py` first, `ui/` second.
**Why not two stages:** the API has no purpose without the UI and no consumer
to test it against. Splitting them would have produced a stage whose only
evidence was its own test suite.

### D10.3 — /meta serves the catalogue's own `Param` objects
**Decided:** `/meta` renders each `Param` as `{kind, required, default, minimum,
maximum, choices, description}`, resolving `region` and `wmo` kinds to their
live values. The UI builds a control from the kind; it never learns what a
region is.
**Why this is the load-bearing decision of the stage:** `catalog.tool_schemas()`
builds the model's tools from `QUERIES` and `LiveValues`. `/meta` builds the
dropdowns from *the same two objects*. The choices a human is offered and the
enums a model is offered are therefore the same list by construction, and
`test_server.py` compares them element by element rather than trusting it.
Change the database, reload the page, and both move together.
**Consequence worth stating at a defence:** a hallucinated region is
unrepresentable for the model *and* unpickable for the human, for one reason,
not two.
**Cost:** `/meta` is rebuilt from the database on every request. Nine regions
and ten floats make that free; a hundred thousand floats would not, and the
answer then is a cache with an explicit invalidation, not a constant in the
server.

### D10.4 — Eleven queries, four display types, declared as data
**Decided:** `ui/src/displays.js` maps every query to `map` (3), `line` (2),
`bar` (2) or `table` (4), carrying the axis keys, the units, and the empty-state
sentence. It is the only file in the UI that knows a query name; every component
below it takes a spec and some rows.
**Alternative:** let each result component decide from the column names it sees —
`mean_psal_psu` ends in `_psu`, so label it PSU.
**Why not:** it works until a column is renamed, and it puts presentation
knowledge in five components instead of one. `invert: true` on the pressure
axis is what makes depth increase downward, and it is declared once.
**Two sub-decisions:**
- `region_summary` is one row of nine columns, which reads as a horizontal
  scroll. `orient: "row"` flips a single-row result to label/value pairs down
  the page. It is a table option, not a fifth display type.
- Every chart carries a "show the N rows behind this" toggle. A chart whose
  numbers cannot be read is the thing the audit panel exists to prevent.

### D10.5 — The audit panel is expanded by default and reads the response
**Decided:** every query that ran, with bound parameters and row count, visible
without a click, on screen at all times.
**Why not collapsed:** the argument for the per-chart row table applies harder
to the panel itself. This is not debug output; it is the evidence that the
number on the chart came out of the database, and evidence behind a disclosure
triangle is evidence nobody reads.
**The mechanism that makes it honest:** an optional field left blank is **not
sent**. The form does not pre-fill defaults. The catalogue binds them
server-side in `Query.validate` and returns them in `params`, and the panel
renders *that*, highlighting the keys the caller never sent. Leave `bin_dbar`
blank and the panel reports `bin_dbar=50`.
**Why it would otherwise be worthless:** if the form filled the default in and
the panel displayed the form, the panel would be echoing the browser and
proving nothing. Verified end to end in a real browser: the POST body carries
three parameters, the panel shows five.

### D10.6 — Every frontend version pinned exactly, and no library was added
**Decided:** react 19.2.8, leaflet 1.9.4, plotly.js-dist-min 4.0.0, vite 8.2.2,
tailwindcss 4.3.3, `@vitejs/plugin-react` 6.1.1, `@tailwindcss/vite` 4.3.3. No
carets anywhere. `fastapi==0.141.1` and `uvicorn==0.52.4` likewise.
**Also decided:** no `react-leaflet`. Leaflet's own API inside a `useEffect` is
a few more lines and one less dependency, and rule 6 makes the default "don't".
No component library, no state management, no data-fetching library — `useState`
and `fetch` carry the whole page.
**Cost, stated:** the Plotly bundle is 4.4 MB minified. Acceptable for a local
demo, and the alternative is a custom partial bundle, which is a build step to
maintain for a demo that runs on localhost.

### D10.7 — Postgres `numeric` arrives as a JSON string
**Found while smoke-testing the API,** before any chart existed: every `round()`
in the catalogue returns `numeric`, psycopg renders it as a `Decimal`, and the
default JSON encoding of a `Decimal` is `"28.093"` — a string.
**Why it mattered more than it looks:** handed to Plotly, `"28.093"` is a
category label. The axis silently becomes ordinal and the line is drawn in row
order rather than value order. It renders. It looks like a chart. It is wrong,
and nothing about it announces that.
**Decided:** `jsonable()` at the API boundary converts `Decimal` to `float` and
leaves `NULL` as `null`, with a check asserting both. Not in the UI, because
the model-facing path deserves the same numbers.
**The null half is the same rule as rule 2:** a level with no salinity reading
is not a level with zero salinity. The deepest bin of the Arabian Sea profile
is exactly this case — 3 levels, temperature present, salinity `None` — and
the chart leaves a gap rather than drawing the line to the seabed.

### D10.8 — Four failure states, each rendered as itself
**Decided:** the dashboard distinguishes, visually and by HTTP status:
| state | code | what the user sees |
|---|---|---|
| API unreachable | fetch throws | the URL tried, the command to start it, and "this is not an empty database" |
| database down | 503 | psycopg's own reason and the host it tried |
| parameter refused | 400 | the catalogue's message, with the valid values as chips |
| no rows | 200 | "no rows", never 0.0 |
**Why the first one is spelled out in words:** an API that is down and a
database with no regions both produce empty dropdowns. So no dropdowns render
at all in that state, and the panel says which of the two it is. The two must
not be able to look alike.
**The refusal renders the catalogue's own sentence.** `QueryError` messages
always name what would have been acceptable, so the UI splits on that phrase
and shows the alternatives — and falls back to plain text if a future message
is worded differently. It composes nothing itself.
**`nearest_profiles` with no results still draws the map**, taking the centre
and the radius from the *bound* parameters rather than from the rows, over the
sentence "0 profiles within 50 km". A blank panel would not tell you whether
the query ran.

### D10.9 — Three bugs the browser found that reading the code did not
**A detached Leaflet circle cannot report its bounds.** `Circle.getBounds()`
projects through `this._map`, so calling it on a circle that was never added
threw — and the exception unmounted the entire dashboard. A white screen is the
one failure mode indistinguishable from every other, which makes it the worst
one available. Fixed at the source, and a `DisplayBoundary` now contains any
repeat so a drawing bug costs the chart and not the audit panel.
**Zero-based bars hid the comparison the query exists to make.** Arabian Sea
35.428 PSU against Bay of Bengal 32.872 PSU, drawn from zero, are two bars the
same height. Truncating the axis is the conventional fix and it lies the other
way — a 2.5 PSU gap becomes a cliff. Decided: keep the zero baseline and print
the value on the bar.
**Selecting a query low in the list scrolled the picker off screen.**
**Why these are logged:** all three build clean, pass every server-side check,
and are invisible in the source. They were found by driving a real browser and
reading the rendered `layout` object back out, which is now how this stage is
verified.

### Stage 10 result
| | |
|---|---:|
| endpoints | 3 (`/meta`, `/regions.geojson`, `/query`) |
| queries with a declared display | 11 of 11 |
| display types | map 3 · line 2 · bar 2 · table 4 |
| new checks | 62 (`api/test_server.py`) |
| total checks | 184 |
| frontend dependencies | 4 runtime, 4 build, all pinned exactly |
| lines that hardcode a region, date or WMO | 0, asserted |

---

## Stage 11 — retrieval, and the AI switched on

### D11.1 — Stage 11 is the RAG layer, exactly as D10.1 reserved it
**Decided:** claim 11 for retrieval, having first done what `CLAUDE.md` now
tells the next session to do: `grep 'Stage 11' DECISIONS.md` (nothing),
`grep -n pgvector` (only D10.1), `git log origin/master` (no new commits).
**Why it needed checking at all:** D8/D9 collided once and D10 nearly did. The
reservation for RAG *was* written down this time — D10.1 says "RAG is Stage 11
when it exists" — so unlike last time the log was binding, and this stage takes
the number the log gave it.

### D11.2 — FAISS, because pgvector is not installable on this machine
**Decided:** `faiss-cpu==1.15.0`, index on disk under `data/rag/`.
**Alternative:** pgvector, which is what D10.1 assumed and which would have kept
everything in one database with no new Python dependency.
**Why not:** `SELECT name FROM pg_available_extensions` on this Postgres lists
`cube` and `earthdistance` and **not** `vector`. pgvector is not merely
uninstalled, it is unavailable — enabling it means a system-level package
install, and the README's standing claim is "no Docker, no PostGIS, no GDAL,
`pip install -r requirements.txt` and run". Trading that for a vector column
was the wrong trade.
**Cost of FAISS:** one wheel, 4.9 MB, whose only dependencies are numpy and
packaging — both already pinned. Rule 6 was followed: the dependency was put
to the author before it was added, alongside sentence-transformers (~2 GB of
torch) and Chroma, and FAISS was chosen.
**What this gives up:** the index is a file, not a table, so it is not
transactional with the data it summarises. A load that changes the database
leaves a stale index behind, and nothing currently notices. `/meta` reports
`built_at` so the staleness is at least visible; making it *impossible* would
mean rebuilding the index from `load_db.py`, which is the honest fix and is
not done.

### D11.3 — Two embedders behind one seam, and the keyless one is called lexical
**Decided:** `embed.Embedder` with three implementations — `GeminiEmbedder`
(`gemini-embedding-001`), `HashingEmbedder` (hashed n-grams + fitted IDF, no
key, no download), and `ScriptedEmbedder` for the suite. `embed.resolve()`
picks by credential exactly as `chat.make_transport` does (D8.1, D8.7).
**Why a second embedder at all:** the `GEMINI_API_KEY` on this machine is the
literal string `YOUR_REAL_KEY`, which the API rejects with `API_KEY_INVALID`.
Not one live embedding call has been made here. Building only the API embedder
would have meant shipping a stage that cannot run, cannot be measured and
cannot be demonstrated — on a project whose README promises that everything
except the chat CLI works with no credentials.
**The honesty constraint:** the local embedder is a bag of hashed word and
character n-grams weighted by inverse document frequency. That is real
retrieval and it is weak — it cannot match a synonym. Nothing in the code, the
README or the UI calls it semantic; `embed.py`'s docstring, `build_index.py`'s
report and the README's limitations all say what it is. Calling a lexical
matcher "semantic search" would have been the easiest lie in this project and
it is the one this decision exists to refuse.

### D11.4 — Every document in the corpus is generated by a query it carries
**Decided:** 131 documents, seven kinds, and each one stores the SQL that
produced its facts (`Document.source`).
**Alternative:** hand-written summaries, which is what a RAG demo usually
indexes and which would have read better.
**Why:** rule 2. A retrieved summary reaches the model and steers the answer;
if it were typed by hand it would be an unverifiable claim sitting one step
from the output. Generated, it is re-derivable — the UI shows the SQL under
every retrieved note, so a judge can run it. The glossary entries are the
sharp case: "delayed-mode is the most trustworthy" is documentation, but the
counts beside it (`523 D profiles`) come from `SELECT data_mode, count(*)`,
and a test asserts the sentence carries the number the table holds.

### D11.5 — A region with no profiles still gets a document
**Decided:** all nine IHO regions get a document; the five with no profiles get
one that says so in as many words.
**Alternative:** index only the four regions that have data.
**Why:** rule 1, applied to retrieval. "The Red Sea has no profiles in this
database" is an answer to a real question. Omitting the document does not make
the question go away, it makes retrieval return four plausible neighbours and
lets the model guess. The empty document is the difference between a stated
absence and a silence.

### D11.6 — Exact search, because approximate search here would be a lie
**Decided:** `faiss.IndexFlatIP` — a full scan of all 131 vectors.
**Alternative:** IVF or HNSW, which is what a vector-database demo is expected
to show.
**Why:** approximate nearest neighbour trades recall for speed. At 131
documents a full scan is a fraction of a millisecond, so there is no speed to
buy and the trade is pure loss. Shipping an ANN index would have been a
performance claim with no performance behind it. The docstring records the
threshold at which this should be revisited rather than inherited.

### D11.7 — The index carries the embedder that built it
**Decided:** the manifest stores the embedder's state, including the fitted IDF
weights, and `retrieval.load()` reconstructs it.
**Why:** searching an index built by embedder A with a query embedded by B is
*silent* nonsense — every score is meaningless and nothing raises. It is
exactly the class of failure this project is organised against. A test builds,
saves, reloads and asserts the same question returns the same hits in the same
order, which only holds because the IDF weights survived the round trip.

### D11.8 — `blake2b`, not `hash()`
**Found:** Python's built-in `hash()` is salted per interpreter
(`PYTHONHASHSEED`). A hashing embedder built on it produces a different vector
for the same string in the next process, so an index written today matches
nothing tomorrow — and it fails by returning bad results, not by raising.
**Decided:** `hashlib.blake2b`, and a check that re-embeds in a subprocess with
`PYTHONHASHSEED=random` and compares the vector element by element.
**Why the check and not just the fix:** rule 5. The fix is one line and looks
like an arbitrary choice of hash function; the check is what stops someone
"simplifying" it back.

### D11.9 — We normalise the API's vectors rather than assuming they are
**Found:** `gemini-embedding-001` returns normalised vectors at its native 3072
dimensions, but **not** after truncation to a smaller `output_dimensionality` —
and 768 is the size this project asks for.
**Decided:** `embed.normalise()` on every vector from every embedder, and the
suite asserts the norms rather than the scores.
**Why:** the index is `IndexFlatIP`, so an inner product is a cosine *only* if
the vectors are unit length. Un-normalised, the scores would still be finite,
still be ordered and still look plausible; long documents would simply win. A
zero vector stays zero instead of becoming NaN, and a document that embeds to
zero is refused at build time with its id named, because it would rank last
against every question and never be retrieved — a silent drop.

### D11.10 — The notes go in the user turn, not the system prompt
**Decided:** the retrieved block is the first content block of the user
message; the question is the second.
**Alternative:** append the notes to the system prompt, which is where context
usually goes.
**Why:** D7.2's system prompt sits in front of the cache breakpoint precisely
because it is byte-stable across questions. Folding a per-question block into
it would invalidate the cache on every single call. A check asserts that two
different questions produce byte-identical `system` arrays.
**Second-order effect:** the system prompt gains a `RETRIEVED NOTES` section
only when notes exist, so there are two stable prefixes rather than one drifting
one — and a question asked with retrieval off is byte-for-byte Stage 7's
request, which is still tested that way.

### D11.11 — Retrieval orients the model. It never answers.
**Decided:** the system prompt says, in as many words, "Do not answer from
them. Every number you state must come from a tool result in this
conversation, even when a note appears to contain that number already."
**Why:** this was the design fork, and it was put to the author explicitly.
Letting retrieved summaries be quoted directly is the textbook RAG demo and it
would have broken rules 2 and 3 together: a number on screen with no catalogue
query behind it, in a project whose entire argument is that every number is
traceable to a named query. The summaries are *derived* from queries, which is
close enough to be tempting and not close enough to be evidence — a summary can
be stale, a tool result cannot.
**What enforces it, beyond the prompt:** nothing, and that is worth stating
plainly. The catalogue enforces that no *invented* number can be produced by a
query, and the read-only role enforces that nothing can be written; but a model
that quotes a retrieved figure instead of running the query would produce a
wrong-looking-right answer, and only the audit trail would show it — the panel
says "no query was run" when the trail is empty, which is the tell.

### D11.12 — A broken index is a named failure, not a dead answer
**Decided:** `chat.ask` catches any retrieval exception, records it in
`answer.retrieved` as a document of kind `error`, and continues without notes.
**Why:** the tool loop worked for three stages with no index and still does.
Retrieval is an addition to the loop, never a requirement of it — so a corrupt
index file must cost the notes, not the answer. Rule 1 still applies: the
failure gets a name in the trail and a line in the UI, rather than looking like
a question that happened to retrieve nothing.

### D11.13 — Retrieval is measured, and the miss is printed
**Decided:** `retrieval.EVALUATION` is eighteen fixed questions with the
documents that should come back, and `evaluate()` reports recall@1/3/5 and MRR.
Current figures on the shipped keyless embedder: **recall@1 77.8%, recall@3
88.9%, recall@5 94.4%, MRR 0.835.**
**Why this matters more than it looks:** the standing open item at Stage 10 was
that routing was *observed* and never *measured* — "there is no fixed question
set with expected query names and no pass rate". This does not close that item
for routing, but it closes it for retrieval, and it is the first number of its
kind in the project.
**The miss stays in.** "How were the region boundaries decided?" does not find
`glossary:regions` in the top five; the lexical embedder has no path from
"decided" to "IHO S-23 polygons". `build_index.py` prints the question and what
it found instead. Rewording the question until it passed would have produced a
better number and a worse test.
**Two guards on the measurement itself:** every expected-document pattern is
checked to match at least one real document before the run, so a target that
matched nothing (a test that cannot fail) is an error; and the measurement runs
with the D11.15 routing floor switched off, because scoring it would flatter
the result.

### D11.14 — 1024 dimensions is a measurement, not a default
**Decided:** `HASHING_DIM = 1024`.
**Why it is not arbitrary:** at 256 the same corpus and the same eighteen
questions give recall@5 of 77.8% against 94.4% — hash collisions, visible in
the number. A check asserts the wider index beats the narrower one, so the
width is defended by a measurement rather than by a comment.

### D11.15 — The routing floor, and why it is excluded from the score
**Decided:** `Index.search` returns top-k, then adds the best-scoring
`query:` document if none made the cut (`ensure_kinds`).
**Why:** 92 of the 131 documents are region-months. A question phrased around a
place and a date fills every slot with region-months and never surfaces the
catalogue query that answers it — which is the one thing retrieval is here to
help with.
**Why it is not cheating:** the floor adds documents and never removes one, the
added document carries its real rank and score so the audit shows it was
floated in rather than won, and `evaluate()` runs with the floor off.

### D11.16 — /ask returns the rows, so the chat panel draws with `displays.js`
**Decided:** `POST /ask` supplies its own `run_query` into `chat.ask`'s existing
seam, capturing full rows, and returns the loop's audit trail with the rows
attached.
**Why:** the alternative is a chat panel that renders text and a row count,
which would have made the conversational path visibly weaker than the manual
one. Feeding the rows back means the chat answer's chart is drawn by
`ResultPanel` from the same `displays.js` spec the catalogue panel uses, from
rows the model's own tool call returned. There is no chat-specific renderer and
no second path into the database.
**A detail that would have been a bug:** the loop's audit trail includes
refusals, and refusals never reach `run_query`. Zipping the two lists by
position would have attached the wrong rows to every entry after the first
refusal. They are matched by advancing an iterator only on entries that ran.

### D11.17 — A model outage is a different 503 from a database outage
**Decided:** `ModelUnavailable` -> `{"error": "model unavailable"}`, beside
`Unavailable` -> `{"error": "database unavailable"}`. The browser reads the
label and renders `ModelFailure`, not `ApiFailure`.
**Why:** both are platform states rather than bad questions, so both are 503 —
but they are fixed in different places. Telling someone to restart Postgres
because their API key expired is a wrong answer delivered confidently, which is
the failure mode D10.8 built four separate states to avoid. The model panel also
says the thing that is true and reassuring: the eleven queries all still work.
**Where the wording comes from:** `chat.diagnose_provider_error`, split out of
`report_provider_error` so the CLI and the HTTP body say the same sentence.
D7.7's diagnoses are now reachable from a browser.

### D11.18 — The chat tab is offered only when `/meta` says it can work
**Decided:** `/meta` gains an `ai` block — provider, which keys are present,
and the retrieval index's document count, embedder and build time. The tab is
muted with the reason on its tooltip when there is no model.
**Why:** a tab that always errors is worse than a tab that is not there. And
reporting the index rather than badging "RAG" statically means deleting
`data/rag/` shows up in the UI instead of quietly changing what the model sees.
**It holds no ARGO knowledge.** Whether a key exists is not a fact about the
ocean, so the five grep checks that assert `api/server.py` names no region,
date or WMO still pass, and two more were added for `api/corpus.py`.

### D11.19 — The suggestions are built from the catalogue's own examples, and
one of them is meant to fail
**Decided:** `displays.js` holds six question *templates*; every value in them
is interpolated from `meta.queries[].example`.
**Why:** `displays.js` is already the one file in the UI allowed to know a
query name, and question wording is presentation. But a hardcoded "Show me
profiles in the Arabian Sea" would have put a region name in the browser, which
D10.3 spent a stage removing. Built this way, pointing the dashboard at a
different database renames the suggestions, and a suggestion cannot propose a
question the data cannot answer — the example it is filled from is the one the
query is tested against.
**The sixth suggestion asks for BGC oxygen profiles, and there are none.** The
problem statement asks for biogeochemical comparisons; these ten floats carry
no BGC parameters. Putting the question a judge would ask on a button, rather
than hoping nobody asks it, is D9.3's rule — lead with what is not proven —
applied to a demo script.
**New drift risk, so a new check:** the suggestions name query names and
example keys, and nothing else re-checked those. Three checks now assert that
every catalogue query has a display, that `displays.js` declares no query that
does not exist, and that every value a suggestion interpolates is present in
that query's example — so a renamed query breaks the suite instead of rendering
the word `undefined` inside a question someone is invited to click.

### D11.20 — One audit trail, two front doors
**Decided:** queries the model chose and queries a human chose land in the same
`AuditPanel`, in one ordered list, with a `model` badge on the former.
**Why:** it is the clearest statement of the safety argument that the interface
can make. The chat panel is not a second way to reach the database; it is a
second way to reach the same eleven queries, and the trail shows them
interleaved with the same parameters and the same row counts.

### D11.22 — A rejected key degrades the build loudly; it never fails the pipeline
**Found by running the flag rather than trusting it (rule 7).**
`run_pipeline.py --fresh` failed at Stage 11. `embed.resolve("auto")` selects
the API embedder when a key *variable* is set, and the variable here holds
`YOUR_REAL_KEY` — so the stage died and took the pipeline with it. The README's
oldest claim is that one command builds everything without credentials, and a
*bad* key broke it where a *missing* key did not.
**Decided:** `auto` catches a recognised provider failure, falls back to the
keyless embedder, and says so in five lines of `!!` — what was rejected, what
was used instead, that the resulting index is lexical rather than semantic, and
the exact command to redo it once the key works.
**Two things it deliberately does not do.** `--embedder=gemini` never falls
back and exits 2: asking for one embedder and being handed another is a lie,
and an explicit request is not negotiable. And an *unrecognised* exception is
re-raised rather than absorbed — substituting an embedder over a bug nobody
understands would produce a working index for the wrong reason, which is
exactly the quiet no-op rule 7 exists to forbid.
**Checked, not just written:** `fallback_reason()` is a pure function over
(requested embedder, exception) so all three rules are asserted with no network
and no key, and one more check greps the notice to make sure it still says
LEXICAL.

### D11.21 — What Stage 11 did NOT prove
Written down because rule 8 says lead with it.
- **The Gemini embedder has never made a live call from this machine.** The key
  in the environment is the placeholder `YOUR_REAL_KEY`. Its batching, its
  asymmetric task types, its short-reply refusal and its normalisation are all
  tested against a fake client that records what it was asked — the same
  technique as `ScriptedTransport` — so what is proven is the translation, not
  the transaction. Every retrieval number in this log is the **keyless lexical**
  embedder's.
- **`/ask` has never been driven by a live model either.** It is tested end to
  end with a scripted transport: the rows come back, the chart data is there,
  a refusal round-trips. What a real model does with the notes is unobserved.
- **The chat panel still has no automated tests**, exactly as D10.9 left the
  rest of the dashboard. It builds clean and the drift checks cover its
  couplings to the catalogue, but nothing re-checks on every run that an
  answer's chart renders.
- **The index is not transactional with the database** (D11.2).

### Stage 11 result
| | |
|---|---:|
| documents indexed | 131 (7 kinds, each generated by a query it carries) |
| vector store | FAISS `IndexFlatIP`, 1024 dims, exact cosine |
| embedders | 3 behind one seam (Gemini · keyless lexical · scripted) |
| recall@1 / @3 / @5 | 77.8% · 88.9% · 94.4% (MRR 0.835), keyless embedder |
| endpoints | 4 (`/meta`, `/regions.geojson`, `/query`, `/ask`) |
| new checks | 125 (`api/test_retrieval.py`) |
| total checks | 309 |
| new runtime dependencies | 1 (`faiss-cpu`, deps already pinned) |
| index build time | 0.3 s, keyless |
| lines that hardcode a region, date or WMO | 0 in `server.py`, `corpus.py` and the UI, asserted |

---

## Stage 12 — answering with no model at all

### D12.1 — The lexical router, and Ollama deferred rather than rejected
**Decided:** Stage 12 is a lexical router that picks one catalogue query from a
question with no model in the loop.
**Alternative, seriously considered:** an Ollama transport — a third adapter on
the `Transport` seam that already took Gemini, running a local model with
constrained tool output. Architecturally the cleaner of the two, and it
restores the demo path this project originally locked in.
**Why not, and it was the stated constraint that decided it:** "must run with
no network and no key, prove it in `run_pipeline.py`". Ollama needs no API key
but it needs a system-level install, a multi-gigabyte model pull and a running
daemon. An Ollama-backed check has two options and both are bad: skip when the
daemon is absent — a quiet no-op, rule 7 — or fail the pipeline on every
machine that has not pulled the model, which breaks D1.1 harder than the bad
Gemini key did in D11.22. What it *could* prove offline is the adapter
translation, mirroring `test_gemini.py`; that proves the adapter, not the demo.
**Two supporting reasons.** Small local models are unreliable at constrained
tool calling against an eleven-tool schema with database-derived enums, and
that is unmeasurable until the model is pulled. And "restores the LLM path"
overstates what is missing: the model path *exists* with 73 checks across
`chat.py` and `gemini.py`. It is not absent, it is uncredentialed. A third
transport adds to a seam that already has two.
**Deferred, not rejected.** When there is a network and time before a demo:
`brew install ollama` (~1 GB), `ollama pull qwen3:8b` or `llama3.1:8b` (~5 GB;
`qwen3:4b` at ~2.5 GB is the floor and should be expected to mis-route),
`pip install ollama`, `ollama serve` on `:11434`. Once, on wifi, never at check
time. Startup must fail the way D11.22 taught: `/meta` reports
`provider: null, reason: "ollama is not running on :11434"` so the tab is muted
with the reason, `/ask` returns the 503 carrying the exact commands, and
`test_ollama.py` runs against a recording fake so `--check` never needs the
daemon.
**And the cheapest fix is still neither.** A working `GEMINI_API_KEY` costs two
minutes and closes D11.21's first bullet and D8.9 together.

### D12.2 — A sibling of `chat.ask`, not a sibling of `Transport`
**Decided, and worth stating in exactly these words:** `api/router.py` is a
**sibling of `chat.ask`, not a sibling of `gemini.GeminiTransport`.** It does
not satisfy the `Transport` protocol and must never be registered as one.
**Why the distinction is not pedantry:** `Transport` is a seam to a *model*.
Registering a model-free router there would make `chat.ask` — which owns the
system prompt, the tool loop, the turn bound and the audit trail — appear to be
running when none of it is. Every guarantee in that function would be claimed
by something that does not have it.
**What it reuses instead:** the *other* seam from D7.4, the injected
`run_query` executor. `api/server.py` builds one closure and hands it to
`chat.ask` or to `router.answer` interchangeably, which is why there is no new
path into the database and why both paths' rows arrive in the same shape and
are drawn by the same `displays.js` spec.
**Checked:** `Router` has no `create()`, `router.py` imports no database driver
and opens no cursor, and the module's own docstring carries the sentence.

### D12.3 — Exemplars are routing fixtures, and D11.4 does not apply to them
**The objection to answer:** D11.4 says every retrievable document is generated
from a SQL query it carries. The exemplars are hand-written text. Is this the
same rule being broken one stage later?
**No, and the line is where the text goes.** D11.4 governs `api/corpus.py`:
things retrieved and shown as evidence *about the ocean*, which is why they
have to be re-derivable. An exemplar carries no fact. It is matched *against*
and never returned *as* content, and no word of one reaches an answer. It is
the same species as `retrieval.EVALUATION`'s patterns and
`catalog.Query.example` — a fixture describing how to *reach* the data.
**Enforced mechanically rather than argued:** no exemplar may contain a digit,
asserted over all 110. A digit in a phrasing means the phrasing is carrying a
value, and values come from `fill_slots` or from the catalogue's own defaults.
**It caught something while being written.** `surface_conditions` naturally
wants "the top 10 metres", and that digit was carrying `max_dbar` — a value
belonging to the catalogue default. Rewritten to "near the surface". The three
queries most suspected of needing a digit (`float_inventory`,
`float_trajectory`, `data_provenance`) needed none: the WMO comes from slot
filling in every case.

### D12.4 — Mask the values out of a question before routing it
**Found:** "is the Bay of Bengal fresher than the Arabian Sea" scored 0.274
against `compare_regions` and fell below the floor. The region names are noise
— they share almost no tokens with "compare two regions" and actively pull the
score down.
**Decided:** replace regions, WMOs and coordinates with a sentinel before
embedding. Routing is about the *shape* of a question; the values are
`fill_slots`'s job. It is the same principle the no-digit rule enforces on the
other side of the match.
**The first attempt was worse than doing nothing.** Substituting the phrase
"a region" made the placeholder a token in its own right, it matched the
exemplar "the depth profile for a region", and the question routed to
`depth_profile`. A masked value must contribute *nothing* to any route, not
contribute equally to the wrong one. The sentinel is punctuation, which
`embed.features` drops.
**Measured, and the first two claims about it were wrong.** Masking does not
change which route wins for that question, and after D12.6 moved the floor it
is not what rescues it from the floor either. What it does is raise the score,
and the aggregate is the evidence that it is worth doing: **routing accuracy
66.7% with masking, 60.6% without.** The check now asserts the weaker true
thing rather than the stronger false one.

### D12.5 — A structural gate, not a tuned floor, for the dangerous case
**Found by the measurement:** "delete all the profiles" scored 0.299 against
`float_inventory`, cleared the floor, and rendered a table of floats. Nothing
was deleted and nothing could be — `floatchat_ro` holds SELECT and nothing else
(D6.3) — but a destructive request was answered with data, looking for all the
world like it had been carried out.
**Decided:** a `NOT_PERFORMED` gate refuses `delete`, `truncate`, `drop`,
`insert`, `update`, `alter`, `export`, `download`, `email` and `train` before
routing, naming that this interface only reads. Not a security control: the
role is. An **honesty** control.
**Why structurally and not by raising the floor:** a floor high enough to
exclude 0.299 wrongly refuses ten legitimate questions, and it would leave the
dangerous case one paraphrase away from clearing it again. The verbs are
deliberately unambiguous ones; `remove` and `send` are excluded because a
legitimate question can contain them.
**Consequence worth having:** "export all of this to NetCDF" — asked for by the
problem statement and not built — now refuses for the *right* structural reason
rather than by scoring low.
**Also checked, so the list cannot rot:** `NOT_MEASURED` is a hardcoded set of
absences, and a test asserts none of those terms is a column in `levels`.
Ingest a biogeochemical parameter and the suite fails, forcing the list to be
corrected instead of quietly refusing data the database now holds.

### D12.6 — The floor is the one constant fitted to the question set
**Declared rather than buried, because it is the measurement's known weakness.**
`ROUTE_FLOOR = 0.23`.
**There is no clean separating value.** In-scope questions score 0.159 to 0.779;
out-of-scope questions that reach the router score up to 0.226. The
distributions **overlap**, and any floor trades in-scope recall against false
accepts:

| floor | false-accept | routing |
|---|---:|---:|
| 0.28 (first try) | 4.0% | 63.6% |
| **0.23 (shipped)** | **0.0%** | **66.7%** |
| 0.20 | 12.0% | higher, and admits a weather question and a satellite question |

**What makes 0.23 defensible rather than merely convenient:** it was chosen
*after* D12.5 removed the one genuinely dangerous case, so it trades paraphrase
recall only. It is not the thing standing between a destructive-sounding
request and a table of results. It is still fitted to this question set, and a
new set would move it.

### D12.7 — Three numbers, and the false-accept rate leads
**Decided:** report false-accept rate, refusal recall and routing accuracy
separately. Current figures, 33 in-scope and 25 out-of-scope questions:

| | |
|---|---:|
| **false-accept rate** | **0.0%** |
| refusal recall | 100.0% |
| routing accuracy | 66.7% |

**Why false-accept leads:** it is the number that measures answering something
we should not. A single figure would let a high routing accuracy hide it.
**Why refusal recall is not simply `1 − false-accept`:** it counts refusals
made *with the right reason kind*. Declining a biogeochemical question because
of the date window is a wrong answer that happens to say no, and only this
number catches it.
**66.7% is the honest ceiling of a lexical router, and the 11 misses are
printed on every run.** They are paraphrases with no shared vocabulary — "give
me the roster of instruments", "what happens to warmth as you go deeper". They
were not tuned away, and adding exemplars for them after seeing them fail would
be fitting to the answer key even without verbatim leakage.

### D12.8 — No evaluation question may contain a routing fixture
**Decided:** `leakage()` fails the suite if any normalised exemplar is a
**substring** of any normalised evaluation question.
**Why substring and not equality:** exact-match disjointness would let "show me
temperature against depth" through while the exemplar "temperature against
depth" sits inside it. The strict form is the point — that is precisely the
leakage worth banning.
**It matters concretely:** "which float went deepest?" is an exemplar and
scores exactly 1.000. Had it been left in the question set the router would
have been scored against its own answer key.
**And the check is checked:** a second test plants a leak and asserts the
detector fires, because a leakage check that cannot fail proves nothing.

### D12.9 — Every bound value says where it came from
**Decided:** `Slot(name, value, source, evidence)` with four sources —
`extracted`, `window-fallback`, `catalogue-default`, `missing` — returned in
the `/ask` response, and rendered by the panel *above* the chart when anything
fell back.
**Why it is not enough to put the dates in the audit trail:** they would be
there, correct, and indistinguishable from dates the question actually
contained. D10.5 spent a decision making the catalogue's defaults visible
rather than assumed; a router that quietly picked a two-year window and showed
it as a bound parameter would undo exactly that work.
**The distinction the sources draw:** `catalogue-default` is the catalogue
filling a blank it documents. `window-fallback` is *us* failing to parse
something. They are different failures and they are coloured differently.
**What parses:** ISO dates, "March 2023", a bare year, "between March and June
2023". **What does not, and falls back loudly:** "last six months", "recently",
"this year". Asserted: no query that binds dates may do so without a slot
recording the source.

### D12.10 — No gazetteer, and region centroids deferred
**Decided:** when `nearest_profiles` routes and the question has no
coordinates, refuse and say what format would work — do not look the place up.
**Why:** the moment "Arabian Sea = 15N 65E" is written into this repository, an
answer contains a number that is nowhere in the database. Rule 2 exists for
that. It costs one of the problem statement's own example questions ("salinity
profiles near the equator"), and that cost is paid deliberately.
**Deferred with a route through:** region centroids are *derivable* from the
IHO polygons already in `regions.poly`, and would be carried with the SQL that
produced them exactly as a corpus document is (D11.4). That resolves "near the
equator" without inventing anything. Not before the demo.

### D12.11 — The evaluation calls `answer()`; it does not re-implement it
**Found immediately.** The first `evaluate()` reproduced the pipeline —
scope gate, route, fill slots — and the copy diverged on its first run: a
below-floor refusal is constructed inside `answer()`, so the reimplementation
reported every correctly-refused question as "routed to None" and inflated the
false-accept rate.
**Decided:** `evaluate()` calls the same function the API calls, against a real
database connection.
**The rule:** a measurement of a reimplementation measures the
reimplementation. This is the same failure as D11.15's floor being excluded
from its own scoring, caught from the other direction.

### D12.12 — No automatic fallback from a failed model call to the router
**Decided:** if a model call fails, `/ask` returns the 503 with D11.22's
diagnosis. It does **not** answer with the router instead.
**Why, given D11.22 chose exactly the opposite for the index build:** the two
are different. Falling back to a keyless *embedder* changes how well retrieval
works and says so in five lines of `!!`. Falling back to a keyless *answering
engine* would hand back something that reads as though a model wrote it. The
path is chosen explicitly, before sending, and named on every reply.
**The affordance instead of the silent swap:** the composer carries a
two-button selector, the panel defaults to whichever path can actually answer,
and the model-failure state points at the one that works.

### D12.13 — What opening a browser found that reading the code did not
Stage 10 logged three such bugs (D10.9); Stage 11's panel had never been
rendered at all until this stage. Driving Chrome over the DevTools Protocol —
using only the already-pinned `websockets`, no new dependency and no download —
found four things:
1. **A stale `uvicorn` from before Stage 11 owned port 8000.** The new server
   hit `errno 48`, exited, and only its log knew; the browser was talking to
   pre-Stage-11 code with no `ai` block, and the Chat tab correctly rendered
   "no model credentials" for entirely the wrong reason. Ten minutes were spent
   suspecting the panel.
2. **The audit trail badged a lexically-routed query as `model`.** `via` was
   hardcoded to `"chat"` in `ChatPanel`. This is a direct violation of the
   badge requirement, it was invisible to every server-side check, and one
   screenshot showed it.
3. **The header claimed `RAG` and "the model picks a catalogue query" while
   the lexical path was answering**, because it keyed off capability rather
   than the live selection. Fixed by lifting the path state into `App`, so the
   header cannot contradict the replies.
4. **The empty thread left ~500px of dead space** between the opening card and
   the composer — `flex-1` on a thread with nothing in it.
Two and three are honesty bugs, which is the class this project is organised
against, and both were caught by looking rather than by testing.

### D12.15 — A dead key must not cost the first question
**Found by the author opening the dashboard**, which is the second time in two
stages that looking has beaten reading (D12.13).
**The failure:** `GEMINI_API_KEY` is set to a placeholder, so
`chat.resolve_provider()` reports `gemini`, so the composer defaulted to the
model path, so the first question anyone asks fails with a 503 — on a stage
whose entire purpose was "the chat panel works without a key". The working path
was one unlabelled click away and nothing pointed at it.
**Why the default could not simply be flipped:** a key variable being set is
the only evidence available before a call is made. Defaulting to the router
whenever *any* key exists would demote a working model on every correctly
configured machine.
**Decided:** learn from the failure instead of guessing ahead of it. When a
model call returns `no-model`, three things happen: the failure panel's
**primary** action becomes "Ask this again without a model", the selector moves
to the lexical path for the next question, and a line of text says it moved and
why.
**Why this is not the automatic fallback D12.12 refuses.** The distinction is
exact and worth keeping: the failed request stays failed and renders as a
failure — nothing is silently re-answered by a different engine. What changes
is the position of a selector, it announces itself on screen, and re-running
the question is an explicit click that the reader makes. D12.12 forbids an
answer whose engine changed without the reader knowing; it does not forbid
pointing at the door.

### D12.14 — What Stage 12 did NOT prove
- **Routing accuracy is 66.7% and that is the ceiling of the method, not a
  bug to be fixed later.** A third of legitimate paraphrases are refused. The
  router cannot follow up, cannot chain queries, cannot synthesise across
  results and writes no prose about the data.
- **The floor is fitted to the question set** (D12.6), and the in-scope and
  out-of-scope score distributions overlap, so a different question set would
  move it and could move the false-accept rate off zero.
- **Still no live model call from this machine**, and Stage 12 does not change
  that — it makes it survivable rather than fixed.
- **The chat panel now has automated checks of its couplings**, not of its
  rendering. What is verified by grep: the badge string, the fallback notice,
  the slot panel, the retrieval text being path-conditional. What is verified
  by having looked once: everything else.

### Stage 12 result
| | |
|---|---:|
| routes · exemplars | 11 · 110, none containing a digit |
| false-accept rate | **0.0%** (25 out-of-scope questions) |
| refusal recall | 100.0% (refused with the right reason) |
| routing accuracy | 66.7% (33 in-scope questions), 11 misses printed |
| leakage | 0 evaluation questions contain a routing fixture |
| new checks | 96 (`api/test_router.py`) |
| total checks | 405 |
| new dependencies | 0 |
| needs a key, a network, a daemon or a download | no, and `run_pipeline.py --check` proves it |

---

## Stage 13 — the dashboard checks itself

Stage 13 was unclaimed; the deferred items with a written home were the Ollama
transport (D12.1) and region centroids (D12.10). Neither was the thing most
likely to embarrass this project in a demo. Item 3 of "where the project
stands" was: **the dashboard's rendering has no automated tests**, and D12.13
and D12.15 had each found honesty bugs by opening a browser that no check
could see. This stage takes that list seriously by a third method and closes
part of it.

### D13.1 — A removed Plotly attribute, dropped in silence
**The failure:** all four dual axes in `PlotlyChart.jsx` set
`titlefont: { color }` to colour each axis title to its series — red for
temperature, blue for salinity. On the two-axis depth profile and the
counts-over-time chart, that colour is the only thing besides the legend that
says which axis belongs to which trace. **Plotly removed the top-level
`titlefont` in v4**; the axis title font now lives at `title.font`, and the old
spelling is discarded without a warning. `package.json` pins
`plotly.js-dist-min` at `4.0.0`. Confirmed against the pinned bundle rather than
from memory: the string `titlefont` occurs five times in it and every one is
`grouptitlefont`; the quoted attribute never appears, while the axis schema
reads `title:{text,font,standoff}`.
**Decided:** one `seriesAxis(series, rest)` helper builds all four axes, so the
correct shape is written once, and `ui/test_ui.py` asserts the removed spelling
never returns to UI *code* (comments may name it, and the one explaining this
does).
**Why it belongs in this log:** the chart rendered, the titles rendered, the
units rendered. Only the colour was gone. That is rule 7 exactly — a thing that
appears to do something and does not — and no amount of looking at a screenshot
would tell you the colour was *meant* to be there.

### D13.2 — Two map markers that only break in a production build
**The failure:** `float_trajectory` marked its first and last cycle with
`L.marker(...)` and no `icon`, which uses Leaflet's default. That default finds
`marker-icon.png` by reading the probe rule `.leaflet-default-icon-path`, and
failing that by looking for a `<link href$="leaflet.css">`. Under Vite **neither
works**: `npm run build` emits the stylesheet as `assets/index-<hash>.css`, and
inlines the probe rule's background as a base64 data URI that Leaflet's
`/^(.*)marker-icon\.png$/` cannot strip. The image path collapses to `""`, the
browser requests a bare `marker-icon.png`, and it 404s.
**Verified, not reasoned:** `npm run build` was run and `dist/assets/` contains
exactly two files — one CSS, one JS. No marker image is emitted at all.
**Why it had survived:** it is invisible in `npm run dev`, on one query, in one
mode. D12.13 opened a browser against the dev server, which is the half where
it works. A demo served from a build would show two broken images.
**Decided:** an `L.divIcon` pill labelled "first" and "last". A `divIcon` is DOM,
so no bundler and no file is involved and the failure is structurally
impossible. It also reads better than two identical unlabelled pins. Slate
chrome deliberately: on this map colour already means `data_mode`, and a marker
in one of those colours would read as data.

### D13.3 — A badge with three states was being reported by a two-way test
**The failure:** the Chat tab's badge read
`usingModel && ai.retrieval?.available ? "RAG" : "no model"`. That is a boolean
over a **three-valued** state, and it was false in both directions:
- a configured, selected model with **no index** was badged **"no model"**;
- a model was still badged **"RAG"** after the reader switched retrieval **off**,
  because `useRetrieval` lived in `ChatPanel` and the header could not see it;
- and a **third case surfaced while reading the diff of the fix**: the Catalogue
  tab renders the same nav, but called `Header` without the path props at all,
  so the Chat tab was badged "no model" on a server that had one — the same
  false sentence, reached from the other tab. It had been there since the badge
  was added.
D12.13 fixed exactly this class of bug by lifting `chatPath` into `App` so the
header could not contradict the replies. The retrieval switch was left behind,
and it is half of the same claim.
**Decided:** lift `useRetrieval` alongside `chatPath`; derive `usingModel` and
`usingRag` once in `App`; badge three states — `RAG`, `model · <provider>`,
`no model`. `usingRag` means *the model path is selected, an index is available,
**and** retrieval is switched on*, not "an index exists".
**And the check that should have caught it was pinned to the wrong thing.**
`test_router.py` asserted the literal string
`'usingModel && ai.retrieval?.available ? "RAG"'`. It was testing a spelling,
not a property, so it passed while the property was false and then failed when
the property was fixed. It now asserts the property, in three parts, and each
part was confirmed to fail when the old two-way expression is restored.

### D13.4 — Where the model goes away between two reads of `/meta`
`chatPath` was set with `p ?? default`, so an explicit choice was never
revisited. "Try again" re-reads `/meta`, and a key can stop working between two
reads. A server that has lost its model would leave `model` selected: the
composer would name an engine the server would not use, and the answer would
come back badged `lexical` underneath it. Kept the explicit choice, but `model`
is never left selected on a server reporting no `model_provider`.

### D13.5 — Small honesty repairs found while reading
- The retrieval checkbox rendered **ticked but disabled** beside the words
  "retrieval unavailable". The server degrades `open_retriever` to `None`, so
  nothing was actually wrong with the request — but a ticked box claims a
  mechanism that is not running. Unavailable now reads as unticked.
- `send()` cleared the composer even when the question came from a suggestion
  chip or the "ask this again without a model" button, throwing away whatever
  the reader had half-typed. Only a send *from* the composer clears it.
- `api.js` documents four failure modes in its header and lists the `kind`
  union on the field; `no-model` had been added to the prose and never to the
  union, and the header still said "three". `ui/test_ui.py` now compares the
  kinds thrown against the kinds listed, both ways.
- `PlotlyChart.bars()` carried two stacked comments that contradicted each
  other — "let each panel frame its own data" directly above "zero baseline,
  always" — left over from a reversal. The code does the second. The first is
  gone.

### D13.6 — Checking a UI from Python, and what that cannot do
**Decided:** `ui/test_ui.py`, 42 checks, reading `ui/src/*.js*` as text and
`api/catalog.py` as a module. It needs **no Postgres, no network, no key, no
npm and no browser** — the same claim Stage 12's suite makes, and it runs on a
fresh clone with nothing installed.
**Why source checks rather than the Playwright suite the log keeps promising:**
rule 6. A browser-driving suite is a new dependency and this one is not, and
every invariant it asserts is one that has *already been broken here at least
once*. It is not a substitute for that suite; it is the part that can ship
without asking.
**What it checks that nothing did before:** every catalogue query has a display
and no display names a query that no longer exists; `displays.js` is still the
only UI file that knows a query name *in code* (comments are stripped first, so
`TableView.jsx` explaining why `region_summary` is flipped stays legal); no
removed Plotly attribute; every `L.marker` sets its own icon; `DATA_MODES` has
three states, not two; dbar, PSU and °C are all still declared and pressure
still inverts; every suggestion names a real query **and** a query with a
non-empty example, so none can render `undefined` into a question someone might
click; and the badge rules of D13.3.
**Each check was mutation-tested.** The three central ones — the Plotly
attribute, the default marker icon, and `usingRag` requiring the switch — were
confirmed to fail when the bug is reintroduced, then the fix restored. A check
whose target cannot be hit is an error, not a pass (rule 5).
**What it cannot see, stated plainly:** whether anything *looks* right. It
cannot see a chart, a colour, a layout or an overlap. D12.13's four bugs were
found by looking, and three of the four would still be invisible to this suite.
It closes the couplings, not the rendering.

### D13.7 — What Stage 13 did NOT prove
- **No browser was opened in this stage.** The Leaflet finding was confirmed by
  running the production build and reading what it emitted, and the Plotly
  finding by reading the pinned bundle's own schema. Both are stronger than a
  screenshot for *these* bugs and weaker than one for everything else.
- **The rendering is still untested**, and the Playwright suite is still the
  honest fix. This stage lowers the number of ways the dashboard can lie; it
  does not make it verified.
- **Still no live model call from this machine.** Unchanged, and still the
  highest-value thing to do before a demo.
- **`float_trajectory`'s new endpoint markers have not been seen on screen.**
  They are structurally sound — a `divIcon` cannot 404 — but their anchor
  offset was chosen by reading, not by looking at the map.

### Stage 13 result
| | |
|---|---:|
| honesty bugs fixed | 3 (D13.3's badge, all three cases) |
| silent no-ops fixed | 2 (D13.1 axis colour, D13.2 marker 404) |
| checks pinned to a spelling, rewritten as properties | 1 (D13.3) |
| new checks | 42 (`ui/test_ui.py`) + 2 (`api/test_router.py`) |
| total checks | 428 |
| new dependencies | 0 |
| needs a key, a network, a daemon or a download | no, and `run_pipeline.py --check` proves it |

---

## Stage 14 — the dashboard, actually rendered

Stage 13 closed the dashboard's couplings and was explicit about what it could
not see — "whether anything *looks* right: no colour, no layout, no overlap"
(D13.6) — and left two of its own fixes marked **not seen on screen** (D13.7).
Item 3 of "where the project stands" had said the same thing for two stages.
This stage runs the dashboard instead of reading it.

### D14.1 — Drive the production build, never the dev server
**This is the whole reason the stage exists in this shape.** D13.2's broken
markers **cannot be reproduced under `npm run dev`**: the bug is in how Vite
*emits* the stylesheet, so it appears only in `npm run build`. A browser suite
pointed at the dev server — the obvious way to write one, and what D12.13 did
by hand — would have passed while the demo was broken.
**Decided:** `ui/test_render.py` runs `npm run build` and serves `dist/` as
static files. `dist/` is rebuilt only when a source file is newer, the way
every other stage skips work it has already done.

### D14.2 — The API is proxied through the page's own origin
The first version baked the API port into the bundle with `VITE_API_BASE` and
served the page on **5173**, because `api/server.py` allows exactly two CORS
origins and editing `DEV_ORIGINS` to make a test pass would be testing
something other than the shipped server.
**Both halves of that were wrong.** The fixed port re-created D12.13's trap —
a suite that dies because something else owns a port. And the baked-in port
made the build cache *unsound*: the API port is chosen free on every run, so a
cached `dist/` pointed at the previous run's dead port and the suite failed for
a reason with nothing to do with the dashboard. **It failed exactly that way
during this stage**, which is how it was found.
**Decided:** the static server forwards `/meta`, `/query`, `/ask`,
`/regions.geojson` and `/health` to `uvicorn`. The page calls them relatively,
so there is no cross-origin request, no CORS to satisfy, no production code
edited to test it, no fixed port, and **no port inside the bundle**. Every port
is picked free.
**And the build cache now records what it was built *with*.** A timestamp alone
cannot see that a bundle was built against a different API base; a stamp file
in `dist/` records the input, and a mismatch rebuilds. A cache keyed on time
alone was the actual bug.

### D14.3 — `_fullLayout`, because it says what SURVIVED
`ui/test_ui.py` can assert that the source no longer says `titlefont`. It
cannot assert that the colour *arrives*. Plotly's `_fullLayout` is the computed
layout — what was actually applied, not what was passed in — so an attribute
this version ignores is simply absent from it.
**This is the check that would have caught D13.1**, and it was confirmed by
putting the bug back: with `titlefont`, all four axis titles compute to
`#334155`, the default slate from `FONT`. With `title.font` they compute to
`#dc2626` and `#2563eb`. The colour really had been silently dropped — read off
the running chart, not inferred from the bundle.
The same mechanism verifies an ARGO rule that had never been checked on screen:
the rendered pressure axis has range `[2136.16, -136.16]`, **descending**, so
pressure genuinely increases downward rather than merely being asked to.

### D14.4 — D13.2, confirmed by the browser's own network log
Putting the default marker back made the browser request
`http://<origin>/marker-icon.png` and `marker-shadow.png`, **both 404**. The
bare filenames are the proof: `Icon.Default.imagePath` had collapsed to `""`,
exactly as predicted from reading Leaflet's `_detectIconPath`. With the fix,
two `div` markers labelled "first" and "last", zero `<img>` markers, and no
image requested at all.
`/favicon.ico` is excluded from the 404 check and the reason is written down:
the browser asks for it unprompted and the dashboard links none, so its 404
says nothing about the app. Tiles are blocked outright, which is also what
keeps this suite offline.

### D14.5 — The driver reads its values off the controls
The suite fills every **required** parameter from what the control itself
offers — the first real option, the date input's own `min`/`max`. No region
name, float id or date is written into it, for the same reason none is written
into the UI: point it at a different database and it fills that database's
values.
**One correction worth recording.** Taking the *first* option takes regions
alphabetically, which selected "Andaman or Burma Sea" — a region holding no
profiles in this subset. The chart assertions then ran against a "no rows"
panel and reported a missing chart. A check that lands on an empty result
**passes through every branch it was written to test without ever drawing the
thing**. The driver now picks the option the database holds the most data for,
reading the count off the label the control already renders
(`"Arabian Sea · 412 profiles"`) — still no hardcoded name.

### D14.6 — A skipped check is not a passed check
This suite needs more than the others: Chrome, and `ui/node_modules`. Exiting 0
without them would be a quiet no-op (rule 7); exiting 1 would fail a clone that
is not broken.
**Decided:** exit **2**, which `run_pipeline.py` renders as its own status —
`skipped`, never `ok` — with the missing piece named in the detail column and
repeated in a line at the bottom, because one row in a table of nine is easy to
read past. The browser path is also not hardcoded to the macOS bundle:
`CHROME_PATH` wins, and six common locations are tried, so a machine that keeps
one elsewhere gets a working suite rather than a silent skip.

### D14.8 — The rejected-key path, driven because a screenshot raised it
**Prompted by looking at the running dashboard**, which is the third time in
three stages that looking has started something (D12.13, D12.15).

The screen showed a question that had **failed** with "No model answered",
beside an audit trail reading **"1 query ran."** — `compare_regions`, two rows,
badged `lexical`. Those two halves of one screen disagree about whether
anything touched the database, which is the exact class of bug this project is
organised against.

**Reproduced before being believed.** The scenario was driven against an API
with a deliberately wrong key: the current code adds **nothing** to the trail,
which goes on saying "Nothing has run yet", and there is exactly one user turn.
The contradiction is not reproducible on this code, so the screenshot came from
a build predating Stage 13. **No fix was made, because none was needed** — and
that is worth an entry precisely because writing a fix would have been writing
a fix for nothing.

**What was built instead is the check.** The property is negative, easy to get
wrong, and had been verified only by having looked once: *a call that failed
must leave nothing behind*. Seventeen checks now drive the panel with a key
that is set and rejected — the state D12.15 exists for — and assert that the
failure panel names the variable to set, the trail stays empty and says so, the
selector moves and announces it, the badge stops claiming a model, and the
question is **not** re-answered behind the reader's back (D12.12). The same
question is then re-asked on the lexical path, and the trail gains exactly one
entry, badged `lexical`, drawing a two-trace chart.

**The key is pinned wrong on purpose.** `REJECTED_KEY` sets `GEMINI_API_KEY` to
a value that is set and invalid, and unsets `ANTHROPIC_API_KEY`. Left to the
ambient environment these checks would take the model path on a correctly
configured machine and spend money on a live call; pinned, they test the same
thing for everyone.

**Mutation-tested against the screenshot itself.** Making a failed ask append
`compare_regions · 2 rows · lexical` to the trail — the screenshot, in code —
turns three of these checks red, one of them reporting the panel's own
"1 query ran."

### D14.7 — What Stage 14 did NOT prove
- **It asserts structure and computed style, not appearance.** It can prove an
  axis title computes to `#dc2626`; it cannot prove two labels do not overlap,
  that a legend is readable, or that a layout survives a narrow window. No
  screenshot is compared against anything.
- **One browser, one viewport, one platform.** Headless Chrome at 1600×1400 on
  macOS. Nothing is checked on Firefox, Safari, or a phone.
- **The chat panel is driven along one route only** (D14.8): a rejected key,
  then the same question on the lexical router. A typed question, the other
  suggestion chips, the slot panel and the refusal path are still verified by
  `test_router.py`'s greps and by having looked once.
- **Still no live model call from this machine.** Unchanged.

### Stage 14 result
| | |
|---|---:|
| what it drives | headless Chrome over CDP, against `npm run build` output |
| bugs re-confirmed by reintroducing them | 2 (D13.1 computes `#334155`; D13.2 requests two 404s) |
| ARGO rules now verified on the rendered axis | 1 (pressure increases downward) |
| the rejected-key path | driven end to end with a key that is set and wrong (D14.8) |
| new checks | 50 (`ui/test_render.py`) |
| total checks | 478 |
| new dependencies | 0 (`websockets` was already pinned; Chrome is not installed by this) |
| needs a key or a network | no — tiles are blocked and every port is localhost |

---

## Stage 15 — deployed, or at least deployable

Nothing here has been deployed. This stage produced the configuration that
would deploy it, the two environment seams it needs, and a suite that checks
both — and the suite is the reason it gets a stage number at all. Everything
below was found by writing the configuration and then asking what about it
could be wrong without saying so.

### D15.1 — Stage 15, and `CLAUDE.md` was wrong about 14
`CLAUDE.md`'s stage-numbering section said "Stage 14 is unclaimed". It is not:
`ui/test_render.py` calls itself Stage 14, `run_pipeline.py` registers it as
Stage 14, and this log closed it with a result table. The reconstruction was
written before that stage landed and was never corrected — the exact failure
D10.1 warns about, arriving from the other direction.
**Decided:** deployment is Stage 15, and `CLAUDE.md` is corrected in the same
commit. **Why:** the numbering rule works only if the two files agree; a stale
"unclaimed" is worse than no note, because it reads as permission.

### D15.2 — The DSN comes from the environment; the default still names the read-only role
`api/catalog.py` hard-coded `postgresql://floatchat_ro:floatchat_ro@localhost`.
A deployment needs a hosted Postgres, and the credentials for one do not belong
in a committed file.
**Decided:** `DSN = os.environ.get("FLOATCHAT_DSN") or <the local default>`.
**Alternative:** a config module, or a `.env` loader. **Why not:** rule 6 —
that is a dependency and a second place for the truth to live, to replace one
`os.environ.get`. The name `catalog.DSN` stays module-level and rebindable
because `api/test_server.py` reassigns it to simulate an unreachable database.
**The part worth writing down:** the whole safety argument at the top of
`catalog.py` assumes the connection has SELECT and nothing else, and an
environment variable can hand it an owner. Nothing in the code can check that.
What can is `api/test_catalog.py`, which attempts a DELETE on whatever DSN is
in force — so run it *against the deployment* before believing the deployment
is read-only. That is the check, and it is a manual step, and saying so is the
honest version.

### D15.3 — Deployed origins are listed; `*` is refused, not honoured
`DEV_ORIGINS` was two localhost entries with a comment explaining that a
wildcard "would still be a claim we have not thought about".
**Decided:** `FLOATCHAT_ORIGINS`, comma-separated, appended to the dev origins.
`*` raises at import. An entry without a scheme raises too.
**Why the second one:** CORS matches origins by exact string, so
`floatchat.vercel.app` matches nothing. It would not error — it would simply
never allow the dashboard, and the failure would surface in a browser console
during a demo rather than in a message at start-up. That is rule 7's failure
mode wearing a different hat.
**Known limitation:** Vercel gives every preview deployment its own hostname,
so a preview UI is not in the list and cannot call the API. Use the production
alias, or add the preview origin. Not solved; recorded.

### D15.4 — `.vercelignore` REPLACES `.gitignore`, so it has to be complete
The CLI uploads the working directory. With no `.vercelignore` it filters by
`.gitignore` — which excludes all of `data/`, so the retrieval index would not
ship. The fix is a `.vercelignore` that keeps `data/rag`; the trap is that
creating one turns the `.gitignore` fallback **off entirely**, and everything
it was quietly excluding comes back: `.venv` alone is 301 MB.
**Decided:** a `.vercelignore` that lists all of it, and a check that computes
the upload set from the working directory and fails over 20 MB. Measured with
`.venv` un-ignored, the upload is 228.3 MB across 6,647 files; correct, it is
1.1 MB across 26.
**Why a computed set rather than a grep for `.venv`:** grepping the file tests
how the file is *written*. Walking the tree tests what would be *sent*.

### D15.5 — The index ships; it is not rebuilt on the platform
`data/rag/index.faiss` and `manifest.json` are 706 KB, and `etl/build_index.py`
needs Postgres and writes to disk — neither of which a build step should
assume.
**Decided:** ship the two files. **Why it is safe:** the manifest records
`embedder.kind == "hashing"`, the keyless lexical embedder, which is
deterministic and has no key to depend on.
**The trap, stated because it is silent:** set `GEMINI_API_KEY` on the
deployment and the embedder used at query time no longer matches the one that
built the index. Retrieval would still return documents; they would be worse,
and nothing would say so. Rebuild the index for the embedder you deploy with.
**And:** `api/retrieval.py` resolves the index at *repo root* `/data/rag`,
outside the function's own directory, so `includeFiles` in `vercel.json` is
what actually puts it in the bundle. Without it `/ask` deploys, works, and
reports `"no index built"` in `/meta` — an API with retrieval silently off.

### D15.6 — Two Vercel projects, which with the CLI means two directories
**Decided:** `vercel link` at the repo root for the API, `cd ui && vercel link`
for the dashboard. **Why:** the CLI takes its root from where it is run, so the
dashboard needs no "Root Directory" project setting and the API's
`vercel.json` never has to describe a static build it does not own. The UI
already reads `VITE_API_BASE` (D10.x) and `ui/vite.config.js` deliberately has
no dev proxy, so the split needs no UI change at all.

### D15.7 — A `builds` array, because zero-config would publish the test suites
Vercel's zero-configuration Python runtime makes a serverless function of every
`.py` file under `api/`. This repository keeps its checks there. Left alone,
the deployment would expose `/catalog`, `/corpus`, `/embed`, `/router`,
`/test_chat`, `/test_router` and five more as public routes.
**Decided:** `vercel.json` names one build, `api/index.py`, which disables the
detection, and one route that sends everything to it. `api/index.py` re-exports
`server.app` — the same object, not a copy, asserted — so the deployed
application cannot drift from the one Stage 10's 62 checks run against.
**Alternative:** move the suites out of `api/`. **Why not:** they are where
they belong; the platform's convention is what is unusual.

### D15.8 — Build on Vercel, never `--prebuilt` from this machine
`vercel build && vercel deploy --prebuilt` builds locally. The Python builder
pip-installs at build time, so on darwin it resolves macOS wheels for `numpy`
and `faiss-cpu`, uploads them, and the function raises `ImportError` on Linux
at the first cold start.
**Decided:** plain `vercel --prod`, remote build. Recorded here because the
prebuilt path is faster, is the one you reach for second, and fails in a way
that looks like a platform problem rather than a wheel problem.

### D15.9 — A second requirements freeze, and the check that keeps it honest
The root `requirements.txt` is a full freeze including `netCDF4`, `xarray` and
`pandas`; installed it is 301 MB against a serverless function's ~250 MB
uncompressed budget. The API imports none of the three.
**Decided:** `api/requirements.txt` — the transitive closure of what `api/`
actually imports, at the same versions, computed from installed metadata rather
than left to the resolver. 41 pins against the root's 47; 127 MB.
**The problem with a second freeze is that it can drift,** which is exactly
what rule 6 pins versions to prevent, so `api/test_deploy.py` asserts it is a
subset with identical versions, that the closure is complete, and — the real
property — that **no third-party module imported anywhere under `api/` is
missing from it**.
**That check earned itself immediately.** The first draft dropped `uvicorn`
deliberately, reasoning that Vercel serves the ASGI app itself. The check
failed: `api/server.py`'s `__main__` block imports it. A freeze that omits an
import its own source makes is wrong about the source, whether or not the
missing line would ever execute in production. `uvicorn` is in, and costs
1.5 MB.

### D15.10 — What Stage 15 does NOT prove
The list matters more than the checks, so it is not at the bottom of a
footnote.
- **Nothing has been deployed.** No Vercel account was touched, no CLI was run,
  no URL exists. The CLI is not installed on this machine. Everything above is
  configuration that has been checked against the repository, not against
  Vercel.
- **The Python version is the platform's choice.** The freeze is built against
  3.13. If the runtime selects a version with no `faiss-cpu` or `numpy` wheel,
  the build fails there — and the pins would need revisiting, the week of a
  demo, which is precisely what rule 6 exists to avoid.
- **The 127 MB figure is measured on darwin.** Linux wheels differ; `faiss-cpu`
  in particular is larger. The margin against 250 MB is real but it is not
  measured on the machine that matters.
- **Cold start is unmeasured.** `/ask` imports `numpy` and `faiss` and then
  runs a tool loop of several round trips. Nobody has timed it against the
  platform's function timeout.
- **`catalog.connect()` opens a connection per query.** That was correct for a
  local uvicorn process and is a liability under serverless concurrency, where
  each invocation is its own process. Use a pooled connection string; the code
  was not changed, and this is a known limitation rather than a fixed one.
- **No environment variable's production value can be checked from here** —
  including whether `FLOATCHAT_DSN` names a read-only role (D15.2).

### D15.11 — It was deployed, and three things the plan had not anticipated
`vercel --prod` from the repo root, 41s, live at `argo-rose.vercel.app`.

**What the deployment settles**, none of which D15.10 could:
- **The function boots on Linux.** `GET /openapi.json` returns 200 in 0.63s, so
  `numpy`, `faiss`, `psycopg`, `anthropic` and `google-genai` all imported from
  `api/requirements.txt` on the platform's Python. The wheel question and the
  "127 MB measured on darwin" caveat are answered *for this deployment*.
- **D15.7 holds in production.** `/test_router`, `/catalog` and
  `/api/test_router` return FastAPI's own `404 {"detail":"Not Found"}` — the
  suites are not routes.
- **D15.2's seam works, including its failure.** `/health` and `/meta` return
  503 `{"detail":"connection to server at \"127.0.0.1\", port 5432 failed",
  "dsn":"localhost:5432/floatchat"}`. That is correct in three ways at once: an
  empty `FLOATCHAT_DSN` is falsy and falls back to the shipped default, the API
  says the platform is down rather than showing an empty database (rule 2), and
  it names the address it tried (D10.x's four failure modes, unedited, from a
  machine 8,000 km away).

**Three things the CLI did that this log had not predicted:**

1. **`vercel link` offered to connect the GitHub repository, and it was
   accepted.** That creates a *second* deploy path whose source is the git
   tree — and `data/rag/` is not in the git tree, because `.gitignore` excludes
   `data/`. A push to `master` therefore deploys an API whose retrieval is off,
   reporting `"no index built"` in `/meta` and answering anyway. Two paths, one
   commit, different artefacts, no warning: this is D15.5's silent failure with
   a trigger attached to every push. **Not fixed in code**, because both fixes
   are choices: disconnect the integration and stay CLI-only, or commit the two
   706 KB index files so both paths ship them.
2. **The git connection changed what linking means.** It wrote a *repo-level*
   link — `.vercel/repo.json`, one project, `"directory": "."` — so
   `vercel link` inside `ui/` resolved to the same `argo` project instead of
   offering a new one, and wrote no `ui/.vercel` at all. D15.6's two projects
   silently became one, and a `vercel --prod` from `ui/` would have replaced
   the API with the dashboard at the same URL.
3. **`vercel env add` accepted an empty value.** It printed `! Value is empty`,
   offered "Leave as is", and saved. `FLOATCHAT_DSN` exists in production and
   is `""`.

**Still unproven after deploying:** whether `data/rag` actually shipped — it
cannot be seen from outside, because `/meta` fails on the database before it
reports `retrieval` — and the cold start of `/ask`, and D15.10's
connection-per-query under concurrency. All three need a database first.

### D15.12 — `vercel link` put a credential in the upload set
It writes `.env.local` holding a `VERCEL_OIDC_TOKEN` and adds it to
`.gitignore`. That does nothing here: `.vercelignore` **replaced** `.gitignore`
as the upload filter (D15.4), so the token was inside the bundle, and nothing
in the CLI's output says so. The deployment now live was built that way.
**Decided:** `.env*` in `.vercelignore`, and a check that fails on any `.env*`
or `project.json` in the computed upload set — the set, again, not a grep.
Made to fail first: removing the line reports `['.env.local']`.
**Why it generalises:** D15.4 said the danger of replacing the ignore filter
was the 301 MB it lets back in. It was also everything a *tool* would add to
`.gitignore` afterwards, expecting that to be enough.

### Stage 15 result
| | |
|---|---:|
| what it produces | `vercel.json`, `.vercelignore`, `api/index.py`, `api/requirements.txt` |
| code changed | 2 seams: `catalog.DSN`, `server.deployed_origins()` |
| new checks | 32 (`api/test_deploy.py`) |
| total checks | 510 |
| new dependencies | 0 |
| the suite needs a key, a network or a Vercel account | no |
| deployed | yes — `argo-rose.vercel.app`, boots, routes, 503s honestly |
| answers a question about ARGO | **no** — there is no database behind it yet |

---

## Stage 16 — the chat panel is the front door

A layout stage, and deliberately nothing more. The dashboard opened in the
catalogue because for two stages the catalogue was the only half that could
answer without a key. Stage 12 removed that reason and nobody moved the door.
What follows is what it cost to move it without letting the emphasis change
into a claim.

### D16.1 — Stage 16, and the dashboard lands in Chat
`CLAUDE.md` recorded Stage 16 as unclaimed and `DECISIONS.md` closed Stage 15;
both agree, checked before the number was used.
**Decided:** `App.jsx` opens in `mode = "chat"`. The chat panel gets a landing
screen — heading, composer and suggestions centred together — instead of a card
pinned above half a screen of blank page.
**Alternative:** leave the default and merely restyle the chat tab.
**Why not:** the tab a reader lands in *is* the argument the dashboard makes
about itself. Landing in a parameter form says the form is the product and the
question box is a demo; that was true at Stage 10 and stopped being true at
Stage 12, when the chat tab started answering with no key, no network and no
model. The reason for the old default expired three stages ago.

### D16.2 — The catalogue is demoted, not removed
**Decided:** the Catalogue tab keeps every control, every display and the same
audit trail; it moves to second position and is one click from the chat panel,
which links to it in words ("the same 11 queries, the same audit trail").
**Alternative:** fold the catalogue away, or drop it, since chat reaches the
same eleven queries.
**Why not:** the catalogue is the evidence that the eleven queries answer
without any model at all, and every rendered-display check in
`ui/test_render.py` runs through it. A dashboard that can only be driven by
asking questions cannot demonstrate that the questions are not what produce the
numbers. **The check that keeps this honest is the second half of D16.4:** the
suite opens the catalogue through the tab and asserts the form is complete, so
"one click away" is re-checked rather than remembered.

### D16.3 — The audit trail stays mounted beside the conversation
**Decided:** the trail keeps its rail in chat mode (narrowed 21rem → 20rem);
the conversation takes the rest and is capped at a reading width.
**Alternative:** fold it behind a toggle, which is what a chat-first layout
usually does with a side panel, and would have given the thread the full width.
**Why not:** D10.5. It is not debug output, it is the evidence that the number
on the chart came out of a query, and evidence you have to go looking for is
evidence nobody reads. Promoting chat while hiding the trail would have made
the dashboard look more like the thing it is arguing it is not.

### D16.4 — The landing view is asserted on the page, not on the default
A check for `useState("chat")` in the source is a check on how the default is
*spelled*: it passes when the page renders the catalogue anyway (a stale
`dist/`, a mode reset on a `/meta` reload) and fails when the same behaviour is
reached another way. That is exactly the failure D13.3's badge check had.
**Decided:** `ui/test_render.py` reads the rendered DOM — `__fc.landing()`
returns whether the chat panel is on screen *and* whether the catalogue's
`Run query` button is — and asserts both halves: chat loaded, form did not.
**Made to fail first,** as rule 5 requires: flipping the default back to
`"catalogue"` and rebuilding turns both into
`FAIL … {'chat': False, 'form': True}`, and the third check (the catalogue is
one click away) stays green, which is the discrimination the pair was written
for. Restored, all three pass.

### D16.5 — Nothing on the panel was reworded to suit the promotion
The temptation in a stage like this is to make the front door read better.
**Decided:** every sentence stays as it was. The landing screen still says
there are no biogeochemical parameters before anything is asked, still names
the units, still describes retrieval only on the path that uses it and the
lexical router only when the router is selected, and the reply badges are
untouched. The suggestion chips are still generated from the catalogue's own
examples, so no question on the landing screen contains a value the dashboard
invented (rule 2). The one sentence added is the link to the catalogue, and it
claims only what D16.2 makes true.
**Why it is worth a decision entry:** a promoted chat panel is precisely where
"lexical" would quietly become "semantic" (rule 9).

### D16.6 — The README's check total was two stages stale
`README.md` said 478 checks in two places and 428 in a third. 478 was Stage
14's total; Stage 15 added `api/test_deploy.py` and made it 510 in this log,
and the README was never brought forward.
**Decided:** count it rather than adjust it. The suites report
28 + 28 + 45 + 62 + 125 + 98 + 42 + 53 + 32 = **513**, which is the number this
log has always meant by "total checks" — `etl/load_db.py`'s 21 verification
checks are counted separately, as they always have been, because they need the
loaded database rather than only Postgres.
**Also corrected:** the Known Limitations bullet claiming "the dashboard's
rendering has no automated tests". It has had one since Stage 14. The honest
limitation is narrower and is now what the bullet says: structure and computed
style are read back, appearance is not.

### D16.7 — The preset questions are measured, not chosen by taste
The landing screen's chips were six questions written by hand at Stage 11 and
never re-examined. Three of the seven queries that *draw something* had no chip
at all, and nothing anywhere checked that a chip routed to the query it was
written for or that the query came back with rows — a chip that misroutes or
lands on an empty result is a trap laid for whoever clicks it first, and the
first click at a demo is not yours.
**Decided:** every drawing query gets a chip — two maps, a trajectory, two
lines, two bars — and every question was routed through `api/router.py` and run
against the database *before* it was written into `displays.js`. The measured
result, printed by the suite on every run:

| chip | query | rows |
|---|---|---:|
| Is the Bay of Bengal fresher than the Arabian Sea? | `compare_regions` | 2 |
| Plot temperature against depth in the Arabian Sea | `depth_profile` | 41 |
| Show me the trajectory of float 6903139 | `float_trajectory` | 200 |
| Show me salinity profiles in the Bay of Bengal in March 2023 | `profiles_in_region` | 6 |
| How many profiles per month in the Bay of Bengal? | `monthly_profile_counts` | 24 |
| What are the surface conditions by region? | `surface_conditions` | 4 |
| Which ARGO floats are nearest to 15°N, 68°E? | `nearest_profiles` | 77 |
| Why does float 2902203 have fewer profiles than the index promised? | `missing_profiles` | 13 |
| Show me the BGC oxygen profiles for the Arabian Sea | *refused* `no-bgc` | — |

**Alternative:** hand-pick them and click each one once. **Why not:** rule 5.
Clicking once proves it worked once; `ui/test_render.py` now reads the chips
off the *rendered* page and asks `/ask` each one on every run, printing the
misses rather than summarising them. **The values are still the catalogue's**
— every chip interpolates `query.example`, so none of these questions contains
a number this dashboard invented (rule 2).
**The refusal is not an exception to "good answers only":** the BGC chip is on
screen on purpose, and the check encodes exactly that — a refusal is a correct
answer for that one chip and a miss for every other.

### D16.8 — The model path is removed from the DASHBOARD, not from the project
The composer offered `model · gemini` beside `lexical router · no model`. On
this machine the key is the placeholder `YOUR_REAL_KEY`, so that button failed
on every click; the panel handled it well (D12.12's explicit re-ask), which
meant the dashboard's most prominent control was a well-handled failure.
**Decided:** the dashboard asks the lexical router and nothing else. Removed
from the UI: the engine selector, the retrieval checkbox, the three-state tab
badge, the "switched to the lexical router" notice, the retrieved-notes panel
and the "ask this again without a model" button. `provider: "lexical"` is now
sent **explicitly on every call**, not inferred from what the server has
configured.
**Kept, untouched:** `api/chat.py`, `api/gemini.py`, `/ask` with
`provider: "gemini" | "anthropic"`, and all 73 of their checks. The model path
is the API's and the command line's; it is not the dashboard's.
**Why the explicit provider matters more than the missing button:** a UI that
sent `provider: null` would follow whatever key the API happened to have, so
setting `GEMINI_API_KEY` on a deployment would silently change what answered
underneath a badge still reading `lexical router · no model`. That is the
sentence D12's badges exist to prevent, arriving from the server side.
**Alternative:** keep the selector and default it to lexical. **Why not:** rule
7 — a control that appears to do something must do it, and on the machine this
is demonstrated from, that one cannot. **What it costs, stated plainly:** with
a working key the dashboard no longer *shows* a model choosing a query. That
demonstration now happens on the command line, and putting the selector back is
one revert.

### D16.9 — Ten checks asserted the two-path UI; seven replaced them
`api/test_router.py` pinned the Stage 12 machinery — the selector moving after
a failure, the re-ask button, the three-valued badge and its two ways of lying
(D13.3). Those properties do not exist any more, and a check for a removed
mechanism is not evidence of anything.
**Decided:** replace, do not delete. The same question is asked of the new UI —
*can what is on screen disagree with what answered?* — as seven checks: the
provider is named in the request, there is exactly **one** `await ask(` call
site so no fallback can exist, no selector remains, the chat tab reads the
**router's** availability rather than `ai.available` (which is true when either
path can answer), and the badge is single-valued. `ui/test_ui.py`'s three badge
checks were rewritten the same way, and the render suite gained a DOM check
that **no control naming a model exists on the page**, made to fail by adding
one back.
**And a gap closed while it was open:** the render suite drove the chat panel
by clicking chips only (D14.8 said so). It now also **types a question into the
composer** and sends it — the second answer in the thread, drawn as a map, both
replies badged, both queries in the trail. The WMO it types is read from
`/meta`, not written into the file, for the same reason the chips interpolate
examples.

### D16.10 — A check that skipped what it could not parse
`api/test_retrieval.py` reads the suggestion list out of `displays.js` with a
regex that matched only template literals. One new suggestion, written with a
plain string because it interpolates nothing, was **silently unread**: eight of
nine entries were checked and the suite reported a clean pass.
**Decided:** accept both quotings, and — the part that matters — assert that
the number of entries parsed equals the number declared. **Made to fail:**
rewriting one `ask` to call a function instead of returning a literal reports
`8 parsed of 9 declared`.
**Why it belongs in this log:** it is rule 5's failure mode in its quietest
form. The check did not break; it looked away, and reported the pass it got
from what remained.

### Stage 16 result
| | |
|---|---:|
| what changed | which tab the page opens in, the chat layout, the preset questions, and the number of engines the dashboard offers |
| preset questions | 9, every one routed and run before it was written down (D16.7) |
| paths the dashboard offers | 1 — the lexical router, named in the request itself (D16.8) |
| server-side model support removed | none — `api/chat.py`, `api/gemini.py` and `/ask` are untouched |
| sentences reworded to flatter a path | 0 (D16.5) |
| checks added | 13, each made to fail first |
| checks replaced | 13 that pinned the two-path UI (D16.9) |
| total checks | 523 |
| new dependencies | 0 |
| needs a key or a network | no — the chat phase runs against a key that is set and rejected, and answers anyway |

---

## Stage 17 — the deployment finished, and the second path it created

Stage 15 deployed an API with no database behind it and left three things the
CLI had done that its plan had not predicted (D15.11). Stage 17 is the rest of
it: a hosted Postgres, the dashboard as its own project, and — the part worth a
stage number — the checks for the failure modes that only exist because there
are now **two deploy paths** and **two projects**.

Nothing on a platform has been touched in this stage either. What is new is
configuration, documentation and 18 checks, and the honest boundary is the same
as D15.10's: this repository can check itself and cannot check Vercel.

### D17.1 — Stage 17, and Vercel is kept rather than replaced
The brief proposed Vercel for the frontend and **Render or Railway** for the
backend. That is the right default for a Python API and it is not right here.
**Decided:** both projects on Vercel, Postgres on Supabase.
**Why:** the deciding argument is not cost — Render's free tier is also ₹0. It
is that Render's free instance **sleeps after 15 minutes and takes ~50 s to
wake**. A judge clicking a link and getting fifty seconds of blank page is the
failure this project is organised against, and it is the only platform
difference the *audience* can see. Against that, Stage 15's configuration is
already written, already checked by 32 assertions, and has already been seen to
boot on Linux (D15.11) — moving would discard all three to solve nothing.
**The alternative that would force the move:** an AI workload that does not fit
a serverless function. It does not exist here. The dashboard's answering engine
is `api/router.py` — arithmetic over 110 written exemplars, no model, no
network — and the vector index is a 536 KB file that ships in the bundle. There
is no GPU service in this architecture because there is nothing to put on one.
**Recorded as a fallback rather than dismissed:** `api/server.py` now reads
`PORT` and `HOST` (D17.6), so `uvicorn api.server:app --host 0.0.0.0 --port
$PORT` works unchanged the day the bundle stops fitting 250 MB.

### D17.2 — The index is committed, which is D15.11's trap closed in the direction the brief forces
D15.11 found that accepting the GitHub connection created a second deploy path
whose source is the **git tree**, while `.gitignore` excluded `data/` — so a
push deployed an API whose retrieval was off, reporting `"no index built"` in
`/meta` and answering anyway. Two paths, one commit, different artefacts, no
warning. It was left unfixed because both fixes were choices.
**The brief chooses.** "GitHub: source control and deployment trigger" makes
push-to-deploy a requirement, so disconnecting the integration is no longer
available and the index has to be in the tree.
**Decided:** `data/*` plus `!data/rag/`, and the two files committed.
**Why it is not a binary blob with no provenance:** the manifest records
`embedder.kind == "hashing"`, the keyless lexical embedder, which is
deterministic and needs no key — `etl/build_index.py` reproduces both files
from the database at any time.
**Written that way for a reason:** git does not descend into an excluded
*directory*, so `data/` with `!data/rag/` would not have worked. It looks
correct and would have silently kept the index out — the same shape of failure
as everything else in this stage.
**And it is checked as a set, not as a grep** (D15.4's argument, second
outing): `git ls-files --cached --others --exclude-standard -- data` must be
exactly the two files. Made to fail by restoring `data/`: both checks caught it.

### D17.3 — `db/roles.sql` had a published password and named a database that does not exist there
Two things in that file were correct for localhost and wrong for a hosted
database, and neither would have failed loudly:
`CREATE ROLE floatchat_ro LOGIN PASSWORD 'floatchat_ro'` is a **committed
credential** the moment the database accepts connections from the internet, and
`GRANT CONNECT ON DATABASE floatchat` names a database that is called
`postgres` on Supabase.
**Decided:** a `\set ro_password` default overridable with `psql -v
ro_password=...`, and `current_database()` instead of the literal name.
**Alternative:** a second file for hosted databases. **Why not:** two files
that can disagree, for one line each — the same argument D15.9 makes about two
requirements freezes, which needed a check to stay honest.
**The mechanical part worth writing down:** psql does **not** substitute
variables inside a dollar-quoted body, so the `DO $$ ... $$` block this file
used could not have taken the password at all. It is now a `format(...) \gexec`
that either CREATEs or ALTERs, which is also idempotent — and `run_pipeline.py`
tells you to apply it once while nothing stops you applying it twice.
**Proven, not assumed:** local `pg_hba` is `trust`, so connecting with the
right password proves nothing. The override was verified by re-deriving
SCRAM-SHA-256's StoredKey from the candidate password against `pg_authid`: with
`-v ro_password=...` in force the new password verifies and the old one does
not, and applying the file with no variable restores the default exactly.

### D17.4 — `.env.example`, and the check that stops it going stale
Seven environment variables were read across `api/` and `etl/` and documented
in three prose files, none of which is a list.
**Decided:** `.env.example` and `ui/.env.example`, names and no values, both
committed — which needed `!.env.example` after `.env*` in each `.gitignore`, or
the rule protecting the credentials would have hidden the file documenting
them.
**Why it needs a check at all:** an example file is a promise that goes stale
in one direction silently. A variable added to the code and not to the file is
a setting nobody knows to configure until a demo. So `api/test_deploy.py`
extracts every `os.environ` read under `api/`, `etl/` and `run_pipeline.py`,
every `import.meta.env.VITE_*` under `ui/src`, and compares **both ways** —
undocumented reads fail, and so do documented variables nothing reads.
**And one more, because the file is committed:** every value is matched against
the shapes of a real credential (`sk-`, `AIza`, a JWT, a 32-hex run). Pasting a
working key into the example is D15.12 happening again by hand, and it would
otherwise be caught by nobody.

### D17.5 — The pooled connection string is the one line that matters
D15.10 recorded that `catalog.connect()` opens a connection per query and that
this is a liability under serverless, where every invocation is its own
process. It is still true and the code is still unchanged.
**Decided:** `FLOATCHAT_DSN` names the **transaction pooler** (Supabase port
6543), and `DEPLOYMENT.md` says so in the one place a reader cannot skip.
**Alternative:** a connection pool in `catalog.py`. **Why not now:** it is a
real change to the file the whole safety argument lives in, for a property that
the pooler already provides from outside — and rule 6 exists so that the week
of a demo is not when that file changes.
**Not fixed, stated:** a pooler is not a connection pool. Under enough
concurrency this is still a connection per query; it is survivable rather than
solved.

### D17.6 — `PORT` and `HOST`, read rather than decorative
`api/server.py`'s `__main__` block hard-coded `127.0.0.1:8000`. Vercel imports
`app` and never reaches it, so on the chosen architecture the block is dead —
but on any container host it is the start command, and a process that ignored
an injected `PORT` would bind 8000, fail the platform's health check, and look
like an application error rather than a binding one. That is rule 7's failure
mode.
**Decided:** `os.environ.get("PORT", "8000")` and `HOST` defaulting to
`127.0.0.1` — the previous literals, so nothing local changes. A container must
set `HOST=0.0.0.0`, and the documented start command passes it explicitly
rather than relying on a default that would be wrong somewhere.

### D17.7 — The dashboard is a second project, so it has a second ignore filter
D15.12 found a `VERCEL_OIDC_TOKEN` inside the API's bundle because
`.vercelignore` had **replaced** `.gitignore` as the upload filter. `vercel
link` writes that file in **every** directory it is run in, and `ui/.env.local`
already exists on this machine with a token in it.
**The dashboard's filter is `ui/.gitignore`**, because the CLI deploys `ui/` as
its own root and the repository's `.gitignore` is outside it. That file listed
`node_modules/` and `dist/` and nothing else.
**Decided:** `.env*` / `!.env.example` in `ui/.gitignore`, and deliberately
**no `ui/.vercelignore`** — creating one would replace that file as the filter
and let `node_modules` straight back in, which is D15.4 arriving a second time
from the same direction.
**Checked by computing the dashboard's upload set** the way D15.4 computes the
API's: 0.25 MB across 24 files, no `.env`, no `node_modules`, no `dist`. The
suite's ignore parser now honours `!` negations rather than treating them as
plain names, because a parser that dropped the `!` would model the wrong
upload — silently, and in the safe-looking direction.
**Also decided, and it is a platform choice rather than a file:** both projects
are imported in the **web console**, not linked with the CLI. D15.11's
repo-level link means `cd ui && vercel link` resolves to the **API** project,
and a `vercel --prod` from there would replace the API with the dashboard at
the API's own URL.

### D17.8 — No Dockerfile
The brief allows one "if it is useful for this project".
**Decided:** none. **Why:** neither chosen platform builds one — Vercel's
Python builder installs `api/requirements.txt` itself and the dashboard is a
static Vite build — so a Dockerfile would be a fourth description of the
dependencies that nothing runs and nothing checks. That is exactly the drift
rule 6 pins versions to prevent, and D15.9 already needed a check to keep
*two* descriptions honest. The day the architecture moves to a container host
is the day to add one.

### D17.9 — What was NOT changed, and why that is the finding
The brief asked for hardcoded localhost URLs, ports, keys and dev-only
configuration to be fixed. The audit found **five** hardcoded localhosts and
every one of them is correct where it is:

- `ui/src/api.js` — the fallback when `VITE_API_BASE` is unset. Verified on the
  real production build: with the variable set the fallback is **constant-folded
  out of the bundle entirely**, and unset it produces the browser's own
  "cannot reach" message naming the address it tried, which is D10.x's
  `unreachable` state and not a silent one.
- `api/server.py` `DEV_ORIGINS` — always allowed, deliberately, so a deployed
  API stays reachable from a local dashboard (D15.3). Asserted by a check.
- `api/catalog.py`'s default DSN — the local development database, and the
  fallback that made the deployed API say `503 localhost:5432` honestly rather
  than show an empty one (D15.11).
- `etl/load_db.py` and `run_pipeline.py` — the pipeline. It builds the local
  database from 155 MB of downloads and is not something a platform runs. The
  hosted database is a **copy** of what it produces (`pg_dump` / `pg_restore`),
  not a second build of it.

**No API key, token or password is committed anywhere in the tree** — and after
this stage that is a check rather than a claim: no `.env` but the two examples
is visible to git, and no example value has the shape of a credential.
**The one credential that was committed was `db/roles.sql`'s**, and D17.3 is
that finding.

### D17.10 — D15.5's silent trap does not exist, and now something says so
D15.5 warned, in bold, that setting `GEMINI_API_KEY` on the deployment would
leave the query-time embedder mismatched against the shipped index: "retrieval
would still return documents; they would be worse, and nothing would say so."
That warning was repeated into `CLAUDE.md`-adjacent prose and would have been
repeated again into `DEPLOYMENT.md`.
**It is wrong.** `retrieval.load()` reconstructs the embedder from the index's
own manifest (`api/retrieval.py:183`) — which is exactly what the module's
docstring says it is for, and which predates D15.5 by a stage. A key in the
environment changes `embed.resolve()` and does not change what searches the
index.
**Found by running it, not by reading it.** With this machine's placeholder
`GEMINI_API_KEY` set, a live `/meta` reported `embedder: hashing:1024:fitted`
while `embed.resolve()` returned `gemini:gemini-embedding-001:768` — the two
disagreeing is the proof, and a search returned Bay-of-Bengal documents with no
network call.
**Decided:** correct the claim wherever it was repeated, and add the check that
holds the correction — a subprocess with a fabricated key, asserting that
`resolve()` and `load()` disagree and that `load()` matches the manifest. Made
to fail by rewriting `load()` to call `resolve()`: both checks caught it.
**Why it is worth a decision entry rather than a quiet edit:** rule 5 says a
claim is only true if something re-checks it, and this was a *limitation* that
nothing re-checked. A false limitation is cheaper than a false capability and
it is still false — it would have sent someone rebuilding an index to fix a
problem they did not have.

### Stage 17 result
| | |
|---|---:|
| what it produces | `DEPLOYMENT.md`, `.env.example`, `ui/.env.example`, `ui/vercel.json` |
| code changed | 2 lines in `api/server.py` (`HOST`/`PORT`), 3 in `db/roles.sql` |
| architecture changed | none — Stage 15's two seams and one entrypoint are untouched |
| functionality removed | none |
| new checks | 20 (`api/test_deploy.py`, 32 → 52), each made to fail first |
| total checks | 543 |
| a documented limitation withdrawn | 1 (D17.10) |
| new dependencies | 0 |
| the suite needs a key, a network or an account | no |
| deployed by this stage | **no** — every platform step is marked **you** in `DEPLOYMENT.md` |

---

## Where the project stands

Complete end to end, and answerable with or without a model: GDAC index ->
filter -> float selection -> NetCDF download -> parse -> regions -> Postgres ->
query catalogue -> vector index over the database's own summaries -> a model
that picks a query (Anthropic **and** Gemini behind one transport seam) **or a
lexical router that picks one with no model at all** -> HTTP API -> dashboard
with both front doors and one audit trail -- and, since Stage 17, a documented
deployment of all three tiers whose configuration is checked on every run.
**17 stages, 149 logged decisions, 543 automated checks, one command to rebuild.**
(Both counts were stale here too -- `grep -c '^## Stage ' DECISIONS.md` and
`grep -c '^### D[0-9]' DECISIONS.md` are where they now come from.)

The dashboard is demonstrable on a machine with no API key, no network and no
model download. The Catalogue tab always was. Since Stage 12 the Chat tab is
too: the lexical router chooses one of the same eleven queries, fills its
parameters from the question, and renders through the same `displays.js` spec —
badged `lexical router · no model` on every reply, in the composer before you
send, and in the audit trail beside the queries a model chose.

This table was two stages stale until Stage 16 (D16.6) and is now written from
what `run_pipeline.py --check` actually prints, suite by suite.  The 21
database checks live inside `etl/load_db.py` and are counted separately from
the 543, exactly as that command reports them.

| check suite | count |
|---|---:|
| query catalogue (`api/test_catalog.py`) | 28 |
| tool loop (`api/test_chat.py`) | 28 |
| Gemini adapter (`api/test_gemini.py`) | 45 |
| HTTP API (`api/test_server.py`) | 62 |
| retrieval, corpus, embedders and `/ask` (`api/test_retrieval.py`) | 132 |
| lexical router, no model (`api/test_router.py`) | 96 |
| dashboard source (`ui/test_ui.py`) | 44 |
| dashboard rendered in Chrome (`ui/test_render.py`) | 56 |
| deployment configuration (`api/test_deploy.py`) | 52 |
| **total** | **543** |
| database verification (`etl/load_db.py`, counted apart) | 21 |

**The measured numbers, in one place.** Everything below is produced by a suite
that runs with no network and no credentials.

| | |
|---|---:|
| retrieval recall@1 / @3 / @5 | 77.8% · 88.9% · 94.4% (MRR 0.835) |
| router false-accept rate | 0.0% |
| router refusal recall | 100.0% |
| router routing accuracy | 66.7% |
| model routing accuracy | **unmeasured** (D11.21) |

**Against the problem statement.** Closed: NetCDF ingest to PostgreSQL; a vector
store of metadata and summaries; a RAG pipeline feeding an LLM that maps natural
language to database queries; geospatial dashboards with trajectories, profiles
and tabular summaries; and a chat interface that guides discovery — which now
works with no credentials at all. Not built, and not claimed: **MCP**; **Parquet**
output; **ASCII/NetCDF export** of query results (the router refuses to fake it,
D12.5); a **depth-time Hovmöller plot**; a **multi-profile comparison** display.
**BGC is out of scope by the data**, and the dashboard puts that question on a
suggestion button so it is asked and answered rather than avoided.

**The three things still open, in order of how much they would cost:**

1. **Nothing has been run against a live model or a live embedding API.** The
   `GEMINI_API_KEY` here is the placeholder `YOUR_REAL_KEY`; there are no
   Anthropic credentials on this machine. Stage 8 observed real routing on
   `gemini-3.6-flash` when a working key existed, but that key no longer works,
   and every Stage 11 and Stage 12 number is the keyless path's. Stage 12 makes
   this **survivable**, not fixed. **A working key closes it in minutes** and is
   still the highest-value thing to do before a demo.

2. **Model routing is still unmeasured, though the router's is not.** D11.13 and
   D12.7 supply fixed question sets, three rates and printed misses for
   retrieval and for the lexical path. The equivalent for the *model* path — the
   same questions, expected query names, a pass rate — does not exist. The
   pattern is now built twice and would transfer directly.

3. **The dashboard is now rendered and read back on every run** — 33 checks
   (`ui/test_render.py`, Stage 14) driving headless Chrome against the
   *production build*, plus 42 source checks (`ui/test_ui.py`, Stage 13). Both
   of Stage 13's own fixes are confirmed on screen, and the ARGO rule that
   pressure increases downward is verified on the drawn axis for the first
   time. **What is still not proven is appearance**: it reads structure and
   computed style, so it can prove an axis title computes to `#dc2626` but not
   that two labels do not overlap, that a legend is legible, or that the layout
   survives a narrow window. One browser, one viewport. The chat panel is
   driven along one route — a rejected key, then the same question on the
   lexical router (D14.8) — but a typed question, the slot panel and the
   refusal path are still verified by grep only.
