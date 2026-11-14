"""MCP server over Streamable HTTP (``/mcp``), ``/healthz``, and the background sync loop.

All four tools are read-only. Each tool result is capped at ``MAX_RESULT_CHARS`` of JSON; the
same JSON is sent as text content and as structured content, so a full response stays well
under 150,000 characters on the wire.
"""

from __future__ import annotations

import json
import logging
import os
import threading
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

MAX_RESULT_CHARS = 45_000
INSTRUCTIONS = (
    "Search the owner's Google Drive documents (Docs, Sheets, Slides, PDF, Office files). "
    "Use `search` first; cite the file name, locator and Drive link from each result. Use `read` "
    "with a document_id to see more text around a hit (pass `chunk` from the search result as "
    "start_chunk, or a PDF page). `list_sources` shows which Drive folders are indexed and "
    "`sync_status` shows indexing progress and errors."
)


class SearchHit(BaseModel):
    document_id: str = Field(description="Drive file ID; pass to `read`.")
    file_name: str
    path: str = Field(description="Path inside the Drive source folder.")
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


def _cap_list(value: dict[str, Any], key: str) -> dict[str, Any]:
    """Drop items from the end of ``value[key]`` until the JSON fits ``MAX_RESULT_CHARS``."""
    while len(_dump(value)) > MAX_RESULT_CHARS and value[key]:
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
    ) -> Annotated[CallToolResult, SearchOutput]:
        """Find passages in the indexed Google Drive files. Returns snippets with the file name,
        locator (page/slide/sheet), Drive link and a document_id for `read`."""
        try:
            result = service.search(query, source, limit)
        except ValueError as exc:
            return fail(exc)
        result["truncated"] = False
        return ok(_cap_list(result, "results"))

    @server.tool(title="Read a document", annotations=local)
    def read(
        document_id: Annotated[str, Field(description="document_id from a search result.")],
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

    @server.tool(title="Indexing status", annotations=local)
    def sync_status() -> Annotated[CallToolResult, StatusOutput]:
        """Show indexing progress: document counts, failures, embedding progress, last sync."""
        status = service.status()
        while len(_dump(status)) > MAX_RESULT_CHARS and status["recent_failures"]:
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


class SyncLoop:
    """Runs ``service.sync()`` at start and then every ``sync_interval_minutes``."""

    def __init__(self, service: Service):
        self.service = service
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self._run, name="gdrive-rag-sync", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        try:
            apply_environment(self.service)
        except Exception as exc:  # a bad .env value must not stop the server
            log.error("could not apply environment settings: %s", type(exc).__name__)
        while not self.stop.is_set():
            try:
                report = self.service.sync()
                log.info(
                    "sync done: %s indexed, %s unchanged, %s removed",
                    report.get("indexed"),
                    report.get("unchanged"),
                    report.get("removed"),
                )
            except SyncBusy:
                log.info("sync skipped: another sync is running")
            except Exception as exc:  # the loop must survive anything
                log.error("sync failed: %s", type(exc).__name__)
            minutes = self.service.settings.sync_interval_minutes
            self.wake.wait(minutes * 60)
            self.wake.clear()

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
