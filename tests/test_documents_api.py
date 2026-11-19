import pytest
from sqlalchemy import text

from app.db.session import db_session


def doc_ids(client, headers):
    return {d["id"] for d in client.get("/documents", headers=headers).json()}


def upload(client, headers, *, title="T", filename="n.txt", content=b"hello world, this is text"):
    return client.post(
        "/documents", headers=headers, data={"title": title}, files={"file": (filename, content, "text/plain")}
    )


# ---------------------------------------------------------------- upload + ingestion
def test_upload_is_ingested_and_audited(client, auth, make_tenant, make_user):
    t = make_tenant()
    editor = make_user(t, "editor")
    r = upload(client, auth(editor), content=b"first paragraph.\n\nsecond paragraph.")
    assert r.status_code == 202
    doc_id = r.json()["id"]
    listing = {d["id"]: d for d in client.get("/documents", headers=auth(editor)).json()}
    assert listing[doc_id]["status"] == "ready"
    with db_session(t.id) as s:
        chunks = s.execute(text("SELECT count(*) FROM chunks WHERE document_id = :d"), {"d": doc_id}).scalar()
        acts = [r[0] for r in s.execute(text("SELECT action FROM audit_log WHERE resource_id = :d ORDER BY id"), {"d": doc_id})]
    assert chunks >= 1
    assert acts == ["document.upload", "document.ingested"]


@pytest.mark.parametrize(
    ("role", "filename", "content", "status"),
    [
        ("viewer", "n.txt", b"hello", 403),
        ("editor", "evil.exe", b"hello", 415),
        ("editor", "n.txt", b"   ", 422),
        ("editor", "n.txt", b"\xff\xfe\xfd", 422),
        ("editor", "n.txt", b"a\x00b", 422),
        ("editor", "n.txt", b"a" * 600_000, 413),
    ],
)
def test_upload_validation(client, auth, make_tenant, make_user, role, filename, content, status):
    t = make_tenant()
    headers = auth(make_user(t, role))
    assert upload(client, headers, filename=filename, content=content).status_code == status


@pytest.mark.parametrize("title", ["", "x" * 201])
def test_upload_title_is_validated(client, auth, make_tenant, make_user, title):
    t = make_tenant()
    assert upload(client, auth(make_user(t, "editor")), title=title).status_code == 422


def test_uploaded_filename_cannot_contain_a_path(client, auth, make_tenant, make_user):
    t = make_tenant()
    headers = auth(make_user(t, "editor"))
    assert upload(client, headers, filename="../../etc/passwd.txt").status_code == 202
    assert [d["filename"] for d in client.get("/documents", headers=headers).json()] == ["passwd.txt"]


# ---------------------------------------------------------------- who can see / manage
def test_viewer_without_grant_cannot_see_or_touch(client, auth, grant, scene):
    h = auth(scene.viewer)
    assert str(scene.doc.id) not in doc_ids(client, h)
    assert client.delete(f"/documents/{scene.doc.id}", headers=h).status_code == 404
    assert client.get(f"/documents/{scene.doc.id}/permissions", headers=h).status_code == 404
    body = {"user_id": str(scene.viewer.id), "permission": "read"}
    assert client.post(f"/documents/{scene.doc.id}/permissions", headers=h, json=body).status_code == 404


def test_viewer_with_read_grant_can_see_but_not_manage(client, auth, grant, scene):
    grant(scene.doc, scene.viewer, "read")
    h = auth(scene.viewer)
    assert str(scene.doc.id) in doc_ids(client, h)
    assert client.delete(f"/documents/{scene.doc.id}", headers=h).status_code == 403
    assert client.get(f"/documents/{scene.doc.id}/permissions", headers=h).status_code == 403
    body = {"user_id": str(scene.viewer.id), "permission": "write"}
    assert client.post(f"/documents/{scene.doc.id}/permissions", headers=h, json=body).status_code == 403


def test_editor_who_is_not_the_creator_needs_a_write_grant(client, auth, grant, scene):
    h, url = auth(scene.editor), f"/documents/{scene.doc.id}"
    assert client.delete(url, headers=h).status_code == 404  # no access: not even visible
    grant(scene.doc, scene.editor, "read")
    assert client.delete(url, headers=h).status_code == 403  # visible, not manageable
    grant(scene.doc, scene.editor, "write")
    assert client.delete(url, headers=h).status_code == 204


def test_creator_can_grant_revoke_and_delete(client, auth, scene):
    h, url = auth(scene.creator), f"/documents/{scene.doc.id}"
    body = {"user_id": str(scene.viewer.id), "permission": "read"}
    assert client.post(f"{url}/permissions", headers=h, json=body).status_code == 200
    assert str(scene.doc.id) in doc_ids(client, auth(scene.viewer))
    assert client.delete(f"{url}/permissions/{scene.viewer.id}", headers=h).status_code == 204
    assert str(scene.doc.id) not in doc_ids(client, auth(scene.viewer))
    assert client.delete(url, headers=h).status_code == 204


def test_admin_manages_any_document_in_the_tenant(client, auth, scene):
    h = auth(scene.admin)
    assert str(scene.doc.id) in doc_ids(client, h)
    body = {"user_id": str(scene.viewer.id), "permission": "read"}
    assert client.post(f"/documents/{scene.doc.id}/permissions", headers=h, json=body).status_code == 200
    assert client.delete(f"/documents/{scene.doc.id}", headers=h).status_code == 204


def test_other_tenants_admin_gets_404_everywhere(client, auth, scene):
    h, url = auth(scene.outsider), f"/documents/{scene.doc.id}"
    assert str(scene.doc.id) not in doc_ids(client, h)
    assert client.get(f"{url}/permissions", headers=h).status_code == 404
    body = {"user_id": str(scene.outsider.id), "permission": "read"}
    assert client.post(f"{url}/permissions", headers=h, json=body).status_code == 404
    assert client.delete(f"{url}/permissions/{scene.viewer.id}", headers=h).status_code == 404
    assert client.delete(url, headers=h).status_code == 404


def test_cannot_grant_access_to_a_user_from_another_tenant(client, auth, scene):
    body = {"user_id": str(scene.outsider.id), "permission": "read"}
    r = client.post(f"/documents/{scene.doc.id}/permissions", headers=auth(scene.creator), json=body)
    assert r.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"user_id": "11111111-1111-1111-1111-111111111111", "permission": "admin"},
        {"user_id": "not-a-uuid", "permission": "read"},
        {"user_id": "11111111-1111-1111-1111-111111111111", "permission": "read", "extra": 1},
        {"permission": "read"},
    ],
)
def test_grant_input_is_validated(client, auth, scene, body):
    r = client.post(f"/documents/{scene.doc.id}/permissions", headers=auth(scene.creator), json=body)
    assert r.status_code == 422


def test_granting_twice_updates_the_permission(client, auth, scene):
    h, url = auth(scene.creator), f"/documents/{scene.doc.id}/permissions"
    for perm in ("read", "write"):
        body = {"user_id": str(scene.viewer.id), "permission": perm}
        assert client.post(url, headers=h, json=body).status_code == 200
    rows = client.get(url, headers=h).json()
    assert [(r["user_id"], r["permission"]) for r in rows] == [(str(scene.viewer.id), "write")]


def test_revoking_a_missing_permission_is_404(client, auth, scene):
    r = client.delete(f"/documents/{scene.doc.id}/permissions/{scene.viewer.id}", headers=auth(scene.creator))
    assert r.status_code == 404


def test_permission_changes_and_deletes_are_audited(client, auth, scene):
    h, url = auth(scene.creator), f"/documents/{scene.doc.id}"
    client.post(f"{url}/permissions", headers=h, json={"user_id": str(scene.viewer.id), "permission": "read"})
    client.delete(f"{url}/permissions/{scene.viewer.id}", headers=h)
    client.delete(url, headers=h)
    with db_session(scene.t.id) as s:
        acts = [r[0] for r in s.execute(
            text("SELECT action FROM audit_log WHERE resource_id = :d ORDER BY id"), {"d": scene.doc.id})]
    assert acts == ["permission.grant", "permission.revoke", "document.delete"]


def test_deleting_a_document_removes_its_chunks_and_permissions(client, auth, grant, scene):
    grant(scene.doc, scene.viewer)
    assert client.delete(f"/documents/{scene.doc.id}", headers=auth(scene.admin)).status_code == 204
    with db_session(scene.t.id) as s:
        for table in ("chunks", "document_permissions"):
            n = s.execute(text(f"SELECT count(*) FROM {table} WHERE document_id = :d"), {"d": scene.doc.id}).scalar()
            assert n == 0