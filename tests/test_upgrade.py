"""Upgrade from v0.2.0 and v0.3.0 data directories: migrations 4 (additive) and 5 (path_rules
rebuilt for the ``ignore`` effect) change no row, nothing is re-extracted or re-embedded, and
the stored identities (fingerprint, embedding identity) still match."""

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
import make_v0_3_0_index as v030  # noqa: E402
from make_v0_2_0_index import FILES, ROOT, upgrade_drive  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "v0_2_0_index.sqlite3"
FIXTURE_V030 = Path(__file__).parent / "fixtures" / "v0_3_0_index.sqlite3"


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
            "document_sources": connection.execute(
                "SELECT * FROM document_sources ORDER BY document_id, source_id"
            ).fetchall(),
            "document_folders": connection.execute(
                "SELECT * FROM document_folders ORDER BY document_id, folder_id"
            ).fetchall(),
            "folders": connection.execute("SELECT * FROM folders ORDER BY folder_id").fetchall(),
            "folder_sources": connection.execute(
                "SELECT * FROM folder_sources ORDER BY folder_id, source_id"
            ).fetchall(),
            "fts": connection.execute(
                "SELECT rowid, text, title FROM chunks_fts ORDER BY rowid"
            ).fetchall(),
        }
    finally:
        connection.close()


def test_upgrade_from_v0_2_0_reextracts_and_reembeds_nothing(tmp_path: Path) -> None:
    shutil.copy(FIXTURE, tmp_path / "index.sqlite3")
    before = snapshot(tmp_path / "index.sqlite3")
    assert len(before["chunks"]) > len(FILES)  # multi-chunk files
    assert len(before["vectors"]) == len({row[5] for row in before["chunks"]})
    assert len(before["fts"]) == len(before["chunks"]) and before["document_folders"]
    drive = upgrade_drive(FakeDrive)
    assert isinstance(drive, FakeDrive)
    embeddings = FakeEmbeddings()
    service = make_service(tmp_path, drive, embeddings)
    with service.db.read() as connection:
        versions = [r[0] for r in connection.execute("SELECT version FROM schema_migrations")]
    assert versions == [1, 2, 3, 4, 5]
    source = service.list_sources()[0]
    assert source["first_sync_done"] is True  # backfilled: no first-sync warning after upgrade
    assert (
        source["path_rules"] == {"demote": [], "exclude": [], "ignore": []}
        and source["skipped"] == 0
    )
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


V030_EXTRA = ("sources", "path_rules", "skipped_files")


def snapshot_v030(path: Path) -> dict[str, list[Any]]:
    """``snapshot`` plus what v0.3.0 added: sources, path rules (with rowid: rules are read in
    rowid order) and skipped files."""
    connection = sqlite3.connect(str(path))
    try:
        extra = {
            "sources": connection.execute(  # all but the time of the last sync
                "SELECT id, link, root_id, kind, drive_id, name, added_at, last_status, "
                "last_error, file_count, first_synced_at FROM sources ORDER BY id"
            ).fetchall(),
            "path_rules": connection.execute(
                "SELECT rowid, * FROM path_rules ORDER BY rowid"
            ).fetchall(),
            "skipped_files": connection.execute(
                "SELECT * FROM skipped_files ORDER BY source_id, file_id"
            ).fetchall(),
        }
    finally:
        connection.close()
    return snapshot(path) | extra


def test_upgrade_from_v0_3_0_keeps_rules_and_skipped_files(tmp_path: Path) -> None:
    shutil.copy(FIXTURE_V030, tmp_path / "index.sqlite3")
    before = snapshot_v030(tmp_path / "index.sqlite3")
    assert len(before["path_rules"]) == 4 and len(before["skipped_files"]) == 2
    assert len(before["vectors"]) == len({row[5] for row in before["chunks"]})
    drive = v030.upgrade_drive(FakeDrive)
    assert isinstance(drive, FakeDrive)
    embeddings = FakeEmbeddings()
    service = make_service(tmp_path, drive, embeddings)
    with service.db.read() as connection:
        versions = [r[0] for r in connection.execute("SELECT version FROM schema_migrations")]
        table = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='path_rules'"
        ).fetchone()[0]
    assert versions == [1, 2, 3, 4, 5] and "'ignore'" in table
    source = service.list_sources()[0]
    assert source["path_rules"] == {**v030.RULES, "ignore": []} and source["skipped"] == 2
    assert snapshot_v030(tmp_path / "index.sqlite3") == before  # migrating changed no row
    hits = {hit["file_name"] for hit in service.search("bản nháp báo cáo")["results"]}
    assert "nhap-thang-3.txt" not in hits  # the stored exclude rule still applies

    report = service.sync()
    assert (report["indexed"], report["unchanged"], report["failed"]) == (0, len(v030.FILES), 0)
    assert drive.downloads == [] and embeddings.document_calls == 0
    assert snapshot_v030(tmp_path / "index.sqlite3") == before

    # the rebuilt table takes the new effect, and the old rules keep their order
    service.set_path_rules(source["id"], {**v030.RULES, "ignore": ["*.zip"]})
    assert service.path_rules(source["id"]) == {**v030.RULES, "ignore": ["*.zip"]}
    service.sync()
    assert drive.downloads == [] and embeddings.document_calls == 0
    base = {key: value for key, value in before.items() if key not in V030_EXTRA}
    assert snapshot(tmp_path / "index.sqlite3") == base
