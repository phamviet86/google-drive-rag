"""Google Drive access: link parsing, a read-only Drive v3 backend, and the backend protocol.

The listing, export and error-classification logic is adapted from local-rag-mcp ``drive.py``
(Apache-2.0). Errors are split in two: ``DriveItemError`` (this file or this root is missing or
not shared with us) and ``DriveSourceError`` (auth, quota, network: keep the index untouched).
"""

from __future__ import annotations

import io
import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

from .extract import SUPPORTED_SUFFIXES

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_DOC = "application/vnd.google-apps.document"
GOOGLE_SHEET = "application/vnd.google-apps.spreadsheet"
GOOGLE_SLIDES = "application/vnd.google-apps.presentation"
EXPORTS = {
    GOOGLE_DOC: ("text/markdown", ".md"),
    GOOGLE_SHEET: ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    GOOGLE_SLIDES: (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
}
MIME_SUFFIXES = {
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/x-markdown": ".md",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/msword": ".doc",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-excel.sheet.binary.macroenabled.12": ".xlsb",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/rtf": ".rtf",
    "text/rtf": ".rtf",
    "text/html": ".html",
    "text/csv": ".csv",
    "application/json": ".json",
    "application/epub+zip": ".epub",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/vnd.oasis.opendocument.presentation": ".odp",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
# 403 reasons that concern one file only; any other 403 (scope, quota, API disabled) is global.
ITEM_REASONS = frozenset(
    {
        "insufficientFilePermissions",
        "fileNotDownloadable",
        "cannotDownloadFile",
        "exportSizeLimitExceeded",
        "notFound",
    }
)
EXPORT_REJECTED_STATUSES = frozenset({400, 415, 501})
_ID = re.compile(r"^[A-Za-z0-9_-]{10,}$")
_PATH_ID = re.compile(r"/(?:folders|d|file/d)/([A-Za-z0-9_-]{10,})")


class DriveItemError(RuntimeError):
    """One item (or a source root) is missing or not accessible."""


class DriveSourceError(RuntimeError):
    """Authentication, quota, transport or configuration failure: retry later, delete nothing."""


@dataclass(frozen=True)
class DriveRoot:
    id: str
    name: str
    kind: str  # "folder" | "file" | "shared_drive"
    drive_id: str = ""


@dataclass(frozen=True)
class DriveItem:
    id: str
    name: str
    mime_type: str
    path: str
    modified_time: str
    checksum: str
    size: int | None
    web_url: str

    @property
    def suffix(self) -> str:
        return item_suffix(self.mime_type, self.name)

    @property
    def fingerprint(self) -> str:
        content = self.checksum or self.modified_time
        return json.dumps(["v1", content, self.size, self.mime_type, self.name, self.path])


@dataclass(frozen=True)
class Listing:
    items: Sequence[DriveItem]
    complete: bool = True  # False: some folder failed; never delete based on this listing
    errors: Sequence[str] = field(default_factory=tuple)


class DriveBackend(Protocol):
    def resolve(self, root_id: str) -> DriveRoot: ...

    def list_files(self, root: DriveRoot) -> Listing: ...

    def download(self, item: DriveItem) -> bytes: ...


def parse_drive_link(link: str) -> str:
    """Return the Drive ID from a folder, shared-drive, file, Docs/Sheets/Slides or open?id link."""
    value = link.strip()
    if _ID.fullmatch(value):
        return value
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if not host.endswith("google.com"):
        raise ValueError(f"not a Google Drive link: {link!r}")
    query = parse_qs(parsed.query)
    for key in ("id", "folderid"):
        values = query.get(key)
        if values and _ID.fullmatch(values[0]):
            return values[0]
    match = _PATH_ID.search(parsed.path)
    if match:
        return match.group(1)
    raise ValueError(f"no Drive file or folder ID found in link: {link!r}")


def item_suffix(mime_type: str, name: str) -> str:
    """Extension used to pick an extractor: export type, then MIME type, then the file name."""
    if mime_type in EXPORTS:
        return EXPORTS[mime_type][1]
    known = MIME_SUFFIXES.get(mime_type.lower())
    if known:
        return known
    if mime_type.startswith("application/vnd.google-apps."):
        return ""
    suffix = Path(name).suffix.lower()
    return suffix if suffix in SUPPORTED_SUFFIXES else ""


def display_segment(value: str) -> str:
    """Readable path component (NFC; ``/`` and control characters percent-encoded)."""
    value = unicodedata.normalize("NFC", value) or "%EMPTY"
    return "".join(
        "".join(f"%{byte:02X}" for byte in char.encode("utf-8", "surrogatepass"))
        if char in "%/\\" or unicodedata.category(char).startswith("C")
        else char
        for char in value
    )


def load_credentials(
    method: str, service_account_file: Path, token_file: Path
) -> Any:  # pragma: no cover - needs real Google credentials
    from google.auth.transport.requests import Request

    if method == "service_account":
        from google.oauth2 import service_account

        if not service_account_file.is_file():
            raise DriveSourceError("service-account key is missing; run: gdrive-rag auth google")
        try:
            return service_account.Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
                str(service_account_file), scopes=SCOPES
            )
        except Exception:
            raise DriveSourceError("service-account key file is invalid") from None
    if method == "oauth":
        from google.oauth2.credentials import Credentials

        from .config import write_private

        if not token_file.is_file():
            raise DriveSourceError("Google sign-in is missing; run: gdrive-rag auth google")
        credentials = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call]
            str(token_file), SCOPES
        )
        if not credentials.valid and credentials.refresh_token:
            try:
                credentials.refresh(Request())
            except Exception:
                raise DriveSourceError(
                    "Google sign-in expired or was revoked; run: gdrive-rag auth google"
                ) from None
            write_private(token_file, credentials.to_json())
        if not credentials.valid:
            raise DriveSourceError("Google sign-in is invalid; run: gdrive-rag auth google")
        return credentials
    raise DriveSourceError("Google access is not configured; run: gdrive-rag setup")


class GoogleDriveBackend:
    """Read-only Drive v3 backend. Tests pass a stub ``service`` instead of credentials."""

    def __init__(self, credentials: Any = None, service: Any = None):
        if service is None:  # pragma: no cover - needs real Google credentials
            from googleapiclient.discovery import build

            service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        self.service = service

    def _execute(self, request: Any) -> dict[str, Any]:
        from google.auth.exceptions import GoogleAuthError
        from googleapiclient.errors import HttpError
        from httplib2 import HttpLib2Error

        try:
            return dict(request.execute(num_retries=5))
        except HttpError as exc:
            raise _api_error(exc) from None
        except (GoogleAuthError, HttpLib2Error, OSError) as exc:
            raise DriveSourceError(f"Drive connection failed: {type(exc).__name__}") from None

    def resolve(self, root_id: str) -> DriveRoot:
        value = self._execute(
            self.service.files().get(
                fileId=root_id,
                supportsAllDrives=True,
                fields="id,name,mimeType,driveId,trashed",
            )
        )
        if value.get("trashed"):
            raise DriveItemError("Drive item is in the trash")
        drive_id = str(value.get("driveId", ""))
        if value.get("mimeType") == FOLDER_MIME:
            if drive_id and drive_id == root_id:
                name = str(value.get("name", ""))
                try:
                    drive = self._execute(self.service.drives().get(driveId=drive_id))
                    name = str(drive.get("name") or name)
                except (DriveItemError, DriveSourceError):
                    pass
                return DriveRoot(root_id, name or root_id, "shared_drive", drive_id)
            return DriveRoot(root_id, str(value.get("name") or root_id), "folder", drive_id)
        return DriveRoot(root_id, str(value.get("name") or root_id), "file", drive_id)

    _FIELDS = "id,name,mimeType,modifiedTime,md5Checksum,size,webViewLink,trashed"

    def list_files(self, root: DriveRoot) -> Listing:
        if root.kind == "file":
            value = self._execute(
                self.service.files().get(
                    fileId=root.id, supportsAllDrives=True, fields=self._FIELDS
                )
            )
            if value.get("trashed"):
                raise DriveItemError("Drive item is in the trash")
            item = _item(value, display_segment(str(value.get("name", ""))))
            return Listing([item] if item and item.suffix else [])
        # files.list on a folder we lost access to returns an empty page, not 404, so check the
        # root itself every cycle: a revoked share must raise DriveItemError, not look empty.
        self.resolve(root.id)
        items: list[DriveItem] = []
        errors: list[str] = []
        pending: list[tuple[str, str]] = [(root.id, "")]
        seen: set[str] = set()
        while pending:
            folder_id, prefix = pending.pop()
            if folder_id in seen:
                continue
            seen.add(folder_id)
            try:
                children = self._children(folder_id, root.drive_id)
            except DriveItemError as exc:
                if folder_id == root.id:
                    raise
                errors.append(f"folder {folder_id}: {exc}")
                continue
            for value in children:
                if value.get("trashed"):
                    continue
                name = str(value.get("name", ""))
                path = f"{prefix}/{display_segment(name)}" if prefix else display_segment(name)
                if value.get("mimeType") == FOLDER_MIME:
                    pending.append((str(value["id"]), path))
                    continue
                item = _item(value, path)
                if item is not None and item.suffix:
                    items.append(item)
        return Listing(items, complete=not errors, errors=errors)

    def _children(self, folder_id: str, drive_id: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            values: dict[str, Any] = {
                "q": f"'{folder_id}' in parents and trashed = false",
                "fields": f"nextPageToken,incompleteSearch,files({self._FIELDS})",
                "pageSize": 1000,
                "supportsAllDrives": True,
                "includeItemsFromAllDrives": True,
            }
            if token:
                values["pageToken"] = token
            if drive_id:
                values.update({"corpora": "drive", "driveId": drive_id})
            payload = self._execute(self.service.files().list(**values))
            if payload.get("incompleteSearch"):
                raise DriveSourceError("Drive returned an incomplete listing; will retry")
            result.extend(payload.get("files", []))
            token = payload.get("nextPageToken")
            if not token:
                return result

    def download(self, item: DriveItem) -> bytes:  # pragma: no cover - real media download
        if item.size is not None and item.size > MAX_DOWNLOAD_BYTES:
            raise DriveItemError("file is larger than the 200 MB download limit")
        if item.mime_type in EXPORTS:
            mime, _ = EXPORTS[item.mime_type]
            try:
                return self._download(
                    self.service.files().export_media(fileId=item.id, mimeType=mime),
                    markdown=mime == "text/markdown",
                )
            except _ExportRejected:
                return self._download(
                    self.service.files().export_media(fileId=item.id, mimeType="text/plain")
                )
        return self._download(
            self.service.files().get_media(fileId=item.id, supportsAllDrives=True)
        )

    def _download(  # pragma: no cover - real media download
        self, request: Any, markdown: bool = False
    ) -> bytes:
        from google.auth.exceptions import GoogleAuthError
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaIoBaseDownload
        from httplib2 import HttpLib2Error

        output, done = io.BytesIO(), False
        downloader = MediaIoBaseDownload(output, request, chunksize=8 * 1024 * 1024)
        while not done:
            try:
                _, done = downloader.next_chunk(num_retries=5)
            except HttpError as exc:
                if markdown and int(exc.resp.status) in EXPORT_REJECTED_STATUSES:
                    raise _ExportRejected from None
                raise _api_error(exc) from None
            except (GoogleAuthError, HttpLib2Error, OSError) as exc:
                raise DriveSourceError(f"Drive connection failed: {type(exc).__name__}") from None
            if output.tell() > MAX_DOWNLOAD_BYTES:
                raise DriveItemError("file is larger than the 200 MB download limit")
        return output.getvalue()


_DATA_IMAGE_DEFINITION = re.compile(rb"^\[[^\]]+\]:\s*<data:[^>]+>[ \t]*\r?$\n?", re.MULTILINE)
_DATA_IMAGE_INLINE = re.compile(rb"!\[[^\]]*\]\(\s*<?data:[^)]*\)")
_IMAGE_REFERENCE = re.compile(rb"!\[[^\]]*\]\[[^\]]*\]")


def strip_data_images(content: bytes) -> bytes:
    """Drop embedded base64 images from a Docs Markdown export (copied from local-rag-mcp)."""
    if b"data:" not in content:
        return content
    content = _DATA_IMAGE_DEFINITION.sub(b"", content)
    content = _DATA_IMAGE_INLINE.sub(b"", content)
    return _IMAGE_REFERENCE.sub(b"", content)


class _ExportRejected(Exception):
    """Drive refused the Markdown export of one Doc (fall back to plain text)."""


def _item(value: dict[str, Any], path: str) -> DriveItem | None:
    try:
        identifier = str(value["id"])
        return DriveItem(
            identifier,
            str(value["name"]),
            str(value["mimeType"]),
            path,
            str(value.get("modifiedTime", "")),
            str(value.get("md5Checksum", "")),
            int(value["size"]) if value.get("size") is not None else None,
            str(value.get("webViewLink") or f"https://drive.google.com/open?id={identifier}"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _api_error(exc: Any) -> RuntimeError:
    status = int(exc.resp.status)
    try:
        reasons = {
            row.get("reason") for row in json.loads(exc.content).get("error", {}).get("errors", [])
        }
    except (ValueError, TypeError, AttributeError):
        reasons = set()
    if status in {404, 410} or (status == 403 and reasons and reasons <= ITEM_REASONS):
        return DriveItemError(f"not found or not shared with this Google account (HTTP {status})")
    if status in {401, 403}:
        return DriveSourceError(f"Google refused access (HTTP {status}); check sign-in and API")
    return DriveSourceError(f"Drive request failed (HTTP {status})")
