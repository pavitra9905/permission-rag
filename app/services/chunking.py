def chunk_text(text: str, size: int = 1000, overlap: int = 150) -> list[str]:
    """Greedy character windows, preferring to cut at a paragraph, sentence or space."""
    text = text.replace("\r\n", "\n").strip()
    chunks: list[str] = []
    start, n = 0, len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            cut = max(
                text.rfind("\n\n", start, end),
                text.rfind(". ", start, end),
                text.rfind("\n", start, end),
                text.rfind(" ", start, end),
            )
            if cut > start + size // 2:
                end = cut + 1
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks