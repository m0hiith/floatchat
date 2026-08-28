-- A read-only role for the query layer.
--
-- The natural-language layer must be incapable of writing, not merely unwilling.
-- Even if the model were prompted into asking for a DELETE, the connection it
-- runs on cannot execute one -- the privilege is not granted.  This is the
-- defence that does not depend on a prompt.
--
-- Run once as the database owner:  psql -h localhost -d floatchat -f db/roles.sql

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'floatchat_ro') THEN
        CREATE ROLE floatchat_ro LOGIN PASSWORD 'floatchat_ro';
    END IF;
END $$;

REVOKE ALL ON SCHEMA public FROM floatchat_ro;
GRANT CONNECT ON DATABASE floatchat TO floatchat_ro;
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
