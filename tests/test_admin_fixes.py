"""Regression tests for four admin review findings: CGNAT addresses, config.json lost updates,
optional MCP ``set_embedding`` arguments, and a stored key bound to its origin."""

from __future__ import annotations

import ipaddress
import json
import threading
from pathlib import Path
from typing import Any

import anyio
import pytest
from helpers import FakeDrive

from gdrive_rag import config as cfg
from gdrive_rag import netcheck
from gdrive_rag.admin import Admin, AdminRefused
from gdrive_rag.config import ENV_FIELDS, load_settings
from gdrive_rag.extract import Extractor
from gdrive_rag.server import create_server
from gdrive_rag.service import Service

KEY = "sk-or-v1-SECRETKEYVALUE0123456789"
OTHER_KEY = "sk-or-v1-ANOTHERKEYVALUE012345678"
SA = json.dumps({"type": "service_account", "client_email": "a@b.iam", "private_key": "x"})
HOSTS = {"cgnat.example": "100.100.100.200"}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        *ENV_FIELDS,
        "GDRAG_DRIVE_LINKS",
        "GDRAG_GOOGLE_CREDENTIALS",
        "GDRAG_ALLOW_LOCAL_EMBEDDINGS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        netcheck,
        "resolver",
        lambda host, port: [ipaddress.ip_address(HOSTS.get(host, "93.184.216.34"))],
    )


def make(data: Path) -> Service:
    return Service(
        lambda: load_settings(data),
        backend_factory=lambda _: FakeDrive(),
        embedding_factory=lambda s: None,
        extractor_factory=lambda s: Extractor(None, ocr_enabled=False, work_dir=s.work_dir),
    )


def stored(data: Path) -> dict[str, Any]:
    return json.loads((data / "config.json").read_text())  # type: ignore[no-any-return]


# a. non-global "public" addresses ------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://cgnat.example/v1",  # resolves to 100.100.100.200 (Alibaba metadata, CGNAT)
        "https://100.64.0.1/v1",
    ],
)
def test_cgnat_needs_the_local_flag(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(netcheck.DestinationRefused, match="GDRAG_ALLOW_LOCAL_EMBEDDINGS"):
        netcheck.check(url, allow_local=False)
    netcheck.check(url, allow_local=True)
    monkeypatch.setenv("GDRAG_ALLOW_LOCAL_EMBEDDINGS", "1")
    netcheck.check(url)


def test_public_and_mapped_public_addresses_still_pass() -> None:
    netcheck.check("https://93.184.216.34/v1", allow_local=False)
    # IPv4-mapped IPv6 literals stay refused (the ::/8 block is reserved), whatever they wrap
    for mapped in ("::ffff:8.8.8.8", "::ffff:127.0.0.1", "::ffff:100.64.0.1"):
        with pytest.raises(netcheck.DestinationRefused):
            netcheck.check(f"https://[{mapped}]/v1", allow_local=True)
    with pytest.raises(netcheck.DestinationRefused, match="metadata"):
        netcheck.check("https://169.254.169.254/v1", allow_local=True)


# b. config.json read-merge-write ------------------------------------------------------------


def test_parallel_config_writes_do_not_lose_updates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "d"
    cfg.save_config(data, {"embedding_api_key": KEY})
    real = cfg.read_config
    first_read, release = threading.Event(), threading.Event()

    def slow_read(directory: Path) -> dict[str, Any]:
        value = real(directory)
        if threading.current_thread().name == "B" and not first_read.is_set():
            first_read.set()
            release.wait(0.5)  # B holds a stale copy while A tries to write
        return value

    monkeypatch.setattr(cfg, "read_config", slow_read)
    b = threading.Thread(
        target=lambda: cfg.save_config(data, {"google_auth": "service_account"}), name="B"
    )
    b.start()
    assert first_read.wait(5)
    a = threading.Thread(target=lambda: cfg.save_config(data, {"embedding_api_key": ""}), name="A")
    a.start()
    release.set()
    a.join(10)
    b.join(10)
    result = stored(data)
    assert result["embedding_api_key"] == ""  # not brought back by B's stale read
    assert result["google_auth"] == "service_account"


def test_many_threads_keep_every_change(tmp_path: Path) -> None:
    data = tmp_path / "d"
    names = [f"n{i}" for i in range(12)]

    def work(i: int) -> None:
        cfg.save_config(data, {"ocr_langs": "eng", "chunk_chars": 1000 + i})

    threads = [threading.Thread(target=work, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(names) == 12
    assert stored(data)["ocr_langs"] == "eng" and 1000 <= stored(data)["chunk_chars"] < 1012
    assert (data / ".config.lock").exists()


# c. MCP set_embedding arguments are optional -------------------------------------------------


def test_mcp_set_embedding_keeps_address_and_model_when_omitted(tmp_path: Path) -> None:
    data = tmp_path / "d"
    service = make(data)
    Admin(service).set_embedding("https://embed.example/v1", "model-a", api_key=KEY, confirm=True)
    server = create_server(service, Admin(service))

    async def call(arguments: dict[str, Any]) -> Any:
        return await server.call_tool("set_embedding", arguments)

    result = anyio.run(call, {"model": "model-b"})
    text = json.dumps(result, default=str)
    assert "is required" not in text and "embed.example" in text
    result = anyio.run(call, {"dimensions": 512})
    assert "is required" not in json.dumps(result, default=str)
    anyio.run(call, {"model": "model-b", "confirm": True})
    after = stored(data)
    assert after["embedding_model"] == "model-b"
    assert after["embedding_base_url"] == "https://embed.example/v1"
    assert after["embedding_api_key"] == KEY


# d. a stored key is bound to its origin -----------------------------------------------------


def test_key_set_under_env_address_is_not_sent_to_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "d"
    service = make(data)
    monkeypatch.setenv("GDRAG_EMBEDDING_BASE_URL", "https://embed.selfhosted.example/v1")
    Admin(service).set_embedding(None, "m", api_key=KEY, confirm=True)
    config = stored(data)
    assert "embedding_base_url" not in config
    assert config["embedding_key_origin"] == "https://embed.selfhosted.example:443"
    assert load_settings(data).embeddings_enabled
    monkeypatch.delenv("GDRAG_EMBEDDING_BASE_URL")  # the owner removes the .env line
    settings = load_settings(data)
    assert settings.embedding_base_url == cfg.DEFAULT_BASE_URL
    assert settings.embedding_key_mismatch and not settings.embeddings_enabled
    assert "embed.selfhosted.example" in str(settings.embedding_warning)
    public = json.dumps(settings.public())
    assert KEY not in public and "embed.selfhosted.example" in public
    from gdrive_rag.embeddings import client_from_settings

    assert client_from_settings(settings) is None  # nothing is sent anywhere
    status = make(data).status()["embeddings"]
    assert status["enabled"] is False and "not used" in status["warning"]
    assert KEY not in json.dumps(status)
    # saving again for the same (now default) address without a new key must not re-bind it
    with pytest.raises(AdminRefused, match="stored API key was set for"):
        Admin(make(data)).set_embedding(None, None, confirm=True)
    Admin(make(data)).set_embedding(None, None, api_key=OTHER_KEY, confirm=True)
    rebound = load_settings(data)
    assert rebound.embeddings_enabled and rebound.embedding_api_key == OTHER_KEY
    assert stored(data)["embedding_key_origin"] == "https://openrouter.ai:443"


def test_environment_key_is_not_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GDRAG_EMBEDDING_API_KEY", KEY)
    monkeypatch.setenv("GDRAG_EMBEDDING_BASE_URL", "https://other.example/v1")
    settings = load_settings(tmp_path / "d")
    assert settings.embeddings_enabled and settings.embedding_warning is None
    assert settings.public()["embedding_key_origin"] == ""


def test_legacy_key_binds_to_the_stored_address(tmp_path: Path) -> None:
    data = tmp_path / "d"
    data.mkdir()
    (data / "config.json").write_text(
        json.dumps({"embedding_api_key": KEY, "embedding_base_url": "https://embed.example/v1"})
    )
    settings = load_settings(data)
    assert settings.embedding_key_origin == "https://embed.example:443"
    assert settings.embeddings_enabled
    (data / "config.json").write_text(json.dumps({"embedding_api_key": KEY}))
    legacy_default = load_settings(data)
    assert legacy_default.embedding_key_origin == "https://openrouter.ai:443"
    assert legacy_default.embeddings_enabled


def test_remove_embedding_clears_the_origin(tmp_path: Path) -> None:
    data = tmp_path / "d"
    service = make(data)
    Admin(service).set_embedding("https://embed.example/v1", "m", api_key=KEY, confirm=True)
    Admin(service).remove_embedding(True)
    config = stored(data)
    assert config["embedding_api_key"] == "" and config["embedding_key_origin"] == ""
