"""Settings: ``<data_dir>/config.json`` (mode 0600) overridden by ``GDRAG_*`` environment variables.

Secrets (the embedding API key, Google credentials and tokens) live only in the data directory or
in the environment. Nothing here prints them; ``public()`` reports only whether a key is set.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
from collections.abc import Iterator
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from . import netcheck

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "qwen/qwen3-embedding-8b"
DEFAULT_DIMENSIONS = 1024
DEFAULT_QUERY_INSTRUCTION = (
    "Given a search question, retrieve relevant passages from documents that answer it"
)
DERIVED_FIELDS = {"data_dir", "embedding_key_from_env"}  # never stored in config.json
AUTH_METHODS = ("none", "service_account", "oauth")

# Environment variable -> config field. Values from the environment win over config.json.
ENV_FIELDS = {
    "GDRAG_EMBEDDING_BASE_URL": "embedding_base_url",
    "GDRAG_EMBEDDING_MODEL": "embedding_model",
    "GDRAG_EMBEDDING_API_KEY": "embedding_api_key",
    "GDRAG_EMBEDDING_DIMENSIONS": "embedding_dimensions",
    "GDRAG_GOOGLE_AUTH": "google_auth",
    "GDRAG_SYNC_INTERVAL_MINUTES": "sync_interval_minutes",
    "GDRAG_OCR": "ocr_enabled",
    "GDRAG_OCR_LANGS": "ocr_langs",
}
DEFAULT_OCR_LANGS = "vie+eng"  # Tesseract language codes joined with "+"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    embedding_base_url: str = DEFAULT_BASE_URL
    embedding_model: str = DEFAULT_MODEL
    embedding_api_key: str = ""
    embedding_key_origin: str = ""  # scheme://host:port the stored key was issued for
    embedding_dimensions: int = DEFAULT_DIMENSIONS
    query_instruction: str = DEFAULT_QUERY_INSTRUCTION
    google_auth: str = "none"
    sync_interval_minutes: int = 15
    ocr_enabled: bool = True
    ocr_langs: str = DEFAULT_OCR_LANGS
    chunk_chars: int = 1500
    chunk_overlap: int = 200
    embedding_key_from_env: bool = False  # derived: GDRAG_EMBEDDING_API_KEY is set

    @property
    def db_path(self) -> Path:
        return self.data_dir / "index.sqlite3"

    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.json"

    @property
    def google_dir(self) -> Path:
        return self.data_dir / "google"

    @property
    def service_account_file(self) -> Path:
        return self.google_dir / "service_account.json"

    @property
    def client_secret_file(self) -> Path:
        return self.google_dir / "client_secret.json"

    @property
    def token_file(self) -> Path:
        return self.google_dir / "token.json"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def embedding_key_mismatch(self) -> bool:
        """A stored key is bound to the origin it was set for. When the address now points at
        another origin the key is not used (never sent), so a removed ``.env`` address cannot
        redirect it. A key from ``GDRAG_EMBEDDING_API_KEY`` is the owner's choice and is exempt."""
        if not self.embedding_api_key.strip() or self.embedding_key_from_env:
            return False
        return self.embedding_key_origin != netcheck.origin(self.embedding_base_url)

    @property
    def embedding_warning(self) -> str | None:
        if not self.embedding_key_mismatch:
            return None
        return (
            "The stored embedding API key was set for "
            f"{self.embedding_key_origin} but the address is now "
            f"{netcheck.origin(self.embedding_base_url)}, so the key is not used and search is "
            "full-text only. Restore the address, or run set_embedding with a key for the new one."
        )

    @property
    def embeddings_enabled(self) -> bool:
        return bool(
            self.embedding_api_key.strip()
            and self.embedding_model.strip()
            and not self.embedding_key_mismatch
        )

    def public(self) -> dict[str, Any]:
        """Settings safe to print: the key is reported as set/unset only."""
        return {
            "data_dir": str(self.data_dir),
            "embedding_base_url": self.embedding_base_url,
            "embedding_model": self.embedding_model,
            "embedding_dimensions": self.embedding_dimensions,
            "embedding_api_key": "set" if self.embedding_api_key else "not set",
            "embedding_key_origin": (
                self.embedding_key_origin
                if self.embedding_api_key and not self.embedding_key_from_env
                else ""
            ),
            "embedding_warning": self.embedding_warning,
            "google_auth": self.google_auth,
            "sync_interval_minutes": self.sync_interval_minutes,
            "ocr_enabled": self.ocr_enabled,
            "ocr_langs": self.ocr_langs,
        }


def resolve_data_dir(value: str | Path | None = None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    env = os.environ.get("GDRAG_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / ".gdrive-rag").resolve()


def load_settings(data_dir: str | Path | None = None, *, use_env: bool = True) -> Settings:
    root = resolve_data_dir(data_dir)
    settings = Settings(data_dir=root)
    stored = read_config(root)
    values: dict[str, Any] = {}
    for key in (
        *ENV_FIELDS.values(),
        "embedding_key_origin",
        "query_instruction",
        "chunk_chars",
        "chunk_overlap",
    ):
        if key in stored:
            values[key] = stored[key]
    if (
        str(values.get("embedding_api_key") or "").strip()
        and not str(values.get("embedding_key_origin") or "").strip()
    ):
        # a config written before keys were bound: bind it to the address stored with it
        # (or the default one), which is where it has been sent so far
        values["embedding_key_origin"] = netcheck.origin(
            str(values.get("embedding_base_url") or DEFAULT_BASE_URL).strip().rstrip("/")
        )
    if use_env:
        for env_name, key in ENV_FIELDS.items():
            raw = os.environ.get(env_name)
            if raw is not None and raw.strip():
                values[key] = raw.strip()
                if key == "embedding_api_key":
                    values["embedding_key_from_env"] = True
    return _coerce(replace(settings, **values))


def _coerce(settings: Settings) -> Settings:
    ocr = settings.ocr_enabled
    if isinstance(ocr, str):
        ocr = ocr.strip().lower() not in {"0", "false", "no", "off"}
    auth = str(settings.google_auth).strip().lower().replace("-", "_")
    if auth not in AUTH_METHODS:
        raise ValueError(f"google_auth must be one of: {', '.join(AUTH_METHODS)}")
    dimensions = int(settings.embedding_dimensions)
    if not 32 <= dimensions <= 8192:
        raise ValueError("embedding_dimensions must be between 32 and 8192")
    return replace(
        settings,
        embedding_base_url=str(settings.embedding_base_url).strip().rstrip("/"),
        embedding_model=str(settings.embedding_model).strip(),
        embedding_api_key=str(settings.embedding_api_key).strip(),
        embedding_key_origin=str(settings.embedding_key_origin or "").strip(),
        embedding_dimensions=dimensions,
        google_auth=auth,
        sync_interval_minutes=max(1, int(settings.sync_interval_minutes)),
        ocr_enabled=bool(ocr),
        ocr_langs=_ocr_langs(settings.ocr_langs),
        chunk_chars=int(settings.chunk_chars),
        chunk_overlap=int(settings.chunk_overlap),
    )


def _ocr_langs(value: object) -> str:
    langs = [part.strip() for part in str(value or "").replace(",", "+").split("+")]
    langs = [part for part in langs if part]
    if not langs or not all(re.fullmatch(r"[A-Za-z0-9_]+", part) for part in langs):
        raise ValueError('ocr_langs must be Tesseract language codes such as "vie+eng"')
    return "+".join(langs)


def setting_origins(data_dir: Path) -> dict[str, str]:
    """Where each setting comes from, never its value: ``environment (GDRAG_...)``, ``stored``
    (config.json, written by the CLI or an admin tool) or ``default``. The environment wins."""
    stored = read_config(data_dir)
    origins: dict[str, str] = {}
    for env_name, key in ENV_FIELDS.items():
        if os.environ.get(env_name, "").strip():
            origins[key] = f"environment ({env_name})"
        elif key in stored:
            origins[key] = "stored"
        else:
            origins[key] = "default"
    return origins


def environment_conflicts(changes: dict[str, Any]) -> list[str]:
    """Names of ``GDRAG_*`` variables that hold a different value for a field in ``changes``.
    Such a change would be overridden by the environment, so callers refuse it."""
    names: list[str] = []
    for env_name, key in ENV_FIELDS.items():
        raw = os.environ.get(env_name, "").strip()
        if key in changes and raw and raw.rstrip("/") != str(changes[key]).strip().rstrip("/"):
            names.append(env_name)
    return names


def read_config(data_dir: Path) -> dict[str, Any]:
    path = data_dir / "config.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"config file is not valid JSON: {path}") from exc
    return value if isinstance(value, dict) else {}


def save_config(data_dir: Path, changes: dict[str, Any]) -> Settings:
    """Merge ``changes`` into config.json (written atomically with mode 0600)."""
    allowed = {f for f in asdict(Settings(data_dir=data_dir)) if f not in DERIVED_FIELDS}
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"unknown config keys: {', '.join(sorted(unknown))}")
    with config_lock(data_dir):  # the read-merge-write is one step, across threads and processes
        current = read_config(data_dir)
        current.update(changes)
        write_private(data_dir / "config.json", json.dumps(current, indent=2, ensure_ascii=False))
    return load_settings(data_dir, use_env=False)


@contextlib.contextmanager
def config_lock(data_dir: Path) -> Iterator[None]:
    """Exclusive lock for every read-merge-write of config.json. ``flock`` locks belong to the
    open file, so it also serialises threads of this process, not only other processes."""
    ensure_private_dir(data_dir)
    with (data_dir / ".config.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def token_lock(token_file: Path) -> Iterator[None]:
    """Exclusive lock shared by the OAuth token refresh-write and ``remove_google_auth``."""
    ensure_private_dir(token_file.parent)
    with (token_file.parent / ".token.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)


def write_private(path: Path, payload: str | bytes) -> None:
    """Atomically write a secret-bearing file with mode 0600."""
    ensure_private_dir(path.parent)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload.encode("utf-8") if isinstance(payload, str) else payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        Path(temporary).unlink(missing_ok=True)
