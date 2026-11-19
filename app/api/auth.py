from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.core.config import settings
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


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest) -> TokenResponse:
    with db_session() as session:  # anonymous: sees nothing, except via the login function
        row = session.execute(
            text("SELECT user_id, tenant_id, password_hash, roles FROM app_login_lookup(:slug, :email)"),
            {"slug": body.tenant, "email": body.email},
        ).first()

        if row is None:
            burn_password_check(body.password)
            raise _invalid()
        if not verify_password(body.password, row.password_hash):
            raise _invalid()

        set_tenant(session, row.tenant_id)  # now RLS lets us write this tenant's audit row
        write_audit(session, tenant_id=row.tenant_id, user_id=row.user_id, action="auth.login")

        token = create_access_token(
            user_id=row.user_id, tenant_id=row.tenant_id, roles=list(row.roles)
        )
    return TokenResponse(access_token=token, expires_in=settings.access_token_minutes * 60)