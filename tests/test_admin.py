"""Admin MCP tools over the real Streamable HTTP server: off by default, confirm flow, secrets
folder confinement, environment precedence, OAuth start/finish and secret non-disclosure."""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from helpers import FakeDrive, FakeEmbeddings
from test_mcp_http import call, free_port

from gdrive_rag.admin import ADMIN_ENV, Admin, admin_tools_enabled
from gdrive_rag.config import ENV_FIELDS, Settings, load_settings
from gdrive_rag.extract import Extractor
from gdrive_rag.search import EMBEDDING_IDENTITY_KEY
from gdrive_rag.server import SyncLoop, apply_environment, create_app
from gdrive_rag.service import Service

ROOT = "folderAAAAAAAAAAAA"
OTHER = "folderBBBBBBBBBBBB"
API_KEY = "sk-or-v1-SECRETKEYVALUE0123456789"
PRIVATE_KEY = "-----BEGIN PRIVATE KEY-----\nPRIVATEKEYMATERIAL\n-----END PRIVATE KEY-----\n"
CLIENT_SECRET = "GOCSPX-CLIENTSECRETVALUE"
REFRESH = "1//REFRESHTOKENVALUE"
CODE = "4/AUTHCODEVALUE"
SECRETS = (API_KEY, "PRIVATEKEYMATERIAL", CLIENT_SECRET, REFRESH, CODE)
READ_ONLY = {"search", "read", "list_sources", "list_folders", "sync_status", "sync_now"}
ADMIN_TOOLS = {
    "add_source",
    "remove_source",
    "google_auth_status",
    "set_google_service_account",
    "google_auth_start",
    "google_auth_finish",
    "remove_google_auth",
    "set_embedding",
    "remove_embedding",
}
CLIENT_LOGGERS = ("mcp.client", "httpx", "httpcore")
CLEARED = (*ENV_FIELDS, "GDRAG_DRIVE_LINKS", "GDRAG_GOOGLE_CREDENTIALS", ADMIN_ENV)


class FakeFlow:
    """OAuth flow stand-in: fixed consent URL and state, returns a token JSON with secrets."""

    def __init__(self, client: dict[str, Any]):
        self.client = client
        self.codes: list[str] = []

    def authorization_url(self) -> tuple[str, str]:
        return "https://accounts.google.com/o/oauth2/auth?client_id=cid&state=STATE42", "STATE42"

    def fetch_token(self, code: str) -> str:
        self.codes.append(code)
        return json.dumps({"refresh_token": REFRESH, "client_secret": CLIENT_SECRET})


@dataclass
class Env:
    url: str
    service: Service
    admin: Admin
    drive: FakeDrive
    data: Path
    secrets: Path
    now: list[float]
    seen: list[str] = field(default_factory=list)

    def tool(self, name: str, **arguments: Any) -> Any:
        result = call(self.url, name, arguments)
        text = "".join(getattr(block, "text", "") for block in result.content)
        self.seen.append(text + json.dumps(result.structured_content or {}))
        if result.is_error:
            return text
        return result.structured_content


def build_service(data: Path, drive: FakeDrive) -> Service:
    """A service on real ``load_settings`` (config.json + environment), like ``serve``."""

    def embeddings(settings: Settings) -> FakeEmbeddings | None:
        return FakeEmbeddings(settings.embedding_model) if settings.embeddings_enabled else None

    return Service(
        lambda: load_settings(data),
        backend_factory=lambda _: drive,
        embedding_factory=embeddings,
        extractor_factory=lambda s: Extractor(None, ocr_enabled=False, work_dir=s.work_dir),
    )


@pytest.fixture()
def env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> Iterator[Env]:
    for name in CLEARED:
        monkeypatch.delenv(name, raising=False)
    caplog.set_level(logging.DEBUG)
    drive = FakeDrive()
    drive.add_root(ROOT, "Kế toán")
    drive.put(ROOT, "doc1111111111111", "hoa-don.txt", "Hóa đơn tiền điện".encode())
    drive.add_root(OTHER, "Nhân sự")
    drive.put(OTHER, "doc2222222222222", "luong.txt", "Bảng lương tháng 3".encode())
    data, secrets = tmp_path / "data", tmp_path / "secrets"
    secrets.mkdir()
    service = build_service(data, drive)
    service.add_sources([ROOT])
    service.sync()
    now = [1000.0]
    admin = Admin(service, secrets, flow_factory=FakeFlow, clock=lambda: now[0])
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(service, admin), host="127.0.0.1", port=port, log_level="debug")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    loop = SyncLoop(service, run_at_start=False)
    loop.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    state = Env(f"http://127.0.0.1:{port}", service, admin, drive, data, secrets, now)
    yield state
    loop.close()
    server.should_exit = True
    thread.join(timeout=10)
    server_logs = "\n".join(  # the server process only has these loggers (no MCP client)
        r.getMessage() for r in caplog.records if not r.name.startswith(CLIENT_LOGGERS)
    )
    everything = "\n".join(state.seen) + server_logs
    for secret in SECRETS:
        assert secret not in everything, "a secret reached a tool result or a log line"
    for line in PRIVATE_KEY.splitlines():
        assert line not in "\n".join(state.seen)


def wait_idle(service: Service) -> None:
    deadline = time.time() + 10
    while (service.take_request() is not None or service.sync_running()) and time.time() < deadline:
        time.sleep(0.05)


def write_secret(env: Env, name: str, value: Any) -> None:
    path = env.secrets / name
    path.write_text(value if isinstance(value, str) else json.dumps(value))
    path.chmod(0o600)


def service_account() -> dict[str, str]:
    return {
        "type": "service_account",
        "client_email": "reader@project.iam.gserviceaccount.com",
        "private_key": PRIVATE_KEY,
    }


def oauth_client() -> dict[str, Any]:
    return {
        "installed": {"client_id": "cid.apps.googleusercontent.com", "client_secret": CLIENT_SECRET}
    }


# -- off by default ----------------------------------------------------------------------------
def test_admin_tools_are_off_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gdrive_rag.server import create_server

    monkeypatch.delenv(ADMIN_ENV, raising=False)
    assert admin_tools_enabled() is False
    for value in ("0", "", "no", "maybe"):
        monkeypatch.setenv(ADMIN_ENV, value)
        assert admin_tools_enabled() is False
    monkeypatch.setenv(ADMIN_ENV, "1")
    assert admin_tools_enabled() is True
    service = build_service(tmp_path, FakeDrive())

    async def names(server: Any) -> set[str]:
        return {tool.name for tool in await server.list_tools()}

    import anyio

    assert anyio.run(names, create_server(service)) == READ_ONLY
    assert anyio.run(names, create_server(service, Admin(service))) == READ_ONLY | ADMIN_TOOLS


def test_admin_listing_and_annotations(env: Env) -> None:
    tools = {tool.name: tool for tool in call(env.url, "tools/list").tools}
    assert set(tools) == READ_ONLY | ADMIN_TOOLS
    destructive = {
        "remove_source",
        "set_google_service_account",
        "google_auth_finish",
        "remove_google_auth",
        "set_embedding",
        "remove_embedding",
    }
    for name in ADMIN_TOOLS:
        notes = tools[name].annotations
        assert notes is not None
        assert notes.read_only_hint is (name == "google_auth_status")
        assert notes.destructive_hint is (name in destructive)
        assert "only when the user explicitly asks" in (tools[name].description or "")
        if name in destructive:
            assert "confirm" in tools[name].input_schema["properties"]
    assert tools["set_embedding"].annotations.open_world_hint is True
    assert tools["add_source"].annotations.open_world_hint is True
    # secret-bearing arguments carry no constraints that could echo them in a validation error
    assert set(tools["set_embedding"].input_schema["properties"]["api_key"]) <= {
        "anyOf",
        "default",
        "title",
    }


# -- sources -----------------------------------------------------------------------------------
def test_add_and_remove_source(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    added = env.tool("add_source", link=f"https://drive.google.com/drive/folders/{OTHER}")
    assert added["applied"] is True and added["source"]["name"] == "Nhân sự"
    assert added["sync"]["queued"] is True
    wait_idle(env.service)
    assert env.tool("list_sources")["sources"][1]["documents"] == 1
    again = env.tool("add_source", link=OTHER)
    assert again["applied"] is False and "already" in again["message"]

    preview = env.tool("remove_source", source="Nhân sự")
    assert preview["applied"] is False and preview["confirm_needed"] is True
    assert preview["documents_removed"] == 1
    assert len(env.tool("list_sources")["sources"]) == 2  # nothing changed

    with env.service._sync_lock():  # a sync is running
        busy = env.tool("remove_source", source="Nhân sự", confirm=True)
    assert "a sync is running" in busy
    assert len(env.tool("list_sources")["sources"]) == 2

    monkeypatch.setenv("GDRAG_DRIVE_LINKS", OTHER)
    assert "GDRAG_DRIVE_LINKS" in env.tool("remove_source", source=OTHER, confirm=True)
    monkeypatch.delenv("GDRAG_DRIVE_LINKS")

    done = env.tool("remove_source", source="Nhân sự", confirm=True)
    assert done["applied"] is True and done["documents_removed"] == 1
    assert [s["name"] for s in env.tool("list_sources")["sources"]] == ["Kế toán"]
    assert "unknown source" in env.tool("remove_source", source="nope")
    assert "could not add" in env.tool("add_source", link="not a link")


# -- secrets folder ----------------------------------------------------------------------------
def test_file_arguments_are_confined_to_the_secrets_folder(env: Env, tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(service_account()))
    (env.secrets / "link.json").symlink_to(outside)
    (env.secrets / "sub").mkdir()
    for name in ("../outside.json", str(outside), "link.json", "sub/../../outside.json"):
        refused = env.tool("set_google_service_account", file=name, confirm=True)
        assert "only files inside the secrets folder" in refused, name
    assert "not found" in env.tool("set_google_service_account", file="missing.json")
    assert "not a regular file" in env.tool("set_google_service_account", file="sub")
    assert env.tool("google_auth_status")["method"] == "none"
    env.admin.secrets_dir = None
    assert "no secrets folder" in env.tool("google_auth_start", oauth_client_file="x.json")


# -- Google access -----------------------------------------------------------------------------
def test_service_account_confirm_and_restart(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    write_secret(env, "sa.json", service_account())
    write_secret(env, "broken.json", "{not json " + API_KEY)
    assert "not valid JSON" in env.tool("set_google_service_account", file="broken.json")
    preview = env.tool("set_google_service_account", file="sa.json")
    assert preview["applied"] is False and "reader@project" in preview["would"]
    assert "emptied from the index" in preview["would"]
    assert not env.service.settings.service_account_file.exists()

    done = env.tool("set_google_service_account", file="sa.json", confirm=True)
    assert done["applied"] is True and done["client_email"].startswith("reader@")
    stored = env.service.settings.service_account_file
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    status = env.tool("google_auth_status")
    assert status["method"] == "service_account" and status["method_from"] == "stored"
    assert status["service_account_email"] == "reader@project.iam.gserviceaccount.com"

    restarted = build_service(env.data, env.drive)  # a restart keeps what the tool stored
    apply_environment(restarted)
    assert restarted.settings.google_auth == "service_account"

    monkeypatch.setenv("GDRAG_GOOGLE_CREDENTIALS", "/secrets/other.json")
    locked = env.tool("remove_google_auth", confirm=True)
    assert "GDRAG_GOOGLE_CREDENTIALS" in locked and stored.exists()
    assert env.tool("google_auth_status")["locked_by_environment"] == ["GDRAG_GOOGLE_CREDENTIALS"]


def test_oauth_start_and_finish(env: Env) -> None:
    write_secret(env, "client.json", oauth_client())
    assert "no sign-in is in progress" in env.tool(
        "google_auth_finish", redirect_url=f"http://localhost:53682/?state=STATE42&code={CODE}"
    )
    started = env.tool("google_auth_start", oauth_client_file="client.json")
    assert started["consent_url"].startswith("https://accounts.google.com/")
    assert started["expires_in_seconds"] == 600
    assert env.tool("google_auth_status")["sign_in_pending"] is True
    assert not env.service.settings.client_secret_file.exists()  # nothing stored yet

    wrong = env.tool(
        "google_auth_finish", redirect_url=f"http://localhost:53682/?state=OTHER&code={CODE}"
    )
    assert "different sign-in attempt" in wrong
    assert "whole address" in env.tool("google_auth_finish", redirect_url=CODE, confirm=True)
    good = f"http://localhost:53682/?state=STATE42&code={CODE}&scope=drive.readonly"
    preview = env.tool("google_auth_finish", redirect_url=good)
    assert preview["applied"] is False and not env.service.settings.token_file.exists()

    done = env.tool("google_auth_finish", redirect_url=good, confirm=True)
    assert done["applied"] is True and done["method"] == "oauth"
    token = env.service.settings.token_file
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert json.loads(token.read_text())["refresh_token"] == REFRESH
    assert env.service.settings.google_auth == "oauth"
    assert "no sign-in is in progress" in env.tool(
        "google_auth_finish", redirect_url=good, confirm=True
    )  # single use

    env.tool("google_auth_start", oauth_client_file="client.json")
    env.now[0] += 601
    assert "expired" in env.tool("google_auth_finish", redirect_url=good, confirm=True)


def test_remove_google_auth_keeps_the_index(env: Env) -> None:
    write_secret(env, "sa.json", service_account())
    env.tool("set_google_service_account", file="sa.json", confirm=True)
    wait_idle(env.service)
    before = env.tool("sync_status")["documents"]["total"]
    preview = env.tool("remove_google_auth")
    assert preview["applied"] is False and env.service.settings.service_account_file.exists()
    done = env.tool("remove_google_auth", confirm=True)
    assert done["applied"] is True and done["removed"] == ["service_account"]
    assert env.service.settings.google_auth == "none"

    # the real backend factory refuses without credentials; nothing may be deleted
    from gdrive_rag.drive import DriveSourceError

    def no_access(_: Settings) -> FakeDrive:
        raise DriveSourceError("Google access is not configured; run: gdrive-rag setup")

    env.service.backend_factory = no_access
    env.service.sync()
    status = env.tool("sync_status")
    assert status["documents"]["total"] == before
    assert status["sources"][0]["status"] == "error"
    assert env.tool("search", query="hóa đơn")["results"]


# -- embeddings --------------------------------------------------------------------------------
def test_set_embedding_preview_confirm_and_restart(env: Env) -> None:
    preview = env.tool(
        "set_embedding",
        base_url="https://embed.example.com/v1/",
        model="m-1",
        api_key=API_KEY,
    )
    assert preview["applied"] is False and preview["sends_text_to"] == "embed.example.com"
    assert "sent to embed.example.com" in preview["would"]
    chunks = env.tool("sync_status")["chunks"]
    assert preview["chunks_to_embed"] == chunks and preview["vectors_dropped"] == 0
    assert env.service.settings.embedding_api_key == ""  # nothing stored by a preview

    done = env.tool(
        "set_embedding",
        base_url="https://embed.example.com/v1/",
        model="m-1",
        api_key=API_KEY,
        confirm=True,
    )
    assert done["applied"] is True
    config = env.data / "config.json"
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert json.loads(config.read_text())["embedding_api_key"] == API_KEY
    wait_idle(env.service)
    status = env.tool("sync_status")
    assert status["embeddings"]["enabled"] is True
    assert status["embeddings"]["host"] == "embed.example.com"
    assert status["settings_from"]["embedding_api_key"] == "stored"
    assert status["settings_from"]["ocr_langs"] == "default"

    restarted = build_service(env.data, env.drive)
    apply_environment(restarted)
    assert restarted.settings.embedding_model == "m-1"
    assert restarted.settings.embedding_base_url == "https://embed.example.com/v1"

    # same address and model: the stored vectors stay
    env.service.db.set_meta(
        EMBEDDING_IDENTITY_KEY, json.dumps(["https://embed.example.com/v1", "m-1", 1024])
    )
    same = env.tool("set_embedding", base_url="https://embed.example.com/v1", model="m-1")
    assert same["reembeds_all"] is False and same["vectors_dropped"] == 0
    other = env.tool("set_embedding", base_url="https://embed.example.com/v1", model="m-2")
    assert (
        other["reembeds_all"] is True
        and other["vectors_dropped"] == status["embeddings"]["vectors"]
    )

    for bad in ("ftp://x/v1", "https://user:pw@x/v1", "https://x/v1?key=1", "nohost"):
        assert "base_url" in env.tool("set_embedding", base_url=bad, model="m", confirm=True)
    assert "one line" in env.tool(
        "set_embedding", base_url="https://x/v1", model="m", api_key="a b", confirm=True
    )


def test_api_key_file_and_remove_embedding(env: Env) -> None:
    write_secret(env, "openrouter.key", API_KEY + "\n")
    done = env.tool(
        "set_embedding",
        base_url="http://10.0.0.5:8080/v1",
        model="m-1",
        api_key_file="openrouter.key",
        confirm=True,
    )
    assert done["applied"] is True and "not encrypted" in done["message"]
    assert env.service.settings.embedding_api_key == API_KEY
    wait_idle(env.service)
    vectors = env.tool("sync_status")["embeddings"]["vectors"]
    assert vectors > 0

    preview = env.tool("remove_embedding")
    assert preview["applied"] is False and env.service.settings.embeddings_enabled
    done = env.tool("remove_embedding", confirm=True)
    assert done["applied"] is True and done["vectors_kept"] == vectors
    status = env.tool("sync_status")
    assert status["embeddings"]["enabled"] is False and status["embeddings"]["vectors"] == vectors
    assert env.tool("search", query="hóa đơn")["mode"] == "full_text"


def test_environment_wins_and_tools_refuse(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GDRAG_EMBEDDING_MODEL", "env-model")
    monkeypatch.setenv("GDRAG_EMBEDDING_BASE_URL", "https://openrouter.ai/api/v1")
    refused = env.tool(
        "set_embedding",
        base_url="https://openrouter.ai/api/v1",
        model="other-model",
        api_key=API_KEY,
        confirm=True,
    )
    assert "GDRAG_EMBEDDING_MODEL" in refused and "GDRAG_EMBEDDING_BASE_URL" not in refused
    # values equal to the environment are accepted and not copied into config.json
    done = env.tool(
        "set_embedding",
        base_url="https://openrouter.ai/api/v1/",
        model="env-model",
        api_key=API_KEY,
        confirm=True,
    )
    assert done["applied"] is True
    stored = json.loads((env.data / "config.json").read_text())
    assert "embedding_model" not in stored and "embedding_base_url" not in stored
    status = env.tool("sync_status")["settings_from"]
    assert status["embedding_model"] == "environment (GDRAG_EMBEDDING_MODEL)"
    assert status["embedding_api_key"] == "stored"

    monkeypatch.setenv("GDRAG_EMBEDDING_API_KEY", "env-key-" + API_KEY)
    assert "GDRAG_EMBEDDING_API_KEY" in env.tool("remove_embedding", confirm=True)
    monkeypatch.setenv("GDRAG_GOOGLE_AUTH", "oauth")
    write_secret(env, "client.json", oauth_client())
    assert "GDRAG_GOOGLE_AUTH" in env.tool("google_auth_start", oauth_client_file="client.json")
    assert env.tool("google_auth_status")["method_from"] == "environment (GDRAG_GOOGLE_AUTH)"


def test_settings_change_during_a_sync_applies_from_the_next_step(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gdrive_rag.server

    monkeypatch.setattr(gdrive_rag.server, "BUSY_RETRY_SECONDS", 0.2)
    with env.service._sync_lock():  # another process (a CLI sync) is syncing
        done = env.tool(
            "set_embedding",
            base_url="https://e.example/v1",
            model="m",
            api_key=API_KEY,
            confirm=True,
        )
        assert done["applied"] is True and done["sync"]["after_running_sync"] is True
        time.sleep(0.5)  # the loop finds the lock taken and keeps the request
        assert env.tool("sync_status")["sync"]["requested"] is True
    wait_idle(env.service)  # the queued sync runs once the running one has finished
    assert env.tool("sync_status")["embeddings"]["vectors"] > 0


def test_validation_errors_do_not_echo_secrets(env: Env) -> None:
    text = env.tool(
        "set_embedding", base_url="https://x/v1", model="m", api_key=API_KEY, dimensions="many"
    )
    assert isinstance(text, str) and API_KEY not in text
    text = env.tool("google_auth_finish", redirect_url=f"http://localhost/?code={CODE}", confirm=7)
    assert isinstance(text, str)


def test_cli_shares_the_admin_functions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from gdrive_rag.cli import main

    for name in CLEARED:
        monkeypatch.delenv(name, raising=False)
    key = tmp_path / "key.txt"
    key.write_text(API_KEY)
    base = ["--data-dir", str(tmp_path / "d"), "--json", "embedding"]
    assert main([*base, "set", "--model", "m", "--api-key-file", str(key)]) == 0
    assert json.loads(capsys.readouterr().out)["applied"] is False
    assert main([*base, "set", "--model", "m", "--api-key-file", str(key), "--yes"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["applied"] is True and API_KEY not in out
    assert load_settings(tmp_path / "d").embedding_api_key == API_KEY
    assert main([*base, "remove", "--yes"]) == 0
    assert load_settings(tmp_path / "d").embedding_api_key == ""
    monkeypatch.setenv("GDRAG_EMBEDDING_API_KEY", "x")
    assert main([*base, "remove", "--yes"]) == 2
    assert "GDRAG_EMBEDDING_API_KEY" in capsys.readouterr().out
    assert os.environ["GDRAG_EMBEDDING_API_KEY"] == "x"
