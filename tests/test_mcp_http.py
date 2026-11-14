"""MCP tools over a real Streamable HTTP server (uvicorn on a free port) and the SDK client."""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from helpers import FakeDrive, make_service

from gdrive_rag.server import MAX_RESULT_CHARS, create_app
from gdrive_rag.service import Service

ROOT = "folderAAAAAAAAAAAA"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def running(tmp_path: Path) -> Iterator[tuple[str, Service]]:
    import uvicorn

    drive = FakeDrive()
    drive.add_root(ROOT, "Kế toán")
    drive.put(ROOT, "doc1111111111111", "hoa-don.txt", "Hóa đơn tiền điện tháng 3".encode())
    big = "\n".join(f"Dòng {i}: nội dung tiếng Việt có dấu đầy đủ ở đây." for i in range(20000))
    drive.put(ROOT, "big2222222222222", "lon.txt", big.encode())
    service = make_service(tmp_path, drive)
    service.add_sources([ROOT])
    service.sync()
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(service), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    yield f"http://127.0.0.1:{port}", service
    server.should_exit = True
    thread.join(timeout=10)


def call(url: str, name: str, arguments: dict[str, Any] | None = None) -> Any:
    from mcp.client import Client

    async def run() -> Any:
        async with Client(f"{url}/mcp") as client:
            if name == "tools/list":
                return await client.list_tools()
            return await client.call_tool(name, arguments or {})

    return anyio.run(run)


def test_healthz(running: tuple[str, Service]) -> None:
    url, _ = running
    with urllib.request.urlopen(f"{url}/healthz") as response:
        assert json.loads(response.read()) == {"ok": True}


def test_tools_list(running: tuple[str, Service]) -> None:
    url, _ = running
    tools = {tool.name: tool for tool in call(url, "tools/list").tools}
    assert set(tools) == {"search", "read", "list_sources", "sync_status"}
    for tool in tools.values():
        assert tool.title
        assert tool.annotations is not None and tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.output_schema is not None


def test_search_and_read(running: tuple[str, Service]) -> None:
    url, _ = running
    result = call(url, "search", {"query": "hoa don dien"})
    assert not result.is_error
    hit = result.structured_content["results"][0]
    assert hit["file_name"] == "hoa-don.txt"
    assert hit["drive_link"] == "https://drive.google.com/file/d/doc1111111111111/view"
    assert hit["locator"] == "line:1"
    assert json.loads(result.content[0].text)["results"][0]["file_name"] == "hoa-don.txt"
    read = call(url, "read", {"document_id": hit["document_id"]})
    assert read.structured_content["chunks"][0]["text"] == "Hóa đơn tiền điện tháng 3"


def test_errors_are_tool_errors(running: tuple[str, Service]) -> None:
    url, _ = running
    assert call(url, "read", {"document_id": "nope"}).is_error
    assert call(url, "search", {"query": "!!!"}).is_error
    assert call(url, "search", {"query": "x", "source": "unknown"}).is_error


def test_sources_and_status(running: tuple[str, Service]) -> None:
    url, _ = running
    sources = call(url, "list_sources").structured_content["sources"]
    assert sources[0]["name"] == "Kế toán" and sources[0]["documents"] == 2
    status = call(url, "sync_status").structured_content
    assert status["documents"]["indexed"] == 2
    assert status["embeddings"]["enabled"] is False


def test_results_are_capped(running: tuple[str, Service]) -> None:
    url, _ = running
    for name, arguments in (
        ("read", {"document_id": "big2222222222222", "max_chunks": 100}),
        ("search", {"query": "nội dung tiếng Việt", "limit": 50}),
    ):
        result = call(url, name, arguments)
        assert not result.is_error
        wire = result.model_dump_json(by_alias=True)
        assert len(result.content[0].text) <= MAX_RESULT_CHARS
        assert len(wire) < 150_000, (name, len(wire))
        print(name, "wire chars", len(wire))
    read = call(url, "read", {"document_id": "big2222222222222", "max_chunks": 100})
    assert read.structured_content["truncated"] is True
    assert read.structured_content["next_chunk"] is not None


def test_foreign_host_header_is_rejected(running: tuple[str, Service]) -> None:
    url, _ = running
    request = urllib.request.Request(
        f"{url}/mcp",
        data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
        headers={
            "Host": "evil.example.com",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code in {400, 403, 421}
