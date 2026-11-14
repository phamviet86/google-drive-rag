"""Upgrade from a v0.2.0 data directory: migration 4 is additive, nothing is re-extracted or
re-embedded, and the stored identities (fingerprint, embedding identity) still match."""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

from helpers import FakeDrive, FakeEmbeddings, make_service

from gdrive_rag.drive import DriveItem
from gdrive_rag.embeddings import OpenAICompatibleEmbeddings

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from make_v0_2_0_index import FILES, ROOT, upgrade_drive  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "v0_2_0_index.sqlite3"


def snapshot(path: Path) -> dict[str, list[Any]]:
    connection = sqlite3.connect(str(path))
    try:
        return {
            "chunks": connection.execute("SELECT * FROM chunks ORDER BY id").fetchall(),
            "vectors": connection.execute("SELECT * FROM vectors ORDER BY chunk_hash").fetchall(),
            "documents": connection.execute(
                "SELECT id, file_id, fingerprint, status, chunk_count, indexed_at, metadata_json "
                "FROM documents ORDER BY id"
            ).fetchall(),
            "identity": connection.execute(
                "SELECT value FROM meta WHERE key='embedding_identity'"
            ).fetchall(),
        }
    finally:
        connection.close()


def test_upgrade_from_v0_2_0_reextracts_and_reembeds_nothing(tmp_path: Path) -> None:
    shutil.copy(FIXTURE, tmp_path / "index.sqlite3")
    before = snapshot(tmp_path / "index.sqlite3")
    assert len(before["chunks"]) == len(FILES) and len(before["vectors"]) == len(FILES)
    drive = upgrade_drive(FakeDrive)
    assert isinstance(drive, FakeDrive)
    embeddings = FakeEmbeddings()
    service = make_service(tmp_path, drive, embeddings)
    with service.db.read() as connection:
        versions = [r[0] for r in connection.execute("SELECT version FROM schema_migrations")]
    assert versions == [1, 2, 3, 4]
    source = service.list_sources()[0]
    assert source["first_sync_done"] is True  # backfilled: no first-sync warning after upgrade
    assert source["path_rules"] == {"demote": [], "exclude": []} and source["skipped"] == 0
    assert service.search("doanh thu", source=ROOT)["warnings"] == []
    assert snapshot(tmp_path / "index.sqlite3") == before  # migrating changed no row

    report = service.sync()
    assert (report["indexed"], report["unchanged"], report["failed"]) == (0, len(FILES), 0)
    assert drive.downloads == [] and embeddings.document_calls == 0
    assert report["embeddings"]["embedded"] == 0
    assert snapshot(tmp_path / "index.sqlite3") == before

    # path rules are search-time only: setting them and syncing again changes nothing stored
    service.set_path_rules(source["id"], {"demote": ["attachments/**"], "exclude": []})
    service.sync()
    assert drive.downloads == [] and embeddings.document_calls == 0
    assert snapshot(tmp_path / "index.sqlite3") == before


def test_stored_identities_keep_their_v0_2_0_format() -> None:
    # documents.fingerprint and meta.embedding_identity written by v0.2.0 must compare equal,
    # or the upgrade would re-download every file or delete every vector
    item = DriveItem("f", "a.txt", "text/plain", "a.txt", "2026-01-01T00:00:00Z", "abc", 3, "u")
    assert item.fingerprint == json.dumps(["v2", "abc", 3, "text/plain"])
    client = OpenAICompatibleEmbeddings("https://openrouter.ai/api/v1/", "k" * 20, "m", 1024, "")
    assert client.identity == '["https://openrouter.ai/api/v1", "m", 1024]'
