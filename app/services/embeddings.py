import threading
from functools import lru_cache

from app.core.constants import EMBEDDING_DIM, EMBEDDING_MODEL_NAME

_lock = threading.Lock()


@lru_cache(maxsize=1)
def _model():
    from sentence_transformers import SentenceTransformer  # slow import, so do it lazily

    model = SentenceTransformer(EMBEDDING_MODEL_NAME)
    dim_fn = getattr(model, "get_embedding_dimension", None) or model.get_sentence_embedding_dimension
    dim = dim_fn()
    if dim != EMBEDDING_DIM:
        raise RuntimeError(f"Embedding model gives {dim} dims, schema expects {EMBEDDING_DIM}")
    return model


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Normalized vectors (cosine distance in pgvector then behaves well)."""
    with _lock:
        vectors = _model().encode(
            texts, batch_size=32, normalize_embeddings=True, show_progress_bar=False
        )
    return vectors.tolist()