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
