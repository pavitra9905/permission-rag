"""Smoke test: RLS isolates tenants and fails closed. Cleans up after itself."""
import sys
import uuid

import psycopg

from app.core.config import settings


def dsn(url: str) -> str:
    return url.replace("+psycopg", "")


failed = False


def check(ok: bool, label: str) -> None:
    global failed
    failed |= not ok
    print(f"{'✔' if ok else '✘'} {label}")


def run(conn, tenant, sql, params=()):
    """One transaction with the tenant set via set_config(..., true) == SET LOCAL."""
    with conn.transaction():
        conn.execute("SELECT set_config('app.current_tenant', %s, true)", (str(tenant or ""),))
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else cur.rowcount


VEC = "[" + ",".join(["0.1"] * 384) + "]"
owner = psycopg.connect(dsn(settings.migration_database_url), autocommit=True)
app = psycopg.connect(dsn(settings.app_database_url), autocommit=True)
tag = uuid.uuid4().hex[:8]
T = {}

try:
    # Seed two tenants as the owner (what a seed script / test factory does).
    for n in ("a", "b"):
        tid = owner.execute(
            "INSERT INTO tenants (slug, name) VALUES (%s, %s) RETURNING id",
            (f"smoke-{n}-{tag}", f"Smoke {n}"),
        ).fetchone()[0]
        uid = owner.execute(
            "INSERT INTO users (tenant_id, email, password_hash) VALUES (%s, %s, 'x') RETURNING id",
            (tid, f"{n}@example.com"),
        ).fetchone()[0]
        did = run(owner, tid,
                  "INSERT INTO documents (tenant_id, title, created_by) VALUES (%s, %s, %s) RETURNING id",
                  (tid, f"doc-{n}", uid))[0][0]
        run(owner, tid,
            "INSERT INTO chunks (tenant_id, document_id, chunk_index, content, embedding) "
            "VALUES (%s, %s, 0, %s, %s::vector)", (tid, did, f"chunk-{n}", VEC))
        T[n] = (tid, uid, did)

    a, b = T["a"], T["b"]

    check(run(app, a[0], "SELECT title FROM documents") == [("doc-a",)], "tenant A sees only its own documents")
    check(run(app, b[0], "SELECT title FROM documents") == [("doc-b",)], "tenant B sees only its own documents")
    check(run(app, None, "SELECT * FROM documents") == [], "no tenant set -> zero rows (fails closed)")
    check(run(app, a[0], "SELECT * FROM documents WHERE id = %s", (b[2],)) == [], "A cannot fetch B's document by id")
    check(run(app, a[0], "UPDATE documents SET title = 'hacked' WHERE id = %s", (b[2],)) == 0, "A cannot UPDATE B's document")
    check(run(app, a[0], "DELETE FROM documents WHERE id = %s", (b[2],)) == 0, "A cannot DELETE B's document")
    check(run(app, a[0], "SELECT content FROM chunks ORDER BY embedding <=> %s::vector LIMIT 5", (VEC,)) == [("chunk-a",)],
          "vector search under A returns only A's chunks")

    try:
        run(app, a[0], "INSERT INTO documents (tenant_id, title, created_by) VALUES (%s, 'x', %s)", (b[0], b[1]))
        check(False, "A cannot INSERT a row into tenant B")
    except psycopg.errors.InsufficientPrivilege:
        check(True, "A cannot INSERT a row into tenant B")

    login = run(app, None, "SELECT tenant_id, roles FROM app_login_lookup(%s, %s)", (f"smoke-a-{tag}", "A@Example.com"))
    check(len(login) == 1 and login[0][0] == a[0], "login lookup finds the user without a tenant set")
finally:
    for tid, _, _ in T.values():
        run(owner, tid, "DELETE FROM documents WHERE tenant_id = %s", (tid,))
        owner.execute("DELETE FROM users WHERE tenant_id = %s", (tid,))
        owner.execute("DELETE FROM tenants WHERE id = %s", (tid,))

print("\nRLS smoke test OK." if not failed else "\nFix the ✘ items above.")
sys.exit(1 if failed else 0)