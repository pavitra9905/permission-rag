import uuid

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db.session import db_session
from app.services.audit import write_audit
from tests.helpers import sqlstate


def append(tenant, user, action=None):
    action = action or f"test.{uuid.uuid4().hex[:10]}"
    with db_session(tenant.id) as s:
        write_audit(s, tenant_id=tenant.id, user_id=user.id, action=action)
    return action


def actions(tenant):
    with db_session(tenant.id) as s:
        return {r[0] for r in s.execute(text("SELECT action FROM audit_log"))}


def test_runtime_role_can_append_and_read_its_own_entries(world):
    action = append(world.a, world.ua)
    assert action in actions(world.a)


def test_audit_entries_are_tenant_isolated(world):
    a_action, b_action = append(world.a, world.ua), append(world.b, world.ub)
    assert a_action in actions(world.a) and b_action not in actions(world.a)
    assert b_action in actions(world.b) and a_action not in actions(world.b)


def test_cannot_write_an_audit_entry_for_another_tenant(world):
    with pytest.raises(DBAPIError) as exc:
        with db_session(world.a.id) as s:
            write_audit(s, tenant_id=world.b.id, user_id=world.ub.id, action="forged")
    assert sqlstate(exc) == "42501"


@pytest.mark.parametrize(
    "statement",
    ["UPDATE audit_log SET action = 'tampered'", "DELETE FROM audit_log", "TRUNCATE audit_log"],
)
def test_runtime_role_cannot_rewrite_history(world, statement):
    action = append(world.a, world.ua)
    with pytest.raises(DBAPIError) as exc:
        with db_session(world.a.id) as s:
            s.execute(text(statement))
    assert sqlstate(exc) == "42501"
    assert action in actions(world.a)


def test_runtime_role_cannot_switch_off_the_protection(world):
    with pytest.raises(DBAPIError) as exc:
        with db_session(world.a.id) as s:
            s.execute(text("ALTER TABLE audit_log DISABLE TRIGGER ALL"))
    assert sqlstate(exc) == "42501"


@pytest.mark.parametrize(
    "statement",
    ["UPDATE audit_log SET action = 'tampered'", "DELETE FROM audit_log", "TRUNCATE audit_log"],
)
def test_even_the_owner_cannot_rewrite_history(owner_conn, world, statement):
    """RLS (no UPDATE/DELETE policy) and the trigger both stand in the owner's way."""
    action = append(world.a, world.ua)
    try:
        with owner_conn.transaction():
            owner_conn.execute("SELECT set_config('app.current_tenant', %s, true)", (str(world.a.id),))
            owner_conn.execute(statement)
    except psycopg.Error:
        pass  # raising is fine; silently doing nothing is fine; changing data is not
    assert action in actions(world.a)