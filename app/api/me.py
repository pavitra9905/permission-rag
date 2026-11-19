from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import text

from app.api.deps import CurrentPrincipal, TenantSession, require_roles
from app.core.security import Principal

router = APIRouter(tags=["me"])


@router.get("/me")
def me(principal: CurrentPrincipal, session: TenantSession) -> dict:
    email = session.execute(
        text("SELECT email FROM users WHERE id = :id"), {"id": principal.user_id}
    ).scalar()
    return {
        "user_id": str(principal.user_id),
        "tenant_id": str(principal.tenant_id),
        "roles": sorted(principal.roles),
        "email": email,
    }


@router.get("/admin/ping")
def admin_ping(
    principal: Annotated[Principal, Depends(require_roles("admin"))],
    session: TenantSession,  # every protected route takes TenantSession
) -> dict:
    return {"ok": True}
