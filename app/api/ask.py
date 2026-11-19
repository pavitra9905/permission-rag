from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import CurrentPrincipal, tenant_tx
from app.services.audit import write_audit
from app.services.embeddings import embed_texts
from app.services.llm import LLMUnavailable, generate_answer
from app.services.rag import NO_ANSWER, SYSTEM_PROMPT, build_user_prompt, cited_numbers
from app.services.retriever import retrieve

router = APIRouter(prefix="/ask", tags=["ask"])


class AskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=1000)
    k: int = Field(default=5, ge=1, le=10)


class Source(BaseModel):
    n: int
    document_id: UUID
    title: str
    chunk_index: int
    score: float
    snippet: str
    cited: bool


class AskOut(BaseModel):
    answer: str
    sources: list[Source]


@router.post("", response_model=AskOut)
def ask(body: AskIn, principal: CurrentPrincipal) -> dict:
    qvec = embed_texts([body.question])[0]

    # 1) Retrieve under the tenant + permission rules, then CLOSE the transaction.
    with tenant_tx(principal) as session:
        hits = retrieve(session, principal, qvec, body.k)

    # 2) Call Claude with no DB connection held. Skip it entirely if nothing is readable.
    answer, llm_ok = NO_ANSWER, True
    if hits:
        try:
            answer = generate_answer(
                system=SYSTEM_PROMPT, user=build_user_prompt(body.question, hits)
            )
        except LLMUnavailable:
            llm_ok = False

    # 3) Audit in a fresh transaction (IDs only: never the question or answer text).
    with tenant_tx(principal) as session:
        write_audit(
            session, tenant_id=principal.tenant_id, user_id=principal.user_id, action="ask",
            details={
                "results": len(hits),
                "document_ids": sorted({str(h["document_id"]) for h in hits}),
                "answered": bool(hits) and llm_ok,
            },
        )

    if not llm_ok:
        raise HTTPException(status_code=503, detail="The answer service is unavailable right now")

    cited = cited_numbers(answer, len(hits))
    return {
        "answer": answer,
        "sources": [
            {
                "n": i, "document_id": h["document_id"], "title": h["title"],
                "chunk_index": h["chunk_index"], "score": h["score"],
                "snippet": h["content"][:300], "cited": i in cited,
            }
            for i, h in enumerate(hits, start=1)
        ],
    }