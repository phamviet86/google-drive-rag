"""Hybrid retrieval: FTS5 (accent-folded) + numpy cosine over stored vectors.

Weighted reciprocal-rank fusion of four signals (``WEIGHTS``):

- text: FTS candidates ordered by rarity-weighted coverage of the query terms plus adjacency
  (``ranking.lexical_score``), so "tháng", "là", "bao nhiêu" and table repetition count little;
- vector: cosine order of the stored embeddings;
- title: the same coverage over the file name and folder path ("20.3.1_bao-cao-2026" gives
  bao, cao, 2026), which names the topic of spreadsheets whose rows are only numbers; all
  chunks of one file share one rank;
- phrase: chunks that contain the whole query as a phrase (short keywords, codes).

Tuned on a copy of a real index with live query vectors (``scripts/eval_ranking.py``); a
lexical-heavy score fusion tried before lost there to plain RRF, because real embeddings are
right more often than a bag-of-words fake suggests. ``tests/test_ranking_eval.py`` guards it.

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
from .ranking import contains_phrase, idf, lexical_score, title_score, tokens

CANDIDATES = 200  # FTS candidates re-scored per query
RRF_K = 60
WEIGHTS = {"text": 1.0, "vector": 1.0, "title": 2.0, "phrase": 2.0}
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
        lexical, phrases, query_terms, weights = self._full_text(terms, scope, pool)
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
        candidates = {c for c, _, _ in lexical} | {c for c, _ in semantic}
        titles = self._title_scores(candidates, query_terms, weights)
        scores = fuse(lexical, semantic, titles, phrases)
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
                    "title_score": _round(titles.get(chunk_id)),
                    "phrase": chunk_id in phrases,
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
    ) -> tuple[list[tuple[int, float, float]], set[int], list[str], dict[str, float]]:
        """FTS (all terms, then any term) finds up to ``CANDIDATES`` chunks, re-scored by
        ``ranking.lexical_score`` with bm25 breaking ties. Returns ``(chunk id, lexical score,
        bm25)`` best first, the chunks containing the query as a phrase, the folded query terms
        and their rarity weights."""
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
            query = tokens(" ".join(terms))
            if not found:
                return [], set(), query, {}
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
        phrases = {c for c in found if contains_phrase(query, texts.get(c, ""))}
        scored.sort(key=lambda item: (-item[1], item[2]))
        return scored[:pool], phrases, query, weights

    def _title_scores(
        self, chunk_ids: set[int], query: list[str], weights: dict[str, float]
    ) -> dict[int, float]:
        """Rarity-weighted share of the query terms found in each chunk's file name and folder
        path ("20.3.1_bao-cao-2026" -> bao, cao, 2026)."""
        if not chunk_ids or not weights:
            return {}
        ids = list(chunk_ids)
        output: dict[int, float] = {}
        with self.db.read() as connection:
            for start in range(0, len(ids), 500):
                batch = ids[start : start + 500]
                for row in connection.execute(
                    "SELECT c.id, d.name, d.path FROM chunks c JOIN documents d "
                    f"ON d.id=c.document_id WHERE c.id IN ({','.join('?' for _ in batch)})",
                    batch,
                ):
                    output[int(row[0])] = title_score(query, weights, f"{row[1]} {row[2]}")
        return output

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
    lexical: list[tuple[int, float, float]],
    semantic: list[tuple[int, float]],
    titles: dict[int, float],
    phrases: set[int],
) -> dict[int, float]:
    """Weighted reciprocal-rank fusion: each signal adds ``WEIGHTS[signal] / (RRF_K + rank)``.
    Title ranks tie for equal title scores; every phrase chunk counts as rank 1."""
    scores: dict[int, float] = {}

    def add(chunk_id: int, signal: str, rank: int) -> None:
        scores[chunk_id] = scores.get(chunk_id, 0.0) + WEIGHTS[signal] / (RRF_K + rank)

    for rank, (chunk_id, _, _) in enumerate(lexical, 1):
        add(chunk_id, "text", rank)
    for rank, (chunk_id, _) in enumerate(semantic, 1):
        add(chunk_id, "vector", rank)
    positive = [value for value in titles.values() if value > 0]
    for chunk_id, value in titles.items():
        if value > 0:
            add(chunk_id, "title", 1 + sum(1 for other in positive if other > value))
    for chunk_id in phrases:
        add(chunk_id, "phrase", 1)
    return scores
