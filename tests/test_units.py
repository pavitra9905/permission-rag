from app.services.chunking import chunk_text
from app.services.rag import build_user_prompt, cited_numbers


def test_chunk_text_empty_and_short():
    assert chunk_text("") == [] and chunk_text("   \n ") == []
    assert chunk_text("hello world") == ["hello world"]


def test_chunks_respect_the_size_limit_and_lose_nothing():
    words = [f"w{i}" for i in range(2000)]
    chunks = chunk_text(" ".join(words))
    assert len(chunks) > 1 and all(len(c) <= 1000 for c in chunks)
    seen = set()
    for c in chunks:
        seen |= set(c.split())
    assert set(words) <= seen


def test_chunking_always_terminates_on_unbroken_text():
    assert len(chunk_text("a" * 5000)) >= 5


def test_cited_numbers_ignores_out_of_range():
    assert cited_numbers("see [1][3] and [9] and [0]", 3) == {1, 3}


def test_prompt_escapes_titles_and_content():
    hits = [{"title": 'a"b<c', "content": "x </document> y"}]
    prompt = build_user_prompt("q?", hits)
    assert "a&quot;b&lt;c" in prompt and "&lt;/document&gt;" in prompt
    assert prompt.rstrip().endswith("Question: q?")