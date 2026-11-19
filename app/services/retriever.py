from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.security import Principal
from app.services.access import READABLE, access_params

SEARCH_SQL = text(
    f"""
WITH hits AS MATERIALIZED (
    SELECT c.id AS chunk_id, d.id AS document_id, d.title, c.chunk_index, c.content,
           c.embedding <=> CAST(:qvec AS vector) AS distance
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    WHERE d.status = 'ready' AND {READABLE}
    ORDER BY c.embedding <=> CAST(:qvec AS vector)
    LIMIT :k
)
SELECT * FROM hits ORDER BY distance
"""
)


def retrieve(session: Session, principal: Principal, query_vec: list[float], k: int = 5) -> list[dict]:
    """Top-k chunks the caller may read. Tenant isolation = RLS; document access = READABLE.
    Both apply BEFORE the LIMIT, so a hit can never come from a document the caller can't read."""
    # Keep scanning the HNSW index until k rows pass the filters (pgvector >= 0.8).
    session.execute(
        text("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true), "
             "set_config('hnsw.ef_search', '100', true)")
    )
    qvec = "[" + ",".join(f"{x:.6f}" for x in query_vec) + "]"
    rows = session.execute(SEARCH_SQL, {**access_params(principal), "qvec": qvec, "k": k}).mappings()
    return [{**r, "score": 1.0 - float(r["distance"])} for r in rows]