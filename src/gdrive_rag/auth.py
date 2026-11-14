"""Google credentials: import a service-account key, or sign in with an OAuth Desktop client.

The OAuth flow is headless-friendly: it prints the consent URL, the owner opens it on their own
computer, and pastes back the address the browser was sent to (the page itself will fail to load,
which is expected). Files are stored with mode 0600 under ``<data_dir>/google``. Nothing here
prints key material or tokens.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import Settings, save_config, write_private
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


def import_client_secret(settings: Settings, payload: str) -> None:
    try:
        value = json.loads(payload)
    except ValueError:
        raise ValueError("the OAuth client file is not valid JSON") from None
    if not isinstance(value, dict) or not value.get("installed", {}).get("client_id"):
        raise ValueError(
            'this is not an OAuth client of type "Desktop app" (expected an "installed" key)'
        )
    write_private(settings.client_secret_file, json.dumps(value))


def parse_redirect(answer: str, expected_state: str) -> str:
    """Return the authorization code from a pasted redirect URL (or a bare code)."""
    answer = answer.strip()
    if not answer:
        raise ValueError("nothing was pasted")
    if "://" not in answer and "code=" not in answer:
        return answer
    query = parse_qs(urlparse(answer).query if "://" in answer else answer.lstrip("?"))
    if "error" in query:
        raise ValueError(f"Google returned an error: {query['error'][0]}")
    state = query.get("state", [""])[0]
    if expected_state and state != expected_state:
        raise ValueError("the pasted address belongs to a different sign-in attempt")
    code = query.get("code", [""])[0]
    if not code:
        raise ValueError("the pasted address does not contain a code")
    return code


def oauth_sign_in(
    settings: Settings,
    ask: Callable[[str], str],
    say: Callable[[str], None],
) -> dict[str, Any]:  # pragma: no cover - needs a real Google consent screen
    from google_auth_oauthlib.flow import Flow

    if not settings.client_secret_file.is_file():
        raise ValueError("OAuth client file is missing; import it first")
    flow = Flow.from_client_secrets_file(
        str(settings.client_secret_file), scopes=SCOPES, redirect_uri=REDIRECT_URI
    )
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    say("1. Mở địa chỉ sau trên máy tính của bạn và đăng nhập Google:")
    say("")
    say(url)
    say("")
    say("2. Sau khi bấm Cho phép (Allow), trình duyệt sẽ báo không mở được trang localhost.")
    say("   Đó là bình thường. Hãy sao chép TOÀN BỘ địa chỉ trên thanh địa chỉ và dán vào đây.")
    code = parse_redirect(ask("Địa chỉ đã dán: "), str(state))
    flow.fetch_token(code=code)
    credentials = flow.credentials
    if not credentials.refresh_token:
        raise ValueError("Google did not return a refresh token; try again")
    write_private(settings.token_file, credentials.to_json())
    save_config(settings.data_dir, {"google_auth": "oauth"})
    return {"method": "oauth", "token_file": str(settings.token_file)}
