"""The ONE definition of who may read / manage a document. Evaluated in SQL at query time."""
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.security import Principal

# Use with alias `d` for documents. Params: :is_admin, :uid  (tenant is enforced by RLS).
READABLE = """(CAST(:is_admin AS boolean) OR d.created_by = :uid OR EXISTS (
    SELECT 1 FROM document_permissions p WHERE p.document_id = d.id AND p.user_id = :uid))"""


def access_params(principal: Principal) -> dict:
    return {"is_admin": "admin" in principal.roles, "uid": principal.user_id}


def require_manage(session: Session, principal: Principal, document_id: UUID) -> None:
    """404 if the caller can't even read it; 403 if readable but not manageable."""
    row = session.execute(
        text(
            f"""SELECT d.created_by,
                   (SELECT p.permission FROM document_permissions p
                     WHERE p.document_id = d.id AND p.user_id = :uid) AS perm
                FROM documents d WHERE d.id = :doc AND {READABLE}"""
        ),
        {**access_params(principal), "doc": document_id},
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if "admin" in principal.roles:
        return
    if "editor" in principal.roles and (row.created_by == principal.user_id or row.perm == "write"):
        return
    raise HTTPException(status_code=403, detail="Insufficient permission on this document")