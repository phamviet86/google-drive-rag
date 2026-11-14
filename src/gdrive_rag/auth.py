"""Google credentials: import a service-account key, or sign in with an OAuth Desktop client.

The OAuth flow is headless-friendly: it prints the consent URL, the owner opens it on their own
computer, and pastes back the address the browser was sent to (the page itself will fail to load,
which is expected). Files are stored with mode 0600 under ``<data_dir>/google``. Nothing here
prints key material or tokens.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

from .config import Settings, save_config, token_lock, write_private
from .drive import SCOPES

REDIRECT_URI = "http://localhost:53682/"


def import_service_account(settings: Settings, payload: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except ValueError:
        raise ValueError("the service-account file is not valid JSON") from None
    if not isinstance(value, dict) or value.get("type") != "service_account":
        raise ValueError('this is not a service-account key ("type" must be service_account)')
    if not value.get("client_email") or not value.get("private_key"):
        raise ValueError("the service-account key is missing client_email or private_key")
    write_private(settings.service_account_file, json.dumps(value))
    save_config(settings.data_dir, {"google_auth": "service_account"})
    return {"method": "service_account", "client_email": str(value["client_email"])}


def parse_client_secret(payload: str) -> dict[str, Any]:
    """The OAuth Desktop client config from a client JSON file (never echoed in errors)."""
    try:
        value = json.loads(payload)
    except ValueError:
        raise ValueError("the OAuth client file is not valid JSON") from None
    if not isinstance(value, dict) or not value.get("installed", {}).get("client_id"):
        raise ValueError(
            'this is not an OAuth client of type "Desktop app" (expected an "installed" key)'
        )
    return value


def import_client_secret(settings: Settings, payload: str) -> None:
    write_private(settings.client_secret_file, json.dumps(parse_client_secret(payload)))


def store_oauth_token(settings: Settings, client: dict[str, Any], token_json: str) -> None:
    """Save the client config and the signed-in token (both 0600) and switch to OAuth."""
    write_private(settings.client_secret_file, json.dumps(client))
    write_private(settings.token_file, token_json)
    save_config(settings.data_dir, {"google_auth": "oauth"})


def remove_google_files(settings: Settings) -> list[str]:
    """Delete the stored service-account key, OAuth client and token; return what was removed."""
    removed: list[str] = []
    with token_lock(settings.token_file):  # a token refresh cannot write it back meanwhile
        for label, path in (
            ("service_account", settings.service_account_file),
            ("oauth_client", settings.client_secret_file),
            ("oauth_token", settings.token_file),
        ):
            if path.is_file():
                path.unlink()
                removed.append(label)
    save_config(settings.data_dir, {"google_auth": "none"})
    return removed


class OAuthFlow(Protocol):
    def authorization_url(self) -> tuple[str, str]:
        """(consent URL, state)."""
        ...

    def fetch_token(self, code: str) -> str:
        """Exchange the code; return the authorized-user JSON (with a refresh token)."""
        ...


class GoogleOAuthFlow:  # pragma: no cover - needs a real Google consent screen
    """``google_auth_oauthlib`` flow with PKCE (the code verifier stays in this object)."""

    def __init__(self, client: dict[str, Any]):
        from google_auth_oauthlib.flow import Flow

        self.flow = Flow.from_client_config(client, scopes=SCOPES, redirect_uri=REDIRECT_URI)

    def authorization_url(self) -> tuple[str, str]:
        url, state = self.flow.authorization_url(access_type="offline", prompt="consent")
        return str(url), str(state)

    def fetch_token(self, code: str) -> str:
        try:
            self.flow.fetch_token(code=code)
        except Exception as exc:  # oauthlib errors: report the kind only
            raise ValueError(f"Google refused the sign-in ({type(exc).__name__})") from None
        credentials = self.flow.credentials
        if not credentials.refresh_token:
            raise ValueError("Google did not return a refresh token; try again")
        return str(credentials.to_json())


def parse_redirect(answer: str, expected_state: str, allow_bare_code: bool = True) -> str:
    """Return the authorization code from a pasted redirect URL (or a bare code, CLI only).
    Errors never repeat the pasted text."""
    answer = answer.strip()
    if not answer:
        raise ValueError("nothing was pasted")
    if "://" not in answer and "code=" not in answer:
        if not allow_bare_code:
            raise ValueError("paste the whole address from the browser's address bar")
        return answer
    query = parse_qs(urlparse(answer).query if "://" in answer else answer.lstrip("?"))
    if "error" in query:
        raise ValueError(
            "Google returned an error instead of a code (sign-in cancelled or refused)"
        )
    state = query.get("state", [""])[0]
    if (expected_state or not allow_bare_code) and state != expected_state:
        raise ValueError("the pasted address belongs to a different sign-in attempt")
    code = query.get("code", [""])[0]
    if not code:
        raise ValueError("the pasted address does not contain a code")
    return code


def oauth_sign_in(
    settings: Settings,
    ask: Callable[[str], str],
    say: Callable[[str], None],
    flow_factory: Callable[[dict[str, Any]], OAuthFlow] = GoogleOAuthFlow,
) -> dict[str, Any]:
    """CLI sign-in: the same flow as the admin tools, with the paste read from ``ask``."""
    if not settings.client_secret_file.is_file():
        raise ValueError("OAuth client file is missing; import it first")
    client = parse_client_secret(settings.client_secret_file.read_text(encoding="utf-8"))
    flow = flow_factory(client)
    url, state = flow.authorization_url()
    say("1. Mở địa chỉ sau trên máy tính của bạn và đăng nhập Google:")
    say("")
    say(url)
    say("")
    say("2. Sau khi bấm Cho phép (Allow), trình duyệt sẽ báo không mở được trang localhost.")
    say("   Đó là bình thường. Hãy sao chép TOÀN BỘ địa chỉ trên thanh địa chỉ và dán vào đây.")
    code = parse_redirect(ask("Địa chỉ đã dán: "), state)
    store_oauth_token(settings, client, flow.fetch_token(code))
    return {"method": "oauth", "token_file": str(settings.token_file)}
