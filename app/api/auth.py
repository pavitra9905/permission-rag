from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.core.config import settings
from app.core.ratelimit import account_key, account_limiter, ip_limiter
from app.core.security import burn_password_check, create_access_token, verify_password
from app.db.session import db_session, set_tenant
from app.services.audit import write_audit

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    email: str = Field(max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


def _invalid() -> HTTPException:
    # Same message for every failure: don't reveal which part was wrong.
    return HTTPException(status_code=401, detail="Invalid credentials")


def _too_many(seconds: int) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail="Too many failed attempts. Try again later.",
        headers={"Retry-After": str(seconds)},
    )


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, request: Request) -> TokenResponse:
    ip = request.client.host if request.client else "unknown"
    acct = account_key(ip, body.tenant, body.email)

    # Checked BEFORE any password work, and keyed on what was submitted, so a
    # real account and a made-up one are limited identically.
    wait = max(ip_limiter.retry_after(ip), account_limiter.retry_after(acct))
    if wait:
        raise _too_many(wait)

    token: str | None = None
    with db_session() as session:  # anonymous: sees nothing, except via the login function
        row = session.execute(
            text("SELECT user_id, tenant_id, password_hash, roles FROM app_login_lookup(:slug, :email)"),
            {"slug": body.tenant, "email": body.email},
        ).first()

        if row is None:
            burn_password_check(body.password)
        elif not verify_password(body.password, row.password_hash):
            # Real account, wrong password: leave an audit trail.
            set_tenant(session, row.tenant_id)
            write_audit(
                session, tenant_id=row.tenant_id, user_id=row.user_id,
                action="auth.login_failed", details={"reason": "bad_password", "ip": ip},
            )
        else:
            set_tenant(session, row.tenant_id)
            write_audit(
                session, tenant_id=row.tenant_id, user_id=row.user_id,
                action="auth.login", details={"ip": ip},
            )
            token = create_access_token(
                user_id=row.user_id, tenant_id=row.tenant_id, roles=list(row.roles)
            )
    # The transaction has COMMITTED here, so the failure audit row is saved
    # before we raise (raising inside the block would roll it back).

    if token is None:
        ip_limiter.record_failure(ip)
        account_limiter.record_failure(acct)
        raise _invalid()

    account_limiter.reset(acct)
    return TokenResponse(access_token=token, expires_in=settings.access_token_minutes * 60)