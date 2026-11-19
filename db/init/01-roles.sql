-- Runs ONCE, on first start of an empty volume, as the superuser.
-- Passwords come from container env vars (never hardcoded here).
\getenv owner_pw RAG_OWNER_PASSWORD
\getenv app_pw RAG_APP_PASSWORD

-- Schema owner: runs Alembic migrations, owns all tables.
CREATE ROLE rag_owner LOGIN PASSWORD :'owner_pw'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;

-- Runtime role: what FastAPI uses. Cannot create objects, cannot bypass RLS.
CREATE ROLE rag_app LOGIN PASSWORD :'app_pw'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;

SELECT current_database() AS dbname \gset
GRANT CONNECT ON DATABASE :"dbname" TO rag_owner, rag_app;

-- pgvector needs superuser to install; do it here once.
CREATE EXTENSION IF NOT EXISTS vector;

-- Owner can create tables in public; app can only look inside it.
-- Table-level grants to rag_app live in Alembic migrations, not here.
ALTER SCHEMA public OWNER TO rag_owner;
GRANT USAGE ON SCHEMA public TO rag_app;