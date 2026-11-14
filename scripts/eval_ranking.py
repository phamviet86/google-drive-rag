"""Rank-of-expected evaluation against a copy of a real index, without network or API key.

Usage::

    python scripts/eval_ranking.py INDEX.sqlite3 QUERY_VECTORS.json [--fts-only] [--show N]

``INDEX.sqlite3`` is a copy of ``<data_dir>/index.sqlite3`` (with its stored vectors). It is copied
again to a temporary directory first, so the file you pass is never modified (opening it runs
schema migrations). ``QUERY_VECTORS.json`` is a list of objects::

    {"query": "...", "expected_file_prefixes": ["12.3.4"], "vector": [1024 floats]}

where ``vector`` is the query embedding the live client produced (instruction prefix included).
A hit counts when its file name starts with one of the expected prefixes. The script prints the
rank of the first such hit per query (``-`` if not in the top ``LIMIT``) and hits@1/3/5.
Keep the data files outside the repository: they hold private documents.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gdrive_rag.config import Settings  # noqa: E402
from gdrive_rag.service import Service  # noqa: E402

LIMIT = 10


class StoredQueryVectors:
    """Embedding client that answers each query with its recorded live vector."""

    def __init__(self, identity: str, vectors: dict[str, list[float]]):
        self._identity = identity
        self.vectors = vectors

    @property
    def identity(self) -> str:
        return self._identity

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        raise RuntimeError("document embedding is not available offline")

    def embed_query(self, text: str) -> list[float]:
        return self.vectors[text]


def run(database: Path, queries: list[dict[str, Any]], fts_only: bool, show: int) -> list[Any]:
    with tempfile.TemporaryDirectory() as directory:
        data = Path(directory)
        source = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        target = sqlite3.connect(str(data / "index.sqlite3"))
        source.backup(target)  # consistent copy, WAL included; the original stays untouched
        source.close()
        target.close()
        identity = (
            sqlite3.connect(str(data / "index.sqlite3"))
            .execute("SELECT value FROM meta WHERE key='embedding_identity'")
            .fetchone()
        )
        client = StoredQueryVectors(
            identity[0] if identity else "", {q["query"]: q["vector"] for q in queries}
        )
        service = Service(
            lambda: Settings(data_dir=data, ocr_enabled=False),
            embedding_factory=lambda _: None if fts_only else client,
        )
        ranks: list[int | None] = []
        for item in queries:
            result = service.search(item["query"], limit=LIMIT)
            names = [hit["file_name"] for hit in result["results"]]
            prefixes = tuple(item["expected_file_prefixes"])
            rank = next((i for i, n in enumerate(names, 1) if n.startswith(prefixes)), None)
            ranks.append(rank)
            line = f"{rank or '-':>3}  {result['mode']:9}  {item['query']}"
            if show:
                line += "  -> " + " | ".join(names[:show])
            print(line)
        shutil.rmtree(data, ignore_errors=True)
    return ranks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("database", type=Path)
    parser.add_argument("queries", type=Path)
    parser.add_argument("--fts-only", action="store_true", help="ignore the stored vectors")
    parser.add_argument("--show", type=int, default=0, help="print the top N file names")
    args = parser.parse_args(argv)
    queries = json.loads(args.queries.read_text(encoding="utf-8"))
    ranks = run(args.database, queries, args.fts_only, args.show)
    summary = {k: sum(1 for r in ranks if r is not None and r <= k) for k in (1, 3, 5)}
    print(f"hits@1 {summary[1]}  hits@3 {summary[3]}  hits@5 {summary[5]}  of {len(ranks)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
