"""Local folder sources: a folder tree (or one file) inside ``GDRAG_LOCAL_ROOT``, read through
the same ``DriveBackend`` protocol as Google Drive.

IDs are ``local:<path relative to the root>`` (the root itself is ``local:.``), so they are
stable across syncs and a rename is a new ID. The fingerprint is size + ``st_mtime_ns`` + MIME
type. Hidden entries (names starting with ``.``) and Office lock files (``~$...``) are ignored.
Symbolic links are followed only to files inside the root; links to folders, links leaving the
root and broken links are listed as skipped.

Errors follow the Drive split: the root missing, unreadable or completely empty (a missing or
wrong Docker mount) is a ``DriveSourceError``, so nothing is deleted; a source path that is gone
while the root is fine is a ``DriveItemError`` (its membership is cleared); a file that cannot be
read is a ``DriveDownloadError`` (retried on every sync). No message repeats a path the caller
passed in.
"""

from __future__ import annotations

import datetime as dt
import mimetypes
import os
import stat
import unicodedata
from pathlib import Path
from typing import Any

from .drive import (
    MAX_DOWNLOAD_BYTES,
    MIME_SUFFIXES,
    Download,
    DriveDownloadError,
    DriveFolder,
    DriveItem,
    DriveItemError,
    DriveRoot,
    DriveSourceError,
    Listing,
    SkippedFile,
    display_segment,
    is_local_id,
    local_id,
    split_supported,
)

KIND_FOLDER = "local_folder"
KIND_FILE = "local_file"
NOT_CONFIGURED = (
    "local folders are not set up: GDRAG_LOCAL_ROOT is not set (in Docker it is /local, "
    "mounted from GDRAG_LOCAL_HOST_DIR)"
)
ROOT_MISSING = "the local folder (GDRAG_LOCAL_ROOT) is missing or not a folder; check the mount"
ROOT_UNREADABLE = "the local folder (GDRAG_LOCAL_ROOT) cannot be read; check the mount and mode"
ROOT_EMPTY = (
    "the local folder (GDRAG_LOCAL_ROOT) is empty, which usually means the folder is not "
    "mounted; nothing is removed from the index until it has files again"
)
NOT_INSIDE = "no such file or folder inside the local folder"
NOT_LOCAL = "not a local path: give local:<path inside the local folder> or an absolute path"
# The built-in extension table only (no /etc/mime.types), so a fingerprint never changes
# because the host's MIME table did.
_MIME = mimetypes.MimeTypes()
_SUFFIX_MIME = {suffix: mime for mime, suffix in reversed(MIME_SUFFIXES.items())} | {
    ".markdown": "text/markdown",
    ".htm": "text/html",
    ".jpeg": "image/jpeg",
}


def is_local_input(value: str) -> bool:
    """A value meant as a local source: ``local:...`` or an absolute path (a Drive link or ID
    never starts with ``/``)."""
    text = value.strip()
    return is_local_id(text) or text.startswith("/")


def ignored(name: str) -> bool:
    """Hidden entries (``.git``, ``.DS_Store``, ``._*``) and Office lock files (``~$x.docx``)."""
    return name.startswith(".") or name.startswith("~$")


def local_mime(name: str) -> str:
    suffix = Path(name).suffix.lower()
    return (
        _SUFFIX_MIME.get(suffix)
        or _MIME.guess_type(f"x{suffix}", strict=False)[0]
        or "application/octet-stream"
    )


def relative_of(identifier: str) -> str:
    """``local:a/b`` -> ``a/b``; ``local:.`` -> ``.``."""
    return identifier[len("local:") :] or "."


def parent_of(identifier: str) -> str:
    """The folder ID holding ``identifier`` (``""`` for the root itself)."""
    relative = relative_of(identifier)
    if relative == ".":
        return ""
    head = relative.rpartition("/")[0]
    return local_id(head)


class LocalBackend:
    """``DriveBackend`` over the folder ``root`` (resolved on every call, so a mount that
    appears or disappears while the server runs is noticed)."""

    def __init__(self, root: Path):
        self.configured = root

    # -- root and paths ----------------------------------------------------------------------
    def _base(self) -> Path:
        try:
            base = self.configured.resolve(strict=True)
        except (OSError, RuntimeError):
            raise DriveSourceError(ROOT_MISSING) from None
        if not base.is_dir():
            raise DriveSourceError(ROOT_MISSING)
        return base

    def check(self) -> None:
        """``DriveSourceError`` when the root is missing, unreadable or empty."""
        self._checked_root()

    def _checked_root(self) -> Path:
        """The root, refused (``DriveSourceError``) when unreadable or without a visible entry."""
        base = self._base()
        try:
            with os.scandir(base) as entries:
                visible = any(not ignored(entry.name) for entry in entries)
        except OSError:
            raise DriveSourceError(ROOT_UNREADABLE) from None
        if not visible:
            raise DriveSourceError(ROOT_EMPTY)
        return base

    def _path(self, identifier: str, base: Path) -> Path:
        """The existing path of a ``local:`` ID; ``DriveItemError`` when it is gone or no
        longer inside the root (a link changed)."""
        relative = relative_of(identifier)
        parts = [] if relative == "." else relative.split("/")
        if not is_local_id(identifier) or any(p in {"", ".", ".."} for p in parts):
            raise DriveItemError(NOT_INSIDE)
        try:
            resolved = base.joinpath(*parts).resolve(strict=True)
        except FileNotFoundError:
            raise DriveItemError("no longer exists in the local folder") from None
        except (OSError, RuntimeError):
            raise DriveSourceError(ROOT_UNREADABLE) from None
        if not resolved.is_relative_to(base):
            raise DriveItemError("no longer inside the local folder (a link changed)")
        return resolved

    def parse(self, value: str) -> str:
        """The canonical ID of an existing file or folder inside the root, from
        ``local:<relative path>`` or an absolute path. Symlinks are resolved first; anything
        outside the root gets the same answer as a missing path (no probing)."""
        text = value.strip()
        if not is_local_input(text):
            raise ValueError(NOT_LOCAL)
        base = self._base()
        relative = relative_of(text).lstrip("/") if is_local_id(text) else None
        candidate = Path(text) if relative is None else base / relative
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            resolved = None
        if resolved is None or not resolved.is_relative_to(base):
            raise ValueError(NOT_INSIDE)
        parts = resolved.relative_to(base).parts
        if any(ignored(part) for part in parts):
            raise ValueError("hidden files and folders (names starting with . or ~$) are ignored")
        if not (resolved.is_dir() or resolved.is_file()):
            raise ValueError("that is not a regular file or folder")
        return local_id("/".join(parts))

    def name_of(self, identifier: str, base: Path) -> str:
        relative = relative_of(identifier)
        name = base.name if relative == "." else relative.rpartition("/")[2]
        return unicodedata.normalize("NFC", name or "local")

    # -- DriveBackend ------------------------------------------------------------------------
    def resolve(self, root_id: str) -> DriveRoot:
        base = self._checked_root()
        path = self._path(root_id, base)
        if path.is_dir():
            kind = KIND_FOLDER
        elif path.is_file():
            kind = KIND_FILE
        else:
            raise DriveItemError("not a regular file or folder")
        return DriveRoot(root_id, self.name_of(root_id, base), kind)

    def list_files(self, root: DriveRoot) -> Listing:
        base = self._checked_root()
        path = self._path(root.id, base)
        if path.is_file():
            item = self._item(root.id, path.stat(), display_segment(self.name_of(root.id, base)))
            kept, skipped = split_supported([item])
            return Listing(kept, skipped=skipped)
        if not path.is_dir():
            raise DriveItemError("not a regular file or folder")
        folders = [DriveFolder(root.id, self.name_of(root.id, base), "")]
        items: list[DriveItem] = []
        skipped_files: list[SkippedFile] = []
        errors: list[str] = []
        pending: list[tuple[Path, str, str]] = [(path, relative_of(root.id), "")]
        while pending:
            directory, relative, prefix = pending.pop()
            folder_id = local_id("" if relative == "." else relative)
            try:
                with os.scandir(directory) as found:
                    entries = sorted(found, key=lambda entry: entry.name)
            except OSError as exc:
                if directory == path:
                    raise DriveSourceError(
                        f"a local source folder cannot be read ({type(exc).__name__})"
                    ) from None
                errors.append(f"folder {prefix or '.'}: cannot be read ({type(exc).__name__})")
                continue
            for entry in entries:
                name = entry.name
                if ignored(name):
                    continue
                shown = display_segment(name)
                shown_path = f"{prefix}/{shown}" if prefix else shown
                if not _utf8(name):
                    skipped_files.append(
                        _skip(
                            shown_path,
                            "",
                            None,
                            "the name is not valid UTF-8; rename it",
                            folder_id,
                        )
                    )
                    continue
                child = name if relative == "." else f"{relative}/{name}"
                try:
                    if entry.is_symlink():
                        reason = _link_reason(Path(entry.path), base)
                        if reason:
                            skipped_files.append(
                                _skip(shown_path, "inode/symlink", None, reason, folder_id)
                            )
                            continue
                    if entry.is_dir(follow_symlinks=False):
                        folders.append(DriveFolder(local_id(child), _nfc(name), folder_id))
                        pending.append((Path(entry.path), child, shown_path))
                        continue
                    info = entry.stat()
                except OSError as exc:
                    errors.append(f"{shown_path}: cannot be read ({type(exc).__name__})")
                    continue
                if stat.S_ISREG(info.st_mode):
                    items.append(self._item(local_id(child), info, shown_path))
        kept, skipped = split_supported(items)
        return Listing(
            kept,
            complete=not errors,
            errors=errors,
            folders=folders,
            skipped=[*skipped_files, *skipped],
        )

    def download(self, item: DriveItem) -> Download:
        if item.size is not None and item.size > MAX_DOWNLOAD_BYTES:
            raise DriveDownloadError("file is larger than the 200 MB limit")
        base = self._checked_root()  # the mount went away: stop the pass, delete nothing
        try:
            path = self._path(item.id, base)
        except (DriveItemError, DriveSourceError) as exc:  # this file only
            raise DriveDownloadError(str(exc)) from None
        try:
            with path.open("rb") as stream:
                data = stream.read(MAX_DOWNLOAD_BYTES + 1)
        except OSError as exc:
            raise DriveDownloadError(f"file cannot be read ({type(exc).__name__})") from None
        if len(data) > MAX_DOWNLOAD_BYTES:
            raise DriveDownloadError("file is larger than the 200 MB limit")
        return Download(data, item.suffix)

    def _item(self, identifier: str, info: Any, shown_path: str) -> DriveItem:
        name = _nfc(relative_of(identifier).rpartition("/")[2])
        modified = dt.datetime.fromtimestamp(info.st_mtime, dt.UTC).replace(microsecond=0)
        return DriveItem(
            identifier,
            name,
            local_mime(name),
            shown_path,
            modified.isoformat(),
            f"mtime_ns:{info.st_mtime_ns}",  # fingerprint: size + mtime_ns + MIME type
            int(info.st_size),
            identifier,
            parent_of(identifier),
        )


def default_local_backend(settings: Any) -> LocalBackend:
    """The backend for local sources, or ``DriveSourceError`` when ``GDRAG_LOCAL_ROOT`` is unset."""
    if settings.local_root is None:
        raise DriveSourceError(NOT_CONFIGURED)
    return LocalBackend(Path(settings.local_root))


def _link_reason(link: Path, base: Path) -> str:
    """Why a symbolic link is skipped, or "" for a link to a file inside the root."""
    try:
        target = link.resolve(strict=True)
    except (OSError, RuntimeError):
        return "link whose target does not exist"
    if not target.is_relative_to(base):
        return "link to a place outside the local folder (not followed)"
    if target.is_dir():
        return "link to a folder (the folder is indexed where it is, if it is in a source)"
    return ""


def _skip(path: str, mime: str, size: int | None, reason: str, parent: str = "") -> SkippedFile:
    return SkippedFile(local_id(path), path, mime, size, reason, parent)


def _utf8(name: str) -> bool:
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)
