"""Hybrid retrieval: FTS5 (accent-folded) + numpy cosine over stored vectors, fused with RRF.

Fusion and the vector-matrix cache follow local-rag-mcp ``search.py`` (Apache-2.0). Vectors are
used only when the stored embedding identity matches the configured client, so vectors from two
models are never mixed; otherwise search is FTS only.

Scope filters (source, folder with all subfolders, specific files) are applied to both the FTS
and the vector candidates before fusion, so a narrow scope still fills ``limit``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import numpy as np

from .db import TERM, Database, fts_fold
from .drive import parse_drive_link
from .embeddings import EmbeddingClient

RRF_K = 60
SNIPPET_CHARS = 800
PER_DOCUMENT = 3  # soft: lifted when fewer than ``limit`` hits would remain
MAX_FILES = 50
EMBEDDING_IDENTITY_KEY = "embedding_identity"
REVISION_KEY = "index_revision"


class SearchEngine:
    def __init__(self, db: Database, embeddings: Callable[[], EmbeddingClient | None]):
        self.db = db
        self.embeddings = embeddings
        self._lock = threading.Lock()
        self._matrix: tuple[str, list[int], Any] | None = None

    def search(
        self,
        query: str,
        source: str | None = None,
        limit: int = 8,
        folder: str | None = None,
        files: list[str] | None = None,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        terms = TERM.findall(query)
        if not terms:
            raise ValueError("query must contain at least one word")
        warnings: list[str] = []
        scope = self._scope(source, folder, files)
        pool = max(limit * 5, 40)
        lexical = self._full_text(terms, scope, pool)
        semantic: list[tuple[int, float]] = []
        client = self.embeddings()
        mode = "full_text"
        if client is not None:
            if self.db.get_meta(EMBEDDING_IDENTITY_KEY) != client.identity:
                warnings.append("vectors are being rebuilt for the configured model; FTS only")
            else:
                try:
                    semantic = self._semantic(client, query, scope, pool)
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

    def source_id(self, value: str) -> int:
        with self.db.read() as connection:
            row = connection.execute(
                "SELECT id FROM sources WHERE CAST(id AS TEXT)=? OR name=? OR root_id=? "
                "ORDER BY id LIMIT 1",
                (value, value, value),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown source: {value}")
        return int(row[0])

    def _scope(
        self, source: str | None, folder: str | None, files: list[str] | None
    ) -> tuple[str, list[Any]]:
        """SQL condition on ``c.document_id`` (``""`` for no scope) and its parameters.
        An unknown source, folder or file is an error naming it, never an empty result."""
        conditions: list[str] = []
        values: list[Any] = []
        if source:
            conditions.append(
                "c.document_id IN (SELECT document_id FROM document_sources WHERE source_id=?)"
            )
            values.append(self.source_id(source))
        with self.db.read() as connection:
            if folder:
                folder_id = parse_drive_link(folder)
                if not connection.execute(
                    "SELECT 1 FROM folders WHERE folder_id=?", (folder_id,)
                ).fetchone():
                    raise ValueError(f"unknown or not indexed folder: {folder}")
                conditions.append(
                    "c.document_id IN (SELECT document_id FROM document_folders WHERE folder_id=?)"
                )
                values.append(folder_id)
            if files:
                if len(files) > MAX_FILES:
                    raise ValueError(f"at most {MAX_FILES} files can be given")
                ids: list[int] = []
                for value in files:
                    row = connection.execute(
                        "SELECT id, name, status, error FROM documents WHERE file_id=?",
                        (parse_drive_link(value),),
                    ).fetchone()
                    if row is None:
                        raise ValueError(f"unknown or not indexed file: {value}")
                    if row["status"] != "indexed":
                        raise ValueError(f"file {row['name']} is not indexed: {row['error']}")
                    ids.append(int(row["id"]))
                conditions.append(f"c.document_id IN ({','.join('?' for _ in ids)})")
                values.extend(ids)
        return " AND ".join(conditions), values

    def _full_text(self, terms: list[str], scope: tuple[str, list[Any]], pool: int) -> list[int]:
        folded = [f'"{fts_fold(term)}"' for term in terms]
        found: list[int] = []
        with self.db.read() as connection:
            for joiner in (" AND ", " OR "):
                sql = (
                    "SELECT f.rowid FROM chunks_fts f JOIN chunks c ON c.id=f.rowid "
                    "WHERE chunks_fts MATCH ?"
                )
                values: list[Any] = [joiner.join(folded)]
                if scope[0]:
                    sql += f" AND {scope[0]}"
                    values.extend(scope[1])
                sql += " ORDER BY bm25(chunks_fts, 1.0, 0.5) LIMIT ?"
                values.append(pool)
                for row in connection.execute(sql, values):
                    if int(row[0]) not in found:
                        found.append(int(row[0]))
                if len(terms) == 1:
                    break
        return found[:pool]

    def _semantic(
        self, client: EmbeddingClient, query: str, scope: tuple[str, list[Any]], pool: int
    ) -> list[tuple[int, float]]:
        ids, matrix = self._load_matrix()
        if not ids:
            return []
        vector = np.asarray(client.embed_query(query), dtype=np.float32)
        if vector.shape[0] != matrix.shape[1]:
            raise RuntimeError("query vector has a different dimension than stored vectors")
        scores = matrix @ vector
        allowed: set[int] | None = None
        if scope[0]:
            with self.db.read() as connection:
                allowed = {
                    int(row[0])
                    for row in connection.execute(
                        f"SELECT c.id FROM chunks c WHERE {scope[0]}", scope[1]
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
        rows: dict[int, Any] = {}
        with self.db.read() as connection:
            for start in range(0, len(ordered), 200):
                batch = ordered[start : start + 200]
                marks = ",".join("?" for _ in batch)
                for row in connection.execute(
                    f"""SELECT c.id, c.ordinal, c.text, c.locator, c.document_id,
                               d.file_id, d.name, d.path, d.web_url, d.chunk_count, d.parent_id,
                               (SELECT group_concat(s.name, ', ') FROM document_sources ds
                                JOIN sources s ON s.id=ds.source_id
                                WHERE ds.document_id=d.id) source_names
                        FROM chunks c JOIN documents d ON d.id=c.document_id
                        WHERE c.id IN ({marks})""",
                    batch,
                ):
                    rows[int(row["id"])] = row
        present = [chunk_id for chunk_id in ordered if chunk_id in rows]
        chosen: list[int] = []
        per_document: dict[int, int] = {}
        for chunk_id in present:  # at most PER_DOCUMENT hits per file first ...
            document = int(rows[chunk_id]["document_id"])
            if per_document.get(document, 0) < PER_DOCUMENT and len(chosen) < limit:
                per_document[document] = per_document.get(document, 0) + 1
                chosen.append(chunk_id)
        if len(chosen) < limit:  # ... then fill from the same files when the scope is narrow
            taken = set(chosen)
            chosen += [c for c in present if c not in taken][: limit - len(chosen)]
            chosen.sort(key=lambda chunk_id: scores[chunk_id], reverse=True)
        return [self._hit(rows[chunk_id], scores[chunk_id]) for chunk_id in chosen]

    @staticmethod
    def _hit(row: Any, score: float) -> dict[str, Any]:
        text = str(row["text"])
        return {
            "document_id": row["file_id"],
            "file_name": row["name"],
            "path": row["path"],
            "parent_folder_id": row["parent_id"],
            "locator": row["locator"],
            "chunk": int(row["ordinal"]),
            "chunk_count": int(row["chunk_count"]),
            "drive_link": row["web_url"],
            "sources": row["source_names"] or "",
            "score": round(score, 6),
            "snippet": text if len(text) <= SNIPPET_CHARS else text[:SNIPPET_CHARS].rstrip() + " …",
        }
