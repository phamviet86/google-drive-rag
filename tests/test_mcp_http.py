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

from gdrive_rag.server import MAX_WIRE_CHARS, SyncLoop, create_app
from gdrive_rag.service import Service

ROOT = "folderAAAAAAAAAAAA"
SUB = "folderThang3000000"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def running(tmp_path: Path) -> Iterator[tuple[str, Service]]:
    import uvicorn

    drive = FakeDrive()
    drive.add_root(ROOT, "Kế toán")
    drive.add_folder(SUB, "Tháng 3", ROOT)
    drive.put(ROOT, "doc1111111111111", "hoa-don.txt", "Hóa đơn tiền điện tháng 3".encode())
    drive.put(ROOT, "doc3333333333333", "nuoc.txt", "Hóa đơn tiền nước".encode(), parent=SUB)
    big = "\n".join(
        f'Dòng {i}: "trích dẫn" \\ nội dung "tiếng Việt" có dấu "đầy đủ" ở đây.'
        for i in range(20000)
    )
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
    loop = SyncLoop(service, run_at_start=False)  # runs sync_now requests, as in `serve`
    loop.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    yield f"http://127.0.0.1:{port}", service
    loop.close()
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
    source_tools = {"add_source", "remove_source", "set_path_rules"}  # always registered
    assert set(tools) == {
        "search",
        "read",
        "list_sources",
        "list_folders",
        "sync_status",
        "sync_now",
        *source_tools,
    }
    for name in source_tools:
        assert tools.pop(name).annotations.read_only_hint is False  # type: ignore[union-attr]
    for name, tool in tools.items():
        assert tool.title
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is (name != "sync_now")
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.idempotent_hint is True
        assert tool.output_schema is not None
    assert "sync_status" in (tools["sync_now"].description or "")


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
    assert sources[0]["name"] == "Kế toán" and sources[0]["documents"] == 3
    status = call(url, "sync_status").structured_content
    assert status["documents"]["indexed"] == 3
    assert status["embeddings"]["enabled"] is False


def test_results_are_capped(running: tuple[str, Service]) -> None:
    url, _ = running
    for name, arguments in (
        ("read", {"document_id": "big2222222222222", "max_chunks": 100}),
        ("search", {"query": "nội dung tiếng Việt", "limit": 50}),
    ):
        result = call(url, name, arguments)
        assert not result.is_error
        wire = result.model_dump_json(by_alias=True)  # what the server encodes
        assert len(wire) <= MAX_WIRE_CHARS, (name, len(wire))
        assert len(wire) < 60_000
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


def test_list_folders_and_folder_search(running: tuple[str, Service]) -> None:
    url, _ = running
    top = call(url, "list_folders")
    assert not top.is_error
    assert [f["folder_id"] for f in top.structured_content["folders"]] == [ROOT]
    level = call(
        url,
        "list_folders",
        {"parent": f"https://drive.google.com/drive/folders/{ROOT}", "include_documents": True},
    ).structured_content
    assert [(f["name"], f["documents"]) for f in level["folders"]] == [("Tháng 3", 1)]
    assert {d["file_name"] for d in level["documents"]} == {"hoa-don.txt", "lon.txt"}
    assert json.loads(call(url, "list_folders", {"parent": SUB}).content[0].text)["folders"] == []
    assert call(url, "list_folders", {"parent": "folderMissing0000"}).is_error
    hits = call(url, "search", {"query": "hóa đơn", "folder": SUB}).structured_content["results"]
    assert [(h["file_name"], h["parent_folder_id"]) for h in hits] == [("nuoc.txt", SUB)]
    files = ["https://drive.google.com/file/d/doc1111111111111/view"]
    hits = call(url, "search", {"query": "hóa đơn", "files": files}).structured_content["results"]
    assert [h["file_name"] for h in hits] == ["hoa-don.txt"]
    missing = call(url, "search", {"query": "hóa đơn", "folder": "folderMissing0000"})
    assert missing.is_error and "folderMissing0000" in missing.content[0].text
    read = call(url, "read", {"document_id": files[0]})
    assert read.structured_content["file_name"] == "hoa-don.txt"


def test_list_folders_pages_through_many_folders(running: tuple[str, Service]) -> None:
    url, service = running
    with service.db.transaction() as connection:  # 300 subfolders with long names
        for i in range(300):
            folder_id = f"folderMany{i:08d}"
            name = f'Thư mục "dài" số {i:03d} ' + "đ" * 120
            connection.execute("INSERT INTO folders VALUES(?,?,?,?)", (folder_id, name, SUB, name))
            connection.execute("INSERT INTO folder_sources VALUES(?,1)", (folder_id,))
    seen: list[str] = []
    offset: int | None = 0
    pages = 0
    while offset is not None:
        result = call(url, "list_folders", {"parent": SUB, "offset": offset})
        assert not result.is_error
        assert len(result.model_dump_json(by_alias=True)) <= MAX_WIRE_CHARS
        page = result.structured_content
        assert page["total"] == 300 and page["truncated"] is (page["next_offset"] is not None)
        seen += [f["folder_id"] for f in page["folders"]]
        offset, pages = page["next_offset"], pages + 1
    assert pages >= 3 and len(seen) == 300 and len(set(seen)) == 300


def wait_until(condition: Any, seconds: float = 10.0) -> None:
    deadline = time.time() + seconds
    while not condition():
        assert time.time() < deadline, "timed out"
        time.sleep(0.05)


def test_sync_now_runs_in_background_and_is_rate_limited(running: tuple[str, Service]) -> None:
    url, service = running
    drive = service.backend_factory(service.settings)
    drive.put(ROOT, "docNew0000000000", "moi.txt", "Biên bản bàn giao mới".encode())
    first = call(url, "sync_now")
    assert not first.is_error
    assert first.structured_content["started"] is True
    assert first.structured_content["running"] is True

    def finished() -> bool:  # picked up, run and done (running alone is false before pick-up)
        sync = call(url, "sync_status").structured_content["sync"]
        return sync["trigger"] == "requested" and not sync["requested"] and not sync["running"]

    wait_until(finished)
    assert call(url, "search", {"query": "bàn giao"}).structured_content["results"]
    again = call(url, "sync_now").structured_content
    assert again["started"] is False and again["reason"] == "rate_limited"
    assert 0 < again["retry_after_seconds"] <= 60
    assert again["last_report"]["indexed"] == 1
    assert call(url, "sync_now", {"source": "no such source"}).is_error


def test_sync_now_never_runs_two_syncs(
    running: tuple[str, Service], monkeypatch: pytest.MonkeyPatch
) -> None:
    import gdrive_rag.service as service_module
    from gdrive_rag.service import SyncBusy

    url, service = running
    monkeypatch.setattr(service_module, "REQUEST_INTERVAL_SECONDS", 0)
    drive = service.backend_factory(service.settings)
    drive.put(ROOT, "docSlow000000000", "cham.txt", "Tệp tải chậm".encode())
    release = threading.Event()
    real_download = drive.download

    def slow_download(item: Any) -> Any:
        if item.id == "docSlow000000000":
            release.wait(10)
        return real_download(item)

    monkeypatch.setattr(drive, "download", slow_download)
    answers: list[dict[str, Any]] = []
    callers = [
        threading.Thread(target=lambda: answers.append(call(url, "sync_now").structured_content))
        for _ in range(4)
    ]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join()
    assert sorted(a["started"] for a in answers) == [False, False, False, True]
    assert {a.get("reason") for a in answers if not a["started"]} == {"already_running"}
    wait_until(service.sync_running)
    with pytest.raises(SyncBusy):  # a CLI sync meanwhile is refused by the same lock
        service.sync()
    assert call(url, "sync_status").structured_content["sync"]["running"] is True
    release.set()
    wait_until(lambda: not service.sync_running() and service.take_request() is None)
    assert call(url, "search", {"query": "tải chậm"}).structured_content["results"]
