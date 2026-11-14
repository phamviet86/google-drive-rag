"""Hybrid retrieval: FTS5 (accent-folded) + numpy cosine over stored vectors, fused with RRF.

Fusion and the vector-matrix cache follow local-rag-mcp ``search.py`` (Apache-2.0). Vectors are
used only when the stored embedding identity matches the configured client, so vectors from two
models are never mixed; otherwise search is FTS only.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import numpy as np

from .db import TERM, Database, fts_fold
from .embeddings import EmbeddingClient

RRF_K = 60
SNIPPET_CHARS = 800
PER_DOCUMENT = 3
EMBEDDING_IDENTITY_KEY = "embedding_identity"
REVISION_KEY = "index_revision"


class SearchEngine:
    def __init__(self, db: Database, embeddings: Callable[[], EmbeddingClient | None]):
        self.db = db
        self.embeddings = embeddings
        self._lock = threading.Lock()
        self._matrix: tuple[str, list[int], Any] | None = None

    def search(self, query: str, source: str | None = None, limit: int = 8) -> dict[str, Any]:
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        terms = TERM.findall(query)
        if not terms:
            raise ValueError("query must contain at least one word")
        warnings: list[str] = []
        source_id = self._source_id(source) if source else None
        pool = max(limit * 5, 40)
        lexical = self._full_text(terms, source_id, pool)
        semantic: list[tuple[int, float]] = []
        client = self.embeddings()
        mode = "full_text"
        if client is not None:
            if self.db.get_meta(EMBEDDING_IDENTITY_KEY) != client.identity:
                warnings.append("vectors are being rebuilt for the configured model; FTS only")
            else:
                try:
                    semantic = self._semantic(client, query, source_id, pool)
                    mode = "hybrid"
                except Exception as exc:  # network/key problems must not break search
                    warnings.append(f"semantic search unavailable ({exc}); FTS only")
        scores: dict[int, float] = {}
        for rank, chunk_id in enumerate(lexical, 1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
        for rank, (chunk_id, _) in enumerate(semantic, 1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
        ordered = sorted(scores, key=lambda chunk_id: scores[chunk_id], reverse=True)
        results = self._rows(ordered, scores, limit)
        return {"mode": mode, "warnings": warnings, "results": results}

    def _source_id(self, value: str) -> int:
        with self.db.read() as connection:
            row = connection.execute(
                "SELECT id FROM sources WHERE CAST(id AS TEXT)=? OR name=? OR root_id=? "
                "ORDER BY id LIMIT 1",
                (value, value, value),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown source: {value}")
        return int(row[0])

    def _full_text(self, terms: list[str], source_id: int | None, pool: int) -> list[int]:
        folded = [f'"{fts_fold(term)}"' for term in terms]
        found: list[int] = []
        with self.db.read() as connection:
            for joiner in (" AND ", " OR "):
                sql = (
                    "SELECT f.rowid FROM chunks_fts f JOIN chunks c ON c.id=f.rowid "
                    "WHERE chunks_fts MATCH ?"
                )
                values: list[Any] = [joiner.join(folded)]
                if source_id is not None:
                    sql += (
                        " AND c.document_id IN "
                        "(SELECT document_id FROM document_sources WHERE source_id=?)"
                    )
                    values.append(source_id)
                sql += " ORDER BY bm25(chunks_fts, 1.0, 0.5) LIMIT ?"
                values.append(pool)
                for row in connection.execute(sql, values):
                    if int(row[0]) not in found:
                        found.append(int(row[0]))
                if len(terms) == 1:
                    break
        return found[:pool]

    def _semantic(
        self, client: EmbeddingClient, query: str, source_id: int | None, pool: int
    ) -> list[tuple[int, float]]:
        ids, matrix = self._load_matrix()
        if not ids:
            return []
        vector = np.asarray(client.embed_query(query), dtype=np.float32)
        if vector.shape[0] != matrix.shape[1]:
            raise RuntimeError("query vector has a different dimension than stored vectors")
        scores = matrix @ vector
        allowed: set[int] | None = None
        if source_id is not None:
            with self.db.read() as connection:
                allowed = {
                    int(row[0])
                    for row in connection.execute(
                        "SELECT c.id FROM chunks c JOIN document_sources s "
                        "ON s.document_id=c.document_id WHERE s.source_id=?",
                        (source_id,),
                    )
                }
        order = np.argsort(-scores)
        output: list[tuple[int, float]] = []
        for index in order:
            chunk_id = ids[int(index)]
            if allowed is not None and chunk_id not in allowed:
                continue
            output.append((chunk_id, float(scores[int(index)])))
            if len(output) >= pool:
                break
        return output

    def _load_matrix(self) -> tuple[list[int], Any]:
        revision = self.db.get_meta(REVISION_KEY) or "0"
        with self._lock:
            if self._matrix is not None and self._matrix[0] == revision:
                return self._matrix[1], self._matrix[2]
            with self.db.read() as connection:
                rows = connection.execute(
                    "SELECT c.id, v.vector FROM chunks c "
                    "JOIN vectors v ON v.chunk_hash=c.chunk_hash ORDER BY c.id"
                ).fetchall()
            ids = [int(row[0]) for row in rows]
            if rows:
                matrix = np.stack([np.frombuffer(row[1], dtype="<f4") for row in rows])
            else:
                matrix = np.zeros((0, 0), dtype=np.float32)
            self._matrix = (revision, ids, matrix)
            return ids, matrix

    def _rows(
        self, ordered: list[int], scores: dict[int, float], limit: int
    ) -> list[dict[str, Any]]:
        if not ordered:
            return []
        output: list[dict[str, Any]] = []
        per_document: dict[int, int] = {}
        with self.db.read() as connection:
            for start in range(0, len(ordered), 200):
                batch = ordered[start : start + 200]
                marks = ",".join("?" for _ in batch)
                rows = {
                    int(row["id"]): row
                    for row in connection.execute(
                        f"""SELECT c.id, c.ordinal, c.text, c.locator, c.document_id,
                                   d.file_id, d.name, d.path, d.web_url, d.chunk_count,
                                   (SELECT group_concat(s.name, ', ') FROM document_sources ds
                                    JOIN sources s ON s.id=ds.source_id
                                    WHERE ds.document_id=d.id) source_names
                            FROM chunks c JOIN documents d ON d.id=c.document_id
                            WHERE c.id IN ({marks})""",
                        batch,
                    )
                }
                for chunk_id in batch:
                    row = rows.get(chunk_id)
                    if row is None:
                        continue
                    document = int(row["document_id"])
                    if per_document.get(document, 0) >= PER_DOCUMENT:
                        continue
                    per_document[document] = per_document.get(document, 0) + 1
                    text = str(row["text"])
                    output.append(
                        {
                            "document_id": row["file_id"],
                            "file_name": row["name"],
                            "path": row["path"],
                            "locator": row["locator"],
                            "chunk": int(row["ordinal"]),
                            "chunk_count": int(row["chunk_count"]),
                            "drive_link": row["web_url"],
                            "sources": row["source_names"] or "",
                            "score": round(scores[chunk_id], 6),
                            "snippet": text
                            if len(text) <= SNIPPET_CHARS
                            else text[:SNIPPET_CHARS].rstrip() + " …",
                        }
                    )
                    if len(output) >= limit:
                        return output
        return output
