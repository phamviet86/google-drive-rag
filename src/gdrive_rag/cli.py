"""Admin CLI: ``gdrive-rag setup | auth google | source add/list/remove | sync | status | search |
serve``.

Output is plain readable text, or JSON with ``--json``. Secrets are never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .config import DEFAULT_BASE_URL, DEFAULT_MODEL, load_settings, save_config


def _service(args: argparse.Namespace) -> Any:
    from .service import Service

    return Service(lambda: load_settings(args.data_dir))


def _read_payload(value: str) -> str:
    if value == "-":
        return sys.stdin.read()
    return Path(value).expanduser().read_text(encoding="utf-8")


def _print(value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    else:
        _plain(value, 0)


def _plain(value: Any, indent: int) -> None:
    pad = "  " * indent
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, dict | list) and item:
                print(f"{pad}{key}:")
                _plain(item, indent + 1)
            else:
                print(f"{pad}{key}: {_scalar(item)}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                print(f"{pad}-")
                _plain(item, indent + 1)
            else:
                print(f"{pad}- {_scalar(item)}")
    else:
        print(f"{pad}{_scalar(value)}")


def _scalar(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, list | dict):
        return "(none)"
    return str(value)


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    serve(args.data_dir, args.host, args.port)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    _print(_service(args).status(), args.json)
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    from .service import SyncBusy

    try:
        report = _service(args).sync()
    except SyncBusy:
        _print(
            {"ok": False, "error": "a sync is already running (the server syncs on its own)"},
            args.json,
        )
        return 2
    _print(report, args.json)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    result = _service(args).search(
        args.query, args.source, args.limit, args.folder, args.file or None, args.explain
    )
    _print(result, args.json)
    return 0


def cmd_source_add(args: argparse.Namespace) -> int:
    results = _service(args).add_sources(args.links)
    _print(results, args.json)
    return 0 if all(r.get("ok") for r in results) else 2


def cmd_source_list(args: argparse.Namespace) -> int:
    _print(_service(args).list_sources(), args.json)
    return 0


def cmd_source_remove(args: argparse.Namespace) -> int:
    _print(_service(args).remove_source(args.source), args.json)
    return 0


def cmd_auth_google(args: argparse.Namespace) -> int:
    from .auth import import_client_secret, import_service_account, oauth_sign_in

    settings = load_settings(args.data_dir)
    if args.service_account:
        result = import_service_account(settings, _read_payload(args.service_account))
        result["next_step"] = (
            f"Share each Drive folder with {result['client_email']} (Viewer is enough)."
        )
        _print(result, args.json)
        return 0
    if args.oauth_client:
        import_client_secret(settings, _read_payload(args.oauth_client))
        if args.oauth_client == "-":
            _print(
                {"ok": True, "next_step": "run again without --oauth-client to sign in"}, args.json
            )
            return 0
    _print(oauth_sign_in(settings, input, print), args.json)
    return 0


def _ask(prompt: str, default: str = "", secret: bool = False) -> str:
    shown = " [đã có, Enter để giữ]" if secret and default else (f" [{default}]" if default else "")
    if secret:
        import getpass

        answer = getpass.getpass(f"{prompt}{shown}: ")
    else:
        answer = input(f"{prompt}{shown}: ")
    return answer.strip() or default


def cmd_setup(args: argparse.Namespace) -> int:
    """Ask for the three inputs: embedding API, Google access, Drive links."""
    current = load_settings(args.data_dir)
    interactive = sys.stdin.isatty() and not args.yes
    env = os.environ
    report: dict[str, Any] = {}

    base_url = env.get("GDRAG_EMBEDDING_BASE_URL") or current.embedding_base_url or DEFAULT_BASE_URL
    model = env.get("GDRAG_EMBEDDING_MODEL") or current.embedding_model or DEFAULT_MODEL
    key = env.get("GDRAG_EMBEDDING_API_KEY") or current.embedding_api_key
    if interactive:
        print("== 1/3 Tìm kiếm theo ý nghĩa (embedding). Nhấn Enter để bỏ qua. ==")
        key = _ask("API key (OpenRouter)", key, secret=True)
        if key:
            base_url = _ask("Địa chỉ API", base_url)
            model = _ask("Model", model)
    save_config(
        current.data_dir,
        {"embedding_base_url": base_url, "embedding_model": model, "embedding_api_key": key},
    )
    report["embeddings"] = "on" if key else "off (full-text search only)"

    settings = load_settings(args.data_dir)
    method = env.get("GDRAG_GOOGLE_AUTH", "").strip().lower().replace("-", "_")
    path = env.get("GDRAG_GOOGLE_CREDENTIALS", "").strip()
    if interactive:
        print("== 2/3 Quyền đọc Google Drive ==")
        print("  1 = service account (khuyên dùng trên VPS)")
        print("  2 = đăng nhập Gmail (OAuth)")
        print("  Enter = bỏ qua")
        choice = _ask("Chọn", {"service_account": "1", "oauth": "2"}.get(method, ""))
        method = {"1": "service_account", "2": "oauth"}.get(choice, "")
        if method:
            path = _ask("Đường dẫn tới file JSON", path)
    if method and path:
        from .auth import import_client_secret, import_service_account, oauth_sign_in

        payload = Path(path).expanduser().read_text(encoding="utf-8")
        if method == "service_account":
            result = import_service_account(settings, payload)
            report["google"] = f"service account {result['client_email']}"
            report["share_folders_with"] = result["client_email"]
        else:
            import_client_secret(settings, payload)
            if interactive:
                oauth_sign_in(settings, input, print)
                report["google"] = "signed in with OAuth"
            else:
                report["google"] = "OAuth client saved; run: gdrive-rag auth google"
    else:
        report["google"] = settings.google_auth if settings.google_auth != "none" else "not set"

    links = env.get("GDRAG_DRIVE_LINKS", "").replace(",", " ").split()
    if interactive:
        print("== 3/3 Link thư mục/tệp Google Drive (mỗi dòng một link, dòng trống để kết thúc) ==")
        while True:
            line = input("Link: ").strip()
            if not line:
                break
            links.append(line)
    if links:
        report["sources"] = [
            {"link": r["link"], "ok": r.get("ok"), "name": r.get("name"), "error": r.get("error")}
            for r in _service(args).add_sources(links)
        ]
    _print(report, args.json)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gdrive-rag", description=__doc__)
    parser.add_argument("--data-dir", default=None, help="data directory (env GDRAG_DATA_DIR)")
    parser.add_argument("--json", action="store_true", help="print JSON")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="run the MCP server and the sync loop")
    serve.add_argument("--host", default=os.environ.get("GDRAG_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("GDRAG_PORT", "8765")))
    serve.set_defaults(func=cmd_serve)

    setup = commands.add_parser("setup", help="ask for embedding key, Google access, Drive links")
    setup.add_argument("--yes", action="store_true", help="do not ask; use environment values")
    setup.set_defaults(func=cmd_setup)

    commands.add_parser("status", help="show index and sync state").set_defaults(func=cmd_status)
    commands.add_parser("sync", help="sync all sources now").set_defaults(func=cmd_sync)

    search = commands.add_parser("search", help="search the index (for manual checks)")
    search.add_argument("query")
    search.add_argument("--source", help="source id, name or link")
    search.add_argument("--folder", help="Drive folder link or ID (includes subfolders)")
    search.add_argument(
        "--file", action="append", help="Drive file link or ID (repeat for several files)"
    )
    search.add_argument("--limit", type=int, default=8)
    search.add_argument(
        "--explain",
        action="store_true",
        help="show each hit's full-text and vector rank/score and the fused score",
    )
    search.set_defaults(func=cmd_search)

    source = commands.add_parser("source", help="manage Drive sources").add_subparsers(
        dest="source_command", required=True
    )
    add = source.add_parser("add", help="add one or more Drive links")
    add.add_argument("links", nargs="+")
    add.set_defaults(func=cmd_source_add)
    source.add_parser("list", help="list sources").set_defaults(func=cmd_source_list)
    remove = source.add_parser("remove", help="remove a source (id, name or link)")
    remove.add_argument("source")
    remove.set_defaults(func=cmd_source_remove)

    auth = commands.add_parser("auth", help="Google credentials").add_subparsers(
        dest="auth_command", required=True
    )
    google = auth.add_parser("google", help="import a service account or sign in with OAuth")
    group = google.add_mutually_exclusive_group()
    group.add_argument("--service-account", metavar="FILE", help="key JSON file, or - for stdin")
    group.add_argument(
        "--oauth-client", metavar="FILE", help="OAuth Desktop client JSON, or - for stdin"
    )
    google.set_defaults(func=cmd_auth_google)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args.func
    from .service import SyncBusy

    try:
        return handler(args)
    except SyncBusy:
        _print({"ok": False, "error": "a sync is running; try again in a few minutes"}, args.json)
        return 2
    except (ValueError, OSError) as exc:
        _print({"ok": False, "error": str(exc)}, args.json)
        return 2
    except KeyboardInterrupt:
        return 130


def entrypoint() -> None:
    raise SystemExit(main())
