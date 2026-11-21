"""Admin CLI: ``gdrive-rag setup | auth google/remove | embedding set/remove |
source add/list/remove | sync | status | search | serve``.

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

from .config import DEFAULT_BASE_URL, DEFAULT_MODEL, load_settings


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
    service = _service(args)
    status = service.status()
    if args.skipped:
        status["skipped_files"] = service.skipped_files()["files"]
    _print(status, args.json)
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


def cmd_read(args: argparse.Namespace) -> int:
    result = _service(args).read(
        args.document, args.start_chunk, args.max_chunks, args.page, source=args.source
    )
    _print(result, args.json)
    return 0


def cmd_source_add(args: argparse.Namespace) -> int:
    # the same rules as the add_source tool: a Drive link only when Google confirms it, a local
    # path only when it exists inside the local folder. No sync is queued in this process; the
    # server reads the source at its next scheduled sync (or run `gdrive-rag sync`).
    from .admin import AdminRefused

    admin = _admin(args)
    results: list[dict[str, Any]] = []
    for link in args.links:
        try:
            results.append({"link": link, "ok": True, **admin.add_source(link, queue=False)})
        except AdminRefused as exc:
            results.append({"link": link, "ok": False, "error": str(exc)})
    _print(results, args.json)
    return 0 if all(r.get("ok") for r in results) else 2


def cmd_source_list(args: argparse.Namespace) -> int:
    _print(_service(args).list_sources(), args.json)
    return 0


def cmd_source_remove(args: argparse.Namespace) -> int:
    # the same rules as the remove_source tool (GDRAG_DRIVE_LINKS, running sync)
    _print(_admin(args).remove_source(args.source, True), args.json)
    return 0


def cmd_source_rules(args: argparse.Namespace) -> int:
    # the same rules and validation as the set_path_rules tool; without options it only shows.
    # No sync is queued in this process (like source add): the server applies an ignore change
    # at its next scheduled sync, or run `gdrive-rag sync`.
    change = any(v is not None for v in (args.demote, args.exclude, args.ignore))
    result = _admin(args).set_path_rules(
        args.source, args.demote, args.exclude, change, ignore=args.ignore, queue=False
    )
    _print(result, args.json)
    return 0


def cmd_auth_google(args: argparse.Namespace) -> int:
    from .admin import require_google_unlocked
    from .auth import import_client_secret, import_service_account, oauth_sign_in

    require_google_unlocked()  # the same precedence rule as the admin tools
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


def _admin(args: argparse.Namespace) -> Any:
    from .admin import Admin

    return Admin(_service(args))


def cmd_auth_remove(args: argparse.Namespace) -> int:
    _print(_admin(args).remove_google(args.yes), args.json)
    return 0


def cmd_embedding_set(args: argparse.Namespace) -> int:
    key = _read_payload(args.api_key_file).strip() if args.api_key_file else None
    result = _admin(args).set_embedding(
        args.base_url, args.model, api_key=key, dimensions=args.dimensions, confirm=args.yes
    )
    _print(result, args.json)
    return 0


def cmd_config_import_env(args: argparse.Namespace) -> int:
    from .admin import import_environment

    _print(import_environment(_service(args)), args.json)
    return 0


def cmd_embedding_remove(args: argparse.Namespace) -> int:
    _print(_admin(args).remove_embedding(args.yes), args.json)
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

    # Embeddings go through Admin.set_embedding, the same rules as the tool: the environment
    # wins, a new address needs a new key, and the address checks apply. Values that come from
    # the environment are not copied to config.json (use `config import-env` for that).
    if interactive:
        print("== 1/3 Tìm kiếm theo ý nghĩa (embedding). Nhấn Enter để bỏ qua. ==")
        new_key = _ask("API key mới (Enter = giữ khóa hiện có)", "", secret=True)
        base_url = _ask("Địa chỉ API", current.embedding_base_url or DEFAULT_BASE_URL)
        model = _ask("Model", current.embedding_model or DEFAULT_MODEL)
        changed = (
            new_key
            or base_url.rstrip("/") != current.embedding_base_url
            or model != current.embedding_model
        )
        if changed and (new_key or current.embedding_api_key):
            _admin(args).set_embedding(base_url, model, api_key=new_key or None, confirm=True)
    settings = load_settings(args.data_dir)
    report["embeddings"] = "on" if settings.embeddings_enabled else "off (full-text search only)"

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
            from .admin import require_google_unlocked

            require_google_unlocked()  # same rule as the tools: .env values win
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

    status = commands.add_parser("status", help="show index and sync state")
    status.add_argument(
        "--skipped", action="store_true", help="list files skipped as unsupported (first 100)"
    )
    status.set_defaults(func=cmd_status)
    commands.add_parser("sync", help="sync all sources now").set_defaults(func=cmd_sync)

    search = commands.add_parser("search", help="search the index (for manual checks)")
    search.add_argument("query")
    search.add_argument("--source", help="source id, name or link")
    search.add_argument("--folder", help="folder link or ID, or local:<path> (includes subfolders)")
    search.add_argument(
        "--file", action="append", help="file link or ID, or local:<path> (repeat for several)"
    )
    search.add_argument("--limit", type=int, default=8)
    search.add_argument(
        "--explain",
        action="store_true",
        help="show each hit's full-text and vector rank/score and the fused score",
    )
    search.set_defaults(func=cmd_search)

    read = commands.add_parser("read", help="read one indexed document (for manual checks)")
    read.add_argument("document", help="document_id from a search result, or a Drive file link")
    read.add_argument(
        "--source",
        help="source id, name or link; the document must be in it (as search --source)",
    )
    read.add_argument("--start-chunk", type=int, default=0)
    read.add_argument("--max-chunks", type=int, default=10)
    read.add_argument("--page", type=int, help="start at this PDF page instead")
    read.set_defaults(func=cmd_read)

    source = commands.add_parser(
        "source", help="manage sources (Drive links and local folders)"
    ).add_subparsers(dest="source_command", required=True)
    add = source.add_parser(
        "add", help="add Drive links or local paths (local:<path> or /local/...)"
    )
    add.add_argument("links", nargs="+")
    add.set_defaults(func=cmd_source_add)
    source.add_parser("list", help="list sources").set_defaults(func=cmd_source_list)
    remove = source.add_parser("remove", help="remove a source (id, name or link)")
    remove.add_argument("source")
    remove.set_defaults(func=cmd_source_remove)
    rules = source.add_parser(
        "rules",
        help="show or set path rules of a source (demote / exclude / ignore)",
        description="Without --demote/--exclude/--ignore: show the rules and what they match. "
        "Each option replaces that list; give it with no patterns to clear it. "
        "--ignore keeps files out of the index (removed at the next sync; a folder link "
        "covers everything below it). "
        "Example: source rules 1 --demote 'attachments/**'",
    )
    rules.add_argument("source", help="source id, name or link")
    rules.add_argument("--demote", nargs="*", metavar="PATTERN", help="files that rank lower")
    rules.add_argument("--exclude", nargs="*", metavar="PATTERN", help="files left out of search")
    rules.add_argument(
        "--ignore",
        nargs="*",
        metavar="ENTRY",
        help="patterns, or Drive folder/file links or IDs, whose files are not indexed at all",
    )
    rules.set_defaults(func=cmd_source_rules)

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
    remove_auth = auth.add_parser("remove", help="delete stored Google credentials")
    remove_auth.add_argument("--yes", action="store_true", help="apply (otherwise preview)")
    remove_auth.set_defaults(func=cmd_auth_remove)

    embedding = commands.add_parser("embedding", help="semantic-search endpoint").add_subparsers(
        dest="embedding_command", required=True
    )
    set_embedding = embedding.add_parser("set", help="set the embedding API (preview first)")
    set_embedding.add_argument("--base-url", help="omit to keep the current address")
    set_embedding.add_argument("--model", help="omit to keep the current model")
    set_embedding.add_argument("--api-key-file", metavar="FILE", help="key file, or - for stdin")
    set_embedding.add_argument("--dimensions", type=int, default=None)
    set_embedding.add_argument("--yes", action="store_true", help="apply (otherwise preview)")
    set_embedding.set_defaults(func=cmd_embedding_set)
    remove_embedding = embedding.add_parser("remove", help="back to full-text search only")
    remove_embedding.add_argument("--yes", action="store_true", help="apply (otherwise preview)")
    remove_embedding.set_defaults(func=cmd_embedding_remove)

    config = commands.add_parser("config", help="stored settings").add_subparsers(
        dest="config_command", required=True
    )
    config.add_parser(
        "import-env",
        help="store the GDRAG_* settings from the environment and list the .env lines to empty",
    ).set_defaults(func=cmd_config_import_env)
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
