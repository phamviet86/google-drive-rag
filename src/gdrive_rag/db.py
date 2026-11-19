"""SQLite storage: WAL, FTS5 with Vietnamese accent folding, and float32 vector blobs.

The connection setup and ``fts_fold`` are adapted from local-rag-mcp (Apache-2.0). FTS rows are
kept in step with ``chunks`` by triggers, so deleting a document removes its search rows too.
"""

from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS = [
    """
    CREATE TABLE sources (
      id INTEGER PRIMARY KEY,
      link TEXT NOT NULL,
      root_id TEXT NOT NULL UNIQUE,
      kind TEXT NOT NULL,
      drive_id TEXT NOT NULL DEFAULT '',
      name TEXT NOT NULL,
      added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      last_sync_at TEXT,
      last_status TEXT NOT NULL DEFAULT 'pending',
      last_error TEXT,
      file_count INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE documents (
      id INTEGER PRIMARY KEY,
      file_id TEXT NOT NULL UNIQUE,
      name TEXT NOT NULL,
      path TEXT NOT NULL,
      mime_type TEXT NOT NULL,
      web_url TEXT NOT NULL,
      modified_time TEXT NOT NULL DEFAULT '',
      fingerprint TEXT NOT NULL,
      media_type TEXT NOT NULL DEFAULT '',
      metadata_json TEXT NOT NULL DEFAULT '{}',
      status TEXT NOT NULL,
      error TEXT,
      chunk_count INTEGER NOT NULL DEFAULT 0,
      indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE document_sources (
      document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
      source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
      PRIMARY KEY (document_id, source_id)
    );
    CREATE INDEX document_sources_source_idx ON document_sources(source_id);
    CREATE TABLE chunks (
      id INTEGER PRIMARY KEY,
      document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
      ordinal INTEGER NOT NULL,
      text TEXT NOT NULL,
      locator TEXT NOT NULL DEFAULT '',
      chunk_hash TEXT NOT NULL
    );
    CREATE INDEX chunks_document_idx ON chunks(document_id, ordinal);
    CREATE INDEX chunks_hash_idx ON chunks(chunk_hash);
    CREATE VIRTUAL TABLE chunks_fts USING fts5(
      text, title, tokenize='unicode61 remove_diacritics 2'
    );
    CREATE TRIGGER chunks_fts_insert AFTER INSERT ON chunks BEGIN
      INSERT INTO chunks_fts(rowid, text, title) VALUES (
        NEW.id, fts_fold(NEW.text),
        fts_fold((SELECT name FROM documents WHERE id = NEW.document_id))
      );
    END;
    CREATE TRIGGER chunks_fts_delete AFTER DELETE ON chunks BEGIN
      DELETE FROM chunks_fts WHERE rowid = OLD.id;
    END;
    CREATE TABLE vectors (
      chunk_hash TEXT PRIMARY KEY,
      vector BLOB NOT NULL
    );
    CREATE TABLE meta (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE embedding_failures (
      chunk_hash TEXT PRIMARY KEY,
      error TEXT NOT NULL
    );
    """,
    # Folder ancestry by Drive ID. ``document_folders`` holds every ancestor folder of a
    # document, recomputed after each sync from ``folders.parent_id`` and the document parent.
    """
    ALTER TABLE documents ADD COLUMN parent_id TEXT NOT NULL DEFAULT '';
    CREATE TABLE folders (
      folder_id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      parent_id TEXT NOT NULL DEFAULT '',
      path TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX folders_parent_idx ON folders(parent_id);
    CREATE TABLE folder_sources (
      folder_id TEXT NOT NULL REFERENCES folders(folder_id) ON DELETE CASCADE,
      source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
      PRIMARY KEY (folder_id, source_id)
    );
    CREATE INDEX folder_sources_source_idx ON folder_sources(source_id);
    CREATE TABLE document_folders (
      document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
      folder_id TEXT NOT NULL REFERENCES folders(folder_id) ON DELETE CASCADE,
      PRIMARY KEY (document_id, folder_id)
    );
    CREATE INDEX document_folders_folder_idx ON document_folders(folder_id);
    """,
    # v0.3.0: first-sync marker, per-source path rules for ranking, files skipped at listing.
    # Additive only: no document, chunk or vector row is touched, so nothing is re-extracted or
    # re-embedded. Sources that already synced (or hold documents) count as first-synced.
    """
    ALTER TABLE sources ADD COLUMN first_synced_at TEXT;
    UPDATE sources SET first_synced_at=COALESCE(last_sync_at, CURRENT_TIMESTAMP)
      WHERE last_status IN ('ok', 'partial')
         OR EXISTS (SELECT 1 FROM document_sources d WHERE d.source_id=sources.id);
    CREATE TABLE path_rules (
      source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
      effect TEXT NOT NULL CHECK (effect IN ('demote', 'exclude')),
      pattern TEXT NOT NULL,
      PRIMARY KEY (source_id, effect, pattern)
    );
    CREATE TABLE skipped_files (
      source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
      file_id TEXT NOT NULL,
      path TEXT NOT NULL,
      mime_type TEXT NOT NULL,
      size INTEGER,
      reason TEXT NOT NULL,
      PRIMARY KEY (source_id, file_id)
    );
    """,
    """
    CREATE TABLE exclusions (
      item_id TEXT PRIMARY KEY,
      added_at TEXT NOT NULL
    );
    """,
]


class Database:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        if os.name != "nt":
            self.path.chmod(0o600)
        connection.row_factory = sqlite3.Row
        connection.create_function("fts_fold", 1, fts_fold, deterministic=True)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            connection.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                connection.close()
                raise
        return connection

    def migrate(self) -> None:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)"
            )
            applied = {
                row[0] for row in connection.execute("SELECT version FROM schema_migrations")
            }
            for version, script in enumerate(MIGRATIONS, 1):
                if version not in applied:
                    for statement in _statements(script):
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_meta(self, key: str) -> str | None:
        with self.read() as connection:
            row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def set_meta(self, key: str, value: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )


def _statements(script: str) -> Iterator[str]:
    """Split a migration script into statements; trigger bodies stay whole."""
    buffer: list[str] = []
    for line in script.splitlines():
        if not line.strip():
            continue
        buffer.append(line)
        candidate = "\n".join(buffer)
        if sqlite3.complete_statement(candidate):
            yield candidate
            buffer = []
    if any(line.strip() for line in buffer):
        raise RuntimeError("incomplete SQL migration statement")


def fts_fold(text: str | None) -> str:
    """NFC-normalize and fold đ/Đ; FTS5 ``remove_diacritics 2`` strips the remaining accents."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    return text.replace("đ", "d").replace("Đ", "D")


TERM = re.compile(r"\w+", re.UNICODE)
