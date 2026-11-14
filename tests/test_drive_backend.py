"""GoogleDriveBackend against a stub Drive v3 ``service`` (real HttpError classification)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httplib2
import pytest
from googleapiclient.errors import HttpError
from helpers import make_service

from gdrive_rag.drive import (
    FOLDER_MIME,
    GOOGLE_DOC,
    DriveItemError,
    DriveRoot,
    DriveSourceError,
    GoogleDriveBackend,
    _api_error,
    strip_data_images,
)

ROOT = "rootFolder00000000"


def http_error(status: int, reasons: tuple[str, ...] = ()) -> HttpError:
    body = {"error": {"errors": [{"reason": reason} for reason in reasons]}}
    return HttpError(httplib2.Response({"status": str(status)}), json.dumps(body).encode())


class Call:
    def __init__(self, value: Any):
        self.value = value

    def execute(self, num_retries: int = 0) -> Any:
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class StubDrive:
    """files.get / files.list / drives.get over an in-memory tree; errors by ID."""

    def __init__(self) -> None:
        self.meta: dict[str, dict[str, Any]] = {}
        self.children: dict[str, list[str]] = {}
        self.errors: dict[str, Exception] = {}
        self.list_errors: dict[str, Exception] = {}
        self.incomplete: set[str] = set()
        self.drive_names: dict[str, str] = {}

    def add(self, parent: str | None, file_id: str, name: str, mime: str, **extra: Any) -> None:
        self.meta[file_id] = {"id": file_id, "name": name, "mimeType": mime, **extra}
        if parent:
            self.children.setdefault(parent, []).append(file_id)

    def files(self) -> StubDrive:
        return self

    def drives(self) -> Any:
        stub = self

        class Drives:
            def get(self, driveId: str) -> Call:  # noqa: N803
                return Call({"id": driveId, "name": stub.drive_names[driveId]})

        return Drives()

    def get(self, fileId: str, **_: Any) -> Call:  # noqa: N803
        if fileId in self.errors:
            return Call(self.errors[fileId])
        return Call(dict(self.meta[fileId]))

    def list(self, q: str, pageToken: str | None = None, **_: Any) -> Call:  # noqa: N803
        folder = q.split("'")[1]
        if folder in self.list_errors:
            return Call(self.list_errors[folder])
        ids = self.children.get(folder, [])
        start = int(pageToken or 0)
        page = ids[start : start + 2]  # two per page to exercise nextPageToken
        result: dict[str, Any] = {"files": [self.meta[i] for i in page]}
        if start + 2 < len(ids):
            result["nextPageToken"] = str(start + 2)
        if folder in self.incomplete:
            result["incompleteSearch"] = True
        return Call(result)


def tree() -> StubDrive:
    stub = StubDrive()
    stub.add(None, ROOT, "Kế toán", FOLDER_MIME)
    stub.add(ROOT, "sub000000000000", "Năm 2026", FOLDER_MIME)
    stub.add(ROOT, "doc000000000000", "Biên bản", GOOGLE_DOC, modifiedTime="2026-01-01")
    stub.add(ROOT, "gif000000000000", "logo.gif", "image/gif")
    stub.add(ROOT, "old000000000000", "cũ.txt", "text/plain", trashed=True)
    stub.add("sub000000000000", "xlsb00000000000", "Sổ.xlsb", "application/octet-stream", size="10")
    stub.add("sub000000000000", "pdf000000000000", "a/b.pdf", "application/pdf", md5Checksum="x")
    return stub


@pytest.mark.parametrize(
    ("status", "reasons", "kind"),
    [
        (404, (), DriveItemError),
        (410, (), DriveItemError),
        (403, ("insufficientFilePermissions",), DriveItemError),
        (403, ("exportSizeLimitExceeded",), DriveItemError),
        (403, (), DriveSourceError),
        (403, ("rateLimitExceeded",), DriveSourceError),
        (403, ("insufficientFilePermissions", "accessNotConfigured"), DriveSourceError),
        (401, (), DriveSourceError),
        (500, (), DriveSourceError),
        (429, (), DriveSourceError),
    ],
)
def test_api_error_classification(status: int, reasons: tuple[str, ...], kind: type) -> None:
    assert isinstance(_api_error(http_error(status, reasons)), kind)


def test_list_folder_tree() -> None:
    backend = GoogleDriveBackend(service=tree())
    root = backend.resolve(ROOT)
    assert root == DriveRoot(ROOT, "Kế toán", "folder", "")
    listing = backend.list_files(root)
    assert listing.complete
    paths = sorted(item.path for item in listing.items)
    assert paths == sorted(["Biên bản", "Năm 2026/a%2Fb.pdf", "Năm 2026/Sổ.xlsb"])
    assert {item.suffix for item in listing.items} == {".md", ".pdf", ".xlsb"}


def test_revoked_root_raises_item_error() -> None:
    stub = tree()
    backend = GoogleDriveBackend(service=stub)
    root = backend.resolve(ROOT)
    stub.errors[ROOT] = http_error(404)
    with pytest.raises(DriveItemError):
        backend.list_files(root)


def test_auth_failure_raises_source_error() -> None:
    stub = tree()
    stub.errors[ROOT] = http_error(401)
    with pytest.raises(DriveSourceError):
        GoogleDriveBackend(service=stub).list_files(DriveRoot(ROOT, "x", "folder"))


def test_subfolder_failure_makes_listing_incomplete() -> None:
    stub = tree()
    stub.list_errors["sub000000000000"] = http_error(404)
    listing = GoogleDriveBackend(service=stub).list_files(DriveRoot(ROOT, "x", "folder"))
    assert not listing.complete and [i.name for i in listing.items] == ["Biên bản"]


def test_incomplete_search_is_a_source_error() -> None:
    stub = tree()
    stub.incomplete.add(ROOT)
    with pytest.raises(DriveSourceError):
        GoogleDriveBackend(service=stub).list_files(DriveRoot(ROOT, "x", "folder"))


def test_shared_drive_and_single_file_roots() -> None:
    stub = tree()
    stub.add(None, "0ADrive0000000000", "Drive", FOLDER_MIME, driveId="0ADrive0000000000")
    stub.drive_names["0ADrive0000000000"] = "Phòng kinh doanh"
    backend = GoogleDriveBackend(service=stub)
    assert backend.resolve("0ADrive0000000000") == DriveRoot(
        "0ADrive0000000000", "Phòng kinh doanh", "shared_drive", "0ADrive0000000000"
    )
    file_root = backend.resolve("pdf000000000000")
    assert file_root.kind == "file"
    assert [i.id for i in backend.list_files(file_root).items] == ["pdf000000000000"]


def test_revoked_root_clears_documents_through_service(tmp_path: Path) -> None:
    stub = tree()
    backend = GoogleDriveBackend(service=stub)
    backend.download = lambda item: b"noi dung bien ban"  # type: ignore[method-assign]
    service = make_service(tmp_path)
    service.backend_factory = lambda _: backend
    service.add_sources([ROOT])
    report = service.sync()
    assert (report["indexed"], report["failed"]) == (1, 2)  # stub bytes are not real pdf/xlsb
    stub.errors[ROOT] = http_error(401)
    service.sync()
    assert service.status()["documents"]["total"] == 3  # auth failure keeps everything
    stub.errors[ROOT] = http_error(404)
    service.sync()
    assert service.status()["documents"]["total"] == 0


def test_docs_export_drops_embedded_images() -> None:
    exported = (
        b"# Title\n\nText ![image1][image1]\n\n"
        b"[image1]: <data:image/png;base64,iVBORw0KGgo" + b"A" * 5000 + b">\n"
        b"Inline ![x](data:image/png;base64,AAAA) end\n"
    )
    cleaned = strip_data_images(exported)
    assert b"base64" not in cleaned and b"# Title" in cleaned and b"end" in cleaned
