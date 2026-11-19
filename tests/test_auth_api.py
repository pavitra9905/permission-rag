from datetime import datetime, timedelta, timezone

import jwt
import pytest
from sqlalchemy import text

from app.core.config import settings
from app.db.session import db_session
from tests.helpers import TEST_PASSWORD


def login(client, tenant, user, password=TEST_PASSWORD, email=None):
    return client.post(
        "/auth/login", json={"tenant": tenant.slug, "email": email or user.email, "password": password}
    )


def test_login_returns_a_token_with_the_right_claims(client, make_tenant, make_user):
    t = make_tenant()
    u = make_user(t, "editor")
    r = login(client, t, u)
    assert r.status_code == 200 and r.json()["token_type"] == "bearer"
    claims = jwt.decode(r.json()["access_token"], options={"verify_signature": False})
    assert claims["sub"] == str(u.id) and claims["tenant_id"] == str(t.id) and claims["roles"] == ["editor"]


def test_login_is_case_insensitive_on_email(client, make_tenant, make_user):
    t = make_tenant()
    u = make_user(t)
    assert login(client, t, u, email=u.email.upper()).status_code == 200


def test_wrong_password_and_unknown_user_are_indistinguishable(client, make_tenant, make_user):
    t = make_tenant()
    u = make_user(t)
    wrong_pw = login(client, t, u, password="nope-nope")
    unknown = login(client, t, u, email="ghost@example.test")
    assert wrong_pw.status_code == unknown.status_code == 401
    assert wrong_pw.json() == unknown.json() == {"detail": "Invalid credentials"}


def test_user_cannot_log_in_through_another_tenant(client, make_tenant, make_user):
    t1, t2 = make_tenant(), make_tenant()
    u = make_user(t1)
    assert login(client, t2, u).status_code == 401


def test_inactive_user_cannot_log_in(client, make_tenant, make_user):
    t = make_tenant()
    u = make_user(t, active=False)
    assert login(client, t, u).status_code == 401


@pytest.mark.parametrize(
    "payload",
    [
        {"tenant": "Bad Slug!", "email": "a@b.co", "password": "x"},
        {"tenant": "acme", "email": "not-an-email", "password": "x"},
        {"tenant": "acme", "email": "a@b.co", "password": ""},
        {"tenant": "acme", "email": "a@b.co", "password": "x" * 129},
        {"tenant": "acme", "email": "a@b.co", "password": "x", "is_admin": True},
    ],
)
def test_login_input_is_validated(client, payload):
    assert client.post("/auth/login", json=payload).status_code == 422


def test_login_is_audited(client, make_tenant, make_user):
    t = make_tenant()
    u = make_user(t)
    assert login(client, t, u).status_code == 200
    with db_session(t.id) as s:
        n = s.execute(
            text("SELECT count(*) FROM audit_log WHERE action = 'auth.login' AND user_id = :u"), {"u": u.id}
        ).scalar()
    assert n == 1


# ---------------------------------------------------------------- token attacks
def _claims(user, **over):
    now = datetime.now(timezone.utc)
    claims = {
        "sub": str(user.id),
        "tenant_id": str(user.tenant_id),
        "roles": ["admin"],
        "iss": settings.jwt_issuer,
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    claims.update(over)
    return claims


def _without(claims, key):
    claims.pop(key)
    return claims


SECRET = settings.jwt_secret
PAST = datetime.now(timezone.utc) - timedelta(minutes=2)

CASES = {
    "wrong_signing_key": lambda u, o: jwt.encode(_claims(u), "x" * 40, algorithm="HS256"),
    "alg_none": lambda u, o: jwt.encode(_claims(u), None, algorithm="none"),
    "expired": lambda u, o: jwt.encode(_claims(u, exp=PAST), SECRET, algorithm="HS256"),
    "missing_exp": lambda u, o: jwt.encode(_without(_claims(u), "exp"), SECRET, algorithm="HS256"),
    "wrong_issuer": lambda u, o: jwt.encode(_claims(u, iss="someone-else"), SECRET, algorithm="HS256"),
    "unknown_role": lambda u, o: jwt.encode(_claims(u, roles=["superadmin"]), SECRET, algorithm="HS256"),
    "roles_not_a_list": lambda u, o: jwt.encode(_claims(u, roles="admin"), SECRET, algorithm="HS256"),
    "missing_tenant_claim": lambda u, o: jwt.encode(_without(_claims(u), "tenant_id"), SECRET, algorithm="HS256"),
    "subject_not_a_uuid": lambda u, o: jwt.encode(_claims(u, sub="123"), SECRET, algorithm="HS256"),
    "other_tenants_id": lambda u, o: jwt.encode(_claims(u, tenant_id=str(o.id)), SECRET, algorithm="HS256"),
    "garbage": lambda u, o: "not.a.jwt",
}


@pytest.mark.parametrize("case", list(CASES))
def test_bad_tokens_are_rejected(client, make_tenant, make_user, case):
    t, other = make_tenant(), make_tenant()
    u = make_user(t, "admin")
    token = CASES[case](u, other)
    r = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_missing_token_is_rejected(client):
    assert client.get("/me").status_code == 401


def test_me_returns_the_callers_own_identity(client, auth, make_tenant, make_user):
    t = make_tenant()
    u = make_user(t, "editor")
    body = client.get("/me", headers=auth(u)).json()
    assert body == {"user_id": str(u.id), "tenant_id": str(t.id), "roles": ["editor"], "email": u.email}


def test_deactivating_a_user_kills_their_existing_token(client, auth, owner_conn, make_tenant, make_user):
    t = make_tenant()
    u = make_user(t, "editor")
    headers = auth(u)
    assert client.get("/me", headers=headers).status_code == 200
    owner_conn.execute("UPDATE users SET is_active = false WHERE id = %s", (u.id,))
    assert client.get("/me", headers=headers).status_code == 401


def test_role_guard(client, auth, make_tenant, make_user):
    t = make_tenant()
    admin, viewer = make_user(t, "admin"), make_user(t, "viewer")
    assert client.get("/admin/ping", headers=auth(viewer)).status_code == 403
    assert client.get("/admin/ping", headers=auth(admin)).status_code == 200
    assert client.get("/admin/ping").status_code == 401