-- A read-only role for the query layer.
--
-- The natural-language layer must be incapable of writing, not merely unwilling.
-- Even if the model were prompted into asking for a DELETE, the connection it
-- runs on cannot execute one -- the privilege is not granted.  This is the
-- defence that does not depend on a prompt.
--
-- Locally, once, as the database owner:
--     psql -h localhost -d floatchat -f db/roles.sql
--
-- Against a hosted database, with a password that is not in this file:
--     psql "$OWNER_DSN" -v ro_password="$(openssl rand -hex 24)" -f db/roles.sql
--
-- Two things about that second form are deliberate (D17.3):
--
--   * The password is a psql variable with a local default, not a literal.
--     `floatchat_ro/floatchat_ro` is fine for a database that only listens on
--     localhost and is a published credential for one that does not.
--   * Nothing here names a database.  Locally it is `floatchat`; on Supabase
--     it is `postgres`.  `current_database()` is correct in both, and a
--     hard-coded name would fail on the hosted one with an error about a
--     database that is not the one you are connected to.
--
-- `\gexec` is used because psql does NOT substitute variables inside a
-- dollar-quoted body, so the DO $$ ... $$ block this file used to open could
-- not have taken the password.

\if :{?ro_password}
\else
  \set ro_password 'floatchat_ro'
\endif

-- Create it, or reset the password of the one that exists.  Idempotent either
-- way, which matters because run_pipeline.py tells you to apply this once and
-- nothing stops you applying it twice.
SELECT format(
    CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'floatchat_ro')
         THEN 'ALTER ROLE floatchat_ro WITH LOGIN PASSWORD %L'
         ELSE 'CREATE ROLE floatchat_ro LOGIN PASSWORD %L'
    END, :'ro_password') \gexec

REVOKE ALL ON SCHEMA public FROM floatchat_ro;
SELECT format('GRANT CONNECT ON DATABASE %I TO floatchat_ro', current_database()) \gexec
GRANT USAGE ON SCHEMA public TO floatchat_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO floatchat_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO floatchat_ro;

-- Belt and braces: no writes even if a GRANT is added by accident later.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public FROM floatchat_ro;

-- On PostgreSQL 14 the `public` schema grants CREATE to PUBLIC by default, so
-- revoking it from floatchat_ro alone does nothing -- the role still inherits
-- it and can create its own tables.  The Stage 6 test caught exactly that.
-- PostgreSQL 15 made this revoke the default; we do it explicitly so the
-- hardening does not depend on the server version.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM floatchat_ro;
