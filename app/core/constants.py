EMBEDDING_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384  # fixed forever; changing it needs a new migration + re-embedding

VALID_ROLES = frozenset({"admin", "editor", "viewer"})