from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import CurrentPrincipal, tenant_tx
from app.services.audit import write_audit
from app.services.embeddings import embed_texts
from app.services.retriever import retrieve

router = APIRouter(prefix="/search", tags=["search"])


class SearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=1000)
    k: int = Field(default=5, ge=1, le=20)


class SearchHit(BaseModel):
    chunk_id: UUID
    document_id: UUID
    title: str
    chunk_index: int
    content: str
    score: float


class SearchOut(BaseModel):
    hits: list[SearchHit]


@router.post("", response_model=SearchOut)
def search(body: SearchIn, principal: CurrentPrincipal) -> dict:
    qvec = embed_texts([body.query])[0]  # embed before opening the DB transaction
    with tenant_tx(principal) as session:
        hits = retrieve(session, principal, qvec, body.k)
        write_audit(
            session, tenant_id=principal.tenant_id, user_id=principal.user_id, action="search",
            details={"k": body.k, "results": len(hits),
                     "document_ids": sorted({str(h["document_id"]) for h in hits})},
        )
    return {"hits": hits}