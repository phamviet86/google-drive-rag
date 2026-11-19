"""The one facade used by both the CLI and the MCP server: sources, sync, embeddings, reads.

Sync is a full listing of every source compared with stored fingerprints (MD5 checksum or
modifiedTime, size and MIME type), which is what local-rag-mcp effectively did after
``changes.list``. Name, path and parent folder are metadata: changing them never re-extracts.
Folders are stored by Drive ID with their parent, and ``document_folders`` holds every ancestor
folder of each document, so "inside folder X or below" is an ID lookup.
Deletion is conservative: a document leaves the index only when every source that held it was
listed completely, or its source root is explicitly gone (404 / not shared). Auth, quota and
network failures never delete anything.

Local folder sources (``local.py``) go through the same pass with their own backend: each source
uses the backend of its kind, so Google access missing or failing never stops local sources and
a missing local mount never stops Drive sources.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import struct
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .chunking import make_chunks
from .config import Settings, ensure_private_dir, setting_origins
from .db import Database
from .drive import (
    GOOGLE_DOC,
    DriveBackend,
    DriveDownloadError,
    DriveFolder,
    DriveItem,
    DriveItemError,
    DriveRoot,
    DriveSourceError,
    Listing,
    display_segment,
    drive_link,
    folder_link,
    is_local_id,
    local_id,
    parse_drive_link,
    split_supported,
    strip_data_images,
)
from .embeddings import EmbeddingClient, EmbeddingError
from .extract import Extractor
from .extraction_errors import ExtractionError
from .local import LocalBackend, default_local_backend, is_local_input
from .rules import EFFECTS, IGNORED_GLOB, clean, clean_ignore, effects, ignore_listing
from .search import EMBEDDING_IDENTITY_KEY, REVISION_KEY, SearchEngine

log = logging.getLogger("gdrive_rag")

BackendFactory = Callable[[Settings], DriveBackend]
EmbeddingFactory = Callable[[Settings], EmbeddingClient | None]
LocalFactory = Callable[[Settings], LocalBackend]
DRIVE, LOCAL = "drive", "local"  # backend families: a source uses the backend of its kind
ERROR_RETRY_HOURS = 24
DOWNLOAD_ERROR_PREFIX = "download: "  # download errors are retried on every sync
EMBED_BATCH = 64
REQUEST_INTERVAL_SECONDS = 60  # sync_now: at most one accepted request per minute
READ_LIMIT_CHARS = 40_000
LIST_PAGE = 100  # folders + documents per list_folders page
SKIPPED_PAGE = 100  # skipped files listed by sync_status(include_skipped=true)
PROGRESS_KEY = "sync_progress"
PROGRESS_INTERVAL_SECONDS = 1.0  # at most one progress write per second (plus phase changes)
_PENDING_SQL = (
    "NOT EXISTS (SELECT 1 FROM vectors v WHERE v.chunk_hash=c.chunk_hash) "
    "AND NOT EXISTS (SELECT 1 FROM embedding_failures f WHERE f.chunk_hash=c.chunk_hash)"
)


class SyncBusy(RuntimeError):
    """Another process or thread is already syncing this data directory."""


class Progress:
    """Live state of the running sync, kept in ``meta`` so ``sync_status`` sees it from another
    thread or process (a CLI sync). Written at phase changes and at most once per
    ``PROGRESS_INTERVAL_SECONDS`` otherwise; removed when the sync ends."""

    def __init__(self, db: Database, sources: list[dict[str, Any]]):
        self.db = db
        self.state: dict[str, Any] = {
            "phase": "listing",
            "started": _now(),
            "sources": [
                {"id": s["id"], "name": s["name"], "files_total": None, "files_done": 0}
                for s in sources
            ],
            "embedding": None,
        }
        self._written = 0.0
        self.write(force=True)

    def source(self, source_id: int) -> dict[str, Any] | None:
        return next((s for s in self.state["sources"] if s["id"] == source_id), None)

    def phase(self, name: str) -> None:
        self.state["phase"] = name
        self.write(force=True)

    def write(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._written < PROGRESS_INTERVAL_SECONDS:
            return
        self._written = now
        self.state["updated"] = _now()
        self.db.set_meta(PROGRESS_KEY, json.dumps(self.state, ensure_ascii=False))

    def clear(self) -> None:
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM meta WHERE key=?", (PROGRESS_KEY,))


def default_backend(settings: Settings) -> DriveBackend:  # pragma: no cover - real Google API
    from .drive import GoogleDriveBackend, load_credentials

    credentials = load_credentials(
        settings.google_auth, settings.service_account_file, settings.token_file
    )
    return GoogleDriveBackend(credentials)


def default_embeddings(settings: Settings) -> EmbeddingClient | None:
    from .embeddings import client_from_settings

    return client_from_settings(settings)


def default_extractor(settings: Settings) -> Extractor:
    from .ocr import TesseractOCR

    return Extractor(
        TesseractOCR(settings.ocr_langs),
        ocr_enabled=settings.ocr_enabled,
        work_dir=settings.work_dir,
    )


class Service:
    def __init__(
        self,
        settings_loader: Callable[[], Settings],
        *,
        backend_factory: BackendFactory = default_backend,
        embedding_factory: EmbeddingFactory = default_embeddings,
        extractor_factory: Callable[[Settings], Extractor] = default_extractor,
        local_factory: LocalFactory = default_local_backend,
    ):
        self._load = settings_loader
        self.settings = settings_loader()
        ensure_private_dir(self.settings.data_dir)
        self.db = Database(self.settings.db_path)
        self.db.migrate()
        self.backend_factory = backend_factory
        self.embedding_factory = embedding_factory
        self.extractor_factory = extractor_factory
        self.local_factory = local_factory
        self.search_engine = SearchEngine(self.db, self._embeddings)
        # On-demand sync requests (MCP ``sync_now``), run by ``server.SyncLoop`` in its thread.
        self.wake = threading.Event()
        self._request_lock = threading.Lock()
        self._requested: tuple[int | None] | None = None  # (source id or None for all)
        self._request_gen = 0  # bumped by every new or widened request
        self._last_request: float | None = None

    # -- settings -----------------------------------------------------------------------------
    def reload(self) -> Settings:
        """Re-read config.json/env so a key or credential added via the CLI takes effect."""
        self.settings = self._load()
        return self.settings

    def _embeddings(self) -> EmbeddingClient | None:
        return self.embedding_factory(self.settings)

    # -- sources ------------------------------------------------------------------------------
    def add_sources(self, links: list[str]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        backend: DriveBackend | None = None
        backend_error = ""
        for link in links:
            if is_local_input(link):
                results.append(self._add_local(link))
                continue
            try:
                root_id = parse_drive_link(link)
            except ValueError as exc:
                results.append({"link": link, "ok": False, "error": str(exc)})
                continue
            existing = self._source_row(root_id)
            if existing is not None:
                results.append({"link": link, "ok": True, "already_added": True, **existing})
                continue
            if backend is None and not backend_error:
                try:
                    backend = self.backend_factory(self.settings)
                except (DriveSourceError, Exception) as exc:
                    backend_error = _message(exc)
            root = DriveRoot(root_id, root_id, "unknown")
            status, error = "pending", backend_error or None
            if backend is not None:
                try:
                    root = backend.resolve(root_id)
                except DriveItemError as exc:
                    status, error = "error", str(exc)
                except DriveSourceError as exc:
                    error = str(exc)
            row = self._insert_source(root, status, error)
            results.append({"link": link, "ok": status != "error", **(row or {})})
        return results

    def _add_local(self, link: str) -> dict[str, Any]:
        """A local source is stored only when the path exists inside the local root now.
        Errors never repeat the path given (it may point outside the root)."""
        try:
            backend = self.local_factory(self.settings)
            root_id = backend.parse(link)
            existing = self._source_row(root_id)
            if existing is not None:
                return {"link": link, "ok": True, "already_added": True, **existing}
            root = backend.resolve(root_id)
        except (ValueError, DriveItemError, DriveSourceError) as exc:
            return {"link": link, "ok": False, "error": str(exc)}
        return {"link": link, "ok": True, **(self._insert_source(root, "pending", None) or {})}

    def _insert_source(
        self, root: DriveRoot, status: str, error: str | None
    ) -> dict[str, Any] | None:
        with self.db.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO sources(link,root_id,kind,drive_id,name,last_status,last_error) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    drive_link(root.id, root.kind),
                    root.id,
                    root.kind,
                    root.drive_id,
                    root.name,
                    status,
                    error,
                ),
            )
            if root.kind in {"folder", "shared_drive", "local_folder"} and status != "error":
                # known at once, so search(folder=<root>) works during the first sync
                _register_folders(
                    connection,
                    int(cursor.lastrowid or 0),
                    [DriveFolder(root.id, root.name, "")],
                )
        return self._source_row(root.id)

    def discard_source(self, source_id: int) -> None:
        """Drop a just-added source that holds no documents yet (no sync lock needed)."""
        with self.db.transaction() as connection:
            connection.execute(
                "DELETE FROM sources WHERE id=? AND NOT EXISTS "
                "(SELECT 1 FROM document_sources WHERE source_id=?)",
                (source_id, source_id),
            )
            _refresh_ancestry(connection)  # drops the root folder registered by add_sources

    def remove_source(self, value: str) -> dict[str, Any]:
        row = self._find_source(value)
        with self._sync_lock(), self.db.transaction() as connection:
            connection.execute("DELETE FROM sources WHERE id=?", (row["id"],))
            removed = _delete_orphans(connection)
            _refresh_ancestry(connection)
            _gc_vectors(connection)
            _bump(connection)
        return {"removed": row["name"], "documents_removed": removed}

    def list_sources(self) -> list[dict[str, Any]]:
        with self.db.read() as connection:
            rows = connection.execute(f"{_SOURCE_SQL} ORDER BY s.id").fetchall()
            rules = _rules_by_source(connection)
        return [_source_json(row, rules.get(int(row["id"]))) for row in rows]

    def _source_row(self, root_id: str) -> dict[str, Any] | None:
        with self.db.read() as connection:
            row = connection.execute(f"{_SOURCE_SQL} WHERE root_id=?", (root_id,)).fetchone()
            rules = _rules_by_source(connection)
        return None if row is None else _source_json(row, rules.get(int(row["id"])))

    # -- path rules ---------------------------------------------------------------------------
    def path_rules(self, source_id: int) -> dict[str, list[str]]:
        with self.db.read() as connection:
            return _rules_by_source(connection).get(source_id) or {e: [] for e in EFFECTS}

    def rule_matches(self, source_id: int, rules: dict[str, list[str]]) -> dict[str, Any]:
        """How many of the source's documents each pattern hits, with a few example paths. For
        ignore entries also the folders, and which other sources lose documents with them (a
        file any source ignores leaves the index)."""
        with self.db.read() as connection:
            rows = connection.execute(
                "SELECT d.id, d.path, d.file_id, d.parent_id FROM documents d "
                "JOIN document_sources ds ON ds.document_id=d.id WHERE ds.source_id=? "
                "ORDER BY d.path",
                (source_id,),
            ).fetchall()
            folder_rows = connection.execute(
                "SELECT f.folder_id, f.name, f.parent_id FROM folders f JOIN folder_sources fs "
                "ON fs.folder_id=f.folder_id WHERE fs.source_id=?",
                (source_id,),
            ).fetchall()
            others = connection.execute(
                "SELECT s.id, s.name, ds.document_id FROM document_sources ds "
                "JOIN sources s ON s.id=ds.source_id WHERE ds.source_id<>? ORDER BY s.id",
                (source_id,),
            ).fetchall()
        documents = [(int(row[0]), str(row[1]), source_id) for row in rows]
        paths = {document_id: path for document_id, path, _ in documents}
        output: dict[str, Any] = {}
        for effect in ("demote", "exclude"):
            per_pattern: dict[str, Any] = {}
            for pattern in rules.get(effect, []):
                hit = effects([(source_id, effect, pattern)], documents)
                per_pattern[pattern] = {
                    "documents": len(hit),
                    "examples": [paths[d] for d in list(hit)[:3]],
                }
            output[effect] = per_pattern
        # the same filter a sync applies, over what the index holds for this source
        listing = Listing(
            [DriveItem(str(r[2]), "", "", str(r[1]), "", "", None, "", str(r[3])) for r in rows],
            folders=[DriveFolder(str(r[0]), str(r[1]), str(r[2])) for r in folder_rows],
        )
        by_file = {str(row[2]): int(row[0]) for row in rows}
        folder_ids = {str(row[0]) for row in folder_rows}

        def ignored(entries: list[str]) -> tuple[set[int], int]:
            _, left_out = ignore_listing(listing, entries)
            return {by_file[i] for i in left_out if i in by_file}, len(left_out & folder_ids)

        output["ignore"] = {}
        for entry in rules.get("ignore", []):
            gone_here, folders = ignored([entry])
            output["ignore"][entry] = {
                "documents": len(gone_here),
                "folders": folders,
                "examples": [path for d, path, _ in documents if d in gone_here][:3],
            }
        gone, gone_folders = ignored(list(rules.get("ignore", [])))
        combined = effects(
            [(source_id, e, p) for e in ("demote", "exclude") for p in rules.get(e, [])],
            documents,
        )
        output["documents"] = len(documents)
        for key, effect in (("demoted", "demote"), ("excluded", "exclude")):
            output[key] = sum(1 for d, (e, _) in combined.items() if e == effect and d not in gone)
        output["ignored"] = len(gone)
        output["ignored_folders"] = gone_folders
        held: dict[int, tuple[str, set[int]]] = {}
        for row in others:
            held.setdefault(int(row[0]), (str(row[1]), set()))[1].add(int(row[2]))
        output["ignored_in_other_sources"] = {
            name: len(ids & gone) for name, ids in held.values() if ids & gone
        }
        output["empties_sources"] = [name for name, ids in held.values() if ids <= gone][:10]
        return output

    def ignore_entries(self, source_id: int, values: Any) -> list[str]:
        """``rules.clean_ignore`` knowing the source's file and folder IDs, so a bare ID of one
        is stored as an ID. An entry naming the source root itself is refused."""
        with self.db.read() as connection:
            root = connection.execute(
                "SELECT root_id FROM sources WHERE id=?", (source_id,)
            ).fetchone()
            known = {
                str(row[0])
                for row in connection.execute(
                    "SELECT d.file_id FROM documents d JOIN document_sources ds "
                    "ON ds.document_id=d.id WHERE ds.source_id=? "
                    "UNION SELECT folder_id FROM folder_sources WHERE source_id=? "
                    "UNION SELECT file_id FROM skipped_files WHERE source_id=?",
                    (source_id, source_id, source_id),
                )
            }
        entries = clean_ignore(values, known.__contains__)
        if root is not None and f"id:{root[0]}" in entries:
            raise ValueError(
                "ignore: an entry names the source itself; remove_source drops a whole source"
            )
        return entries

    def set_path_rules(self, source_id: int, rules: dict[str, list[str]]) -> dict[str, Any]:
        """Replace the source's rules. demote and exclude are search-time only: no document,
        chunk or vector changes. When the ignore entries change, the source's 'ignored by rule'
        skipped files are dropped (the next sync lists them under the new entries) and a sync
        of every source is queued: it removes newly ignored files from the index (from every
        source that lists them) and indexes files no longer ignored."""
        cleaned = {effect: clean(rules.get(effect, []), effect) for effect in ("demote", "exclude")}
        cleaned["ignore"] = self.ignore_entries(source_id, rules.get("ignore", []))
        changed = cleaned["ignore"] != self.path_rules(source_id)["ignore"]
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM path_rules WHERE source_id=?", (source_id,))
            connection.executemany(
                "INSERT INTO path_rules(source_id, effect, pattern) VALUES(?,?,?)",
                [(source_id, e, p) for e in EFFECTS for p in cleaned[e]],
            )
            if changed:
                connection.execute(
                    "DELETE FROM skipped_files WHERE source_id=? AND reason GLOB ?",
                    (source_id, IGNORED_GLOB),
                )
        return {"sync": self.queue_sync() if changed else None}

    def skipped_files(self, limit: int = SKIPPED_PAGE) -> dict[str, Any]:
        with self.db.read() as connection:
            total = int(connection.execute("SELECT count(*) FROM skipped_files").fetchone()[0])
            rows = connection.execute(
                "SELECT s.name source, k.path, k.mime_type, k.size, k.reason "
                "FROM skipped_files k JOIN sources s ON s.id=k.source_id "
                "ORDER BY s.id, k.path LIMIT ?",
                (limit,),
            ).fetchall()
        return {"total": total, "files": [dict(row) for row in rows]}

    def _refuse_vanished_folder(self, source: dict[str, Any], listing: Listing) -> None:
        """A local folder source that lists nothing at all while it holds indexed documents is
        most likely an unplugged disk or a failed mount that left an empty mount point: stop
        (``DriveSourceError``, nothing deleted) instead of clearing it. A folder that was always
        empty, and one that still lists subfolders, is fine."""
        if source["kind"] != "local_folder" or listing.items or listing.skipped:
            return
        if not listing.complete or len(listing.folders) > 1:
            return
        with self.db.read() as connection:
            held = connection.execute(
                "SELECT 1 FROM document_sources WHERE source_id=? LIMIT 1", (source["id"],)
            ).fetchone()
        if held is not None:
            raise DriveSourceError(
                "this local folder is empty now, which usually means the disk or mount is "
                "missing; nothing is removed from the index until it has files again (remove "
                "the source to clear it on purpose)"
            )

    def _find_source(self, value: str) -> dict[str, Any]:
        with contextlib.suppress(ValueError):
            value = parse_drive_link(value)
        if value.startswith("/") and self.settings.local_root is not None:
            # an absolute path inside the local root names that local source (lexically: the
            # path itself may be gone, which is why the source is being removed)
            # resolved like LocalBackend.parse does, so a root reached through a link works
            root = os.path.realpath(self.settings.local_root)
            inside = os.path.realpath(value)
            if os.path.commonpath([root, inside]) == root:
                value = local_id(os.path.relpath(inside, root).replace(os.sep, "/"))
        with self.db.read() as connection:
            row = connection.execute(
                "SELECT * FROM sources WHERE CAST(id AS TEXT)=? OR root_id=? OR name=? "
                "ORDER BY id LIMIT 1",
                (value, value, value),
            ).fetchone()
        if row is None:
            raise ValueError("unknown source (list_sources shows the names and ids)")
        return dict(row)

    # -- sync ---------------------------------------------------------------------------------
    @contextlib.contextmanager
    def _sync_lock(self) -> Iterator[None]:
        path = self.settings.data_dir / "sync.lock"
        handle = path.open("a+")
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SyncBusy("another sync is already running") from None
            marker = self.settings.data_dir / "sync.running"
            marker.write_text(str(os.getpid()))
            try:
                yield
            finally:
                marker.unlink(missing_ok=True)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def sync_running(self) -> bool:
        """Read the PID marker written under the lock; never touch the lock itself, so a status
        call cannot make a scheduled sync skip its cycle."""
        marker = self.settings.data_dir / "sync.running"
        try:
            pid = int(marker.read_text().strip())
        except (OSError, ValueError):
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False  # stale marker from a killed process
        except PermissionError:
            return True
        return True

    def request_sync(self, source: str | None = None) -> dict[str, Any]:
        """Ask the sync loop for a sync now (all sources, or one). Returns at once."""
        source_id = self.search_engine.source_id(source) if source else None
        with self._request_lock:
            now = time.monotonic()
            if self._last_request is not None:
                wait = REQUEST_INTERVAL_SECONDS - (now - self._last_request)
                if wait > 0:
                    return self._request_answer(
                        False, "rate_limited", retry_after_seconds=math.ceil(wait)
                    )
            if self._requested is not None or self.sync_running():
                return self._request_answer(False, "already_running")
            self._requested = (source_id,)
            self._request_gen += 1
            self._last_request = now
        self.wake.set()
        return self._request_answer(True, None)

    def queue_sync(self, source_id: int | None = None) -> dict[str, Any]:
        """Queue a sync for an admin change (a new source, a new embedding endpoint).

        Not subject to the ``sync_now`` rate limit and does not use up its minute: admin changes
        are rare and confirmed by the user. Calls coalesce, so at most one queued sync waits; a
        sync that is running (requested or scheduled) finishes first and the queued one runs
        once after it, because ``finish_request`` only clears a request that did not change
        while it ran."""
        with self._request_lock:
            if self._requested is not None and self._requested[0] != source_id:
                self._requested = (None,)  # two different requests: one sync of everything
            elif self._requested is None:
                self._requested = (source_id,)
            self._request_gen += 1
            running = self.sync_running()
        self.wake.set()
        return {"queued": True, "after_running_sync": running}

    def source_preview(self, value: str) -> dict[str, Any]:
        """What ``remove_source`` would do: the source and how many documents only it holds."""
        row = self._find_source(value)
        with self.db.read() as connection:
            only = connection.execute(
                """SELECT count(*) FROM document_sources d WHERE d.source_id=? AND NOT EXISTS
                     (SELECT 1 FROM document_sources o
                      WHERE o.document_id=d.document_id AND o.source_id<>d.source_id)""",
                (row["id"],),
            ).fetchone()[0]
        return {
            "id": int(row["id"]),
            "name": row["name"],
            "link": row["link"],
            "root_id": row["root_id"],
            "documents_removed": int(only),
        }

    def take_request(self) -> tuple[int | None] | None:
        """The pending request, if any (status uses it; the sync loop uses ``begin_request``)."""
        with self._request_lock:
            return self._requested

    def begin_request(self) -> tuple[int | None, int] | None:
        """(source id, generation) of the pending request; pass the generation back to
        ``finish_request`` after running it."""
        with self._request_lock:
            if self._requested is None:
                return None
            return self._requested[0], self._request_gen

    def finish_request(self, generation: int | None = None) -> None:
        """Clear the request just run, unless a newer one was queued meanwhile."""
        with self._request_lock:
            if generation is None or generation == self._request_gen:
                self._requested = None

    def _request_answer(self, started: bool, reason: str | None, **extra: Any) -> dict[str, Any]:
        answer: dict[str, Any] = {"started": started}
        if reason:
            answer["reason"] = reason
        answer.update(extra)
        answer["running"] = started or self.sync_running()
        answer["last_report"] = json.loads(self.db.get_meta("last_sync_report") or "null")
        return answer

    def sync(self, trigger: str = "manual", source_id: int | None = None) -> dict[str, Any]:
        """Run one sync. ``trigger`` is ``scheduled``, ``requested`` (MCP sync_now) or ``manual``
        (CLI); ``source_id`` limits it to one source."""
        self.reload()
        with self._sync_lock():
            started = _now()
            self.db.set_meta("last_sync_started", started)
            self.db.set_meta("last_sync_trigger", trigger)
            every = self.list_sources()
            progress = Progress(
                self.db, [s for s in every if source_id is None or s["id"] == source_id]
            )
            try:
                report = self._sync(source_id, progress)
                report["trigger"] = trigger
                report["embeddings"] = self.embed_pending(progress)
                self.db.set_meta("last_sync_finished", _now())
                self.db.set_meta("last_sync_report", json.dumps(_summary(report)))
            finally:
                progress.clear()
            return report

    def _sync(
        self, source_id: int | None = None, progress: Progress | None = None
    ) -> dict[str, Any]:
        every = self.list_sources()
        sources = [s for s in every if source_id is None or s["id"] == source_id]
        report: dict[str, Any] = {"sources": [], "indexed": 0, "unchanged": 0, "failed": 0}
        if not sources:
            return report
        # sources whose first sync has not finished: their documents and folders are linked
        # while the pass runs, so search can scope to them before the pass ends
        fresh = {s["id"] for s in sources if not s["first_sync_done"]}
        # one backend per family, made only when a source of that family is synced: Google
        # access not configured (or failing) leaves local sources alone, and the other way round
        backends: dict[str, DriveBackend] = {}
        unavailable: dict[str, str] = {}
        for family in sorted({_family(s["root_id"]) for s in sources}):
            try:
                if family == LOCAL:
                    backends[family] = self.local_factory(self.settings)
                else:
                    backends[family] = self.backend_factory(self.settings)
            except Exception as exc:
                unavailable[family] = _message(exc)
        if not backends:
            for source in sources:
                message = unavailable[_family(source["root_id"])]
                self._set_source_status(source["id"], "error", message)
                report["sources"].append({"name": source["name"], "ok": False, "error": message})
            return report
        listings: dict[int, Listing | None] = {}  # None: root gone -> clear membership
        # failed, incomplete or left out of this pass (a one-source sync): such a source may
        # still hold a folder with its parent, so that parent is kept
        degraded: set[int] = {s["id"] for s in every} - {s["id"] for s in sources}
        ignore = {s["id"]: s["path_rules"]["ignore"] for s in sources}
        left_out: dict[int, set[str]] = {}  # IDs each listing's own ignore entries dropped
        for source in sources:
            outcome = {"name": source["name"], "ok": True}
            family = _family(source["root_id"])
            if family in unavailable:
                degraded.add(source["id"])
                self._set_source_status(source["id"], "error", unavailable[family])
                outcome.update(ok=False, error=unavailable[family])
                report["sources"].append(outcome)
                continue
            backend = backends[family]
            try:
                root = DriveRoot(
                    source["root_id"], source["name"], source["kind"], source["drive_id"]
                )
                if root.kind == "unknown" or root.name == root.id:
                    root = backend.resolve(root.id)
                    self._update_root(source["id"], root)
                    outcome["name"] = root.name
                listing = backend.list_files(root)
                self._refuse_vanished_folder(source, listing)
                # ignore rules: never downloaded, not listed membership, folders not registered
                listing, left_out[source["id"]] = ignore_listing(listing, ignore[source["id"]])
                kept, skipped = split_supported(listing.items)  # also for backends that list all
                listing = Listing(
                    kept,
                    listing.complete,
                    listing.errors,
                    listing.folders,
                    [*listing.skipped, *skipped],
                )
                listings[source["id"]] = listing
                with self.db.transaction() as connection:
                    _register_folders(connection, source["id"], listing.folders)
                if progress is not None and (entry := progress.source(source["id"])):
                    entry["files_total"] = len(listing.items)
                    progress.write()
                status = "ok" if listing.complete else "partial"
                if not listing.complete:
                    degraded.add(source["id"])
                error = "; ".join(listing.errors[:3]) or None
                self._set_source_status(source["id"], status, error, len(listing.items))
                outcome.update(files=len(listing.items), complete=listing.complete)
            except DriveItemError as exc:
                listings[source["id"]] = None
                self._set_source_status(source["id"], "error", str(exc), 0)
                outcome.update(ok=False, error=str(exc))
            except Exception as exc:  # auth/quota/network: keep everything as it is
                degraded.add(source["id"])
                self._set_source_status(source["id"], "error", _message(exc))
                outcome.update(ok=False, error=_message(exc))
            report["sources"].append(outcome)
        self._ignore_everywhere(listings, left_out)
        unique: dict[str, DriveItem] = {}
        holders: dict[str, list[int]] = {}
        for listed_by, found in listings.items():
            for item in found.items if found else ():
                unique.setdefault(item.id, item)
                holders.setdefault(item.id, []).append(listed_by)
        extractor = self.extractor_factory(self.settings)
        stopped: dict[str, str] = {}  # family -> why its pass stopped
        if progress is not None:
            progress.phase("extracting")
        for item in unique.values():
            family = _family(item.id)
            if family in stopped:
                continue
            try:
                report[self._index_item(backends[family], extractor, item)] += 1
            except DriveSourceError as exc:  # sign-in, connection or mount: stop, delete nothing
                stopped[family] = str(exc)
                report["failed"] += 1
            early = [h for h in holders[item.id] if h in fresh]
            if early and family not in stopped:
                self._link_early(item, early)
            if progress is not None:
                for holder in holders[item.id]:
                    if entry := progress.source(holder):
                        entry["files_done"] += 1
                progress.write()
        if stopped:
            report["stopped"] = "; ".join(stopped[f] for f in sorted(stopped))
        # sources whose family stopped: their membership is kept as it is, like a failed source
        root_ids = {s["id"]: str(s["root_id"]) for s in sources}
        halted = {sid for sid in listings if _family(root_ids[sid]) in stopped}
        folders: dict[str, DriveFolder] = {}
        for found in listings.values():  # a folder seen as a root and as a child: keep the parent
            for folder in found.folders if found else ():
                known = folders.get(folder.id)
                if known is None or (folder.parent_id and not known.parent_id):
                    folders[folder.id] = folder
        with self.db.transaction() as connection:
            # A folder no listing gives a parent keeps its stored parent only when a source that
            # held it failed or was incomplete (or the pass stopped): that source may still have it.
            held: dict[str, set[int]] = {}
            for row in connection.execute("SELECT folder_id, source_id FROM folder_sources"):
                held.setdefault(str(row[0]), set()).add(int(row[1]))
            connection.executemany(
                """INSERT INTO folders(folder_id, name, parent_id) VALUES(?,?,?)
                   ON CONFLICT(folder_id) DO UPDATE SET name=excluded.name,
                     parent_id=CASE WHEN excluded.parent_id='' AND ? THEN folders.parent_id
                                    ELSE excluded.parent_id END""",
                [
                    (
                        f.id,
                        f.name,
                        f.parent_id,
                        int(bool(held.get(f.id, set()) & (degraded | halted))),
                    )
                    for f in folders.values()
                ],
            )
            ids = {
                str(row[0]): int(row[1])
                for row in connection.execute("SELECT file_id, id FROM documents")
            }
            for source_id, found in listings.items():
                if found is None or (found.complete and source_id not in halted):  # as documents
                    connection.execute("DELETE FROM folder_sources WHERE source_id=?", (source_id,))
                connection.executemany(
                    "INSERT OR IGNORE INTO folder_sources VALUES(?,?)",
                    [(folder.id, source_id) for folder in (found.folders if found else ())],
                )
                if found is None or (found.complete and source_id not in halted):
                    connection.execute(
                        "DELETE FROM document_sources WHERE source_id=?", (source_id,)
                    )
                for item in found.items if found else ():
                    if item.id in ids:
                        connection.execute(
                            "INSERT OR IGNORE INTO document_sources VALUES(?,?)",
                            (ids[item.id], source_id),
                        )
                if found is None or found.complete:
                    connection.execute("DELETE FROM skipped_files WHERE source_id=?", (source_id,))
                connection.executemany(
                    "INSERT OR REPLACE INTO skipped_files VALUES(?,?,?,?,?,?)",
                    [
                        (source_id, f.id, f.path, f.mime_type, f.size, f.reason)
                        for f in (found.skipped if found else ())
                    ],
                )
                if found is not None and source_id not in halted:
                    connection.execute(
                        "UPDATE sources SET first_synced_at=? "
                        "WHERE id=? AND first_synced_at IS NULL",
                        (_now(), source_id),
                    )
            report["removed"] = _delete_orphans(connection)
            _refresh_ancestry(connection)
            _gc_vectors(connection)
            _bump(connection)
        return report

    def _ignore_everywhere(
        self, listings: dict[int, Listing | None], left_out: dict[int, set[str]]
    ) -> None:
        """A file or folder that one source's ignore entries dropped is dropped from every
        listing of this pass, and so is a file a source left out of this pass ignored when it
        was last listed (its 'ignored by rule' skipped rows)."""
        listed = sorted(listings)
        with self.db.read() as connection:
            remembered = {
                str(row[0])
                for row in connection.execute(
                    "SELECT file_id FROM skipped_files WHERE reason GLOB ? "
                    f"AND source_id NOT IN ({','.join('?' for _ in listed)})",
                    [IGNORED_GLOB, *listed],
                )
            }
        everywhere = remembered.union(*left_out.values())
        if not everywhere:
            return
        for source_id, found in listings.items():
            if found is not None:
                listings[source_id], _ = ignore_listing(found, (), everywhere)

    def _link_early(self, item: DriveItem, source_ids: list[int]) -> None:
        """During a first sync: link a processed file to its sources and ancestor folders now
        (additive; the end of the pass replaces membership from complete listings)."""
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT id FROM documents WHERE file_id=?", (item.id,)
            ).fetchone()
            if row is None:
                return
            connection.executemany(
                "INSERT OR IGNORE INTO document_sources VALUES(?,?)",
                [(int(row[0]), source_id) for source_id in source_ids],
            )
            connection.execute(
                """WITH RECURSIVE up(id) AS (
                     SELECT ? UNION
                     SELECT f.parent_id FROM folders f JOIN up ON f.folder_id=up.id
                     WHERE f.parent_id!='')
                   INSERT OR IGNORE INTO document_folders
                   SELECT ?, id FROM up WHERE id IN (SELECT folder_id FROM folders)""",
                (item.parent_id, int(row[0])),
            )

    def _index_item(self, backend: DriveBackend, extractor: Extractor, item: DriveItem) -> str:
        """Index one file; return the report counter: indexed, unchanged or failed."""
        with self.db.read() as connection:
            current = connection.execute(
                "SELECT id, fingerprint, status, error, indexed_at, name, path, web_url, "
                "parent_id, metadata_json FROM documents WHERE file_id=?",
                (item.id,),
            ).fetchone()
        if current is not None and current["fingerprint"] == item.fingerprint:
            retry = (
                current["status"] == "error"
                and (
                    str(current["error"] or "").startswith(DOWNLOAD_ERROR_PREFIX)
                    or _older_than(current["indexed_at"], ERROR_RETRY_HOURS)
                )
            ) or _stale_ocr(current["metadata_json"], extractor)
            if not retry:
                if (
                    current["name"],
                    current["path"],
                    current["web_url"],
                    current["parent_id"],
                ) != (item.name, item.path, item.web_url, item.parent_id):
                    self._update_metadata(int(current["id"]), item)
                return "unchanged"
        ensure_private_dir(self.settings.work_dir)
        temporary: Path | None = None
        try:
            try:
                download = backend.download(item)
            except DriveDownloadError as exc:
                self._store_failure(item, f"{DOWNLOAD_ERROR_PREFIX}{exc}")
                log.warning("could not download %s: %s", item.path, exc)
                return "failed"
            try:
                data = download.data
                if item.mime_type == GOOGLE_DOC:
                    data = strip_data_images(data)
                temporary = self.settings.work_dir / f"{_temp_name(item.id)}{download.suffix}"
                temporary.write_bytes(data)
                document = extractor.extract(temporary)
            except Exception as exc:
                self._store_failure(item, _message(exc))
                log.warning("could not index %s: %s", item.path, _message(exc))
                return "failed"
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        chunks = make_chunks(document, self.settings.chunk_chars, self.settings.chunk_overlap)
        metadata = dict(document.metadata)
        with self.db.transaction() as connection:
            document_id = self._upsert_document(
                connection, item, document.media_type, metadata, "indexed", None, len(chunks)
            )
            connection.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
            connection.executemany(
                "INSERT INTO chunks(document_id,ordinal,text,locator,chunk_hash) VALUES(?,?,?,?,?)",
                [(document_id, c.ordinal, c.text, c.locator, c.chunk_hash) for c in chunks],
            )
        log.info("indexed %s (%d chunks)", item.path, len(chunks))
        return "indexed"

    def _update_metadata(self, document_id: int, item: DriveItem) -> None:
        """Rename or move: refresh name/path/link/parent and the FTS title, keep chunks and
        vectors. Ancestry follows from ``parent_id`` at the end of the sync."""
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE documents SET name=?, path=?, web_url=?, modified_time=?, parent_id=? "
                "WHERE id=?",
                (
                    item.name,
                    item.path,
                    item.web_url,
                    item.modified_time,
                    item.parent_id,
                    document_id,
                ),
            )
            connection.execute(
                "UPDATE chunks_fts SET title=fts_fold(?) "
                "WHERE rowid IN (SELECT id FROM chunks WHERE document_id=?)",
                (item.name, document_id),
            )

    def _store_failure(self, item: DriveItem, error: str) -> None:
        with self.db.transaction() as connection:
            document_id = self._upsert_document(connection, item, "", {}, "error", error, 0)
            connection.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))

    @staticmethod
    def _upsert_document(
        connection: Any,
        item: DriveItem,
        media_type: str,
        metadata: dict[str, Any],
        status: str,
        error: str | None,
        chunk_count: int,
    ) -> int:
        connection.execute(
            """INSERT INTO documents(file_id,name,path,mime_type,web_url,modified_time,fingerprint,
                                     media_type,metadata_json,status,error,chunk_count,indexed_at,
                                     parent_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(file_id) DO UPDATE SET name=excluded.name, path=excluded.path,
                 parent_id=excluded.parent_id,
                 mime_type=excluded.mime_type, web_url=excluded.web_url,
                 modified_time=excluded.modified_time, fingerprint=excluded.fingerprint,
                 media_type=excluded.media_type, metadata_json=excluded.metadata_json,
                 status=excluded.status, error=excluded.error, chunk_count=excluded.chunk_count,
                 indexed_at=excluded.indexed_at""",
            (
                item.id,
                item.name,
                item.path,
                item.mime_type,
                item.web_url,
                item.modified_time,
                item.fingerprint,
                media_type,
                json.dumps(metadata, ensure_ascii=False, default=str),
                status,
                error,
                chunk_count,
                _now(),
                item.parent_id,
            ),
        )
        row = connection.execute("SELECT id FROM documents WHERE file_id=?", (item.id,)).fetchone()
        return int(row[0])

    def _set_source_status(
        self, source_id: int, status: str, error: str | None, file_count: int | None = None
    ) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE sources SET last_status=?, last_error=?, last_sync_at=?, "
                "file_count=COALESCE(?, file_count) WHERE id=?",
                (status, error, _now(), file_count, source_id),
            )

    def _update_root(self, source_id: int, root: DriveRoot) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE sources SET name=?, kind=?, drive_id=? WHERE id=?",
                (root.name, root.kind, root.drive_id, source_id),
            )

    # -- embeddings ---------------------------------------------------------------------------
    def embed_pending(self, progress: Progress | None = None) -> dict[str, Any]:
        """Embed chunks without a vector. An item the endpoint rejects is isolated by splitting
        its batch, recorded in ``embedding_failures`` and skipped; any other failure stops the
        pass (it resumes on the next sync)."""
        client = self._embeddings()
        if client is None:
            return {"enabled": False, "embedded": 0, "pending": self._pending_vectors()}
        if self.db.get_meta(EMBEDDING_IDENTITY_KEY) != client.identity:
            with self.db.transaction() as connection:
                connection.execute("DELETE FROM vectors")
                connection.execute("DELETE FROM embedding_failures")
                connection.execute(
                    "INSERT INTO meta(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (EMBEDDING_IDENTITY_KEY, client.identity),
                )
                _bump(connection)
        embedded = 0
        if progress is not None:
            progress.state["embedding"] = {"done": 0, "pending": self._pending_vectors()}
            progress.phase("embedding")
        while True:
            with self.db.read() as connection:
                rows = connection.execute(
                    f"""SELECT chunk_hash, text FROM chunks c WHERE {_PENDING_SQL}
                        GROUP BY chunk_hash LIMIT ?""",
                    (EMBED_BATCH,),
                ).fetchall()
            if not rows:
                break
            try:
                vectors, failures = self._embed_rows(client, rows)
            except Exception as exc:
                message = _message(exc)
                self.db.set_meta("last_embedding_error", message)
                log.warning("embedding stopped: %s", message)
                return {
                    "enabled": True,
                    "embedded": embedded,
                    "pending": self._pending_vectors(),
                    "error": message,
                }
            with self.db.transaction() as connection:
                connection.executemany(
                    "INSERT OR REPLACE INTO vectors(chunk_hash, vector) VALUES(?,?)",
                    [(key, struct.pack(f"<{len(v)}f", *v)) for key, v in vectors.items()],
                )
                connection.executemany(
                    "INSERT OR REPLACE INTO embedding_failures(chunk_hash, error) VALUES(?,?)",
                    list(failures.items()),
                )
                _bump(connection)
            embedded += len(vectors)
            if progress is not None:
                progress.state["embedding"]["done"] = embedded + len(failures)
                progress.write()
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM meta WHERE key='last_embedding_error'")
        return {"enabled": True, "embedded": embedded, "pending": 0}

    def _embed_rows(
        self, client: EmbeddingClient, rows: list[Any]
    ) -> tuple[dict[str, list[float]], dict[str, str]]:
        """Embed ``rows``; on a content rejection split the batch until the bad item is alone."""
        try:
            vectors = client.embed_documents([str(row["text"]) for row in rows])
        except EmbeddingError as exc:
            if not exc.rejected:
                raise
            if len(rows) == 1:
                return {}, {str(rows[0]["chunk_hash"]): str(exc)}
            middle = len(rows) // 2
            left = self._embed_rows(client, rows[:middle])
            right = self._embed_rows(client, rows[middle:])
            return {**left[0], **right[0]}, {**left[1], **right[1]}
        return {str(row["chunk_hash"]): v for row, v in zip(rows, vectors, strict=True)}, {}

    def _pending_vectors(self) -> int:
        with self.db.read() as connection:
            return int(
                connection.execute(
                    f"SELECT count(DISTINCT chunk_hash) FROM chunks c WHERE {_PENDING_SQL}"
                ).fetchone()[0]
            )

    def _failed_vectors(self) -> int:
        with self.db.read() as connection:
            return int(connection.execute("SELECT count(*) FROM embedding_failures").fetchone()[0])

    # -- reads --------------------------------------------------------------------------------
    def search(
        self,
        query: str,
        source: str | None = None,
        limit: int = 8,
        folder: str | None = None,
        files: list[str] | None = None,
        explain: bool = False,
    ) -> dict[str, Any]:
        self.reload()
        return self.search_engine.search(query, source, limit, folder, files, explain)

    def list_folders(
        self,
        source: str | None = None,
        parent: str | None = None,
        include_documents: bool = False,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Direct child folders of ``parent`` (or the top folders), with indexed-document counts
        underneath; optionally the documents directly inside ``parent``. Folders then documents
        form one sequence paged by ``offset``; ``next_offset`` is None on the last page."""
        if offset < 0:
            raise ValueError("offset must be >= 0")
        source_id = self.search_engine.source_id(source) if source else None
        with self.db.read() as connection:
            parent_row = None
            values: list[Any]
            if parent:
                parent_id = parse_drive_link(parent)
                parent_row = connection.execute(
                    "SELECT * FROM folders WHERE folder_id=?", (parent_id,)
                ).fetchone()
                if parent_row is None:
                    raise ValueError(f"unknown or not indexed folder: {parent}")
                where, values = "f.parent_id=?", [parent_id]
            elif source_id is not None:
                where, values = "f.folder_id=(SELECT root_id FROM sources WHERE id=?)", [source_id]
            else:
                where, values = "f.parent_id NOT IN (SELECT folder_id FROM folders)", []
            if source_id is not None:
                where += (
                    " AND f.folder_id IN (SELECT folder_id FROM folder_sources WHERE source_id=?)"
                )
                values.append(source_id)
            folder_total = int(
                connection.execute(
                    f"SELECT count(*) FROM folders f WHERE {where}", values
                ).fetchone()[0]
            )
            in_source = ""
            count_values: list[Any] = []
            if source_id is not None:
                in_source = (
                    " AND d.id IN (SELECT document_id FROM document_sources WHERE source_id=?)"
                )
                count_values.append(source_id)
            folders = connection.execute(
                f"""SELECT f.folder_id, f.name, f.path,
                      (SELECT count(*) FROM document_folders df
                       JOIN documents d ON d.id=df.document_id
                       WHERE df.folder_id=f.folder_id AND d.status='indexed'{in_source})
                       documents,
                      (SELECT count(*) FROM folders c WHERE c.parent_id=f.folder_id) subfolders
                    FROM folders f WHERE {where} ORDER BY f.name, f.folder_id
                    LIMIT ? OFFSET ?""",
                [*count_values, *values, LIST_PAGE, offset],
            ).fetchall()
            documents: list[Any] = []
            document_total = 0
            if include_documents and parent_row is not None:
                sql = "SELECT * FROM documents d WHERE d.parent_id=?"
                params: list[Any] = [parent_row["folder_id"]]
                if source_id is not None:
                    sql += (
                        " AND d.id IN (SELECT document_id FROM document_sources WHERE source_id=?)"
                    )
                    params.append(source_id)
                document_total = int(
                    connection.execute(
                        sql.replace("SELECT *", "SELECT count(*)", 1), params
                    ).fetchone()[0]
                )
                room = LIST_PAGE - len(folders)
                if room > 0:
                    documents = connection.execute(
                        f"{sql} ORDER BY d.name, d.file_id LIMIT ? OFFSET ?",
                        [*params, room, max(0, offset - folder_total)],
                    ).fetchall()
        total = folder_total + document_total
        end = offset + len(folders) + len(documents)
        return {
            "parent": None if parent_row is None else _folder_json(parent_row),
            "folders": [
                _folder_json(row)
                | {"documents": int(row["documents"]), "subfolders": int(row["subfolders"])}
                for row in folders
            ],
            "documents": [
                {
                    "document_id": row["file_id"],
                    "file_name": row["name"],
                    "path": row["path"],
                    "drive_link": row["web_url"],
                    "status": row["status"],
                }
                for row in documents
            ],
            "offset": offset,
            "total": total,
            "next_offset": end if end < total else None,
            "truncated": end < total,
        }

    def read(
        self,
        document_id: str,
        start_chunk: int = 0,
        max_chunks: int = 10,
        page: int | None = None,
        max_chars: int = READ_LIMIT_CHARS,
    ) -> dict[str, Any]:
        if start_chunk < 0 or not 1 <= max_chunks <= 100:
            raise ValueError("start_chunk must be >= 0 and max_chunks between 1 and 100")
        keys = [document_id.strip()]
        with contextlib.suppress(ValueError):
            keys.append(parse_drive_link(document_id))  # a Drive file link or ID
        with self.db.read() as connection:
            document = connection.execute(
                "SELECT * FROM documents WHERE file_id IN (?,?) LIMIT 1", (keys[0], keys[-1])
            ).fetchone()
            if document is None:
                raise ValueError(f"unknown document id: {document_id}")
            rows = connection.execute(
                "SELECT ordinal, text, locator FROM chunks WHERE document_id=? ORDER BY ordinal",
                (document["id"],),
            ).fetchall()
        if int(document["id"]) in self.search_engine.ignored_documents():
            # being removed by the next sync: already treated as not indexed
            raise ValueError(f"document {document_id} is ignored by a path rule; not indexed")
        if page is not None:
            matching = [r for r in rows if _locator_has_page(str(r["locator"]), page)]
            start_chunk = int(matching[0]["ordinal"]) if matching else len(rows)
        selected = [r for r in rows if int(r["ordinal"]) >= start_chunk][:max_chunks]
        parts: list[dict[str, Any]] = []
        used = 0
        truncated = False
        for row in selected:
            text = str(row["text"])
            if used + len(text) > max_chars:
                if not parts:
                    parts.append(
                        {
                            "chunk": int(row["ordinal"]),
                            "locator": row["locator"],
                            "text": text[:max_chars],
                        }
                    )
                truncated = True
                break
            parts.append({"chunk": int(row["ordinal"]), "locator": row["locator"], "text": text})
            used += len(text)
        next_chunk = parts[-1]["chunk"] + 1 if parts else start_chunk
        metadata = json.loads(document["metadata_json"] or "{}")
        return {
            "document_id": document["file_id"],
            "file_name": document["name"],
            "path": document["path"],
            "drive_link": document["web_url"],
            "status": document["status"],
            "error": document["error"],
            "ocr_unavailable": bool(metadata.get("ocr_unavailable")),
            "chunk_count": int(document["chunk_count"]),
            "chunks": parts,
            "next_chunk": next_chunk if next_chunk < len(rows) else None,
            "truncated": truncated,
        }

    def status(self) -> dict[str, Any]:
        settings = self.reload()
        with self.db.read() as connection:
            counts = connection.execute(
                """SELECT count(*) total,
                          sum(status='indexed') n_indexed,
                          sum(status='error') n_failed,
                          sum(json_extract(metadata_json,'$.ocr_unavailable') = 1) ocr_missing
                   FROM documents"""
            ).fetchone()
            chunks = int(connection.execute("SELECT count(*) FROM chunks").fetchone()[0])
            vectors = int(connection.execute("SELECT count(*) FROM vectors").fetchone()[0])
            failures = [
                {"file_name": r["name"], "path": r["path"], "error": r["error"]}
                for r in connection.execute(
                    "SELECT name, path, error FROM documents WHERE status='error' "
                    "ORDER BY indexed_at DESC LIMIT 20"
                )
            ]
        running = self.sync_running()
        progress = json.loads(self.db.get_meta(PROGRESS_KEY) or "null") if running else None
        with self.db.read() as connection:
            skipped = int(connection.execute("SELECT count(*) FROM skipped_files").fetchone()[0])
        return {
            "sources": self.list_sources(),
            "skipped": {"total": skipped},
            "documents": {
                "total": int(counts["total"] or 0),
                "indexed": int(counts["n_indexed"] or 0),
                "failed": int(counts["n_failed"] or 0),
                "ocr_unavailable": int(counts["ocr_missing"] or 0),
            },
            "chunks": chunks,
            "recent_failures": failures,
            "embeddings": {
                "enabled": settings.embeddings_enabled,
                "host": urlparse(settings.embedding_base_url).hostname or "",
                "model": settings.embedding_model,
                "dimensions": settings.embedding_dimensions,
                "vectors": vectors,
                "pending": self._pending_vectors(),
                "rejected": self._failed_vectors(),
                "last_error": self.db.get_meta("last_embedding_error"),
                "key_origin": settings.public()["embedding_key_origin"],
                "warning": settings.embedding_warning,
            },
            "google_auth": settings.google_auth,
            "local_folder": self._local_state(),
            "settings_from": setting_origins(settings.data_dir),
            "sync": {
                "running": running,
                "progress": progress,
                "requested": self.take_request() is not None,
                "trigger": self.db.get_meta("last_sync_trigger"),
                "interval_minutes": settings.sync_interval_minutes,
                "last_started": self.db.get_meta("last_sync_started"),
                "last_finished": self.db.get_meta("last_sync_finished"),
                "last_report": json.loads(self.db.get_meta("last_sync_report") or "null"),
            },
        }

    def _local_state(self) -> dict[str, Any]:
        """Whether local sources can be read: GDRAG_LOCAL_ROOT set, mounted and not empty."""
        try:
            backend = self.local_factory(self.settings)
        except DriveSourceError as exc:
            return {"configured": False, "ok": False, "error": str(exc)}
        try:
            backend.check()
        except DriveSourceError as exc:
            return {"configured": True, "ok": False, "error": str(exc)}
        return {"configured": True, "ok": True, "error": None}


_SOURCE_SQL = """SELECT s.*,
    (SELECT count(*) FROM document_sources d WHERE d.source_id=s.id) documents,
    (SELECT count(*) FROM skipped_files k WHERE k.source_id=s.id) skipped
    FROM sources s"""


def _rules_by_source(connection: Any) -> dict[int, dict[str, list[str]]]:
    rules: dict[int, dict[str, list[str]]] = {}
    for row in connection.execute(
        "SELECT source_id, effect, pattern FROM path_rules ORDER BY rowid"
    ):
        entry = rules.setdefault(int(row[0]), {e: [] for e in EFFECTS})
        entry[str(row[1])].append(str(row[2]))
    return rules


def _register_folders(connection: Any, source_id: int, folders: Any) -> None:
    """Additive: make listed folders known (new ones only) with this source as a holder, and
    recompute paths. The end of a sync replaces membership as usual."""
    connection.executemany(
        "INSERT OR IGNORE INTO folders(folder_id, name, parent_id) VALUES(?,?,?)",
        [(f.id, f.name, f.parent_id) for f in folders],
    )
    connection.executemany(
        "INSERT OR IGNORE INTO folder_sources VALUES(?,?)", [(f.id, source_id) for f in folders]
    )
    _refresh_ancestry(connection)


def _source_json(row: Any, rules: dict[str, list[str]] | None = None) -> dict[str, Any]:
    keys = row.keys()
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "kind": row["kind"],
        "link": row["link"],
        "root_id": row["root_id"],
        "drive_id": row["drive_id"],
        "status": row["last_status"],
        "error": row["last_error"],
        "files": int(row["file_count"]),
        "documents": int(row["documents"]) if "documents" in keys else None,
        "last_sync_at": row["last_sync_at"],
        "first_sync_done": row["first_synced_at"] is not None,
        "skipped": int(row["skipped"]) if "skipped" in keys else 0,
        "path_rules": rules or {e: [] for e in EFFECTS},
    }


def _stale_ocr(metadata_json: str | None, extractor: Extractor) -> bool:
    """A document with OCR'd pages is re-extracted once when its text came from another OCR
    engine (the PP-OCR text before Tesseract), or when OCR was unavailable and now works.
    Documents without OCR pages are never touched, so their vectors stay as they are."""
    metadata = json.loads(metadata_json or "{}")
    if not metadata.get("pages_needing_ocr") or not extractor.ocr_ready():
        return False  # no OCR pages, or OCR cannot run now: keep the stored text
    engine = extractor.ocr.name if extractor.ocr is not None else None
    return metadata.get("ocr_engine") != engine or bool(metadata.get("ocr_unavailable"))


def _folder_json(row: Any) -> dict[str, Any]:
    return {
        "folder_id": row["folder_id"],
        "name": row["name"],
        "path": row["path"],
        "drive_link": folder_link(str(row["folder_id"])),
    }


def _delete_orphans(connection: Any) -> int:
    cursor = connection.execute(
        "DELETE FROM documents WHERE id NOT IN (SELECT document_id FROM document_sources)"
    )
    return int(cursor.rowcount or 0)


def _refresh_ancestry(connection: Any) -> None:
    """Drop folders no source lists any more, then recompute folder paths and the
    document -> ancestor-folder table from parent IDs (cycle-safe)."""
    connection.execute(
        "DELETE FROM folders WHERE folder_id NOT IN (SELECT folder_id FROM folder_sources)"
    )
    folders = {
        str(row[0]): (str(row[1]), str(row[2]), str(row[3]))
        for row in connection.execute("SELECT folder_id, name, parent_id, path FROM folders")
    }
    chains: dict[str, list[str]] = {}

    def chain(folder_id: str) -> list[str]:
        """``folder_id`` and its known ancestors, nearest first."""
        if folder_id in chains:
            return chains[folder_id]
        result: list[str] = []
        current = folder_id
        while current in folders and current not in result:
            result.append(current)
            current = folders[current][1]
        chains[folder_id] = result
        return result

    paths = []
    for folder_id, (_, _, old_path) in folders.items():
        path = "/".join(display_segment(folders[f][0]) for f in reversed(chain(folder_id)))
        if path != old_path:
            paths.append((path, folder_id))
    connection.executemany("UPDATE folders SET path=? WHERE folder_id=?", paths)
    wanted = {
        (int(row[0]), folder_id)
        for row in connection.execute("SELECT id, parent_id FROM documents WHERE parent_id!=''")
        for folder_id in chain(str(row[1]))
    }
    existing = {
        (int(row[0]), str(row[1]))
        for row in connection.execute("SELECT document_id, folder_id FROM document_folders")
    }
    connection.executemany(
        "DELETE FROM document_folders WHERE document_id=? AND folder_id=?", existing - wanted
    )
    connection.executemany("INSERT INTO document_folders VALUES(?,?)", wanted - existing)


def _gc_vectors(connection: Any) -> None:
    for table in ("vectors", "embedding_failures"):
        connection.execute(
            f"DELETE FROM {table} WHERE chunk_hash NOT IN (SELECT chunk_hash FROM chunks)"
        )


def _bump(connection: Any) -> None:
    connection.execute(
        "INSERT INTO meta(key,value) VALUES(?, '1') "
        "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1",
        (REVISION_KEY,),
    )


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in report.items() if key != "sources"} | {
        "sources_with_errors": sum(1 for source in report["sources"] if not source.get("ok"))
    }


def _locator_has_page(locator: str, page: int) -> bool:
    import re

    for start, end in re.findall(r"page:(\d+)(?:-(\d+))?", locator):
        if int(start) <= page <= int(end or start):
            return True
    return False


def _family(identifier: str) -> str:
    """Which backend reads a source root or file: ``local`` for ``local:`` IDs, else ``drive``."""
    return LOCAL if is_local_id(identifier) else DRIVE


def _temp_name(identifier: str) -> str:
    """A file name for the extraction copy: Drive IDs as they are, others (local paths) hashed."""
    if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier):
        return identifier
    return hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:32]


def _message(exc: BaseException) -> str:
    if isinstance(exc, ExtractionError):
        return f"{exc.code}: {exc}"
    text = str(exc).strip()
    return text if text else type(exc).__name__


def _now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def _older_than(value: str | None, hours: int) -> bool:
    if not value:
        return True
    try:
        moment = dt.datetime.fromisoformat(value)
    except ValueError:
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return dt.datetime.now(dt.UTC) - moment > dt.timedelta(hours=hours)


def private_path(path: Path) -> bool:
    return os.name == "nt" or not path.stat().st_mode & 0o077
