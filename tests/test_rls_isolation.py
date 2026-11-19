import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db.session import db_session
from app.services.audit import write_audit
from tests.helpers import sqlstate, unit_vec, vec_literal

TABLES = ["tenants", "users", "user_roles", "documents", "document_permissions", "chunks", "audit_log"]


def doc_ids(tenant_id):
    with db_session(tenant_id) as s:
        return {r[0] for r in s.execute(text("SELECT id FROM documents"))}


def test_each_tenant_sees_only_its_own_documents(world):
    assert doc_ids(world.a.id) == {world.da.id}
    assert doc_ids(world.b.id) == {world.db.id}


def test_each_tenant_sees_only_its_own_tenant_row(world):
    with db_session(world.a.id) as s:
        assert [r[0] for r in s.execute(text("SELECT slug FROM tenants"))] == [world.a.slug]


@pytest.mark.parametrize("table", TABLES)
def test_no_tenant_set_means_zero_rows_everywhere(world, table):
    with db_session(world.a.id) as s:  # make sure the tables are not simply empty
        write_audit(s, tenant_id=world.a.id, user_id=world.ua.id, action="test.seed")
    with db_session() as s:
        assert s.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0


def test_cannot_fetch_another_tenants_row_by_id(world):
    with db_session(world.a.id) as s:
        rows = s.execute(text("SELECT * FROM documents WHERE id = :i"), {"i": world.db.id}).all()
    assert rows == []


def test_cannot_update_or_delete_another_tenants_rows(world):
    with db_session(world.a.id) as s:
        assert s.execute(text("UPDATE documents SET title='hacked' WHERE id=:i"), {"i": world.db.id}).rowcount == 0
        assert s.execute(text("DELETE FROM documents WHERE id=:i"), {"i": world.db.id}).rowcount == 0
        assert s.execute(text("DELETE FROM chunks WHERE document_id=:i"), {"i": world.db.id}).rowcount == 0
    with db_session(world.b.id) as s:
        title = s.execute(text("SELECT title FROM documents WHERE id=:i"), {"i": world.db.id}).scalar()
        chunks = s.execute(text("SELECT count(*) FROM chunks WHERE document_id=:i"), {"i": world.db.id}).scalar()
    assert title == world.db.title and chunks == 1


def test_cannot_insert_a_row_into_another_tenant(world):
    with pytest.raises(DBAPIError) as exc:
        with db_session(world.a.id) as s:
            s.execute(
                text("INSERT INTO documents (tenant_id, title, created_by) VALUES (:t, 'x', :u)"),
                {"t": world.b.id, "u": world.ub.id},
            )
    assert sqlstate(exc) == "42501"


def test_cannot_move_a_row_into_another_tenant(world):
    with pytest.raises(DBAPIError) as exc:
        with db_session(world.a.id) as s:
            s.execute(
                text("UPDATE documents SET tenant_id = :b WHERE id = :i"),
                {"b": world.b.id, "i": world.da.id},
            )
    assert sqlstate(exc) == "42501"


def test_vector_search_is_scoped_to_the_tenant(world):
    with db_session(world.a.id) as s:
        rows = s.execute(
            text("SELECT document_id FROM chunks ORDER BY embedding <=> CAST(:v AS vector) LIMIT 10"),
            {"v": vec_literal(unit_vec(0))},
        ).all()
    assert {r[0] for r in rows} == {world.da.id}


def test_tenant_setting_does_not_leak_between_pooled_transactions(world):
    for _ in range(25):
        with db_session(world.a.id) as s:
            assert s.execute(text("SELECT count(*) FROM documents")).scalar() == 1
        with db_session() as s:
            assert s.execute(text("SELECT count(*) FROM documents")).scalar() == 0
            assert s.execute(text("SELECT coalesce(current_setting('app.current_tenant', true), '')")).scalar() == ""


def test_garbage_tenant_setting_errors_instead_of_leaking():
    with pytest.raises(DBAPIError) as exc:
        with db_session() as s:
            s.execute(text("SELECT set_config('app.current_tenant', 'not-a-uuid', true)"))
            s.execute(text("SELECT count(*) FROM documents"))
    assert sqlstate(exc) == "22P02"


# ---- composite foreign keys: even a buggy query cannot link data across tenants
def _as_tenant(conn, tenant_id):
    conn.execute("SELECT set_config('app.current_tenant', %s, true)", (str(tenant_id),))


def test_chunk_cannot_point_at_another_tenants_document(owner_conn, world):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with owner_conn.transaction():
            _as_tenant(owner_conn, world.a.id)
            owner_conn.execute(
                "INSERT INTO chunks (tenant_id, document_id, chunk_index, content, embedding) "
                "VALUES (%s, %s, 99, 'x', %s::vector)",
                (world.a.id, world.db.id, vec_literal(unit_vec(0))),
            )


def test_document_cannot_be_created_by_another_tenants_user(owner_conn, world):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with owner_conn.transaction():
            _as_tenant(owner_conn, world.a.id)
            owner_conn.execute(
                "INSERT INTO documents (tenant_id, title, created_by) VALUES (%s, 'x', %s)",
                (world.a.id, world.ub.id),
            )


def test_permission_cannot_be_granted_to_another_tenants_user(owner_conn, world):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with owner_conn.transaction():
            _as_tenant(owner_conn, world.a.id)
            owner_conn.execute(
                "INSERT INTO document_permissions (tenant_id, document_id, user_id, permission, granted_by) "
                "VALUES (%s, %s, %s, 'read', %s)",
                (world.a.id, world.da.id, world.ub.id, world.ua.id),
            )


# ---- the one door that works without a tenant: login lookup
def lookup(slug, email):
    with db_session() as s:
        return s.execute(
            text("SELECT user_id, tenant_id, roles FROM app_login_lookup(:s, :e)"), {"s": slug, "e": email}
        ).all()


def test_login_lookup_finds_user_and_is_case_insensitive(make_tenant, make_user):
    t = make_tenant()
    u = make_user(t, roles=["editor", "viewer"])
    rows = lookup(t.slug, u.email.upper())
    assert len(rows) == 1 and rows[0][0] == u.id and rows[0][1] == t.id
    assert sorted(rows[0][2]) == ["editor", "viewer"]


def test_login_lookup_is_scoped_to_the_tenant_slug(make_tenant, make_user):
    t1, t2 = make_tenant(), make_tenant()
    u = make_user(t1)
    assert lookup(t2.slug, u.email) == []
    assert lookup("no-such-tenant", u.email) == []


def test_login_lookup_hides_inactive_users(make_tenant, make_user):
    t = make_tenant()
    u = make_user(t, active=False)
    assert lookup(t.slug, u.email) == []