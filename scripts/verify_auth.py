"""End-to-end auth smoke test via FastAPI's TestClient (no server needed)."""
import sys
from datetime import timedelta
from uuid import UUID

import jwt
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.security import create_access_token
from app.db.session import db_session
from app.main import app

PW = "Passw0rd!dev"
failed = False


def check(ok: bool, label: str) -> None:
    global failed
    failed |= not ok
    print(f"{'✔' if ok else '✘'} {label}")


def login(client, tenant, role, password=PW):
    return client.post(
        "/auth/login",
        json={"tenant": tenant, "email": f"{role}@{tenant}.test", "password": password},
    )


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def denied(tenant_id: UUID, sql: str) -> bool:
    try:
        with db_session(tenant_id) as s:
            s.execute(text(sql))
        return False
    except DBAPIError:
        return True


with TestClient(app) as client:
    r = login(client, "acme", "admin")
    check(r.status_code == 200 and r.json()["token_type"] == "bearer", "valid login returns a bearer token")
    token = r.json()["access_token"]
    claims = jwt.decode(token, options={"verify_signature": False})
    acme_id = UUID(claims["tenant_id"])

    g = login(client, "globex", "admin")
    globex_id = UUID(jwt.decode(g.json()["access_token"], options={"verify_signature": False})["tenant_id"])

    me = client.get("/me", headers=bearer(token))
    check(
        me.status_code == 200 and me.json()["email"] == "admin@acme.test" and me.json()["roles"] == ["admin"],
        "/me returns the caller's own identity",
    )

    check(login(client, "acme", "admin", "wrong-password").status_code == 401, "wrong password -> 401")
    check(login(client, "acme", "nobody").status_code == 401, "unknown user -> 401")
    check(login(client, "nope", "admin").status_code == 401, "unknown tenant -> 401")
    r = client.post("/auth/login", json={"tenant": "globex", "email": "admin@acme.test", "password": PW})
    check(r.status_code == 401, "acme user cannot log in through globex")

    check(client.get("/me").status_code == 401, "no token -> 401")
    check(client.get("/me", headers=bearer("garbage")).status_code == 401, "garbage token -> 401")
    forged = jwt.encode(claims, "x" * 40, algorithm="HS256")
    check(client.get("/me", headers=bearer(forged)).status_code == 401, "token signed with wrong key -> 401")
    none_alg = jwt.encode(claims, None, algorithm="none")
    check(client.get("/me", headers=bearer(none_alg)).status_code == 401, "alg=none token -> 401")
    expired = create_access_token(
        user_id=UUID(claims["sub"]), tenant_id=acme_id, roles=["admin"], expires_delta=timedelta(seconds=-120)
    )
    check(client.get("/me", headers=bearer(expired)).status_code == 401, "expired token -> 401")
    wrong_tenant = create_access_token(
        user_id=UUID(claims["sub"]), tenant_id=globex_id, roles=["admin"]
    )
    check(
        client.get("/me", headers=bearer(wrong_tenant)).status_code == 401,
        "validly signed token with another tenant's id -> 401",
    )

    viewer = login(client, "acme", "viewer").json()["access_token"]
    check(client.get("/admin/ping", headers=bearer(viewer)).status_code == 403, "viewer on admin route -> 403")
    check(client.get("/admin/ping", headers=bearer(token)).status_code == 200, "admin on admin route -> 200")

with db_session(acme_id) as s:
    tenants_seen = {row[0] for row in s.execute(text("SELECT DISTINCT tenant_id FROM audit_log"))}
check(tenants_seen == {acme_id}, "audit_log under acme shows only acme's entries (and has some)")
check(denied(acme_id, "UPDATE audit_log SET action = 'x'"), "audit_log UPDATE is denied")
check(denied(acme_id, "DELETE FROM audit_log"), "audit_log DELETE is denied")
check(denied(acme_id, "TRUNCATE audit_log"), "audit_log TRUNCATE is denied")

print("\nAuth smoke test OK." if not failed else "\nFix the ✘ items above.")
sys.exit(1 if failed else 0)