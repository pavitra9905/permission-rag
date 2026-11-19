from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

# The runtime engine connects as rag_app (limited user, subject to RLS).
engine = create_engine(settings.app_database_url, pool_pre_ping=True, pool_size=5, max_overflow=10)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def set_tenant(session: Session, tenant_id: UUID) -> None:
    # Same as SET LOCAL, but accepts a bind parameter. `true` = transaction-local.
    session.execute(
        text("SELECT set_config('app.current_tenant', :tenant, true)"),
        {"tenant": str(tenant_id)},
    )


@contextmanager
def db_session(tenant_id: UUID | None = None) -> Iterator[Session]:
    """One transaction. Commits on success, rolls back on any exception.

    tenant_id=None gives an anonymous session that sees nothing under RLS
    (used only for login). Do NOT call session.commit() inside: the tenant
    setting ends with the transaction.
    """
    with SessionLocal() as session:
        with session.begin():
            if tenant_id is not None:
                set_tenant(session, tenant_id)
            yield session