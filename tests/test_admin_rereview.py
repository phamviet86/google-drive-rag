"""Regression tests for the re-review of the admin tools: each fails on 60553c9."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from helpers import FakeDrive
from test_admin import API_KEY, CLEARED, ROOT, Env, build_service, write_secret

from gdrive_rag.config import load_settings, save_config
from gdrive_rag.drive import DriveSourceError
from gdrive_rag.embeddings import EmbeddingError, OpenAICompatibleEmbeddings

pytest_plugins = ["test_admin"]  # the `env` fixture: a live server with admin tools


def attempt(env: Env, url: str, **extra: Any) -> Any:
    return env.tool("set_embedding", base_url=url, model="m", api_key=API_KEY, **extra)


# A -------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "https://169%2e254%2e169%2e254/v1",
        "https://2852039166/v1",
        "https://0xa9fea9fe/v1",
        "https://0251.0376.0251.0376/v1",
        "https://169.254.43518/v1",
        "https://metadata.google.internal./v1",
        "https://metadata.nip.example/v1",  # a public-looking name resolving to 169.254.169.254
        "https://router.nip.example/v1",  # resolving to a private address
        "https://mixed.example/v1",  # one public and one private record
        "https://unresolvable.example/v1",
        "https://bad_host.example/v1",
        "https://[::ffff:169.254.169.254]/v1",
    ],
)
def test_disguised_or_inside_hosts_are_refused(env: Env, url: str) -> None:
    refused = attempt(env, url, confirm=True)
    assert isinstance(refused, str) and refused.startswith("Error: base_url"), refused
    assert env.service.settings.embedding_api_key == ""


def test_trailing_dot_is_dropped_and_names_resolve(env: Env) -> None:
    preview = attempt(env, "https://embed.example./v1")
    assert preview["endpoint"]["host"] == "embed.example"


def test_request_time_check_refuses_an_inside_address() -> None:
    # set while it resolved to a public address; now the name points at the metadata service
    client = OpenAICompatibleEmbeddings("https://metadata.nip.example/v1", API_KEY, "m", 32, "")
    with pytest.raises(EmbeddingError) as raised:
        client.embed_query("x")
    assert "refused" in str(raised.value) and API_KEY not in str(raised.value)


# B + D (setup) -------------------------------------------------------------------------------
def store_key(data: Path) -> None:
    save_config(
        data,
        {
            "embedding_base_url": "https://good.example/v1",
            "embedding_model": "m",
            "embedding_api_key": API_KEY,
        },
    )


def test_setup_yes_never_moves_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from gdrive_rag.cli import main

    for name in CLEARED:
        monkeypatch.delenv(name, raising=False)
    data = tmp_path / "d"
    store_key(data)
    monkeypatch.setenv("GDRAG_EMBEDDING_BASE_URL", "http://evil.example/v1")
    main(["--data-dir", str(data), "--json", "setup", "--yes"])
    capsys.readouterr()
    stored = json.loads((data / "config.json").read_text())
    assert stored["embedding_base_url"] == "https://good.example/v1"


def test_setup_interactive_uses_the_tool_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import getpass
    import sys

    from gdrive_rag.cli import main

    for name in CLEARED:
        monkeypatch.delenv(name, raising=False)
    data = tmp_path / "d"
    store_key(data)
    answers = iter(["https://evil.example/v1", "m", ""])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "")  # keep the current key
    assert main(["--data-dir", str(data), "--json", "setup"]) == 2
    assert "needs its own API key" in capsys.readouterr().out
    assert load_settings(data).embedding_base_url == "https://good.example/v1"

    monkeypatch.setenv("GDRAG_GOOGLE_CREDENTIALS", "/secrets/google.json")
    answers = iter(["https://good.example/v1", "m", "1", "/tmp/x.json", ""])
    assert main(["--data-dir", str(data), "--json", "setup"]) == 2
    assert "GDRAG_GOOGLE_CREDENTIALS" in capsys.readouterr().out


# C -------------------------------------------------------------------------------------------
def test_unverified_sources_are_never_stored(env: Env) -> None:
    def no_access(_: Any) -> FakeDrive:
        raise DriveSourceError("Google access is not configured; run: gdrive-rag setup")

    env.service.backend_factory = no_access
    before = env.tool("list_sources")["sources"]
    for link in (API_KEY, "sk-or-v1-abcdefABCDEF0123456789"):
        refused = env.tool("add_source", link=link)
        assert isinstance(refused, str) and "did not confirm" in refused
    assert env.tool("list_sources")["sources"] == before
    assert "unknown source" in env.tool("remove_source", source=API_KEY)


def test_oauth_error_value_is_not_echoed(env: Env) -> None:
    write_secret(env, "client.json", {"installed": {"client_id": "cid", "client_secret": "s"}})
    env.tool("google_auth_start", oauth_client_file="client.json")
    text = env.tool(
        "google_auth_finish", redirect_url=f"http://localhost/?state=STATE42&error={API_KEY}"
    )
    assert isinstance(text, str) and "returned an error" in text
    # the fixture's teardown asserts API_KEY is in no result and no server log line


# D (source remove) ---------------------------------------------------------------------------
def test_cli_source_remove_honours_drive_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from gdrive_rag.cli import main

    for name in CLEARED:
        monkeypatch.delenv(name, raising=False)
    drive = FakeDrive()
    drive.add_root(ROOT, "Kế toán")
    service = build_service(tmp_path, drive)
    service.add_sources([ROOT])
    monkeypatch.setenv("GDRAG_DRIVE_LINKS", ROOT)
    assert main(["--data-dir", str(tmp_path), "--json", "source", "remove", ROOT]) == 2
    assert "GDRAG_DRIVE_LINKS" in capsys.readouterr().out
    assert len(service.list_sources()) == 1


# E -------------------------------------------------------------------------------------------
def test_refresh_waits_for_removal_and_does_not_restore(tmp_path: Path) -> None:
    from gdrive_rag.config import token_lock
    from gdrive_rag.drive import save_refreshed_token

    token = tmp_path / "google" / "token.json"
    token.parent.mkdir()
    token.write_text("{}")
    results: list[bool] = []
    with token_lock(token):  # removal holds the lock
        worker = threading.Thread(
            target=lambda: results.append(save_refreshed_token(token, '{"t": 1}'))
        )
        worker.start()
        time.sleep(0.2)
        assert results == []  # the refresh waits instead of checking and writing now
        token.unlink()
    worker.join(5)
    assert results == [False] and not token.exists()


def test_remove_google_files_takes_the_token_lock(tmp_path: Path) -> None:
    from gdrive_rag.auth import remove_google_files
    from gdrive_rag.config import Settings, token_lock

    settings = Settings(data_dir=tmp_path)
    settings.google_dir.mkdir()
    settings.token_file.write_text("{}")
    done = threading.Event()
    with token_lock(settings.token_file):
        worker = threading.Thread(target=lambda: (remove_google_files(settings), done.set()))
        worker.start()
        assert not done.wait(0.2)  # removal waits for a refresh that holds the lock
    worker.join(5)
    assert done.is_set() and not settings.token_file.exists()


# F -------------------------------------------------------------------------------------------
def test_import_env_resolves_sources_with_the_imported_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gdrive_rag.admin import import_environment
    from gdrive_rag.service import Service

    for name in CLEARED:
        monkeypatch.delenv(name, raising=False)
    drive = FakeDrive()
    drive.add_root(ROOT, "Kế toán")

    def backend(settings: Any) -> FakeDrive:
        if settings.google_auth != "service_account":
            raise DriveSourceError("Google access is not configured; run: gdrive-rag setup")
        return drive

    service = Service(lambda: load_settings(tmp_path / "d"), backend_factory=backend)
    account = tmp_path / "sa.json"
    account.write_text(
        json.dumps({"type": "service_account", "client_email": "r@p", "private_key": "k"})
    )
    monkeypatch.setenv("GDRAG_GOOGLE_CREDENTIALS", str(account))
    monkeypatch.setenv("GDRAG_DRIVE_LINKS", ROOT)
    import_environment(service)
    source = service.list_sources()[0]
    assert source["kind"] == "folder" and source["name"] == "Kế toán" and source["error"] is None


# G -------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "content",
    [
        "GOCSPX-abcdefghijklmnopqrstuvwxyz12",
        "ya29.a0AfH6SMBabcdefghijklmnopqrstuv",
        "1//0gabcdefghijklmnopqrstuvwxyz0123",
        "-----BEGIN",
    ],
)
def test_google_credentials_are_not_api_keys(env: Env, content: str) -> None:
    write_secret(env, "looks.key", content + "\n")
    refused = env.tool(
        "set_embedding",
        base_url="https://good.example/v1",
        model="m",
        api_key_file="looks.key",
        confirm=True,
    )
    assert isinstance(refused, str) and "Google credential" in refused
    assert env.service.settings.embedding_api_key == ""
