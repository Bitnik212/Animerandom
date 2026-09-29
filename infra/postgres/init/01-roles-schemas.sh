#!/usr/bin/env bash
# Runs once on an empty data volume (docker-entrypoint-initdb.d).
# Databases, service roles, schemas, grants, extensions. Tables are migrated by
# the owning service, never here. See infra/README.md#postgres.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<SQL
CREATE DATABASE keycloak;
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname anime \
  -v ingest_password="$INGEST_DB_PASSWORD" \
  -v api_password="$API_DB_PASSWORD" \
  -v rec_password="$REC_DB_PASSWORD" <<'SQL'
CREATE ROLE ingest_svc LOGIN PASSWORD :'ingest_password';
CREATE ROLE api_svc    LOGIN PASSWORD :'api_password';
CREATE ROLE rec_svc    LOGIN PASSWORD :'rec_password';

CREATE EXTENSION IF NOT EXISTS vector;     -- in public, usable by every role
CREATE EXTENSION IF NOT EXISTS pg_trgm;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

CREATE SCHEMA catalog AUTHORIZATION ingest_svc;
CREATE SCHEMA ingest  AUTHORIZATION ingest_svc;
CREATE SCHEMA app     AUTHORIZATION api_svc;
CREATE SCHEMA rec     AUTHORIZATION rec_svc;

-- catalog is read by api and rec-engine; REFERENCES allows their foreign keys
GRANT USAGE ON SCHEMA catalog TO api_svc, rec_svc;
ALTER DEFAULT PRIVILEGES FOR ROLE ingest_svc IN SCHEMA catalog
  GRANT SELECT, REFERENCES ON TABLES TO api_svc, rec_svc;

-- rec-engine reads user signals from app
GRANT USAGE ON SCHEMA app TO rec_svc;
ALTER DEFAULT PRIVILEGES FOR ROLE api_svc IN SCHEMA app
  GRANT SELECT ON TABLES TO rec_svc;

ALTER ROLE ingest_svc SET search_path = ingest, public;
ALTER ROLE api_svc    SET search_path = app, public;
ALTER ROLE rec_svc    SET search_path = rec, public;
SQL
