import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db.session import db_session
from tests.helpers import sqlstate

TENANT_TABLES = {"tenants", "users", "user_roles", "documents", "document_permissions", "chunks", "audit_log"}
FORCED_TABLES = {"documents", "document_permissions", "chunks", "audit_log"}


def test_runtime_role_is_unprivileged():
    with db_session() as s:
        assert s.execute(text("SELECT current_user")).scalar() == "rag_app"
        flags = s.execute(
            text("SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = current_user")
        ).one()
    assert tuple(flags) == (False, False, False, False)


def test_runtime_role_cannot_create_objects():
    with pytest.raises(DBAPIError) as exc:
        with db_session() as s:
            s.execute(text("CREATE TABLE evil (id int)"))
    assert sqlstate(exc) == "42501"


def test_runtime_role_cannot_read_migration_bookkeeping():
    with pytest.raises(DBAPIError) as exc:
        with db_session() as s:
            s.execute(text("SELECT * FROM alembic_version"))
    assert sqlstate(exc) == "42501"


def test_every_tenant_table_has_rls_enabled(owner_conn):
    """Future-proof: any new table with a tenant_id column must have RLS, or this fails."""
    rows = owner_conn.execute(
        """
        SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
          AND (c.relname = 'tenants' OR EXISTS (
                SELECT 1 FROM information_schema.columns col
                WHERE col.table_schema = 'public' AND col.table_name = c.relname
                  AND col.column_name = 'tenant_id'))
        """
    ).fetchall()
    assert TENANT_TABLES <= {r[0] for r in rows}
    assert [r[0] for r in rows if not r[1]] == [], "table(s) without row level security"
    assert FORCED_TABLES <= {r[0] for r in rows if r[2]}, "RLS must be FORCED on these tables"


@pytest.mark.parametrize("privilege", ["UPDATE", "DELETE", "TRUNCATE"])
def test_runtime_role_has_no_destructive_rights_on_audit_log(owner_conn, privilege):
    row = owner_conn.execute("SELECT has_table_privilege('rag_app', 'audit_log', %s)", (privilege,)).fetchone()
    assert row[0] is False


def test_runtime_role_can_append_and_read_audit_log(owner_conn):
    for privilege in ("INSERT", "SELECT"):
        row = owner_conn.execute("SELECT has_table_privilege('rag_app', 'audit_log', %s)", (privilege,)).fetchone()
        assert row[0] is True