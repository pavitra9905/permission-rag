from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.security import Principal, decode_access_token
from app.db.session import db_session

_bearer = HTTPBearer(auto_error=False)


def unauthorized(detail: str = "Invalid or missing credentials") -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})


def get_principal(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    if creds is None:
        raise unauthorized()
    try:
        claims = decode_access_token(creds.credentials)
    except (jwt.PyJWTError, ValueError, KeyError):
        raise unauthorized() from None

    # The token proves WHO the caller is. What they may do is read fresh from the
    # database on every request (under RLS, so a forged tenant claim finds no user),
    # which makes demotion and deactivation take effect immediately.
    with db_session(claims.tenant_id) as session:
        active = session.execute(
            text("SELECT is_active FROM users WHERE id = :id"), {"id": claims.user_id}
        ).scalar()
        roles = (
            session.execute(
                text("SELECT role FROM user_roles WHERE user_id = :id"), {"id": claims.user_id}
            ).scalars().all()
            if active
            else []
        )
    if not active:
        raise unauthorized("Unknown or inactive user")
    return Principal(user_id=claims.user_id, tenant_id=claims.tenant_id, roles=frozenset(roles))


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


def assert_active_user(session: Session, principal: Principal) -> None:
    """Under RLS: a user from another tenant is invisible, so a forged tenant claim fails here."""
    active = session.execute(
        text("SELECT is_active FROM users WHERE id = :id"), {"id": principal.user_id}
    ).scalar()
    if not active:
        raise unauthorized("Unknown or inactive user")


@contextmanager
def tenant_tx(principal: Principal) -> Iterator[Session]:
    """One transaction with the tenant set. Commits when the `with` block exits,
    so exit it BEFORE returning a response or scheduling a background task."""
    with db_session(principal.tenant_id) as session:
        assert_active_user(session, principal)
        yield session


def get_tenant_session(principal: CurrentPrincipal) -> Iterator[Session]:
    with tenant_tx(principal) as session:
        yield session


TenantSession = Annotated[Session, Depends(get_tenant_session)]


def require_roles(*allowed: str) -> Callable[..., Principal]:
    def checker(principal: CurrentPrincipal) -> Principal:
        if not principal.roles & set(allowed):
            raise HTTPException(status_code=403, detail="Insufficient role")
        return principal

    return checker