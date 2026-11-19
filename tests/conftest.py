"""Test harness: disposable Postgres 16 + pgvector (testcontainers), migrated by Alembic
exactly like production, plus factory fixtures that create isolated tenants."""
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from psycopg import sql

from tests.helpers import TEST_PASSWORD, unit_vec, vec_literal

ROOT = Path(__file__).resolve().parents[1]
DB_NAME = "ragtest"
SUPER_PW, OWNER_PW, APP_PW = "super_test_pw", "owner_test_pw", "app_test_pw"
_container = None


# ---------------------------------------------------------------- container lifecycle
def _wait_for_postgres(dsn: str, timeout: float = 90.0) -> None:
    end = time.time() + timeout
    while True:
        try:
            with psycopg.connect(dsn, connect_timeout=3) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.OperationalError:
            if time.time() > end:
                raise
            time.sleep(0.5)


def _create_roles(dsn: str) -> None:
    """Mirrors db/init/01-roles.sql (keep the two in sync)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        for name, pw in (("rag_owner", OWNER_PW), ("rag_app", APP_PW)):
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
                ).format(sql.Identifier(name), sql.Literal(pw))
            )
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO rag_owner, rag_app").format(sql.Identifier(DB_NAME))
        )
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute("ALTER SCHEMA public OWNER TO rag_owner")
        conn.execute("GRANT USAGE ON SCHEMA public TO rag_app")


def pytest_configure(config):
    global _container
    from testcontainers.core.container import DockerContainer

    try:
        _container = (
            DockerContainer("pgvector/pgvector:pg16")
            .with_env("POSTGRES_USER", "postgres")
            .with_env("POSTGRES_PASSWORD", SUPER_PW)
            .with_env("POSTGRES_DB", DB_NAME)
            .with_exposed_ports(5432)
        )
        _container.start()
    except Exception as exc:  # noqa: BLE001
        raise pytest.UsageError(f"Could not start the Postgres test container. Is Docker Desktop running? ({exc})")

    host = _container.get_container_host_ip()
    host = "127.0.0.1" if host == "localhost" else host
    port = int(_container.get_exposed_port(5432))

    su_dsn = f"postgresql://postgres:{SUPER_PW}@{host}:{port}/{DB_NAME}"
    _wait_for_postgres(su_dsn)
    _create_roles(su_dsn)

    # Env vars override .env, and MUST be set before any `app.*` module is imported.
    os.environ.update(
        {
            "APP_DATABASE_URL": f"postgresql+psycopg://rag_app:{APP_PW}@{host}:{port}/{DB_NAME}",
            "MIGRATION_DATABASE_URL": f"postgresql+psycopg://rag_owner:{OWNER_PW}@{host}:{port}/{DB_NAME}",
            "JWT_SECRET": "test-only-secret-" + "x" * 40,
            # Any LLM call that is not explicitly faked hits a dead port -> clean 503.
            "LLM_PROVIDER": "ollama",
            "OLLAMA_BASE_URL": "http://127.0.0.1:9",
            "LLM_TIMEOUT_SECONDS": "2",
            "ANTHROPIC_API_KEY": "",
            "GEMINI_API_KEY": "",
        }
    )

    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, "head")  # the real migration, run as rag_owner


def pytest_unconfigure(config):
    if _container is not None:
        _container.stop()


# ---------------------------------------------------------------- data classes
@dataclass(frozen=True)
class Tenant:
    id: uuid.UUID
    slug: str


@dataclass(frozen=True)
class User:
    id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    roles: tuple


@dataclass(frozen=True)
class Doc:
    id: uuid.UUID
    tenant_id: uuid.UUID
    created_by: uuid.UUID
    title: str
    marker: str  # unique string inside the document text, to assert (non-)leakage


# ---------------------------------------------------------------- core fixtures
@pytest.fixture(scope="session")
def owner_conn():
    """Seeding connection as rag_owner (what migrations / seed scripts use)."""
    dsn = os.environ["MIGRATION_DATABASE_URL"].replace("+psycopg", "")
    with psycopg.connect(dsn, autocommit=True) as conn:
        yield conn


@pytest.fixture(scope="session")
def password_hash():
    from app.core.security import hash_password

    return hash_password(TEST_PASSWORD)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def fake_embeddings(monkeypatch, request):
    """No model download in the fast suite: every text embeds to unit_vec(0)."""
    if request.node.get_closest_marker("slow"):
        return

    def fake(texts):
        return [unit_vec(0) for _ in texts]

    for target in (
        "app.api.search.embed_texts",
        "app.api.ask.embed_texts",
        "app.services.ingestion.embed_texts",
    ):
        monkeypatch.setattr(target, fake)


@dataclass
class FakeLLM:
    calls: list
    fail: bool = False


@pytest.fixture
def fake_llm(monkeypatch):
    from app.services.llm import LLMUnavailable

    state = FakeLLM(calls=[])

    def fake(*, system, user):
        state.calls.append({"system": system, "user": user})
        if state.fail:
            raise LLMUnavailable("test")
        return "Fake answer [1]."

    monkeypatch.setattr("app.api.ask.generate_answer", fake)
    return state


# ---------------------------------------------------------------- factories
@pytest.fixture
def make_tenant(owner_conn):
    def _make(name="Test Corp"):
        slug = f"t-{uuid.uuid4().hex[:10]}"
        tid = owner_conn.execute(
            "INSERT INTO tenants (slug, name) VALUES (%s, %s) RETURNING id", (slug, name)
        ).fetchone()[0]
        return Tenant(tid, slug)

    return _make


@pytest.fixture
def make_user(owner_conn, password_hash):
    def _make(tenant, role="viewer", *, roles=None, email=None, active=True):
        roles = tuple(roles or (role,))
        email = email or f"{roles[0]}-{uuid.uuid4().hex[:8]}@example.test"
        uid = owner_conn.execute(
            "INSERT INTO users (tenant_id, email, password_hash, is_active) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (tenant.id, email, password_hash, active),
        ).fetchone()[0]
        for r in roles:
            owner_conn.execute(
                "INSERT INTO user_roles (tenant_id, user_id, role) VALUES (%s, %s, %s)",
                (tenant.id, uid, r),
            )
        return User(uid, tenant.id, email, roles)

    return _make


@pytest.fixture
def make_document(owner_conn):
    def _make(tenant, creator, *, title=None, text=None, vec=None, status="ready"):
        marker = f"MARKER-{uuid.uuid4().hex[:12]}"
        title = title or f"Doc {marker}"
        text = text if text is not None else f"Confidential note {marker}"
        vec = vec if vec is not None else unit_vec(0)
        with owner_conn.transaction():
            owner_conn.execute("SELECT set_config('app.current_tenant', %s, true)", (str(tenant.id),))
            did = owner_conn.execute(
                "INSERT INTO documents (tenant_id, title, filename, status, created_by) "
                "VALUES (%s, %s, 'seed.txt', %s, %s) RETURNING id",
                (tenant.id, title, status, creator.id),
            ).fetchone()[0]
            owner_conn.execute(
                "INSERT INTO chunks (tenant_id, document_id, chunk_index, content, embedding) "
                "VALUES (%s, %s, 0, %s, %s::vector)",
                (tenant.id, did, text, vec_literal(vec)),
            )
        return Doc(did, tenant.id, creator.id, title, marker)

    return _make


@pytest.fixture
def grant(owner_conn):
    def _grant(doc, user, permission="read"):
        with owner_conn.transaction():
            owner_conn.execute("SELECT set_config('app.current_tenant', %s, true)", (str(doc.tenant_id),))
            owner_conn.execute(
                "INSERT INTO document_permissions (tenant_id, document_id, user_id, permission, granted_by) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (document_id, user_id) DO UPDATE SET permission = EXCLUDED.permission",
                (doc.tenant_id, doc.id, user.id, permission, doc.created_by),
            )

    return _grant


@pytest.fixture
def auth():
    """Authorization headers for a user (a valid JWT, skipping the login endpoint)."""
    from app.core.security import create_access_token

    def _auth(user, **kwargs):
        token = create_access_token(
            user_id=user.id, tenant_id=user.tenant_id, roles=list(user.roles), **kwargs
        )
        return {"Authorization": f"Bearer {token}"}

    return _auth


# ---------------------------------------------------------------- ready-made worlds
@pytest.fixture
def world(make_tenant, make_user, make_document):
    """Two tenants, one editor and one document each."""
    a, b = make_tenant(), make_tenant()
    ua, ub = make_user(a, "editor"), make_user(b, "editor")
    return SimpleNamespace(a=a, b=b, ua=ua, ub=ub, da=make_document(a, ua), db=make_document(b, ub))


@pytest.fixture
def scene(make_tenant, make_user, make_document):
    """One tenant with admin / creator-editor / other editor / viewer and one document;
    plus a second tenant with an admin (the outsider)."""
    t, other = make_tenant(), make_tenant()
    s = SimpleNamespace(
        t=t,
        other=other,
        admin=make_user(t, "admin"),
        creator=make_user(t, "editor"),
        editor=make_user(t, "editor"),
        viewer=make_user(t, "viewer"),
        outsider=make_user(other, "admin"),
    )
    s.doc = make_document(t, s.creator)
    return s



@pytest.fixture(autouse=True)
def reset_rate_limiters():
    from app.core.ratelimit import account_limiter, ip_limiter

    account_limiter.clear()
    ip_limiter.clear()
    yield