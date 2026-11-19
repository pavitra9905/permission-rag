import logging
from uuid import UUID

from sqlalchemy import insert, text

from app.db.models import Chunk
from app.db.session import db_session
from app.services.audit import write_audit
from app.services.chunking import chunk_text
from app.services.embeddings import embed_texts

log = logging.getLogger(__name__)


def _set_status(tenant_id: UUID, document_id: UUID, status: str, error: str | None = None) -> None:
    with db_session(tenant_id) as s:
        s.execute(
            text("UPDATE documents SET status = :st, error = :err WHERE id = :id"),
            {"st": status, "err": error, "id": document_id},
        )


def ingest_document(tenant_id: UUID, document_id: UUID, body_text: str) -> None:
    """Runs as a FastAPI background task. Never raises."""
    try:
        _set_status(tenant_id, document_id, "processing")
        pieces = chunk_text(body_text)
        if not pieces:
            raise ValueError("no text to index")
        vectors = embed_texts(pieces)  # CPU-heavy: done OUTSIDE any DB transaction
        rows = [
            {
                "tenant_id": tenant_id,
                "document_id": document_id,
                "chunk_index": i,
                "content": piece,
                "embedding": vec,
            }
            for i, (piece, vec) in enumerate(zip(pieces, vectors))
        ]
        with db_session(tenant_id) as s:
            s.execute(insert(Chunk), rows)
            s.execute(
                text("UPDATE documents SET status = 'ready', error = NULL WHERE id = :id"),
                {"id": document_id},
            )
            write_audit(
                s, tenant_id=tenant_id, user_id=None, action="document.ingested",
                resource_type="document", resource_id=document_id, details={"chunks": len(rows)},
            )
    except Exception:
        log.exception("ingestion failed for document %s", document_id)
        try:
            _set_status(tenant_id, document_id, "failed", "Ingestion failed")
        except Exception:
            log.exception("could not mark document %s as failed", document_id)