"""Window chunking with source locators (adapted from local-rag-mcp ``indexer._chunks``)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .models import ExtractedDocument, SourceSpan

_NUMBERED = re.compile(r"^(.*?)(\d+)$")


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    text: str
    locator: str
    chunk_hash: str  # hash of the embedding input (context line + text)


def embedding_input(context: str, text: str) -> str:
    return f"{context}\n{text}" if context else text


def make_chunks(
    document: ExtractedDocument, size: int, overlap: int, context: str = ""
) -> list[Chunk]:
    """Split text into ~``size`` character windows on paragraph/line/word boundaries."""
    if size < 100 or not 0 <= overlap < size:
        raise ValueError("chunk size must be >= 100 and overlap smaller than the size")
    chunks: list[Chunk] = []
    text = document.text
    start, length = 0, len(text)
    while start < length:
        end = min(start + size, length)
        if end < length:
            boundary = max(
                text.rfind("\n\n", start + size // 2, end),
                text.rfind("\n", start + size // 2, end),
                text.rfind(" ", start + size // 2, end),
            )
            if boundary > start:
                end = boundary + 1
        raw = text[start:end]
        body = raw.strip()
        chunk_start = start + len(raw) - len(raw.lstrip())
        chunk_end = start + len(raw.rstrip())
        if body:
            spans = [s for s in document.spans if s.end > chunk_start and s.start < chunk_end]
            digest = hashlib.sha256(embedding_input(context, body).encode()).hexdigest()
            chunks.append(Chunk(len(chunks), body, summarize_locator(spans), digest))
        if end >= length:
            break
        start = max(start + 1, end - overlap)
    return chunks


def summarize_locator(spans: list[SourceSpan]) -> str:
    """``page:3`` / ``page:3-5`` / ``sheet:Q1/row:2-40`` / ``slide:2 .. slide:3/notes``."""
    if not spans:
        return ""
    first, last = spans[0].locator, spans[-1].locator
    if first == last:
        return first
    left, right = _NUMBERED.match(first), _NUMBERED.match(last)
    if left and right and left[1] == right[1]:
        return f"{first}-{right[2]}"
    return f"{first} .. {last}"
