"""Hybrid retrieval: FTS5 (accent-folded) + numpy cosine over stored vectors.

FTS finds candidates; they are re-scored by rarity-weighted coverage of the query terms plus
adjacency (``ranking.py``). Fusion is score-aware: ``LEXICAL_WEIGHT`` x lexical score (0..1) plus
the rest x cosine scaled within the vector list. Rank-only RRF let chunks that were mediocre in
both lists beat one strong lexical match (see ``tests/test_ranking_eval.py``).

The vector-matrix cache follows local-rag-mcp ``search.py`` (Apache-2.0). Vectors are
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
from .ranking import idf, lexical_score, tokens

CANDIDATES = 200  # FTS candidates re-scored per query
LEXICAL_WEIGHT = 0.7  # chosen with tests/test_ranking_eval.py; the vectors keep 0.3
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
        explain: bool = False,
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
        scores = fuse(lexical, semantic)
        ordered = sorted(scores, key=lambda chunk_id: scores[chunk_id], reverse=True)
        results = self._rows(ordered, scores, limit)
        if explain:
            fts = {c: (rank, score, bm25) for rank, (c, score, bm25) in enumerate(lexical, 1)}
            vec = {c: (rank, score) for rank, (c, score) in enumerate(semantic, 1)}
            for hit in results:
                chunk_id = hit["chunk_id"]
                lexical_hit = fts.get(chunk_id, (None, None, None))
                vector_hit = vec.get(chunk_id, (None, None))
                hit["explain"] = {
                    "fts_rank": lexical_hit[0],
                    "fts_score": _round(lexical_hit[1]),
                    "bm25": _round(lexical_hit[2]),
                    "vector_rank": vector_hit[0],
                    "vector_score": _round(vector_hit[1]),
                    "fused": hit["score"],
                }
        for hit in results:
            del hit["chunk_id"]
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

    def _full_text(
        self, terms: list[str], scope: tuple[str, list[Any]], pool: int
    ) -> list[tuple[int, float, float]]:
        """``(chunk id, lexical score 0..1, bm25)``, best first. FTS (all terms, then any term)
        finds up to ``CANDIDATES`` chunks; they are re-scored by rarity-weighted coverage of the
        distinct query terms plus adjacency (``ranking.lexical_score``), bm25 breaking ties."""
        folded = [f'"{fts_fold(term)}"' for term in terms]
        found: dict[int, float] = {}
        with self.db.read() as connection:
            for joiner in (" AND ", " OR "):
                sql = (
                    "SELECT f.rowid, bm25(chunks_fts, 1.0, 0.5) FROM chunks_fts f "
                    "JOIN chunks c ON c.id=f.rowid "
                    "WHERE chunks_fts MATCH ?"
                )
                values: list[Any] = [joiner.join(folded)]
                if scope[0]:
                    sql += f" AND {scope[0]}"
                    values.extend(scope[1])
                sql += " ORDER BY bm25(chunks_fts, 1.0, 0.5) LIMIT ?"
                values.append(CANDIDATES)
                for row in connection.execute(sql, values):
                    found.setdefault(int(row[0]), float(row[1]))
                if len(terms) == 1:
                    break
            if not found:
                return []
            query = tokens(" ".join(terms))
            total = int(connection.execute("SELECT count(*) FROM chunks").fetchone()[0])
            weights = {
                term: idf(
                    int(
                        connection.execute(
                            "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?",
                            (f'"{term}"',),
                        ).fetchone()[0]
                    ),
                    total,
                )
                for term in dict.fromkeys(query)
            }
            ids = list(found)
            texts = {
                int(row[0]): f"{row[1]}\n{row[2]}"
                for start in range(0, len(ids), 500)
                for row in connection.execute(
                    "SELECT c.id, d.name, c.text FROM chunks c "
                    "JOIN documents d ON d.id=c.document_id WHERE c.id IN "
                    f"({','.join('?' for _ in ids[start : start + 500])})",
                    ids[start : start + 500],
                )
            }
        scored = [(c, lexical_score(query, weights, texts.get(c, "")), b) for c, b in found.items()]
        scored.sort(key=lambda item: (-item[1], item[2]))
        return scored[:pool]

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
                               d.file_id, d.name, d.path, d.web_url, d.chunk_count,
                               (SELECT folder_id FROM folders
                                WHERE folder_id=d.parent_id) parent_id,
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
            "chunk_id": int(row["id"]),
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


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def fuse(
    lexical: list[tuple[int, float, float]], semantic: list[tuple[int, float]]
) -> dict[int, float]:
    """Relative score fusion of the lexical list ``(id, score 0..1, bm25)`` and the vector list
    ``(id, cosine)``: the lexical score is used as is (it is comparable across queries), the
    cosine as a share of the best cosine. Min-max scaling was tried and rejected: in a narrow
    ``files``/``folder`` scope with a handful of chunks it turned small cosine gaps into 0 and 1."""
    scores = {chunk_id: LEXICAL_WEIGHT * score for chunk_id, score, _ in lexical}
    high = semantic[0][1] if semantic else 0.0
    for chunk_id, cosine in semantic:
        scaled = max(0.0, cosine) / high if high > 0 else 0.0
        scores[chunk_id] = scores.get(chunk_id, 0.0) + (1 - LEXICAL_WEIGHT) * scaled
    return scores
