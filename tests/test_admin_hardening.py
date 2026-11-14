"""Regression tests for the security review of the admin tools: each fails on 8608843."""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from helpers import FakeDrive
from test_admin import (
    API_KEY,
    CLEARED,
    CLIENT_SECRET,
    ROOT,
    Env,
    build_service,
    oauth_client,
    wait_idle,
    write_secret,
)

from gdrive_rag.config import load_settings
from gdrive_rag.drive import parse_drive_link
from gdrive_rag.embeddings import EmbeddingError, OpenAICompatibleEmbeddings

pytest_plugins = ["test_admin"]  # the `env` fixture: a live server with admin tools

OTHER_KEY = "sk-other-NEWKEYVALUE9876543210"


def configure(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    done = env.tool(
        "set_embedding",
        base_url="https://good.example/v1",
        model="m",
        api_key=API_KEY,
        confirm=True,
    )
    assert done["applied"] is True


# 1 -------------------------------------------------------------------------------------------
def test_new_host_needs_its_own_key(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    configure(env, monkeypatch)
    for port in ("", ":8443"):
        refused = env.tool(
            "set_embedding", base_url=f"https://evil.example{port}/v1", model="m", confirm=True
        )
        assert "needs its own API key" in refused
    refused = env.tool(
        "set_embedding", base_url="https://good.example:8443/v1", model="m", confirm=True
    )
    assert "needs its own API key" in refused  # same host, other port: another origin
    assert env.service.settings.embedding_base_url == "https://good.example/v1"
    # same origin, other path or model: the key may stay
    same = env.tool("set_embedding", base_url="https://good.example/v2", model="n")
    assert same["host_changes"] is False
    moved = env.tool(
        "set_embedding", base_url="https://other.example/v1", model="m", api_key=OTHER_KEY
    )
    assert moved["host_changes"] is True
    assert "Your API key and the text of every indexed document" in moved["would"]
    assert moved["endpoint"] == {
        "scheme": "https",
        "host": "other.example",
        "port": 443,
        "path": "/v1",
    }


def test_host_change_refused_while_key_comes_from_environment(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GDRAG_EMBEDDING_API_KEY", API_KEY)
    refused = env.tool("set_embedding", base_url="https://evil.example/v1", model="m")
    assert "GDRAG_EMBEDDING_API_KEY" in refused or "own API key" in refused
    refused = env.tool(
        "set_embedding", base_url="https://evil.example/v1", model="m", api_key=API_KEY
    )
    assert "GDRAG_EMBEDDING_API_KEY" in refused


# 2 -------------------------------------------------------------------------------------------
def test_api_key_file_must_look_like_a_key(env: Env) -> None:
    compact = json.dumps(oauth_client(), separators=(",", ":"))
    write_secret(env, "client.json", compact)
    write_secret(env, "short.key", "abc123")
    for name in ("client.json", "short.key"):
        refused = env.tool(
            "set_embedding",
            base_url="https://good.example/v1",
            model="m",
            api_key_file=name,
            confirm=True,
        )
        assert "not an API key" in refused or "Google credential" in refused, name
    assert env.service.settings.embedding_api_key == ""
    assert CLIENT_SECRET not in json.dumps(load_settings(env.data).public())


# 3 -------------------------------------------------------------------------------------------
class _Recorder(http.server.BaseHTTPRequestHandler):
    seen: list[str | None] = []
    target = ""

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))  # unread body: reset
        if self.path.startswith("/v1/"):
            self.send_response(302)
            self.send_header("Location", self.target)
            self.end_headers()
            return
        self.seen.append(self.headers.get("Authorization"))
        body = json.dumps({"data": [{"index": 0, "embedding": [1.0] * 32}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - urllib turns a redirected POST into a GET
        self.seen.append(self.headers.get("Authorization"))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture()
def redirecting() -> Iterator[str]:
    server = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
    port = server.server_address[1]
    _Recorder.seen = []
    _Recorder.target = f"http://localhost:{port}/stolen/embeddings"  # other host name, http
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/v1"
    server.shutdown()


def test_embedding_requests_never_follow_redirects(redirecting: str) -> None:
    client = OpenAICompatibleEmbeddings(
        redirecting, API_KEY, "m", 32, "", retries=0, allow_local=True
    )
    with pytest.raises(EmbeddingError) as raised:
        client.embed_documents(["x"])
    assert "HTTP 302" in str(raised.value) and "stolen" not in str(raised.value)
    assert _Recorder.seen == []  # the redirect target never got the Authorization header


# 4 -------------------------------------------------------------------------------------------
def test_confirm_must_be_a_real_boolean(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    configure(env, monkeypatch)
    for value in ("yes", "t", "true", 1, "1"):
        text = env.tool("remove_embedding", confirm=value)
        assert isinstance(text, str) and "boolean" in text, value
        assert env.service.settings.embeddings_enabled
    assert env.tool("remove_embedding", confirm=False)["applied"] is False
    assert env.tool("remove_embedding", confirm=True)["applied"] is True


# 5 + 6 ---------------------------------------------------------------------------------------
def test_no_argument_value_is_echoed(env: Env) -> None:
    """A key pasted into the wrong field is never echoed, stored or shown by list_sources."""
    text = env.tool(
        "set_embedding", base_url="https://g.example/v1", model="m", api_key_file=API_KEY
    )
    assert isinstance(text, str) and "no such file" in text
    calls: list[tuple[str, dict[str, Any]]] = [
        ("add_source", {"link": [API_KEY]}),
        ("add_source", {"link": API_KEY}),
        ("add_source", {}),
        ("remove_source", {"source": {"k": API_KEY}}),
        ("remove_source", {"source": API_KEY, "confirm": "yes"}),
        ("set_google_service_account", {"file": API_KEY}),
        ("set_google_service_account", {"file": "x.json", "confirm": [API_KEY]}),
        ("google_auth_start", {"oauth_client_file": 5}),
        ("google_auth_start", {"oauth_client_file": API_KEY}),
        ("remove_google_auth", {"confirm": API_KEY}),
        ("remove_embedding", {"confirm": {"k": API_KEY}}),
    ]
    for name, arguments in calls:
        text = env.tool(name, **arguments)
        assert isinstance(text, str) and "validation error" not in text, (name, arguments)
    # the fixture's teardown asserts API_KEY appears in no result and no server log line


# 7 -------------------------------------------------------------------------------------------
def test_queued_sync_survives_a_running_request(tmp_path: Path) -> None:
    service = build_service(tmp_path, FakeDrive())
    assert service.request_sync()["started"] is True
    running = service.begin_request()
    assert running is not None
    service.queue_sync(None)  # e.g. add_source while that requested sync runs
    service.queue_sync(None)  # coalesces: still one queued sync
    service.finish_request(running[1])
    assert service.take_request() == (None,)  # runs once more
    again = service.begin_request()
    assert again is not None
    service.finish_request(again[1])
    assert service.take_request() is None


def test_queue_sync_and_the_sync_now_rate_limit(tmp_path: Path) -> None:
    service = build_service(tmp_path, FakeDrive())
    service.queue_sync(None)  # exempt from the limit, and does not use up sync_now's minute
    assert service.request_sync()["reason"] == "already_running"  # one pending is enough
    pending = service.begin_request()
    assert pending is not None
    service.finish_request(pending[1])
    assert service.request_sync()["started"] is True
    pending = service.begin_request()
    assert pending is not None
    service.finish_request(pending[1])
    assert service.request_sync()["reason"] == "rate_limited"
    assert service.queue_sync(None)["queued"] is True  # admin changes are not rate limited


def test_sync_loop_runs_a_sync_queued_during_a_requested_sync(env: Env) -> None:
    gate = threading.Event()
    calls: list[str] = []
    original = env.service.sync

    def slow_sync(trigger: str = "manual", source_id: int | None = None) -> dict[str, Any]:
        calls.append(trigger)
        if len(calls) == 1:
            gate.wait(5)
        return original(trigger, source_id)

    env.service.sync = slow_sync  # type: ignore[method-assign]
    assert env.tool("sync_now")["started"] is True
    deadline = threading.Event()
    while not calls and not deadline.wait(0.02):
        pass
    added = env.tool("add_source", link="folderBBBBBBBBBBBB")
    assert added["applied"] is True
    gate.set()
    wait_idle(env.service)
    assert calls == ["requested", "requested"]
    assert env.tool("list_sources")["sources"][1]["documents"] == 1


# 8 -------------------------------------------------------------------------------------------
def test_cli_uses_the_same_precedence_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from gdrive_rag.cli import main

    for name in CLEARED:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GDRAG_EMBEDDING_BASE_URL", "https://embed.example/v1")
    monkeypatch.setenv("GDRAG_EMBEDDING_MODEL", "env-model")
    key = tmp_path / "key.txt"
    key.write_text(API_KEY)
    data = ["--data-dir", str(tmp_path / "d"), "--json"]
    assert main([*data, "embedding", "set", "--api-key-file", str(key), "--yes"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["applied"] is True and result["sends_text_to"] == "embed.example"
    assert load_settings(tmp_path / "d").embedding_api_key == API_KEY

    monkeypatch.setenv("GDRAG_GOOGLE_CREDENTIALS", "/secrets/google.json")
    client = tmp_path / "client.json"
    client.write_text(json.dumps(oauth_client()))
    assert main([*data, "auth", "google", "--oauth-client", str(client)]) == 2
    assert "GDRAG_GOOGLE_CREDENTIALS" in capsys.readouterr().out
    assert not (tmp_path / "d" / "google" / "client_secret.json").exists()


# 9 -------------------------------------------------------------------------------------------
def test_only_exact_drive_hosts_and_normalised_links(env: Env) -> None:
    for link in (
        f"https://evilgoogle.com/drive/folders/{ROOT}",
        f"https://drive.google.com.evil.example/drive/folders/{ROOT}",
        f"javascript://drive.google.com/drive/folders/{ROOT}",
    ):
        with pytest.raises(ValueError):
            parse_drive_link(link)
    assert parse_drive_link(f"https://docs.google.com/document/d/{ROOT}/edit") == ROOT
    added = env.tool(
        "add_source",
        link="https://drive.google.com/drive/folders/folderBBBBBBBBBBBB?usp=sharing&note=x",
    )
    assert added["source"]["link"] == "https://drive.google.com/drive/folders/folderBBBBBBBBBBBB"
    assert "note=x" not in json.dumps(env.tool("list_sources"))


# 10 ------------------------------------------------------------------------------------------
def test_base_url_rules(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    def attempt(url: str) -> Any:
        return env.tool("set_embedding", base_url=url, model="m", api_key=API_KEY)

    assert "https" in attempt("http://embed.example/v1")
    assert "GDRAG_ALLOW_LOCAL_EMBEDDINGS" in attempt("http://192.168.1.5:8080/v1")
    for url in (
        "https://169.254.169.254/v1",
        "http://169.254.169.254/latest",
        "https://metadata.google.internal/v1",
        "https://[fe80::1]/v1",
    ):
        assert "link-local" in attempt(url), url
    monkeypatch.setenv("GDRAG_ALLOW_LOCAL_EMBEDDINGS", "1")
    assert "https" in attempt("http://embed.example/v1")  # public hosts still need https
    assert "literal" in attempt("http://localhost/v1")  # http only to a literal address
    for url in ("http://192.168.1.5:8080/v1", "http://127.0.0.1:11434/v1"):
        preview = attempt(url)
        assert preview["applied"] is False and "not encrypted" in preview["would"], url


# 11 ------------------------------------------------------------------------------------------
def test_refresh_cannot_restore_a_removed_token(env: Env, tmp_path: Path) -> None:
    from gdrive_rag.drive import save_refreshed_token

    token = tmp_path / "token.json"
    assert save_refreshed_token(token, "{}") is False and not token.exists()
    token.write_text("{}")
    assert save_refreshed_token(token, '{"x": 1}') is True
    done = env.tool("remove_google_auth", confirm=True)
    assert "not revoked at Google" in done["not_revoked_at_google"]
    assert "myaccount.google.com/permissions" in done["not_revoked_at_google"]


# migration -----------------------------------------------------------------------------------
def test_import_env_stores_settings_and_lists_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from gdrive_rag.cli import main

    for name in CLEARED:
        monkeypatch.delenv(name, raising=False)
    account = tmp_path / "sa.json"
    account.write_text(
        json.dumps(
            {
                "type": "service_account",
                "client_email": "r@p.iam.gserviceaccount.com",
                "private_key": "PRIVATEKEYMATERIAL",
            }
        )
    )
    monkeypatch.setenv("GDRAG_EMBEDDING_API_KEY", API_KEY)
    monkeypatch.setenv("GDRAG_EMBEDDING_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("GDRAG_EMBEDDING_MODEL", "qwen/qwen3-embedding-8b")
    monkeypatch.setenv("GDRAG_GOOGLE_CREDENTIALS", str(account))
    data = tmp_path / "d"
    assert main(["--data-dir", str(data), "--json", "config", "import-env"]) == 0
    out = capsys.readouterr().out
    result = json.loads(out)
    assert result["may_blank_in_env"] == [
        "GDRAG_EMBEDDING_BASE_URL",
        "GDRAG_EMBEDDING_MODEL",
        "GDRAG_EMBEDDING_API_KEY",
        "GDRAG_GOOGLE_CREDENTIALS",
    ]
    assert API_KEY not in out and "PRIVATEKEYMATERIAL" not in out
    config = data / "config.json"
    assert config.stat().st_mode & 0o777 == 0o600
    # after the operator empties those lines, the stored values are what applies
    for name in ("GDRAG_EMBEDDING_API_KEY", "GDRAG_EMBEDDING_BASE_URL", "GDRAG_EMBEDDING_MODEL"):
        monkeypatch.delenv(name)
    monkeypatch.delenv("GDRAG_GOOGLE_CREDENTIALS")
    settings = load_settings(data)
    assert settings.embedding_api_key == API_KEY
    assert settings.embedding_model == "qwen/qwen3-embedding-8b"
    assert settings.google_auth == "service_account" and settings.service_account_file.is_file()
    assert main(["--data-dir", str(data), "--json", "config", "import-env"]) == 0
    assert json.loads(capsys.readouterr().out)["may_blank_in_env"] == []


def test_import_env_with_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gdrive_rag.admin import import_environment

    for name in CLEARED:
        monkeypatch.delenv(name, raising=False)
    drive = FakeDrive()
    drive.add_root(ROOT, "Kế toán")
    service = build_service(tmp_path, drive)
    monkeypatch.setenv("GDRAG_DRIVE_LINKS", f"https://drive.google.com/drive/folders/{ROOT}")
    assert import_environment(service)["may_blank_in_env"] == ["GDRAG_DRIVE_LINKS"]
    assert [s["root_id"] for s in service.list_sources()] == [ROOT]
    monkeypatch.setenv("GDRAG_DRIVE_LINKS", "https://evilgoogle.com/x")
    result = import_environment(service)
    assert result["may_blank_in_env"] == [] and "GDRAG_DRIVE_LINKS" in result["not_imported"]
