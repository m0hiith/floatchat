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

## Where the project stands

Complete end to end: GDAC index -> filter -> float selection -> NetCDF download
-> parse -> regions -> Postgres -> query catalogue -> natural-language layer
(Anthropic **and** Gemini behind one transport seam) -> vector index over the
database's own summaries -> HTTP API -> dashboard with both front doors.
**11 stages, 87 logged decisions, 309 automated checks, one command to rebuild.**

The platform is demonstrable with the model switched off and switched on. With
it off: eleven queries, every one with a defined visual output, reachable from
dropdowns built out of the database. With it on: the same eleven queries,
chosen by a model that has been handed retrieved summaries to orient it, with
the notes, the bound parameters and the rows all on screen — and the two paths
sharing one audit trail.

| check suite | count |
|---|---:|
| database verification (`etl/load_db.py`) | 21 |
| query catalogue (`api/test_catalog.py`) | 28 |
| tool loop (`api/test_chat.py`) | 28 |
| Gemini adapter (`api/test_gemini.py`) | 45 |
| HTTP API (`api/test_server.py`) | 62 |
| retrieval, corpus, embedders and `/ask` (`api/test_retrieval.py`) | 125 |
| **total** | **309** |

**Against the problem statement.** Closed: NetCDF ingest to PostgreSQL; a
vector store of metadata and summaries; a RAG pipeline feeding an LLM that maps
natural language to database queries; geospatial dashboards with trajectories,
profiles and tabular summaries; and a chat interface that guides discovery. Not
built, and not claimed: **MCP** (the catalogue is already a tool list, so the
adapter is small, but it does not exist); **Parquet** export; **ASCII/NetCDF
export** of query results; a **depth-time (Hovmöller) plot** and a
**multi-profile comparison** display. **BGC is out of scope by the data**, not
by omission — these ten floats carry no biogeochemical parameters, and the
dashboard puts that question on a suggestion button so it is asked and answered
rather than avoided.

**The three things still open, in order of how much they would cost:**

1. **Nothing here has been run against a live model or a live embedding API.**
   The `GEMINI_API_KEY` in this environment is the placeholder string
   `YOUR_REAL_KEY` and the API rejects it; there are no Anthropic credentials on
   this machine at all. Stage 8 *did* observe real routing on
   `gemini-3.6-flash` when a working key was present — eight questions, eight
   correct query choices — but that key no longer works, and every Stage 11
   number in this log is the keyless lexical embedder's. Both the Gemini
   embedder and `/ask` are tested against recording fakes, which proves the
   translation and not the transaction. **A working key closes this in
   minutes**, and it is the single highest-value thing to do before a demo.

2. **Routing is still unmeasured, even though retrieval now is not.** D11.13
   supplies a fixed question set with expected documents and a recall figure;
   the equivalent for routing — a fixed question set with expected *query
   names* and a pass rate — still does not exist. The pieces are now in place
   to build it cheaply, since `EVALUATION` is the pattern and
   `ScriptedTransport` is not what it needs.

3. **The dashboard has no automated tests of its own.** Unchanged from Stage
   10, and now larger: the chat panel, the retrieved-notes disclosure and the
   two-tab chrome are all unverified by anything that runs on every build. The
   Stage 11 drift checks cover the *couplings* — every query has a display,
   every suggestion names a real query and interpolates a real example key —
   but nothing re-checks that depth still plots downward or that an answer's
   chart renders. A Playwright suite over D10.8's states remains the honest fix.
