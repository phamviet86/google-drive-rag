"""What a running or unfinished sync looks like from search and sync_status: first-sync
warnings (fix 1), live progress (fix 3) and files skipped as unsupported (fix 4)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from helpers import FakeDrive, FakeEmbeddings, make_service
from test_drive_backend import ROOT as STUB_ROOT
from test_drive_backend import StubDrive, tree

from gdrive_rag import service as service_module
from gdrive_rag.drive import Download, DriveItem, GoogleDriveBackend
from gdrive_rag.service import Service

ROOT = "rootFirstSync0001"
SUB = "folderLuong00001"


class HookDrive(FakeDrive):
    """FakeDrive that calls ``hook`` before each download, i.e. in the middle of a sync."""

    def __init__(self) -> None:
        super().__init__()
        self.hook: Callable[[DriveItem], None] | None = None

    def download(self, item: DriveItem) -> Download:
        if self.hook is not None:
            self.hook(item)
        return super().download(item)


def first_sync_drive() -> HookDrive:
    drive = HookDrive()
    drive.add_root(ROOT, "Kế toán")
    drive.add_folder(SUB, "Lương", ROOT)
    drive.put(ROOT, "fileA00000000001", "a.txt", "doanh thu tháng một".encode())
    drive.put(ROOT, "fileB00000000001", "b.txt", "doanh thu tháng hai".encode())
    drive.put(ROOT, "fileC00000000001", "c.txt", "bảng lương doanh thu".encode(), parent=SUB)
    return drive


# fix 1 ---------------------------------------------------------------------------------------
def test_first_sync_scopes_warn_instead_of_failing(tmp_path: Path) -> None:
    drive = first_sync_drive()
    service = make_service(tmp_path, drive)
    service.add_sources([ROOT])
    assert service.list_sources()[0]["first_sync_done"] is False

    # added, not synced: the root folder is already known; a subfolder is not listed yet
    for scope in ({"folder": ROOT}, {"source": ROOT}):
        result = service.search("doanh thu", **scope)
        assert result["results"] == []
        assert result["warnings"] == ["source 'Kế toán' is still on its first sync (listing files)"]
    early = service.search("doanh thu", folder=SUB)
    assert early["results"] == [] and "not indexed yet" in early["warnings"][0]
    assert "still on its first sync" in early["warnings"][0]

    seen: list[dict[str, Any]] = []

    def during(item: DriveItem) -> None:
        if len(drive.downloads) == 2:  # two files done, the third is being downloaded
            seen.append(service.search("doanh thu", source=ROOT))
            seen.append(service.search("doanh thu", folder=SUB))
            seen.append(service.search("doanh thu", folder=ROOT))

    drive.hook = during
    service.sync()
    by_source, by_sub, by_root = seen
    assert by_source["warnings"] == [
        "source 'Kế toán' is still on its first sync (2 of 3 files indexed)"
    ]
    assert len(by_source["results"]) == 2  # what is indexed so far is searchable in scope
    assert by_sub["warnings"] == by_source["warnings"]  # subfolder known since the listing
    assert len(by_root["results"]) == 2

    done = service.search("doanh thu", source=ROOT)
    assert done["warnings"] == [] and len(done["results"]) == 3
    assert service.list_sources()[0]["first_sync_done"] is True
    assert {h["file_name"] for h in service.search("doanh thu", folder=SUB)["results"]} == {"c.txt"}


def test_failed_first_sync_still_warns_and_unknown_folders_still_error(tmp_path: Path) -> None:
    drive = first_sync_drive()
    service = make_service(tmp_path, drive)
    service.add_sources([ROOT])
    drive.root_errors[ROOT] = RuntimeError("quota exceeded")  # auth/quota: pass fails
    service.sync()
    warning = service.search("doanh thu", source=ROOT)["warnings"]
    assert warning == [
        "source 'Kế toán' has not finished its first sync "
        "(the last attempt failed; see sync_status)"
    ]
    del drive.root_errors[ROOT]
    service.sync()
    with pytest.raises(ValueError, match="unknown or not indexed folder"):
        service.search("doanh thu", folder="folderUnknown0001")


def test_discarded_source_leaves_no_folder(tmp_path: Path) -> None:
    service = make_service(tmp_path, first_sync_drive())
    added = service.add_sources([ROOT])[0]
    assert service.list_folders()["folders"][0]["folder_id"] == ROOT
    service.discard_source(added["id"])
    assert service.list_folders()["folders"] == []


# fix 3 ---------------------------------------------------------------------------------------
class HookEmbeddings(FakeEmbeddings):
    def __init__(self) -> None:
        super().__init__()
        self.hook: Callable[[], None] | None = None

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if self.hook is not None:
            self.hook()
        return super().embed_documents(texts)


def test_sync_status_shows_live_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_module, "PROGRESS_INTERVAL_SECONDS", 0.0)
    drive = first_sync_drive()
    embeddings = HookEmbeddings()
    service: Service = make_service(tmp_path, drive, embeddings)
    service.add_sources([ROOT])
    seen: list[dict[str, Any]] = []
    drive.hook = lambda item: seen.append(service.status()["sync"]) if not seen else None
    embeddings.hook = lambda: seen.append(service.status()["sync"]) if len(seen) == 1 else None
    report = service.sync()
    extracting, embedding = seen
    assert extracting["running"] is True
    assert extracting["progress"]["phase"] == "extracting"
    assert extracting["progress"]["sources"] == [
        {"id": 1, "name": "Kế toán", "files_total": 3, "files_done": 0}
    ]
    assert embedding["progress"]["phase"] == "embedding"
    assert embedding["progress"]["sources"][0]["files_done"] == 3
    assert embedding["progress"]["embedding"] == {"done": 0, "pending": 3}
    assert extracting["last_report"] is None  # last_report is unchanged while running
    after = service.status()["sync"]
    assert after["running"] is False and after["progress"] is None
    assert after["last_report"]["indexed"] == report["indexed"] == 3


# fix 4 ---------------------------------------------------------------------------------------
def skipped_tree() -> StubDrive:
    stub = tree()  # already holds logo.gif (image/gif has no extractor)
    stub.add(STUB_ROOT, "rar000000000000", "backup.rar", "application/x-rar", size="2048")
    stub.add(STUB_ROOT, "jsonl00000000000", "log.jsonl", "application/octet-stream", size="9")
    stub.add(STUB_ROOT, "short00000000000", "lối tắt", "application/vnd.google-apps.shortcut")
    stub.add(STUB_ROOT, "form000000000000", "Khảo sát", "application/vnd.google-apps.form")
    return stub


def test_listing_records_skipped_files_with_reasons() -> None:
    backend = GoogleDriveBackend(service=skipped_tree())
    listing = backend.list_files(backend.resolve(STUB_ROOT))
    skipped = {f.path: (f.mime_type, f.size, f.reason) for f in listing.skipped}
    assert skipped == {
        "logo.gif": ("image/gif", None, "no extractor for this file type"),
        "backup.rar": ("application/x-rar", 2048, "no extractor for this file type"),
        "log.jsonl": ("application/octet-stream", 9, "no extractor for this file type"),
        "lối tắt": (
            "application/vnd.google-apps.shortcut",
            None,
            "Drive shortcut (the target file is indexed where it lives)",
        ),
        "Khảo sát": (
            "application/vnd.google-apps.form",
            None,
            "Google file type that cannot be exported as text",
        ),
    }
    assert "backup.rar" not in {item.path for item in listing.items}
    single = backend.list_files(backend.resolve("rar000000000000"))  # a file-kind source
    assert single.items == [] and [f.path for f in single.skipped] == ["backup.rar"]


def test_skipped_files_in_status_and_sources(tmp_path: Path) -> None:
    stub = skipped_tree()
    backend = GoogleDriveBackend(service=stub)
    backend._export = lambda file_id, mime: b"noi dung"  # type: ignore[method-assign]
    backend._media = lambda file_id: b"not a real file"  # type: ignore[method-assign]
    service = make_service(tmp_path)
    service.backend_factory = lambda _: backend
    service.add_sources([STUB_ROOT])
    service.sync()
    status = service.status()
    assert status["skipped"] == {"total": 5}
    assert status["sources"][0]["skipped"] == 5 and status["sources"][0]["files"] == 3
    listed = service.skipped_files()
    assert listed["total"] == 5 and {f["path"] for f in listed["files"]} >= {"backup.rar"}
    assert listed["files"][0].keys() == {"source", "path", "mime_type", "size", "reason"}
    assert service.skipped_files(limit=2)["files"].__len__() == 2

    stub.children[STUB_ROOT].remove("rar000000000000")  # deleted in Drive
    service.sync()
    assert service.status()["skipped"] == {"total": 4}


def test_service_skips_unsupported_items_of_any_backend(tmp_path: Path) -> None:
    drive = FakeDrive()  # lists every file, supported or not
    drive.add_root(ROOT, "Kế toán")
    drive.put(ROOT, "fileA00000000001", "a.txt", b"doanh thu")
    drive.put(ROOT, "fileZ00000000001", "z.zip", b"PK", mime="application/zip")
    service = make_service(tmp_path, drive)
    service.add_sources([ROOT])
    report = service.sync()
    assert report["indexed"] == 1 and report["failed"] == 0
    assert drive.downloads == ["fileA00000000001"]
    assert service.skipped_files()["files"][0]["path"] == "z.zip"
