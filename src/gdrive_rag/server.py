"""MCP server over Streamable HTTP (``/mcp``), ``/healthz``, and the background sync loop.

Five tools are read-only; ``sync_now`` only asks the sync loop for an early sync.
The same JSON is sent as structured content and, escaped again,
as text content; ``wire_size`` measures both after encoding and results are trimmed until it is
at most ``MAX_WIRE_CHARS``, so quote-heavy text cannot blow up the response.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .config import load_settings
from .service import Service, SyncBusy

log = logging.getLogger("gdrive_rag")

MAX_WIRE_CHARS = 56_000  # + JSON-RPC envelope stays under ~60,000 characters
INSTRUCTIONS = (
    "Search the owner's Google Drive documents (Docs, Sheets, Slides, PDF, Office files). "
    "Use `search` first; cite the file name, locator and Drive link from each result. To stay "
    "inside one Drive folder and its subfolders pass `folder` (a folder link or ID); to stay "
    "inside specific files pass `files`; `source` limits to one added source. `list_folders` "
    "browses the folder tree and gives folder IDs. Use `read` with a document_id or Drive file "
    "link to see more text around a hit (pass `chunk` from the search result as start_chunk, or "
    "a PDF page). `list_sources` shows which Drive sources are indexed and `sync_status` shows "
    "indexing progress and errors. When the user says a file was just added or changed in "
    "Drive, call `sync_now`, then check `sync_status` until it is no longer running."
)


class SearchHit(BaseModel):
    document_id: str = Field(description="Drive file ID; pass to `read`.")
    file_name: str
    path: str = Field(description="Path inside the Drive source folder.")
    parent_folder_id: str | None = Field(
        description="Indexed folder holding the file (usable as `folder`); null if not indexed."
    )
    locator: str = Field(description="Where in the file: page:N, slide:N, sheet:NAME/row:N, ...")
    chunk: int = Field(description="Chunk number; pass as start_chunk to `read`.")
    chunk_count: int
    drive_link: str
    sources: str
    score: float
    snippet: str


class SearchOutput(BaseModel):
    mode: str = Field(description="hybrid (full-text + vectors) or full_text.")
    warnings: list[str]
    results: list[SearchHit]
    truncated: bool = False


class ReadChunk(BaseModel):
    chunk: int
    locator: str
    text: str


class ReadOutput(BaseModel):
    document_id: str
    file_name: str
    path: str
    drive_link: str
    status: str
    error: str | None
    ocr_unavailable: bool = Field(description="True when scanned pages could not be OCR'd.")
    chunk_count: int
    chunks: list[ReadChunk]
    next_chunk: int | None = Field(description="Pass as start_chunk to continue reading.")
    truncated: bool


class SourceInfo(BaseModel):
    id: int
    name: str
    kind: str
    link: str
    status: str
    error: str | None
    files: int
    documents: int | None
    last_sync_at: str | None


class SourcesOutput(BaseModel):
    sources: list[SourceInfo]


class FolderInfo(BaseModel):
    folder_id: str = Field(description="Drive folder ID; pass as `folder` to search.")
    name: str
    path: str
    drive_link: str
    documents: int = Field(description="Indexed documents in this folder and all subfolders.")
    subfolders: int


class FolderRef(BaseModel):
    folder_id: str
    name: str
    path: str
    drive_link: str


class FolderDocument(BaseModel):
    document_id: str
    file_name: str
    path: str
    drive_link: str
    status: str


class FoldersOutput(BaseModel):
    parent: FolderRef | None
    folders: list[FolderInfo]
    documents: list[FolderDocument]
    offset: int
    total: int = Field(description="Folders plus documents in this listing, over all pages.")
    next_offset: int | None = Field(description="Pass as offset for the next page.")
    truncated: bool


class SyncNowOutput(BaseModel):
    started: bool = Field(description="True when a sync was started now.")
    reason: str | None = Field(
        default=None, description="Why not started: rate_limited or already_running."
    )
    retry_after_seconds: int | None = Field(
        default=None, description="When rate_limited: wait this long before asking again."
    )
    running: bool
    last_report: dict[str, Any] | None


class StatusOutput(BaseModel):
    sources: list[SourceInfo]
    documents: dict[str, int]
    chunks: int
    recent_failures: list[dict[str, Any]]
    embeddings: dict[str, Any]
    google_auth: str
    sync: dict[str, Any]


def _dump(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def wire_size(value: dict[str, Any]) -> int:
    """Characters of structured content plus the text block (the JSON as an escaped string)."""
    dumped = _dump(value)
    return len(dumped) + len(json.dumps(dumped, ensure_ascii=False))


def _cap_list(value: dict[str, Any], key: str) -> dict[str, Any]:
    """Drop items from the end of ``value[key]`` until ``wire_size`` fits ``MAX_WIRE_CHARS``."""
    while wire_size(value) > MAX_WIRE_CHARS and value[key]:
        value[key] = value[key][:-1]
        value["truncated"] = True
    return value


def create_server(service: Service) -> Any:
    server = MCPServer(
        "google-drive-rag", version=__version__, instructions=INSTRUCTIONS, log_level="WARNING"
    )
    local = ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )

    def ok(value: dict[str, Any]) -> CallToolResult:
        return CallToolResult(content=[TextContent(text=_dump(value))], structured_content=value)

    def fail(exc: Exception) -> CallToolResult:
        return CallToolResult(content=[TextContent(text=f"Error: {exc}")], is_error=True)

    @server.tool(title="Search Google Drive documents", annotations=local)
    def search(
        query: Annotated[str, Field(description="Words or a question to look for.")],
        source: Annotated[
            str | None, Field(description="Optional source name or id from list_sources.")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=50, description="Maximum results.")] = 8,
        folder: Annotated[
            str | None,
            Field(description="Only this Drive folder and its subfolders (folder link or ID)."),
        ] = None,
        files: Annotated[
            list[str] | None,
            Field(description="Only these files (Drive file links or IDs).", max_length=50),
        ] = None,
    ) -> Annotated[CallToolResult, SearchOutput]:
        """Find passages in the indexed Google Drive files. Returns snippets with the file name,
        locator (page/slide/sheet), Drive link and a document_id for `read`. Narrow with
        `folder` (one folder tree), `files` (named files) or `source`; they combine."""
        try:
            result = service.search(query, source, limit, folder, files)
        except ValueError as exc:
            return fail(exc)
        result["truncated"] = False
        return ok(_cap_list(result, "results"))

    @server.tool(title="Read a document", annotations=local)
    def read(
        document_id: Annotated[
            str, Field(description="document_id from a search result, or a Drive file link.")
        ],
        start_chunk: Annotated[int, Field(ge=0, description="First chunk to return.")] = 0,
        max_chunks: Annotated[int, Field(ge=1, le=100, description="How many chunks.")] = 10,
        page: Annotated[
            int | None, Field(ge=1, description="Start at this PDF page instead.")
        ] = None,
    ) -> Annotated[CallToolResult, ReadOutput]:
        """Read consecutive text of one document, by chunk range or starting at a PDF page."""
        try:
            result = service.read(document_id, start_chunk, max_chunks, page)
        except ValueError as exc:
            return fail(exc)
        capped = _cap_list(result, "chunks")
        if capped["chunks"]:
            capped["next_chunk"] = (
                capped["chunks"][-1]["chunk"] + 1
                if capped["chunks"][-1]["chunk"] + 1 < capped["chunk_count"]
                else None
            )
        return ok(capped)

    @server.tool(title="List Drive sources", annotations=local)
    def list_sources() -> Annotated[CallToolResult, SourcesOutput]:
        """List the Google Drive folders/files that are indexed, with their sync state."""
        return ok({"sources": service.list_sources()})

    @server.tool(title="List Drive folders", annotations=local)
    def list_folders(
        source: Annotated[
            str | None, Field(description="Optional source name or id from list_sources.")
        ] = None,
        parent: Annotated[
            str | None,
            Field(description="Folder link or ID to list; omit for the top folders."),
        ] = None,
        include_documents: Annotated[
            bool, Field(description="Also list the documents directly inside `parent`.")
        ] = False,
        offset: Annotated[int, Field(ge=0, description="next_offset from the last page.")] = 0,
    ) -> Annotated[CallToolResult, FoldersOutput]:
        """Browse the indexed folder tree: direct subfolders with their IDs and document counts.
        Use it to find a folder ID for `search`'s `folder` filter."""
        try:
            result = service.list_folders(source, parent, include_documents, offset)
        except ValueError as exc:
            return fail(exc)
        capped = _cap_list(_cap_list(result, "documents"), "folders")  # trims the page's tail
        end = offset + len(capped["folders"]) + len(capped["documents"])
        capped["next_offset"] = end if end < capped["total"] else None
        capped["truncated"] = capped["next_offset"] is not None
        return ok(capped)

    @server.tool(
        title="Sync Google Drive now",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    )
    def sync_now(
        source: Annotated[
            str | None,
            Field(description="Optional source name or id from list_sources; omit for all."),
        ] = None,
    ) -> Annotated[CallToolResult, SyncNowOutput]:
        """Start reading Google Drive again now, in the background, instead of waiting for the
        next scheduled sync. Use it when the user says a file was just added or changed. Returns
        at once; then call `sync_status` until `running` is false and search again. At most one
        request per minute; a refused call says when to retry."""
        try:
            return ok(service.request_sync(source))
        except ValueError as exc:
            return fail(exc)

    @server.tool(title="Indexing status", annotations=local)
    def sync_status() -> Annotated[CallToolResult, StatusOutput]:
        """Show indexing progress: document counts, failures, embedding progress, last sync."""
        status = service.status()
        while wire_size(status) > MAX_WIRE_CHARS and status["recent_failures"]:
            status["recent_failures"] = status["recent_failures"][:-1]
        return ok(status)

    @server.custom_route("/healthz", methods=["GET"])  # type: ignore[untyped-decorator]
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    return server


def create_app(service: Service) -> Any:
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    hosts += [h.strip() for h in os.environ.get("GDRAG_ALLOWED_HOSTS", "").split(",") if h.strip()]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"http://{host}" for host in hosts],
    )
    return create_server(service).streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )


BUSY_RETRY_SECONDS = 30  # a scheduled cycle that finds another sync running tries again soon


class SyncLoop:
    """One thread runs every sync of this process: a scheduled one at start and every
    ``sync_interval_minutes``, and on-demand ones that ``sync_now`` requests in between.

    The schedule is a fixed deadline, so an on-demand run neither delays nor replaces the next
    scheduled cycle; a scheduled cycle that finds the lock taken (a CLI sync) retries after
    ``BUSY_RETRY_SECONDS`` instead of waiting a whole interval."""

    def __init__(self, service: Service, run_at_start: bool = True):
        self.service = service
        self.stop = threading.Event()
        self.wake = service.wake
        self.run_at_start = run_at_start
        self.thread = threading.Thread(target=self._run, name="gdrive-rag-sync", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        try:
            apply_environment(self.service)
        except Exception as exc:  # a bad .env value must not stop the server
            log.error("could not apply environment settings: %s", type(exc).__name__)
        due = time.monotonic() if self.run_at_start else self._next_due()
        while not self.stop.is_set():
            self.wake.wait(max(0.0, due - time.monotonic()))
            self.wake.clear()
            if self.stop.is_set():
                break
            request = self.service.take_request()
            if request is not None:
                try:
                    self._sync("requested", request[0])
                finally:
                    self.service.finish_request()
                continue  # the scheduled deadline stays where it was
            if time.monotonic() >= due:
                ran = self._sync("scheduled", None)
                due = self._next_due() if ran else time.monotonic() + BUSY_RETRY_SECONDS

    def _next_due(self) -> float:
        return time.monotonic() + self.service.settings.sync_interval_minutes * 60

    def _sync(self, trigger: str, source_id: int | None) -> bool:
        """Run one sync; False only when another process holds the sync lock."""
        try:
            report = self.service.sync(trigger, source_id)
        except SyncBusy:
            log.info("%s sync waiting: another sync is running", trigger)
            return False
        except Exception as exc:  # the loop must survive anything
            log.error("%s sync failed: %s", trigger, type(exc).__name__)
            return True
        log.info(
            "%s sync done: %s indexed, %s unchanged, %s removed",
            trigger,
            report.get("indexed"),
            report.get("unchanged"),
            report.get("removed"),
        )
        return True

    def close(self) -> None:
        self.stop.set()
        self.wake.set()


def apply_environment(service: Service) -> None:
    """Idempotently import GDRAG_GOOGLE_CREDENTIALS and add GDRAG_DRIVE_LINKS at startup."""
    from pathlib import Path

    from .auth import import_client_secret, import_service_account

    settings = service.settings
    credentials = os.environ.get("GDRAG_GOOGLE_CREDENTIALS", "").strip()
    if credentials:
        try:
            payload = Path(credentials).read_text(encoding="utf-8")
            value = json.loads(payload)
            kind = value.get("type") if isinstance(value, dict) else None
            if kind == "service_account":
                import_service_account(settings, payload)
            else:
                import_client_secret(settings, payload)
        except Exception as exc:
            log.error("GDRAG_GOOGLE_CREDENTIALS could not be imported: %s", type(exc).__name__)
        service.reload()
    links = [part for part in os.environ.get("GDRAG_DRIVE_LINKS", "").replace(",", " ").split()]
    if links:
        for result in service.add_sources(links):
            if not result.get("ok"):
                log.error("Drive link %s: %s", result.get("link"), result.get("error"))


def serve(data_dir: str | None, host: str, port: int) -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    service = Service(lambda: load_settings(data_dir))
    loop = SyncLoop(service)  # applies GDRAG_GOOGLE_CREDENTIALS / GDRAG_DRIVE_LINKS first
    loop.start()
    try:
        uvicorn.run(create_app(service), host=host, port=port, log_level="warning")
    finally:
        loop.close()
