"""Admin operations shared by the CLI and the optional admin MCP tools: Drive sources, Google
access and the embedding endpoint.

Rules every operation follows:

- The environment wins. A change to a field that a ``GDRAG_*`` variable sets to another value is
  refused with the variable's name (``config.environment_conflicts``), so nothing is stored that
  the next restart would silently undo. ``GDRAG_GOOGLE_AUTH`` / ``GDRAG_GOOGLE_CREDENTIALS`` lock
  Google access the same way, and a source listed in ``GDRAG_DRIVE_LINKS`` cannot be removed.
- Destructive operations take ``confirm``; without it they change nothing and say what would
  happen.
- File inputs are names inside the secrets folder (``GDRAG_SECRETS_DIR``, ``/secrets`` in
  Docker), resolved with symlinks and refused outside it.
- No result or error contains a key, token, client secret or file contents. The pasted OAuth
  redirect and the API key are never repeated.
- Settings changes are saved at once: search uses them immediately, the sync loop from its next
  step (a Drive listing already running keeps the credentials it started with). Removing a source
  is refused while a sync runs, because it deletes documents that sync may be writing.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import netcheck
from .auth import (
    GoogleOAuthFlow,
    OAuthFlow,
    import_client_secret,
    import_service_account,
    parse_client_secret,
    parse_redirect,
    remove_google_files,
    store_oauth_token,
)
from .config import (
    DEFAULT_BASE_URL,
    ENV_FIELDS,
    environment_conflicts,
    save_config,
    setting_origins,
)
from .drive import parse_drive_link
from .embeddings import OpenAICompatibleEmbeddings
from .rules import clean
from .search import EMBEDDING_IDENTITY_KEY
from .service import Service, SyncBusy

ADMIN_ENV = "GDRAG_ADMIN_TOOLS"
SECRETS_ENV = "GDRAG_SECRETS_DIR"
GOOGLE_ENV = ("GDRAG_GOOGLE_AUTH", "GDRAG_GOOGLE_CREDENTIALS")
OAUTH_EXPIRY_SECONDS = 600
MAX_SECRET_FILE_BYTES = 1_000_000
MAX_API_KEY_CHARS = 4096
KEY_PATTERN = re.compile(rf"[A-Za-z0-9._~+/=-]{{16,{MAX_API_KEY_CHARS}}}")
GOOGLE_SECRET_MARKERS = ("GOCSPX-", "ya29.", "1//", "-----BEGIN")
TRUE = {"1", "true", "yes", "on"}
REVOKE_HINT = (
    "Access is not revoked at Google. For a Gmail sign-in, remove this app at "
    "https://myaccount.google.com/permissions; for a service account, delete its key in Google "
    "Cloud console (IAM & Admin > Service accounts > Keys) or stop sharing the folders with it."
)


class AdminRefused(ValueError):
    """The change was not made; the message says why in plain words (never a secret)."""


def admin_tools_enabled() -> bool:
    """Admin MCP tools are off unless ``GDRAG_ADMIN_TOOLS`` is 1/true/yes/on (environment only,
    so no tool or config file can switch them on)."""
    return os.environ.get(ADMIN_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def secrets_dir_from_env() -> Path | None:
    value = os.environ.get(SECRETS_ENV, "").strip()
    return Path(value) if value else None


@dataclass
class _PendingSignIn:
    flow: OAuthFlow
    client: dict[str, Any]
    state: str
    expires: float


class Admin:
    def __init__(
        self,
        service: Service,
        secrets_dir: Path | None = None,
        *,
        flow_factory: Callable[[dict[str, Any]], OAuthFlow] = GoogleOAuthFlow,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.service = service
        self.secrets_dir = secrets_dir
        self.flow_factory = flow_factory
        self.clock = clock
        self._lock = threading.Lock()
        self._pending: _PendingSignIn | None = None

    # -- files ---------------------------------------------------------------------------------
    def secret_file(self, value: str) -> Path:
        """A regular file inside the secrets folder; symlinks are resolved before the check."""
        if self.secrets_dir is None:
            raise AdminRefused(f"no secrets folder is configured ({SECRETS_ENV})")
        name = str(value or "").strip()
        if not name:
            raise AdminRefused("give a file name inside the secrets folder")
        root = self.secrets_dir.resolve()
        candidate = Path(name) if Path(name).is_absolute() else root / name
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            resolved = None
        if resolved is None or not resolved.is_relative_to(root):  # one answer: no probing
            raise AdminRefused("no such file inside the secrets folder")
        if not resolved.is_file():
            raise AdminRefused("that name is not a regular file in the secrets folder")
        if resolved.stat().st_size > MAX_SECRET_FILE_BYTES:
            raise AdminRefused("that file is too large")
        return resolved

    def _read_secret(self, value: str) -> str:
        path = self.secret_file(value)
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raise AdminRefused("cannot read that file (check its owner and mode)") from None

    # -- sources -------------------------------------------------------------------------------
    def add_source(self, link: str) -> dict[str, Any]:
        result = self.service.add_sources([link])[0]
        if "id" not in result:  # not a Drive link: nothing was stored
            raise AdminRefused(
                "could not add: not a Google Drive folder or file link, or a local: path"
            )
        answer = {"applied": not result.get("already_added"), "source": _source(result)}
        if result.get("already_added"):
            answer["message"] = "This source was already added; nothing changed."
            return answer
        if result.get("status") == "error" or result.get("kind") == "unknown":
            # Google did not confirm it (404, not shared, or Google access not working): keep
            # nothing, so a mistyped key that looks like an ID never reaches list_sources
            self.service.discard_source(int(result["id"]))
            if str(result["root_id"]).startswith("local:"):  # our own message, no secret in it
                raise AdminRefused(
                    f"could not add: {result.get('error') or 'local folder not available'}"
                )
            raise AdminRefused(
                "Google did not confirm that link or ID (not found, not shared with the Google "
                "account in use, or Google access is not set up); nothing was added"
            )
        answer["sync"] = self.service.queue_sync(int(result["id"]))
        answer["message"] = "Source added; it is read in the background. Check sync_status."
        return answer

    def remove_source(self, source: str, confirm: bool) -> dict[str, Any]:
        preview = self.service.source_preview(source)
        if preview["root_id"] in _env_links():
            raise AdminRefused(
                "this source is listed in GDRAG_DRIVE_LINKS and would come back at the next "
                "restart; remove it from .env first"
            )
        message = (
            f"Removing source {preview['name']!r} drops {preview['documents_removed']} documents "
            "from the index (documents another source also holds stay). Drive and local files are "
            "not changed."
        )
        if not confirm:
            return {"applied": False, "confirm_needed": True, "would": message, **preview}
        try:
            removed = self.service.remove_source(str(preview["id"]))
        except SyncBusy:
            raise AdminRefused(
                "a sync is running; try again when sync_status shows running: false"
            ) from None
        return {"applied": True, "message": "Source removed.", **removed}

    def set_path_rules(
        self,
        source: str,
        demote: list[str] | None = None,
        exclude: list[str] | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Set a source's search path rules. An omitted list keeps the current patterns; an
        empty list clears them. Without confirm it only previews how many documents each
        pattern hits. Search-time only: nothing is re-extracted or re-embedded."""
        preview = self.service.source_preview(source)
        source_id = int(preview["id"])
        current = self.service.path_rules(source_id)
        wanted = {
            "demote": current["demote"] if demote is None else clean(demote, "demote"),
            "exclude": current["exclude"] if exclude is None else clean(exclude, "exclude"),
        }
        answer: dict[str, Any] = {
            "source": {"id": source_id, "name": preview["name"]},
            "current": current,
            "rules": wanted,
            "matches": self.service.rule_matches(source_id, wanted),
        }
        found = answer["matches"]
        if wanted["exclude"] and found["documents"] and found["excluded"] == found["documents"]:
            raise AdminRefused(
                "these exclude patterns would hide every document of this source from search; "
                "use narrower patterns, or remove_source to drop the source"
            )
        if not confirm:
            answer["would"] = (
                "Search would use these rules for this source: demote lowers a file's score, "
                "exclude leaves it out of results (still readable by ID). Nothing is re-indexed."
            )
            return {"applied": False, "confirm_needed": True, **answer}
        self.service.set_path_rules(source_id, wanted)
        return {"applied": True, "message": "Path rules saved; search uses them now.", **answer}

    # -- Google access -------------------------------------------------------------------------
    def google_status(self) -> dict[str, Any]:
        settings = self.service.reload()
        email = None
        with contextlib.suppress(OSError, ValueError, AttributeError):
            value = json.loads(settings.service_account_file.read_text(encoding="utf-8"))
            email = str(value.get("client_email") or "") or None
        with self._lock:
            pending = self._pending
            left = None if pending is None else max(0, int(pending.expires - self.clock()))
        return {
            "method": settings.google_auth,
            "method_from": setting_origins(settings.data_dir)["google_auth"],
            "service_account_stored": settings.service_account_file.is_file(),
            "service_account_email": email,
            "oauth_client_stored": settings.client_secret_file.is_file(),
            "oauth_token_stored": settings.token_file.is_file(),
            "locked_by_environment": [n for n in GOOGLE_ENV if os.environ.get(n, "").strip()],
            "sign_in_pending": bool(left),
            "sign_in_expires_in_seconds": left or None,
            "secrets_dir_configured": self.secrets_dir is not None,
        }

    def _google_unlocked(self) -> None:
        require_google_unlocked()

    def set_service_account(self, file: str, confirm: bool) -> dict[str, Any]:
        self._google_unlocked()
        payload = self._read_secret(file)
        try:
            value = json.loads(payload)
        except ValueError:
            raise AdminRefused("the service-account file is not valid JSON") from None
        if not isinstance(value, dict) or value.get("type") != "service_account":
            raise AdminRefused('this is not a service-account key ("type" must be service_account)')
        email = str(value.get("client_email") or "")
        message = (
            f"Google access will use the service account {email}. Each Drive source must be "
            "shared with it; a source it cannot open is emptied from the index at the next sync."
        )
        if not confirm:
            return {"applied": False, "confirm_needed": True, "would": message}
        result = import_service_account(self.service.settings, payload)
        self.service.reload()
        return {
            "applied": True,
            "method": "service_account",
            "client_email": result["client_email"],
            "message": (
                f"Saved. Share each Drive folder with {result['client_email']} (Viewer), then "
                "call sync_now. The next scheduled sync also uses it."
            ),
        }

    def oauth_start(self, oauth_client_file: str) -> dict[str, Any]:
        self._google_unlocked()
        client = parse_client_secret(self._read_secret(oauth_client_file))
        flow = self.flow_factory(client)
        url, state = flow.authorization_url()
        with self._lock:
            self._pending = _PendingSignIn(flow, client, state, self.clock() + OAUTH_EXPIRY_SECONDS)
        return {
            "applied": False,
            "consent_url": url,
            "expires_in_seconds": OAUTH_EXPIRY_SECONDS,
            "instructions": (
                "Open consent_url in a browser on your own computer, sign in and press Allow. "
                "The browser then fails to open a localhost page: that is expected. Copy the "
                "whole address from the address bar and give it to google_auth_finish. Nothing "
                "is changed until then."
            ),
        }

    def oauth_finish(self, redirect_url: str, confirm: bool) -> dict[str, Any]:
        self._google_unlocked()
        with self._lock:
            pending = self._pending
            if pending is not None and self.clock() > pending.expires:
                self._pending = pending = None
        if pending is None:
            raise AdminRefused("no sign-in is in progress or it expired; call google_auth_start")
        code = parse_redirect(redirect_url, pending.state, allow_bare_code=False)
        if not confirm:
            return {
                "applied": False,
                "confirm_needed": True,
                "would": (
                    "Google access will switch to this signed-in account. A source it cannot "
                    "open is emptied from the index at the next sync."
                ),
            }
        with self._lock:
            if self._pending is not pending:
                raise AdminRefused("this sign-in was already used or replaced; start again")
            self._pending = None  # the code is single-use
        token = pending.flow.fetch_token(code)
        store_oauth_token(self.service.settings, pending.client, token)
        self.service.reload()
        return {
            "applied": True,
            "method": "oauth",
            "message": "Signed in. Call sync_now to read Drive with this account.",
        }

    def remove_google(self, confirm: bool) -> dict[str, Any]:
        self._google_unlocked()
        message = (
            "Stored Google credentials are deleted and syncing stops (sources show an error). "
            "The index is kept and search keeps working."
        )
        if not confirm:
            return {"applied": False, "confirm_needed": True, "would": message}
        with self._lock:
            self._pending = None
        removed = remove_google_files(self.service.settings)
        self.service.reload()
        return {
            "applied": True,
            "removed": removed,
            "message": message,
            "not_revoked_at_google": REVOKE_HINT,
        }

    # -- embeddings ----------------------------------------------------------------------------
    def set_embedding(
        self,
        base_url: str | None = None,
        model: str | None = None,
        api_key_file: str | None = None,
        api_key: str | None = None,
        dimensions: int | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        settings = self.service.reload()
        # omitted address or model: keep the current one (stored, environment or default)
        base_url = _endpoint(base_url or settings.embedding_base_url or DEFAULT_BASE_URL)
        model = str(model or settings.embedding_model or "").strip()
        if not model:
            raise AdminRefused("give the embedding model name")
        if api_key_file and api_key:
            raise AdminRefused("give api_key_file or api_key, not both")
        new_key = ""
        if api_key_file:
            new_key = _api_key(self._read_secret(api_key_file))
        elif api_key:
            new_key = _api_key(api_key)
        moves = _origin(base_url) != _origin(settings.embedding_base_url)
        if moves and settings.embedding_api_key and not new_key:
            # the current key was issued for the current host; never send it anywhere else
            raise AdminRefused(
                "a new address needs its own API key: pass api_key_file (or api_key) as well"
            )
        if moves and os.environ.get("GDRAG_EMBEDDING_API_KEY", "").strip():
            raise AdminRefused(
                "the API key comes from GDRAG_EMBEDDING_API_KEY in the environment (.env); the "
                "address cannot change while it does. Empty it and restart first"
            )
        key = new_key or settings.embedding_api_key
        if not key:
            raise AdminRefused("an API key is needed (api_key_file or api_key)")
        dims = settings.embedding_dimensions if dimensions is None else int(dimensions)
        if not 32 <= dims <= 8192:
            raise AdminRefused("dimensions must be between 32 and 8192")
        changes: dict[str, Any] = {
            "embedding_base_url": base_url,
            "embedding_model": model,
            "embedding_api_key": key,
            "embedding_dimensions": dims,
        }
        changes = _refuse_environment(changes)
        identity = OpenAICompatibleEmbeddings(base_url, "", model, dims, "").identity
        stored = self.service.db.get_meta(EMBEDDING_IDENTITY_KEY)
        with self.service.db.read() as connection:
            vectors = int(connection.execute("SELECT count(*) FROM vectors").fetchone()[0])
            chunks = int(
                connection.execute("SELECT count(DISTINCT chunk_hash) FROM chunks").fetchone()[0]
            )
        same = stored == identity
        scheme, host, port, path = netcheck.parse(base_url)
        plan: dict[str, Any] = {
            "sends_text_to": host,
            "endpoint": {
                "scheme": scheme,
                "host": host,
                "port": port,
                "path": path,
            },
            "host_changes": moves,
            "model": model,
            "dimensions": dims,
            "reembeds_all": not same,
            "vectors_dropped": 0 if same else vectors,
            "chunks_to_embed": max(0, chunks - vectors) if same else chunks,
        }
        warning = (
            f"Your API key and the text of every indexed document will be sent to {host} "
            f"({scheme}, port {port}, path {path}) to "
            "compute embeddings."
        )
        if not same and vectors:
            warning += (
                f" The {vectors} stored vectors are dropped and {chunks} chunks are embedded "
                "again; search is full-text only until that finishes."
            )
        if scheme == "http":
            warning += " The address uses http, so the text is not encrypted on the way."
        if not confirm:
            return {"applied": False, "confirm_needed": True, "would": warning, **plan}
        save_config(settings.data_dir, changes)
        self.service.reload()
        return {
            "applied": True,
            "message": warning + " Embedding runs with the next sync.",
            **plan,
            "sync": self.service.queue_sync(None),
        }

    def remove_embedding(self, confirm: bool) -> dict[str, Any]:
        settings = self.service.reload()
        _refuse_environment({"embedding_api_key": ""})
        with self.service.db.read() as connection:
            vectors = int(connection.execute("SELECT count(*) FROM vectors").fetchone()[0])
        message = (
            "The stored API key is deleted and search becomes full-text only. The "
            f"{vectors} stored vectors are kept: setting the same address and model again "
            "reuses them; a different model replaces them."
        )
        if not confirm:
            return {"applied": False, "confirm_needed": True, "would": message}
        save_config(settings.data_dir, {"embedding_api_key": ""})
        self.service.reload()
        return {"applied": True, "message": message, "vectors_kept": vectors}


def import_environment(service: Service) -> dict[str, Any]:
    """Copy the settings the environment provides into stored config (key 0600, credentials
    under ``<data_dir>/google``, links as sources) and list the ``.env`` lines that may then be
    emptied, so the CLI and the admin tools can manage those settings. Never returns a value."""
    settings = service.settings
    blank: list[str] = []
    problems: dict[str, str] = {}
    values = {
        key: os.environ[name].strip()
        for name, key in ENV_FIELDS.items()
        if os.environ.get(name, "").strip()
    }
    if values:
        save_config(settings.data_dir, values)
        blank += [name for name, key in ENV_FIELDS.items() if key in values]
    settings = service.reload()
    credentials = os.environ.get("GDRAG_GOOGLE_CREDENTIALS", "").strip()
    if credentials:
        try:
            payload = Path(credentials).read_text(encoding="utf-8")
            kind = json.loads(payload).get("type")
            if kind == "service_account":
                import_service_account(settings, payload)
            else:
                import_client_secret(settings, payload)
            blank.append("GDRAG_GOOGLE_CREDENTIALS")
        except (OSError, ValueError, AttributeError):
            problems["GDRAG_GOOGLE_CREDENTIALS"] = "the file could not be read or imported"
    service.reload()  # sources below are resolved with the credentials just stored
    links = os.environ.get("GDRAG_DRIVE_LINKS", "").replace(",", " ").split()
    if links:
        results = service.add_sources(links)
        if all("id" in result for result in results):
            blank.append("GDRAG_DRIVE_LINKS")
        else:
            problems["GDRAG_DRIVE_LINKS"] = "a link is not a Google Drive link; fix it first"
    service.reload()
    return {
        "may_blank_in_env": blank,
        "not_imported": problems,
        "next_step": (
            "Empty these lines in .env (keep the name, delete the value), then restart with "
            "`docker compose up -d`. Until then the environment still wins."
            if blank
            else "Nothing to import: no GDRAG_* setting is set in the environment."
        ),
    }


def require_google_unlocked() -> None:
    """The one precedence rule for Google access, used by the admin tools and the CLI."""
    locked = [n for n in GOOGLE_ENV if os.environ.get(n, "").strip()]
    if locked:
        raise AdminRefused(
            f"Google access is set by {', '.join(locked)} in the environment (.env), which "
            "wins at every restart; change it there, or empty it and restart first"
        )


def _source(row: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "name", "kind", "link", "status", "error")
    return {key: row.get(key) for key in keys}


def _env_links() -> set[str]:
    ids: set[str] = set()
    for link in os.environ.get("GDRAG_DRIVE_LINKS", "").replace(",", " ").split():
        with contextlib.suppress(ValueError):
            ids.add(parse_drive_link(link))
    return ids


def _refuse_environment(changes: dict[str, Any]) -> dict[str, Any]:
    """Refuse a change the environment would override; return the changes minus the fields
    the environment already sets to the same value (nothing from .env is copied to disk)."""
    names = environment_conflicts(changes)
    if names:
        raise AdminRefused(
            f"{', '.join(names)} is set in the environment (.env) and wins at every restart; "
            "change it there, or empty it and restart first"
        )
    from_env = {key for name, key in ENV_FIELDS.items() if os.environ.get(name, "").strip()}
    return {key: value for key, value in changes.items() if key not in from_env}


def _endpoint(value: str) -> str:
    """The address rules of ``netcheck`` (shape, resolution, http only to literal local
    addresses), applied when the address is set; the client applies them again per request."""
    netcheck.check(value)
    return str(value).strip().rstrip("/")


def _origin(base_url: str) -> tuple[str, str, int | None]:
    try:
        scheme, host, port, _ = netcheck.parse(base_url)
    except ValueError:
        return "", str(base_url or ""), None  # unparsable: never the same origin
    return scheme, host, port


def _api_key(value: str) -> str:
    """Only key-like text: one token of ``[A-Za-z0-9._~+/=-]``, 16 to 4096 characters. A JSON
    file (an OAuth client, a service-account key) is refused, so it is never sent as a key."""
    key = value.strip()
    if any(marker in key for marker in GOOGLE_SECRET_MARKERS):
        raise AdminRefused("that looks like a Google credential, not an embedding API key")
    if not KEY_PATTERN.fullmatch(key):
        raise AdminRefused(
            "that is not an API key (expected one line of letters, digits and ._~+/=-)"
        )
    return key
