"""The one facade used by both the CLI and the MCP server: sources, sync, embeddings, reads.

Sync is a full listing of every source compared with stored fingerprints (MD5 checksum or
modifiedTime, size and MIME type), which is what local-rag-mcp effectively did after
``changes.list``. Name, path and parent folder are metadata: changing them never re-extracts.
Folders are stored by Drive ID with their parent, and ``document_folders`` holds every ancestor
folder of each document, so "inside folder X or below" is an ID lookup.
Deletion is conservative: a document leaves the index only when every source that held it was
listed completely, or its source root is explicitly gone (404 / not shared). Auth, quota and
network failures never delete anything.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import logging
import os
import struct
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from .chunking import make_chunks
from .config import Settings, ensure_private_dir
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
    folder_link,
    parse_drive_link,
    strip_data_images,
)
from .embeddings import EmbeddingClient, EmbeddingError
from .extract import Extractor
from .extraction_errors import ExtractionError
from .search import EMBEDDING_IDENTITY_KEY, REVISION_KEY, SearchEngine

log = logging.getLogger("gdrive_rag")

BackendFactory = Callable[[Settings], DriveBackend]
EmbeddingFactory = Callable[[Settings], EmbeddingClient | None]
ERROR_RETRY_HOURS = 24
DOWNLOAD_ERROR_PREFIX = "download: "  # download errors are retried on every sync
EMBED_BATCH = 64
READ_LIMIT_CHARS = 40_000
LIST_PAGE = 100  # folders + documents per list_folders page
_PENDING_SQL = (
    "NOT EXISTS (SELECT 1 FROM vectors v WHERE v.chunk_hash=c.chunk_hash) "
    "AND NOT EXISTS (SELECT 1 FROM embedding_failures f WHERE f.chunk_hash=c.chunk_hash)"
)


class SyncBusy(RuntimeError):
    """Another process or thread is already syncing this data directory."""


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
    from .ocr_runtime import OCRRuntimeManager

    runtime = None
    if settings.ocr_dir is not None:
        runtime = OCRRuntimeManager(settings.ocr_dir / "runtime", settings.ocr_dir / "models")
    return Extractor(runtime, ocr_enabled=settings.ocr_enabled, work_dir=settings.work_dir)


class Service:
    def __init__(
        self,
        settings_loader: Callable[[], Settings],
        *,
        backend_factory: BackendFactory = default_backend,
        embedding_factory: EmbeddingFactory = default_embeddings,
        extractor_factory: Callable[[Settings], Extractor] = default_extractor,
    ):
        self._load = settings_loader
        self.settings = settings_loader()
        ensure_private_dir(self.settings.data_dir)
        self.db = Database(self.settings.db_path)
        self.db.migrate()
        self.backend_factory = backend_factory
        self.embedding_factory = embedding_factory
        self.extractor_factory = extractor_factory
        self.search_engine = SearchEngine(self.db, self._embeddings)

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
            with self.db.transaction() as connection:
                connection.execute(
                    "INSERT INTO sources(link,root_id,kind,drive_id,name,last_status,last_error) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (link.strip(), root.id, root.kind, root.drive_id, root.name, status, error),
                )
            row = self._source_row(root_id)
            results.append({"link": link, "ok": status != "error", **(row or {})})
        return results

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
            rows = connection.execute(
                """SELECT s.*, (SELECT count(*) FROM document_sources d WHERE d.source_id=s.id)
                          documents FROM sources s ORDER BY s.id"""
            ).fetchall()
        return [_source_json(row) for row in rows]

    def _source_row(self, root_id: str) -> dict[str, Any] | None:
        with self.db.read() as connection:
            row = connection.execute(
                """SELECT s.*, (SELECT count(*) FROM document_sources d WHERE d.source_id=s.id)
                          documents FROM sources s WHERE root_id=?""",
                (root_id,),
            ).fetchone()
        return None if row is None else _source_json(row)

    def _find_source(self, value: str) -> dict[str, Any]:
        with contextlib.suppress(ValueError):
            value = parse_drive_link(value)
        with self.db.read() as connection:
            row = connection.execute(
                "SELECT * FROM sources WHERE CAST(id AS TEXT)=? OR root_id=? OR name=? "
                "ORDER BY id LIMIT 1",
                (value, value, value),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown source: {value}")
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

    def sync(self) -> dict[str, Any]:
        self.reload()
        with self._sync_lock():
            started = _now()
            self.db.set_meta("last_sync_started", started)
            report = self._sync()
            report["embeddings"] = self.embed_pending()
            self.db.set_meta("last_sync_finished", _now())
            self.db.set_meta("last_sync_report", json.dumps(_summary(report)))
            return report

    def _sync(self) -> dict[str, Any]:
        sources = self.list_sources()
        report: dict[str, Any] = {"sources": [], "indexed": 0, "unchanged": 0, "failed": 0}
        if not sources:
            return report
        try:
            backend = self.backend_factory(self.settings)
        except Exception as exc:
            message = _message(exc)
            for source in sources:
                self._set_source_status(source["id"], "error", message)
                report["sources"].append({"name": source["name"], "ok": False, "error": message})
            return report
        listings: dict[int, Listing | None] = {}  # None: root gone -> clear membership
        degraded: set[int] = set()  # failed or incomplete: keep the parents of its folders
        for source in sources:
            outcome = {"name": source["name"], "ok": True}
            try:
                root = DriveRoot(
                    source["root_id"], source["name"], source["kind"], source["drive_id"]
                )
                if root.kind == "unknown" or root.name == root.id:
                    root = backend.resolve(root.id)
                    self._update_root(source["id"], root)
                    outcome["name"] = root.name
                listing = backend.list_files(root)
                listings[source["id"]] = listing
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
        unique: dict[str, DriveItem] = {}
        for found in listings.values():
            for item in found.items if found else ():
                unique.setdefault(item.id, item)
        extractor = self.extractor_factory(self.settings)
        stop = ""
        for item in unique.values():
            if stop:
                break
            try:
                report[self._index_item(backend, extractor, item)] += 1
            except DriveSourceError as exc:  # sign-in or connection: stop, delete nothing
                stop = str(exc)
                report["failed"] += 1
        if stop:
            report["stopped"] = stop
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
                    (f.id, f.name, f.parent_id, int(bool(stop or held.get(f.id, set()) & degraded)))
                    for f in folders.values()
                ],
            )
            ids = {
                str(row[0]): int(row[1])
                for row in connection.execute("SELECT file_id, id FROM documents")
            }
            for source_id, found in listings.items():
                if found is None or (found.complete and not stop):  # same rule as documents
                    connection.execute("DELETE FROM folder_sources WHERE source_id=?", (source_id,))
                connection.executemany(
                    "INSERT OR IGNORE INTO folder_sources VALUES(?,?)",
                    [(folder.id, source_id) for folder in (found.folders if found else ())],
                )
                if found is None or (found.complete and not stop):
                    connection.execute(
                        "DELETE FROM document_sources WHERE source_id=?", (source_id,)
                    )
                for item in found.items if found else ():
                    if item.id in ids:
                        connection.execute(
                            "INSERT OR IGNORE INTO document_sources VALUES(?,?)",
                            (ids[item.id], source_id),
                        )
            report["removed"] = _delete_orphans(connection)
            _refresh_ancestry(connection)
            _gc_vectors(connection)
            _bump(connection)
        return report

    def _index_item(self, backend: DriveBackend, extractor: Extractor, item: DriveItem) -> str:
        """Index one file; return the report counter: indexed, unchanged or failed."""
        with self.db.read() as connection:
            current = connection.execute(
                "SELECT id, fingerprint, status, error, indexed_at, name, path, web_url, "
                "parent_id FROM documents WHERE file_id=?",
                (item.id,),
            ).fetchone()
        if current is not None and current["fingerprint"] == item.fingerprint:
            retry = current["status"] == "error" and (
                str(current["error"] or "").startswith(DOWNLOAD_ERROR_PREFIX)
                or _older_than(current["indexed_at"], ERROR_RETRY_HOURS)
            )
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
                temporary = self.settings.work_dir / f"{item.id}{download.suffix}"
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
    def embed_pending(self) -> dict[str, Any]:
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
    ) -> dict[str, Any]:
        self.reload()
        return self.search_engine.search(query, source, limit, folder, files)

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
        return {
            "sources": self.list_sources(),
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
                "model": settings.embedding_model,
                "dimensions": settings.embedding_dimensions,
                "vectors": vectors,
                "pending": self._pending_vectors(),
                "rejected": self._failed_vectors(),
                "last_error": self.db.get_meta("last_embedding_error"),
            },
            "google_auth": settings.google_auth,
            "sync": {
                "running": self.sync_running(),
                "interval_minutes": settings.sync_interval_minutes,
                "last_started": self.db.get_meta("last_sync_started"),
                "last_finished": self.db.get_meta("last_sync_finished"),
                "last_report": json.loads(self.db.get_meta("last_sync_report") or "null"),
            },
        }


def _source_json(row: Any) -> dict[str, Any]:
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
    }


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
