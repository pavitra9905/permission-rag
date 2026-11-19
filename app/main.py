from fastapi import FastAPI

from app.api import ask, auth, documents, me, search

app = FastAPI(title="Permission-aware RAG", version="0.3.0")
app.include_router(auth.router)
app.include_router(me.router)
app.include_router(documents.router)
app.include_router(search.router)
app.include_router(ask.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}