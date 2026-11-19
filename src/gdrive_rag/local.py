"""Local folder sources: a folder tree (or one file) inside ``GDRAG_LOCAL_ROOT``, read through
the same ``DriveBackend`` protocol as Google Drive.

IDs are ``local:<path relative to the root>`` in NFC (the root itself is ``local:.``), so they
are stable across syncs and a rename is a new ID; a name stored in NFD on disk (copied from a
Mac) gets the same ID as its NFC spelling, and lookups match path components by NFC equality.
The fingerprint is size + ``st_mtime_ns`` + MIME type. Hidden entries (names starting with
``.``) and Office lock files (``~$...``) are ignored.

Symbolic links are followed only to files inside the root; links to folders, links leaving the
root, broken links, links to hidden or protected places and links to a file the same source
lists anyway are skipped with a reason. The data folder and the secrets folder
(``GDRAG_SECRETS_DIR``) are never listed, added or read, wherever they appear under the root
(matched by real path and by device/inode, so a bind mount of the same folder is caught): a
local root that is the owner's home folder must not make credentials searchable. Files that
look like Google credentials or private keys are refused at download. A file is opened with
``O_NOFOLLOW`` and the opened descriptor's real path is checked, so a folder swapped for a link
between check and read cannot escape the root.

Errors follow the Drive split: the root missing, unreadable or completely empty (a missing or
wrong Docker mount) is a ``DriveSourceError``, so nothing is deleted (``Service._sync`` does the
same for a folder source that lists nothing while it holds indexed documents: an unplugged disk
leaves an empty mount point); a source path that is gone while the root is fine is a
``DriveItemError`` (its membership is cleared); a file that cannot be read is a
``DriveDownloadError`` (retried on every sync). No message repeats a path the caller passed in.
"""

from __future__ import annotations

import datetime as dt
import mimetypes
import os
import re
import stat
import unicodedata
from collections.abc import Iterable
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
SECRETS_ENV = "GDRAG_SECRETS_DIR"  # environment only, like admin.py reads it
NOT_INSIDE = "no such file or folder inside the local folder"
PROTECTED = "protected folder (the data or secrets folder of this service); never indexed"
SAME_NAME = "another name here is the same after Unicode normalisation (NFC); rename one"
CREDENTIAL_FILE = "looks like a credential file (private key or token); not indexed"
# Whole-file text patterns of Google credentials and private keys: tight enough that a document
# that merely mentions "service account" does not match.
_CREDENTIAL = re.compile(
    rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    rb"|\"type\"\s*:\s*\"service_account\""
    rb"|\"refresh_token\"\s*:\s*\"[^\"]{8,}\""
    rb"|\"client_secret\"\s*:\s*\"[^\"]{8,}\""
    rb"|GOCSPX-[A-Za-z0-9_-]{16,}"
    rb"|ya29\.[A-Za-z0-9_-]{30,}"
    rb"|(?<![A-Za-z0-9_/-])1//[A-Za-z0-9_-]{40,}"
)
CREDENTIAL_SCAN_BYTES = 1_048_576  # larger files are documents, not key files
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
    appears or disappears while the server runs is noticed). ``protected`` are folders (the data
    and secrets folders) that are never listed, added or read."""

    def __init__(self, root: Path, protected: Iterable[Path] = ()):
        self.configured = root
        self.protected = tuple(protected)

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

    # -- protected folders -------------------------------------------------------------------
    def _protected_keys(self) -> tuple[list[Path], set[tuple[int, int]]]:
        """Real paths and (device, inode) of the protected folders that exist now."""
        paths: list[Path] = []
        keys: set[tuple[int, int]] = set()
        for folder in self.protected:
            try:
                real = folder.resolve(strict=True)
                info = real.stat()
            except (OSError, RuntimeError):
                continue
            paths.append(real)
            keys.add((info.st_dev, info.st_ino))
        return paths, keys

    def _is_protected(self, real: Path, base: Path) -> bool:
        """Whether ``real`` (a resolved path) is, or lies below, a protected folder: by real path,
        or because it or an ancestor up to the root is the same folder (device and inode) seen
        through another mount, which is how a host folder mounted twice looks."""
        paths, keys = self._protected_keys()
        if not paths:
            return False
        if any(real == p or real.is_relative_to(p) for p in paths):
            return True
        current = real
        while True:
            try:
                info = current.stat()
            except OSError:
                return False
            if (info.st_dev, info.st_ino) in keys:
                return True
            if current == base or current.parent == current:
                return False
            current = current.parent

    def _locate(self, base: Path, parts: list[str]) -> Path:
        """``base / parts`` with each component matched to the directory entry that has the same
        NFC form, so ``local:Hợp đồng`` typed in NFC finds a folder stored in NFD."""
        current = base
        for part in parts:
            exact = current / part
            if os.path.lexists(exact):
                current = exact
                continue
            wanted = _nfc(part)
            try:
                with os.scandir(current) as found:
                    names = sorted(entry.name for entry in found if _nfc(entry.name) == wanted)
            except OSError:
                names = []
            current = current / (names[0] if names else part)
        return current

    def _path(self, identifier: str, base: Path) -> Path:
        """The existing path of a ``local:`` ID; ``DriveItemError`` when it is gone, no longer
        inside the root (a link changed) or protected."""
        relative = relative_of(identifier)
        parts = [] if relative == "." else relative.split("/")
        if not is_local_id(identifier) or any(p in {"", ".", ".."} for p in parts):
            raise DriveItemError(NOT_INSIDE)
        try:
            resolved = self._locate(base, parts).resolve(strict=True)
        except FileNotFoundError:
            raise DriveItemError("no longer exists in the local folder") from None
        except (OSError, RuntimeError):
            raise DriveSourceError(ROOT_UNREADABLE) from None
        if not resolved.is_relative_to(base):
            raise DriveItemError("no longer inside the local folder (a link changed)")
        if self._is_protected(resolved, base):
            raise DriveItemError(NOT_INSIDE)
        return resolved

    def parse(self, value: str) -> str:
        """The canonical ID of an existing file or folder inside the root, from
        ``local:<relative path>`` or an absolute path. Symlinks are resolved first; anything
        outside the root, or in a protected folder, gets the same answer as a missing path (no
        probing)."""
        text = value.strip()
        if not is_local_input(text):
            raise ValueError(NOT_LOCAL)
        base = self._base()
        if is_local_id(text):
            parts = [p for p in relative_of(text).lstrip("/").split("/") if p not in {"", "."}]
            candidate = self._locate(base, parts)
        else:
            candidate = Path(text)
        try:
            resolved: Path | None = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            resolved = None
        if resolved is None or not resolved.is_relative_to(base):
            raise ValueError(NOT_INSIDE)
        if self._is_protected(resolved, base):
            raise ValueError(NOT_INSIDE)
        found = list(resolved.relative_to(base).parts)
        if any(ignored(part) for part in found):
            raise ValueError("hidden files and folders (names starting with . or ~$) are ignored")
        if not (resolved.is_dir() or resolved.is_file()):
            raise ValueError("that is not a regular file or folder")
        return local_id("/".join(found))

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
        _, protected_keys = self._protected_keys()
        folders = [DriveFolder(root.id, self.name_of(root.id, base), "")]
        items: list[DriveItem] = []
        skipped_files: list[SkippedFile] = []
        errors: list[str] = []
        seen: set[str] = set()  # IDs of this listing: two names equal after NFC share an ID
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
                    # no valid ID can be made from the bytes: the ID uses the shown text
                    own = local_id(shown if relative == "." else f"{relative}/{shown}")
                    reason = "the name is not valid UTF-8; rename it"
                    skipped_files.append(_skip(own, shown_path, "", reason, folder_id))
                    continue
                child = name if relative == "." else f"{relative}/{name}"
                own = local_id(child)
                try:
                    if entry.is_symlink():
                        why = self._link_reason(Path(entry.path), base, path)
                        if why:
                            skipped_files.append(
                                _skip(own, shown_path, "inode/symlink", why, folder_id)
                            )
                            continue
                    if entry.is_dir(follow_symlinks=False):
                        info = entry.stat(follow_symlinks=False)
                        if (info.st_dev, info.st_ino) in protected_keys:
                            skipped_files.append(
                                _skip(own, shown_path, "inode/directory", PROTECTED, folder_id)
                            )
                            continue
                        if own in seen:
                            skipped_files.append(
                                _skip(own, shown_path, "inode/directory", SAME_NAME, folder_id)
                            )
                            continue
                        seen.add(own)
                        folders.append(DriveFolder(own, _nfc(name), folder_id))
                        pending.append((Path(entry.path), _nfc(child), shown_path))
                        continue
                    info = entry.stat()
                except OSError as exc:
                    errors.append(f"{shown_path}: cannot be read ({type(exc).__name__})")
                    continue
                if stat.S_ISREG(info.st_mode):
                    if own in seen:
                        skipped_files.append(
                            _skip(own, shown_path, local_mime(name), SAME_NAME, folder_id)
                        )
                        continue
                    seen.add(own)
                    items.append(self._item(own, info, shown_path))
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
        data = self._read(path, base)
        if len(data) > MAX_DOWNLOAD_BYTES:
            raise DriveDownloadError("file is larger than the 200 MB limit")
        if len(data) <= CREDENTIAL_SCAN_BYTES and _CREDENTIAL.search(data):
            raise DriveDownloadError(CREDENTIAL_FILE)
        return Download(data, item.suffix)

    def _read(self, path: Path, base: Path) -> bytes:
        """Read ``path`` without trusting the name it was checked under: open it without
        following a link in the last component, then check that the opened file really lives
        inside the root (a folder swapped for a link after the check would lead outside) and is
        not protected."""
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _O_CLOEXEC)
        except OSError as exc:
            raise DriveDownloadError(f"file cannot be read ({type(exc).__name__})") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise DriveDownloadError("not a regular file")
            if not self._fd_inside(fd, path, info, base):
                raise DriveDownloadError("no longer inside the local folder (a link changed)")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                return stream.read(MAX_DOWNLOAD_BYTES + 1)
        except OSError as exc:
            raise DriveDownloadError(f"file cannot be read ({type(exc).__name__})") from None
        finally:
            os.close(fd)

    def _fd_inside(self, fd: int, path: Path, info: os.stat_result, base: Path) -> bool:
        """Whether the open descriptor is a file inside the root and outside protected folders."""
        try:
            real = Path(os.readlink(f"/proc/self/fd/{fd}"))
        except OSError:  # no /proc: it must be the same file the path resolves to now
            try:
                real = path.resolve(strict=True)
                again = real.stat()
            except (OSError, RuntimeError):
                return False
            if (again.st_dev, again.st_ino) != (info.st_dev, info.st_ino):
                return False
        return (
            real.is_absolute() and real.is_relative_to(base) and not self._is_protected(real, base)
        )

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

    def _link_reason(self, link: Path, base: Path, scope: Path) -> str:
        """Why a symbolic link is skipped, or "" for a link to a file inside the root that this
        listing does not reach another way. ``scope`` is the folder being listed."""
        try:
            target = link.resolve(strict=True)
        except (OSError, RuntimeError):
            return "link whose target does not exist"
        if not target.is_relative_to(base):
            return "link to a place outside the local folder (not followed)"
        if self._is_protected(target, base):
            return "link to a protected place (not followed)"
        if target.is_dir():
            return "link to a folder (the folder is indexed where it is, if it is in a source)"
        if any(ignored(part) for part in target.relative_to(base).parts):
            return "link to a hidden file or folder (not followed)"
        if target.is_relative_to(scope):
            return "link to a file this source indexes where it is (duplicate not indexed)"
        return ""


def default_local_backend(settings: Any) -> LocalBackend:
    """The backend for local sources, or ``DriveSourceError`` when ``GDRAG_LOCAL_ROOT`` is unset.
    The data folder and the secrets folder are protected."""
    if settings.local_root is None:
        raise DriveSourceError(NOT_CONFIGURED)
    protected = [Path(settings.data_dir)]
    if secrets := os.environ.get(SECRETS_ENV, "").strip():
        protected.append(Path(secrets))
    return LocalBackend(Path(settings.local_root), protected)


def _skip(identifier: str, path: str, mime: str, reason: str, parent: str = "") -> SkippedFile:
    return SkippedFile(identifier, path, mime, None, reason, parent)


def _utf8(name: str) -> bool:
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
