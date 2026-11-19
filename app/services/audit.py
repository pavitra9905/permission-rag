import json
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session


def write_audit(
    session: Session,
    *,
    tenant_id: UUID,
    user_id: UUID | None,
    action: str,
    resource_type: str | None = None,
    resource_id: UUID | None = None,
    details: dict | None = None,
) -> None:
    """Append one audit row. The session must already have the tenant set (RLS checks it).
    Never put passwords, tokens or document text in `details`."""
    session.execute(
        text(
            "INSERT INTO audit_log (tenant_id, user_id, action, resource_type, resource_id, details) "
            "VALUES (:tenant_id, :user_id, :action, :resource_type, :resource_id, CAST(:details AS jsonb))"
        ),
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "action": action,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "details": json.dumps(details or {}),
        },
    )