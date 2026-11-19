from datetime import datetime
from pathlib import PureWindowsPath
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentPrincipal, require_roles, tenant_tx
from app.core.security import Principal
from app.services.access import READABLE, access_params, require_manage
from app.services.audit import write_audit
from app.services.ingestion import ingest_document

router = APIRouter(prefix="/documents", tags=["documents"])

MAX_UPLOAD_BYTES = 500_000
ALLOWED_SUFFIXES = (".txt", ".md")


class DocumentOut(BaseModel):
    id: UUID
    title: str
    filename: str | None
    status: str
    created_by: UUID
    created_at: datetime


class PermissionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: UUID
    permission: Literal["read", "write"]


class PermissionOut(BaseModel):
    user_id: UUID
    permission: str
    granted_by: UUID
    created_at: datetime


@router.post("", status_code=202, response_model=DocumentOut)
def upload_document(
    background: BackgroundTasks,
    principal: Annotated[Principal, Depends(require_roles("admin", "editor"))],
    title: Annotated[str, Form(min_length=1, max_length=200)],
    file: UploadFile,
) -> dict:
    filename = PureWindowsPath(file.filename or "upload").name[:255]
    if not filename.lower().endswith(ALLOWED_SUFFIXES):
        raise HTTPException(status_code=415, detail="Only .txt and .md files are supported")
    raw = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large")
    try:
        body_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail="File must be UTF-8 text") from None
    if "\x00" in body_text or not body_text.strip():
        raise HTTPException(status_code=422, detail="File is empty or not plain text")

    with tenant_tx(principal) as session:
        row = session.execute(
            text(
                "INSERT INTO documents (tenant_id, title, filename, created_by) "
                "VALUES (:t, :title, :fn, :uid) "
                "RETURNING id, title, filename, status, created_by, created_at"
            ),
            {"t": principal.tenant_id, "title": title.strip(), "fn": filename, "uid": principal.user_id},
        ).mappings().one()
        doc = dict(row)
        write_audit(
            session, tenant_id=principal.tenant_id, user_id=principal.user_id,
            action="document.upload", resource_type="document", resource_id=doc["id"],
            details={"filename": filename, "bytes": len(raw)},
        )
    # The transaction above has COMMITTED, so the background task can see the row.
    background.add_task(ingest_document, principal.tenant_id, doc["id"], body_text)
    return doc


@router.get("", response_model=list[DocumentOut])
def list_documents(principal: CurrentPrincipal) -> list[dict]:
    with tenant_tx(principal) as session:
        rows = session.execute(
            text(
                "SELECT d.id, d.title, d.filename, d.status, d.created_by, d.created_at "
                f"FROM documents d WHERE {READABLE} ORDER BY d.created_at DESC LIMIT 200"
            ),
            access_params(principal),
        ).mappings().all()
        return [dict(r) for r in rows]


@router.delete("/{document_id}", status_code=204)
def delete_document(document_id: UUID, principal: CurrentPrincipal) -> None:
    with tenant_tx(principal) as session:
        require_manage(session, principal, document_id)
        session.execute(text("DELETE FROM documents WHERE id = :id"), {"id": document_id})
        write_audit(
            session, tenant_id=principal.tenant_id, user_id=principal.user_id,
            action="document.delete", resource_type="document", resource_id=document_id,
        )


@router.get("/{document_id}/permissions", response_model=list[PermissionOut])
def list_permissions(document_id: UUID, principal: CurrentPrincipal) -> list[dict]:
    with tenant_tx(principal) as session:
        require_manage(session, principal, document_id)
        rows = session.execute(
            text(
                "SELECT user_id, permission, granted_by, created_at "
                "FROM document_permissions WHERE document_id = :id ORDER BY created_at"
            ),
            {"id": document_id},
        ).mappings().all()
        return [dict(r) for r in rows]


@router.post("/{document_id}/permissions", response_model=PermissionOut)
def grant_permission(document_id: UUID, body: PermissionIn, principal: CurrentPrincipal) -> dict:
    with tenant_tx(principal) as session:
        require_manage(session, principal, document_id)
        try:
            row = session.execute(
                text(
                    "INSERT INTO document_permissions "
                    "(tenant_id, document_id, user_id, permission, granted_by) "
                    "VALUES (:t, :doc, :user, :perm, :by) "
                    "ON CONFLICT (document_id, user_id) DO UPDATE "
                    "SET permission = EXCLUDED.permission, granted_by = EXCLUDED.granted_by "
                    "RETURNING user_id, permission, granted_by, created_at"
                ),
                {"t": principal.tenant_id, "doc": document_id, "user": body.user_id,
                 "perm": body.permission, "by": principal.user_id},
            ).mappings().one()
        except IntegrityError:
            # Composite FK: the user must belong to THIS tenant.
            raise HTTPException(status_code=422, detail="User not found in this tenant") from None
        write_audit(
            session, tenant_id=principal.tenant_id, user_id=principal.user_id,
            action="permission.grant", resource_type="document", resource_id=document_id,
            details={"user_id": str(body.user_id), "permission": body.permission},
        )
        return dict(row)


@router.delete("/{document_id}/permissions/{user_id}", status_code=204)
def revoke_permission(document_id: UUID, user_id: UUID, principal: CurrentPrincipal) -> None:
    with tenant_tx(principal) as session:
        require_manage(session, principal, document_id)
        deleted = session.execute(
            text("DELETE FROM document_permissions WHERE document_id = :doc AND user_id = :user"),
            {"doc": document_id, "user": user_id},
        ).rowcount
        if deleted == 0:
            raise HTTPException(status_code=404, detail="Permission not found")
        write_audit(
            session, tenant_id=principal.tenant_id, user_id=principal.user_id,
            action="permission.revoke", resource_type="document", resource_id=document_id,
            details={"user_id": str(user_id)},
        )