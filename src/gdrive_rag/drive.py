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


class DriveDownloadError(RuntimeError):
    """Downloading or exporting one file failed; only that file is affected (retried next sync)."""


@dataclass(frozen=True)
class Download:
    data: bytes
    suffix: str  # extension the data should be extracted as (may differ after an export fallback)


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
    parent_id: str = ""  # folder this file was listed in (not part of the fingerprint)

    @property
    def suffix(self) -> str:
        return item_suffix(self.mime_type, self.name)

    @property
    def fingerprint(self) -> str:
        """Content identity only: a rename or move updates metadata without re-downloading."""
        content = self.checksum or self.modified_time
        return json.dumps(["v2", content, self.size, self.mime_type])


@dataclass(frozen=True)
class DriveFolder:
    id: str
    name: str
    parent_id: str  # "" for a source root whose parent is outside the source


@dataclass(frozen=True)
class Listing:
    items: Sequence[DriveItem]
    complete: bool = True  # False: some folder failed; never delete based on this listing
    errors: Sequence[str] = field(default_factory=tuple)
    folders: Sequence[DriveFolder] = field(default_factory=tuple)  # includes a folder root
    skipped: Sequence[SkippedFile] = field(default_factory=tuple)  # listed but not indexable


@dataclass(frozen=True)
class SkippedFile:
    """A listed file that is not indexed because no extractor handles its type."""

    id: str
    path: str
    mime_type: str
    size: int | None
    reason: str


SHORTCUT_MIME = "application/vnd.google-apps.shortcut"


def skip_reason(item: DriveItem) -> str:
    """Why ``item`` cannot be indexed, or "" when an extractor handles it."""
    if item.suffix:
        return ""
    if item.mime_type == SHORTCUT_MIME:
        return "Drive shortcut (the target file is indexed where it lives)"
    if item.mime_type.startswith("application/vnd.google-apps."):
        return "Google file type that cannot be exported as text"
    return "no extractor for this file type"


def split_supported(items: Sequence[DriveItem]) -> tuple[list[DriveItem], list[SkippedFile]]:
    """Indexable items and the skipped rest, with a reason for each."""
    kept: list[DriveItem] = []
    skipped: list[SkippedFile] = []
    for item in items:
        reason = skip_reason(item)
        if reason:
            skipped.append(SkippedFile(item.id, item.path, item.mime_type, item.size, reason))
        else:
            kept.append(item)
    return kept, skipped


LOCAL_PREFIX = "local:"


def is_local_id(value: str) -> bool:
    return value.startswith(LOCAL_PREFIX)


def normalize_local_id(value: str) -> str:
    """``local:<path>`` with the path relative to the local folder: no leading ``/``, no empty
    or ``.`` parts, and no ``..``, backslash or control character. ``local:`` alone is the whole
    local folder."""
    path = value.strip()[len(LOCAL_PREFIX) :]
    if "\\" in path or any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise ValueError("not a valid local path")
    parts = [part for part in path.split("/") if part not in {"", "."}]
    if ".." in parts:
        raise ValueError("a local path cannot contain '..'")
    return LOCAL_PREFIX + "/".join(parts)


def folder_link(folder_id: str) -> str:
    if is_local_id(folder_id):
        return folder_id  # a local item has no web link; its ID is its address
    return f"https://drive.google.com/drive/folders/{folder_id}"


class DriveBackend(Protocol):
    def resolve(self, root_id: str) -> DriveRoot: ...

    def list_files(self, root: DriveRoot) -> Listing: ...

    def download(self, item: DriveItem) -> Download: ...


DRIVE_HOSTS = {"drive.google.com", "docs.google.com"}


def drive_link(drive_id: str, kind: str) -> str:
    """The link stored for a source, rebuilt from its ID (never the text the user pasted)."""
    if kind in {"folder", "shared_drive"} or is_local_id(drive_id):
        return folder_link(drive_id)
    return f"https://drive.google.com/open?id={drive_id}"


def save_refreshed_token(token_file: Path, payload: str) -> bool:
    """Write a refreshed OAuth token only if the token file still exists, so a refresh that
    races with ``remove_google_auth`` cannot put the deleted token back."""
    from .config import token_lock, write_private

    with token_lock(token_file):  # the same lock as the removal: check and write are one step
        if not token_file.is_file():
            return False
        write_private(token_file, payload)
        return True


def parse_drive_link(link: str) -> str:
    """Return the Drive ID from a folder, shared-drive, file, Docs/Sheets/Slides or open?id link,
    or the normalized ``local:<path>`` ID of a local file or folder."""
    value = link.strip()
    if is_local_id(value):
        return normalize_local_id(value)
    if _ID.fullmatch(value):
        return value
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"https", "http"} or host not in DRIVE_HOSTS:
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
            save_refreshed_token(token_file, credentials.to_json())
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

    _FIELDS = "id,name,mimeType,modifiedTime,md5Checksum,size,webViewLink,trashed,parents"

    def list_files(self, root: DriveRoot) -> Listing:
        if root.kind == "file":
            value = self._execute(
                self.service.files().get(
                    fileId=root.id, supportsAllDrives=True, fields=self._FIELDS
                )
            )
            if value.get("trashed"):
                raise DriveItemError("Drive item is in the trash")
            parents = value.get("parents") or [""]
            item = _item(value, display_segment(str(value.get("name", ""))), str(parents[0]))
            kept, skipped = split_supported([item] if item else [])
            return Listing(kept, skipped=skipped)
        # files.list on a folder we lost access to returns an empty page, not 404, so check the
        # root itself every cycle: a revoked share must raise DriveItemError, not look empty.
        resolved = self.resolve(root.id)
        folders: list[DriveFolder] = [DriveFolder(root.id, resolved.name, "")]
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
                    folders.append(DriveFolder(str(value["id"]), name, folder_id))
                    continue
                item = _item(value, path, folder_id)
                if item is not None:
                    items.append(item)
        kept, skipped = split_supported(items)
        return Listing(kept, complete=not errors, errors=errors, folders=folders, skipped=skipped)

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

    def download(self, item: DriveItem) -> Download:
        """Fetch one file. Any failure that concerns only this file is a DriveDownloadError;
        DriveSourceError is kept for sign-in and connection failures."""
        if item.size is not None and item.size > MAX_DOWNLOAD_BYTES:
            raise DriveDownloadError("file is larger than the 200 MB download limit")
        if item.mime_type not in EXPORTS:
            return Download(self._media(item.id), item.suffix)
        mime, suffix = EXPORTS[item.mime_type]
        try:
            return Download(self._export(item.id, mime), suffix)
        except _ExportRejected as exc:
            if mime != "text/markdown":
                raise DriveDownloadError(f"Google refused to export this file ({exc})") from None
            fallback = "Markdown export refused"
        except _ExportTooLarge:
            if item.mime_type == GOOGLE_SHEET:
                raise DriveDownloadError(
                    "Google Sheet is larger than Google's 10 MB export limit"
                ) from None
            fallback = "over the 10 MB export limit"
        try:  # Docs and Slides: plain text is much smaller than Markdown/PPTX
            return Download(self._export(item.id, "text/plain"), ".txt")
        except _ExportTooLarge:
            raise DriveDownloadError(
                "Google file is larger than Google's 10 MB export limit, even as plain text"
            ) from None
        except _ExportRejected as exc:
            raise DriveDownloadError(f"{fallback}; plain-text export failed ({exc})") from None

    def _export(self, file_id: str, mime: str) -> bytes:
        request = self.service.files().export_media(fileId=file_id, mimeType=mime)
        return self._download(request, export=True)

    def _media(self, file_id: str) -> bytes:
        request = self.service.files().get_media(fileId=file_id, supportsAllDrives=True)
        return self._download(request)

    def _download(  # pragma: no cover - real media download
        self, request: Any, export: bool = False
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
                raise download_error(exc, export) from None
            except (GoogleAuthError, HttpLib2Error, OSError) as exc:
                raise DriveSourceError(f"Drive connection failed: {type(exc).__name__}") from None
            if output.tell() > MAX_DOWNLOAD_BYTES:
                raise DriveDownloadError("file is larger than the 200 MB download limit")
        return output.getvalue()


class _ExportRejected(Exception):
    """Drive refused this export format for this file (HTTP 400/415/501)."""


class _ExportTooLarge(Exception):
    """Drive refused the export because it exceeds the 10 MB export limit."""


def download_error(exc: Any, export: bool = False) -> Exception:
    """Classify an HttpError raised while downloading or exporting one file.

    Only HTTP 401 (sign-in) stops the sync. Everything else, including 5xx after retries and
    403 reasons such as downloadQuotaExceeded, is a failure of this file only.
    """
    status = int(exc.resp.status)
    reasons = _reasons(exc)
    if status == 401:
        return DriveSourceError(
            "Google sign-in was rejected (HTTP 401); run: gdrive-rag auth google"
        )
    if export and "exportSizeLimitExceeded" in reasons:
        return _ExportTooLarge()
    if export and status in EXPORT_REJECTED_STATUSES:
        return _ExportRejected(f"HTTP {status}")
    if status in {404, 410}:
        return DriveDownloadError(f"file disappeared or is no longer shared (HTTP {status})")
    detail = f": {', '.join(sorted(reasons))}" if reasons else ""
    return DriveDownloadError(f"download failed (HTTP {status}{detail}); will retry")


def _reasons(exc: Any) -> set[str]:
    try:
        rows = json.loads(exc.content).get("error", {}).get("errors", [])
        return {str(row.get("reason")) for row in rows if row.get("reason")}
    except (ValueError, TypeError, AttributeError):
        return set()


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


def _item(value: dict[str, Any], path: str, parent_id: str = "") -> DriveItem | None:
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
            parent_id,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _api_error(exc: Any) -> RuntimeError:
    status = int(exc.resp.status)
    reasons = _reasons(exc)
    if status in {404, 410} or (status == 403 and reasons and reasons <= ITEM_REASONS):
        return DriveItemError(f"not found or not shared with this Google account (HTTP {status})")
    if status in {401, 403}:
        return DriveSourceError(f"Google refused access (HTTP {status}); check sign-in and API")
    return DriveSourceError(f"Drive request failed (HTTP {status})")
