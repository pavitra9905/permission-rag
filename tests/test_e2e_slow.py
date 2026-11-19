import pytest

pytestmark = pytest.mark.slow  # real embedding model: run with `pytest -m slow`


def test_real_embeddings_end_to_end(client, auth, make_tenant, make_user):
    t = make_tenant()
    editor, viewer = make_user(t, "editor"), make_user(t, "viewer")
    text = b"Project Bluefalcon is the confidential launch codename. The launch is planned for March."
    r = client.post("/documents", headers=auth(editor), data={"title": "Launch"}, files={"file": ("a.txt", text, "text/plain")})
    assert r.status_code == 202
    hits = client.post("/search", headers=auth(editor), json={"query": "What is the launch codename?", "k": 3}).json()["hits"]
    assert hits and "Bluefalcon" in hits[0]["content"]
    assert client.post("/search", headers=auth(viewer), json={"query": "What is the launch codename?"}).json()["hits"] == []