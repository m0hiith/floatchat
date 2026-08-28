-- FloatChat schema.  Stage 4.
--
-- Rebuilt from scratch on every load: the CSVs in data/parsed are the source
-- of truth, so a re-run must produce a byte-identical database rather than
-- append to whatever was there.  That is why this file drops first.
--
-- No PostGIS.  Positions are plain lat/lon doubles plus a native `point`.
-- Postgres core can already answer "which profiles are inside this polygon"
-- with `polygon @> point` and a GiST index, which is the whole of what the
-- named-region query needs (D4.4).  PostGIS earns its install only if we need
-- geodesic distance or reprojection, and we do not.

DROP TABLE IF EXISTS profile_regions CASCADE;
DROP TABLE IF EXISTS regions CASCADE;
DROP TABLE IF EXISTS levels CASCADE;
DROP TABLE IF EXISTS dropped_profiles CASCADE;
DROP TABLE IF EXISTS profiles CASCADE;
DROP TABLE IF EXISTS floats CASCADE;
DROP TABLE IF EXISTS ingest_run CASCADE;

-- One row per float in the demo set, carrying the reason it was chosen so the
-- selection argument lives in the database and not only in a markdown file.
CREATE TABLE floats (
    wmo               text PRIMARY KEY,
    dac               text NOT NULL,
    profiler_type     integer,
    institution       text,
    selection_reason  text NOT NULL,
    n_profiles_index  integer NOT NULL,   -- what the Stage 1 index promised
    n_delayed_index   integer NOT NULL,
    n_realtime_index  integer NOT NULL,
    dm_status         text NOT NULL,
    first_seen        timestamptz,
    last_seen         timestamptz,
    lat_mean          double precision,
    lon_mean          double precision,
    approx_area       text                -- ADVISORY ONLY (D1.7): not a region
);

CREATE TABLE profiles (
    profile_id        text PRIMARY KEY,   -- <wmo>_<cycle:04d><direction>
    wmo               text NOT NULL REFERENCES floats(wmo),
    cycle             integer NOT NULL,
    direction         char(1) NOT NULL CHECK (direction IN ('A', 'D')),
    -- R real-time, A real-time adjusted, D delayed-mode.  Three states, not
    -- two -- the index filename could only ever say two of them (D2.4).
    data_mode         char(1) NOT NULL CHECK (data_mode IN ('R', 'A', 'D')),
    juld              timestamptz NOT NULL,
    juld_qc           char(1),
    lat               double precision NOT NULL CHECK (lat BETWEEN -90 AND 90),
    lon               double precision NOT NULL CHECK (lon BETWEEN -180 AND 180),
    position_qc       char(1),
    geom              point NOT NULL,     -- (lon, lat); x is longitude
    in_study_box      boolean NOT NULL,   -- D3.4: false for the two drifters
    n_levels          integer NOT NULL CHECK (n_levels > 0),
    pres_max          double precision,
    -- Which copy of the value we took: adjusted / raw / raw_fallback / empty.
    -- raw_fallback means DATA_MODE claimed an adjusted copy that was empty.
    pres_source       text NOT NULL,
    temp_source       text NOT NULL,
    psal_source       text NOT NULL,
    profile_pres_qc   char(1),
    profile_temp_qc   char(1),
    profile_psal_qc   char(1),
    UNIQUE (wmo, cycle, direction)
);

CREATE TABLE levels (
    profile_id        text NOT NULL REFERENCES profiles(profile_id) ON DELETE CASCADE,
    level_index       integer NOT NULL,
    pres              double precision NOT NULL,
    pres_qc           char(1),
    temp              double precision,
    temp_qc           char(1),
    psal              double precision,
    psal_qc           char(1),
    PRIMARY KEY (profile_id, level_index),
    -- A level with neither measurement should never have been written.
    CHECK (temp IS NOT NULL OR psal IS NOT NULL)
);

-- Project rule: silent dropping is forbidden.  The profiles we refused are in
-- the database too, so "why does float 2902203 have 61 profiles and not 74?"
-- is a query, not an argument.
CREATE TABLE dropped_profiles (
    profile_id        text PRIMARY KEY,
    wmo               text NOT NULL,
    reason            text NOT NULL,
    detail            text,
    was_indexed       boolean NOT NULL
);

-- One row.  Where this database came from, in its own words.
CREATE TABLE ingest_run (
    loaded_at         timestamptz NOT NULL DEFAULT now(),
    gdac_index_date   text,
    window_start      timestamptz NOT NULL,
    window_end        timestamptz NOT NULL,
    good_qc_flags     text NOT NULL,
    index_rows_total  bigint NOT NULL,
    index_rows_kept   bigint NOT NULL,
    profiles_in_files integer NOT NULL,
    profiles_written  integer NOT NULL,
    levels_written    integer NOT NULL
);

-- The named regions D1.7 promised: real IHO boundaries, each traceable to an
-- MRGID, not the advisory longitude cut used to pick candidate floats.  Island
-- holes are dropped and the outline is simplified to fit a core `polygon`;
-- both costs are recorded per row and were verified to change no profile's
-- region (D5.2).
CREATE TABLE regions (
    name              text PRIMARY KEY,
    mrgid             integer NOT NULL UNIQUE,
    source            text NOT NULL,
    vertices_source   integer NOT NULL,
    holes_dropped     integer NOT NULL,
    vertices_stored   integer NOT NULL,
    tolerance_deg     double precision NOT NULL,
    min_lon           double precision, min_lat double precision,
    max_lon           double precision, max_lat double precision,
    poly              polygon NOT NULL
);

-- Which profiles fall in which region.  Composite key, not a column on
-- profiles: IHO areas are supposed to be disjoint, and if that ever stops
-- being true we want to see the duplicate rather than lose one silently.
CREATE TABLE profile_regions (
    profile_id        text NOT NULL REFERENCES profiles(profile_id) ON DELETE CASCADE,
    region            text NOT NULL REFERENCES regions(name),
    PRIMARY KEY (profile_id, region)
);

CREATE INDEX profiles_wmo_juld_idx  ON profiles (wmo, juld);
CREATE INDEX profiles_juld_idx      ON profiles (juld);
CREATE INDEX profiles_data_mode_idx ON profiles (data_mode);
-- The region query: polygon @> point, answered by core Postgres.
CREATE INDEX profiles_geom_idx      ON profiles USING gist (geom);
CREATE INDEX levels_pres_idx        ON levels (pres);
CREATE INDEX profile_regions_region_idx ON profile_regions (region);
