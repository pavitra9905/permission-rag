"""End-to-end: upload -> ingest -> permission-aware search, across two tenants."""
import sys
import time

import jwt
from fastapi.testclient import TestClient

from app.main import app

PW = "Passw0rd!dev"
failed = False


def check(ok: bool, label: str) -> None:
    global failed
    failed |= not ok
    print(f"{'✔' if ok else '✘'} {label}")


def H(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def login(c, tenant, role) -> str:
    r = c.post("/auth/login", json={"tenant": tenant, "email": f"{role}@{tenant}.test", "password": PW})
    return r.json()["access_token"]


def uid(token: str) -> str:
    return jwt.decode(token, options={"verify_signature": False})["sub"]


def upload(c, token, title, content, filename="note.txt"):
    return c.post("/documents", headers=H(token), data={"title": title},
                  files={"file": (filename, content.encode(), "text/plain")})


def search(c, token, q, k=5):
    return c.post("/search", headers=H(token), json={"query": q, "k": k})


def hit_docs(r) -> set[str]:
    return {h["document_id"] for h in r.json()["hits"]}


def wait_ready(c, token, doc_id, timeout=300) -> str:
    end = time.time() + timeout
    while time.time() < end:
        docs = c.get("/documents", headers=H(token)).json()
        status = next((d["status"] for d in docs if d["id"] == doc_id), None)
        if status in ("ready", "failed"):
            return status
        time.sleep(1)
    return "timeout"


ACME_TEXT = (
    "Project Bluefalcon is Acme's confidential launch codename. The launch is planned for March.\n\n"
    "Only the Acme leadership team knows the Bluefalcon budget and the supplier list."
)
GLOBEX_TEXT = "Globex quarterly budget review: marketing spend rose and the Globex roadmap slipped by a month."

print("(first run downloads the embedding model, ~90 MB, so this can take a few minutes)")
created: list[tuple[str, str]] = []  # (token, doc_id) for cleanup

with TestClient(app) as c:
    try:
        acme_admin, acme_editor, acme_viewer = (login(c, "acme", r) for r in ("admin", "editor", "viewer"))
        globex_admin, globex_editor = login(c, "globex", "admin"), login(c, "globex", "editor")

        check(upload(c, acme_viewer, "nope", "hello").status_code == 403, "viewer cannot upload (403)")
        check(upload(c, acme_editor, "bad", "x", "evil.exe").status_code == 415, "unsupported file type -> 415")
        big = upload(c, acme_editor, "big", "a" * 600_000)
        check(big.status_code == 413, "oversized upload -> 413")
        check(upload(c, acme_editor, "empty", "   ").status_code == 422, "blank file -> 422")

        r = upload(c, acme_editor, "Bluefalcon plan", ACME_TEXT)
        check(r.status_code == 202 and r.json()["status"] in ("pending", "processing", "ready"), "editor uploads (202)")
        acme_doc = r.json()["id"]
        created.append((acme_editor, acme_doc))
        check(wait_ready(c, acme_editor, acme_doc) == "ready", "acme document ingested -> ready")

        r = upload(c, globex_editor, "Globex budget", GLOBEX_TEXT)
        globex_doc = r.json()["id"]
        created.append((globex_editor, globex_doc))
        check(wait_ready(c, globex_editor, globex_doc) == "ready", "globex document ingested -> ready")

        q = "What is the Bluefalcon launch codename?"
        check(acme_doc in hit_docs(search(c, acme_editor, q)), "creator finds own document")
        check(acme_doc in hit_docs(search(c, acme_admin, q)), "tenant admin finds it without a grant")
        check(acme_doc not in hit_docs(search(c, acme_viewer, q)), "viewer WITHOUT grant gets nothing")
        gh = hit_docs(search(c, globex_admin, q))
        check(acme_doc not in gh and gh <= {globex_doc}, "other tenant (even admin) never sees acme data")

        check(acme_doc not in {d["id"] for d in c.get("/documents", headers=H(acme_viewer)).json()},
              "viewer's document list excludes ungranted doc")
        check(c.delete(f"/documents/{acme_doc}", headers=H(globex_admin)).status_code == 404,
              "other tenant cannot delete it (404)")
        check(c.post(f"/documents/{acme_doc}/permissions", headers=H(globex_admin),
                     json={"user_id": uid(globex_admin), "permission": "read"}).status_code == 404,
              "other tenant cannot grant on it (404)")
        check(c.post(f"/documents/{acme_doc}/permissions", headers=H(acme_viewer),
                     json={"user_id": uid(acme_viewer), "permission": "read"}).status_code == 404,
              "viewer cannot self-grant (404)")

        r = c.post(f"/documents/{acme_doc}/permissions", headers=H(acme_editor),
                   json={"user_id": uid(globex_admin), "permission": "read"})
        check(r.status_code == 422, "cannot grant to a user from another tenant (422)")
        r = c.post(f"/documents/{acme_doc}/permissions", headers=H(acme_editor),
                   json={"user_id": uid(acme_viewer), "permission": "admin"})
        check(r.status_code == 422, "invalid permission value rejected (422)")

        r = c.post(f"/documents/{acme_doc}/permissions", headers=H(acme_editor),
                   json={"user_id": uid(acme_viewer), "permission": "read"})
        check(r.status_code == 200, "creator grants viewer read access")
        check(acme_doc in hit_docs(search(c, acme_viewer, q)), "viewer WITH grant now finds it")
        check(c.delete(f"/documents/{acme_doc}", headers=H(acme_viewer)).status_code == 403,
              "granted viewer still cannot delete (403)")
        check(c.post(f"/documents/{acme_doc}/permissions", headers=H(acme_viewer),
                     json={"user_id": uid(acme_viewer), "permission": "write"}).status_code == 403,
              "granted viewer cannot grant others (403)")

        check(c.delete(f"/documents/{acme_doc}/permissions/{uid(acme_viewer)}", headers=H(acme_editor)).status_code == 204,
              "creator revokes access")
        check(acme_doc not in hit_docs(search(c, acme_viewer, q)), "after revoke, viewer finds nothing again")
    finally:
        for token, doc_id in created:
            c.delete(f"/documents/{doc_id}", headers=H(token))

print("\nDocuments + retrieval smoke test OK." if not failed else "\nFix the ✘ items above.")
sys.exit(1 if failed else 0)