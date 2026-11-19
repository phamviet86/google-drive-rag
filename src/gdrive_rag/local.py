"""Local files: a read-only folder on the machine (mounted into the container) indexed with the
same pipeline as Drive.

``GDRAG_LOCAL_DIR`` names the folder (``/files`` in Docker). It is read from the environment only,
so no tool or config file can widen it. A local source is ``local:<path>`` with the path relative
to that folder (``local:`` alone is the whole folder); the same ID is the document and folder ID
in search, ``read`` and exclusions. Every path is resolved with symlinks and refused outside the
folder. Symlinks inside it, hidden names (starting with ``.``) and Office lock files (``~$``) are
not indexed. Renaming a file changes its ID, so it is read again (its embeddings are reused).
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Callable
from pathlib import Path

from .drive import (
    MAX_DOWNLOAD_BYTES,
    Download,
    DriveBackend,
    DriveDownloadError,
    DriveFolder,
    DriveItem,
    DriveItemError,
    DriveRoot,
    DriveSourceError,
    Listing,
    display_segment,
    is_local_id,
)

LOCAL_ENV = "GDRAG_LOCAL_DIR"
WHOLE_FOLDER_NAME = "Local files"
NOT_ENABLED = "local files are not enabled (mount a folder and set GDRAG_LOCAL_DIR)"


def local_root_from_env() -> Path | None:
    value = os.environ.get(LOCAL_ENV, "").strip()
    return Path(value) if value else None


def _hidden(name: str) -> bool:
    return name.startswith((".", "~$"))


def _join(parent: str, name: str) -> str:
    return f"{parent}/{name}" if parent else name


class LocalBackend:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _locate(self, local_id: str) -> tuple[Path, str]:
        """The real path and the relative path of a local ID; ``FileNotFoundError`` when it is
        missing, hidden or outside the local folder (one answer, so nothing is probed)."""
        relative = local_id[len("local:") :]
        parts = [part for part in relative.split("/") if part]
        if any(_hidden(part) or part == ".." for part in parts):
            raise FileNotFoundError(local_id)
        real = self.root.joinpath(*parts).resolve(strict=True)
        if real != self.root and not real.is_relative_to(self.root):
            raise FileNotFoundError(local_id)
        return real, "/".join(parts)

    def resolve(self, root_id: str) -> DriveRoot:
        try:
            path, relative = self._locate(root_id)
        except (OSError, RuntimeError):
            raise DriveItemError("no such folder or file inside the local folder") from None
        name = path.name if relative else WHOLE_FOLDER_NAME
        if path.is_dir():
            return DriveRoot(root_id, name, "folder")
        if path.is_file():
            return DriveRoot(root_id, name, "file")
        raise DriveItemError("that local path is not a folder or a regular file")

    def list_files(self, root: DriveRoot) -> Listing:
        try:
            path, relative = self._locate(root.id)
        except (OSError, RuntimeError):
            # the folder may be an unmounted volume: keep the index, delete nothing
            raise DriveSourceError(
                "the local folder is not available right now (is the volume mounted?)"
            ) from None
        if root.kind == "file":
            if not path.is_file() or path.is_symlink():
                raise DriveSourceError("the local file is not available right now")
            parent = relative.rpartition("/")[0]
            return Listing([self._item(path, relative, path.name, f"local:{parent}")])
        folders = [DriveFolder(root.id, root.name, "")]
        items: list[DriveItem] = []
        errors: list[str] = []
        self._walk(path, relative, "", root.id, folders, items, errors)
        return Listing(items, complete=not errors, errors=errors, folders=folders)

    def _walk(
        self,
        directory: Path,
        relative: str,
        shown: str,
        folder_id: str,
        folders: list[DriveFolder],
        items: list[DriveItem],
        errors: list[str],
    ) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            errors.append(f"folder {display_segment(directory.name)}: {type(exc).__name__}")
            return
        for entry in entries:
            if _hidden(entry.name) or entry.is_symlink():
                continue
            child = _join(relative, entry.name)
            child_shown = _join(shown, display_segment(entry.name))
            try:
                if entry.is_dir(follow_symlinks=False):
                    child_id = f"local:{child}"
                    folders.append(DriveFolder(child_id, entry.name, folder_id))
                    self._walk(
                        Path(entry.path), child, child_shown, child_id, folders, items, errors
                    )
                elif entry.is_file(follow_symlinks=False):
                    items.append(self._item(Path(entry.path), child, child_shown, folder_id))
            except OSError as exc:
                errors.append(f"{child_shown}: {type(exc).__name__}")

    @staticmethod
    def _item(path: Path, relative: str, shown: str, parent_id: str) -> DriveItem:
        stat = path.stat()
        modified = dt.datetime.fromtimestamp(stat.st_mtime, dt.UTC).isoformat()
        return DriveItem(
            id=f"local:{relative}",
            name=path.name,
            mime_type="application/octet-stream",  # the file name picks the extractor
            path=shown,
            modified_time=modified,
            checksum="",
            size=stat.st_size,
            web_url=f"local:{relative}",
            parent_id=parent_id,
        )

    def download(self, item: DriveItem) -> Download:
        try:
            path, _ = self._locate(item.id)
            if not path.is_file():
                raise FileNotFoundError(item.id)
            if path.stat().st_size > MAX_DOWNLOAD_BYTES:
                raise DriveDownloadError("file is larger than the 200 MB limit")
            return Download(path.read_bytes(), item.suffix)
        except (OSError, RuntimeError):
            raise DriveDownloadError(
                "cannot read this local file (moved, deleted or no access)"
            ) from None


class RoutingBackend:
    """Sends ``local:`` IDs to the local folder and every other ID to Google Drive. The Drive
    client is built only when a Drive source needs it, so local files work with no Google access."""

    def __init__(self, drive_factory: Callable[[], DriveBackend], local: LocalBackend | None):
        self._drive_factory = drive_factory
        self._drive: DriveBackend | None = None
        self.local = local

    def _google(self) -> DriveBackend:
        if self._drive is None:
            try:
                self._drive = self._drive_factory()
            except DriveSourceError:
                raise
            except Exception as exc:
                raise DriveSourceError(str(exc) or type(exc).__name__) from exc
        return self._drive

    def _local(self, *, adding: bool = False) -> LocalBackend:
        if self.local is None:
            raise (DriveItemError if adding else DriveSourceError)(NOT_ENABLED)
        return self.local

    def resolve(self, root_id: str) -> DriveRoot:
        if is_local_id(root_id):
            return self._local(adding=True).resolve(root_id)
        return self._google().resolve(root_id)

    def list_files(self, root: DriveRoot) -> Listing:
        if is_local_id(root.id):
            return self._local().list_files(root)
        return self._google().list_files(root)

    def download(self, item: DriveItem) -> Download:
        if is_local_id(item.id):
            return self._local().download(item)
        return self._google().download(item)
