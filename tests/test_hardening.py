import dataclasses
import time

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.ratelimit import FailureLimiter
from app.db.session import db_session
from app.main import app
from tests.helpers import TEST_PASSWORD


def login(client, tenant, email, password):
    return client.post("/auth/login", json={"tenant": tenant.slug, "email": email, "password": password})


# ---------------------------------------------------------------- failed-login audit
def test_failed_login_for_a_real_account_is_audited_without_secrets(client, make_tenant, make_user):
    t = make_tenant()
    u = make_user(t)
    assert login(client, t, u.email, "super-secret-wrong-guess").status_code == 401
    with db_session(t.id) as s:
        rows = s.execute(
            text("SELECT user_id, details::text FROM audit_log WHERE action = 'auth.login_failed'")
        ).all()
    assert len(rows) == 1 and rows[0][0] == u.id
    assert "bad_password" in rows[0][1] and "super-secret-wrong-guess" not in rows[0][1]


def test_failed_login_audit_is_tenant_isolated(client, make_tenant, make_user):
    a, b = make_tenant(), make_tenant()
    ua = make_user(a)
    login(client, a, ua.email, "bad")
    with db_session(b.id) as s:
        n = s.execute(text("SELECT count(*) FROM audit_log WHERE action = 'auth.login_failed'")).scalar()
    assert n == 0


# ---------------------------------------------------------------- rate limiting
def test_account_is_locked_after_repeated_failures(client, make_tenant, make_user):
    t = make_tenant()
    u = make_user(t)
    for _ in range(5):
        assert login(client, t, u.email, "bad").status_code == 401
    r = login(client, t, u.email, "bad")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    assert login(client, t, u.email, TEST_PASSWORD).status_code == 429  # locked, even for the right password


def test_lockout_is_per_ip_so_an_attacker_cannot_lock_out_the_real_user(client, make_tenant, make_user):
    t = make_tenant()
    u = make_user(t)
    for _ in range(5):
        login(client, t, u.email, "bad")
    assert login(client, t, u.email, "bad").status_code == 429
    with TestClient(app, client=("203.0.113.9", 4000)) as elsewhere:
        assert login(elsewhere, t, u.email, TEST_PASSWORD).status_code == 200


def test_successful_login_resets_the_counter(client, make_tenant, make_user):
    t = make_tenant()
    u = make_user(t)
    for _ in range(4):
        assert login(client, t, u.email, "bad").status_code == 401
    assert login(client, t, u.email, TEST_PASSWORD).status_code == 200
    for _ in range(4):
        assert login(client, t, u.email, "bad").status_code == 401  # would be 429 without the reset


def test_unknown_accounts_are_limited_exactly_like_real_ones(client, make_tenant):
    t = make_tenant()
    for _ in range(5):
        assert login(client, t, "ghost@example.test", "bad").status_code == 401
    assert login(client, t, "ghost@example.test", "bad").status_code == 429


def test_per_ip_limit_stops_credential_stuffing_across_accounts(client, make_tenant):
    t = make_tenant()
    for i in range(20):
        assert login(client, t, f"ghost{i}@example.test", "bad").status_code == 401
    assert login(client, t, "another-ghost@example.test", "bad").status_code == 429


def test_limiter_window_expires():
    limiter = FailureLimiter(max_failures=2, window_seconds=0.2)
    limiter.record_failure("k")
    limiter.record_failure("k")
    assert limiter.retry_after("k") > 0
    time.sleep(0.3)
    assert limiter.retry_after("k") == 0


def test_limiter_memory_is_bounded():
    limiter = FailureLimiter(max_failures=1, window_seconds=60, max_keys=3)
    for i in range(50):
        limiter.record_failure(f"key-{i}")
    assert len(limiter._hits) <= 3


# ---------------------------------------------------------------- roles come from the database
def test_demoted_admin_loses_access_immediately(client, auth, owner_conn, make_tenant, make_user):
    t = make_tenant()
    u = make_user(t, "admin")
    headers = auth(u)  # the token still says "admin"
    assert client.get("/admin/ping", headers=headers).status_code == 200
    owner_conn.execute("DELETE FROM user_roles WHERE user_id = %s", (u.id,))
    owner_conn.execute(
        "INSERT INTO user_roles (tenant_id, user_id, role) VALUES (%s, %s, 'viewer')", (t.id, u.id)
    )
    assert client.get("/admin/ping", headers=headers).status_code == 403


def test_promoted_user_gains_access_without_logging_in_again(client, auth, make_tenant, make_user):
    t = make_tenant()
    admin = make_user(t, "admin")
    stale_token_user = dataclasses.replace(admin, roles=("viewer",))  # token claims only "viewer"
    assert client.get("/admin/ping", headers=auth(stale_token_user)).status_code == 200


def test_token_claiming_admin_is_not_enough(client, auth, make_tenant, make_user):
    t = make_tenant()
    viewer = make_user(t, "viewer")
    lying = dataclasses.replace(viewer, roles=("admin",))  # validly signed, but the claim is false
    assert client.get("/admin/ping", headers=auth(lying)).status_code == 403
    assert client.get("/me", headers=auth(lying)).json()["roles"] == ["viewer"]