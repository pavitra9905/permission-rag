"""Proves the DB is set up safely: limited app user, pgvector present."""
import sys

import psycopg

from app.core.config import settings

failed = False


def check(ok: bool, label: str, detail: str = "") -> None:
    global failed
    failed |= not ok
    print(f"{'✔' if ok else '✘'} {label:<42} {detail}")


def dsn(url: str) -> str:
    return url.replace("+psycopg", "")


# --- as the limited app user ---
with psycopg.connect(dsn(settings.app_database_url), autocommit=True) as conn:
    user = conn.execute("SELECT current_user").fetchone()[0]
    check(user == "rag_app", "Connected as rag_app", user)

    su, bypass = conn.execute(
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    check(not su, "rag_app is NOT superuser")
    check(not bypass, "rag_app can NOT bypass RLS")

    ext = conn.execute(
        "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
    ).fetchone()
    check(ext is not None, "pgvector extension installed", ext[0] if ext else "")

    try:
        conn.execute("CREATE TABLE should_fail (id int)")
        check(False, "rag_app cannot CREATE TABLE", "it could! fix grants")
    except psycopg.errors.InsufficientPrivilege:
        check(True, "rag_app cannot CREATE TABLE")

# --- as the owner ---
with psycopg.connect(dsn(settings.migration_database_url), autocommit=True) as conn:
    owner = conn.execute(
        "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'public'"
    ).fetchone()[0]
    check(owner == "rag_owner", "rag_owner owns schema public", owner)

print("\nDB foundation OK." if not failed else "\nFix the ✘ items above.")
sys.exit(1 if failed else 0)