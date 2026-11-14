# CLAUDE.md

google-drive-rag is a single-process RAG service over Google Drive: an MCP server (Streamable HTTP at `/mcp`, plus `/healthz`) and a background sync loop, packaged as one Docker container. The owner is not a programmer and reads Vietnamese; `README.md` is their install guide and must stay in Vietnamese.

## Commands

Python 3.12 or 3.13, editable install in `.venv`:

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

Run all four before calling a change done:

```bash
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/pytest
```

- One test: `.venv/bin/pytest tests/test_sync.py -k overlapping`.
- Manual search: `.venv/bin/gdrive-rag --data-dir /tmp/gdr search "query" --folder <link> --file <link>`.
- Real OCR test (skipped by default): `python -m gdrive_rag.ocr_runtime /tmp/ocr`, then `GDRAG_TEST_OCR_DIR=/tmp/ocr .venv/bin/pytest -k real_ocr`.
- `mypy` is strict over `gdrive_rag`; ruff line length is 100.
- Run the server locally against a throwaway data dir, never a real one: `.venv/bin/gdrive-rag --data-dir /tmp/gdr serve --port 18765`, then `curl http://127.0.0.1:18765/healthz`.
- Docker: `docker compose up -d --build`; the CLI runs inside the container as `docker compose exec gdrive-rag gdrive-rag <command>`.

## Architecture

All code is in `src/gdrive_rag`. Console script `gdrive-rag` → `gdrive_rag.cli:entrypoint`.

```text
cli.py / server.py  →  service.Service  →  drive.py (DriveBackend protocol, GoogleDriveBackend)
                                        →  extract.py (+ formats.py, xlsx_compat.py, ocr_runtime.py)
                                        →  chunking.py → db.py (SQLite WAL, FTS5, vectors)
                                        →  embeddings.py (OpenAI-compatible client)
                       search.SearchEngine reads the same database
```

- `service.Service` is the one facade for CLI and MCP. It takes factories (`backend_factory`, `embedding_factory`, `extractor_factory`); tests inject fakes through them (`tests/helpers.py`: `FakeDrive`, `FakeEmbeddings`, `EmbeddingStub`, `make_service`; `FakeDrive.add_folder` and `put(parent=...)` build a folder tree whose paths follow folder renames and moves). `Service.reload()` re-reads settings, so a key added through the CLI takes effect on the next sync or search.
- Sync is a full listing per source compared with `DriveItem.fingerprint` (checksum or modifiedTime, size, MIME type; no name or path); there is no `changes.list`. A rename or move only updates `documents` and the FTS title (`Service._update_metadata`). `chunk_hash` is the hash of the chunk text alone, and embeddings get the text without a file-name prefix, so vectors survive renames. Documents are keyed by Drive file ID with a `document_sources` membership table, so overlapping sources share one copy.
- `GoogleDriveBackend.list_files` re-resolves the root every cycle, because `files.list` on a folder we lost access to returns an empty page instead of 404. `tests/test_drive_backend.py` drives it through a stub `service` with real `HttpError`s.
- Google Docs are exported as Markdown; `strip_data_images` removes embedded base64 images first. Drive shortcuts are skipped.
- Deletion safety: membership is replaced only from a complete listing, or cleared when the source root raises `DriveItemError` (404 / not shared). `DriveSourceError` (auth, quota, network) and incomplete listings never delete. Keep this split when touching `drive.py` or `Service._sync`.
- Download and export errors are per file: `GoogleDriveBackend.download` raises `DriveDownloadError` (stored with the `download: ` prefix and retried on every sync); only HTTP 401 and connection errors raise `DriveSourceError`, which stops the pass and skips deletions. Docs and Slides over the 10 MB export limit fall back to a `text/plain` export (`Download.suffix` becomes `.txt`).
- A sync holds an `flock` on `<data_dir>/sync.lock` and writes its PID to `sync.running`; `sync_running()` reads only the PID file. A second sync raises `SyncBusy`.
- Extraction failures are per file (stored as `status='error'`, retried after 24 hours or when the file changes). Missing LibreOffice raises `ExtractionError("converter_missing")`; missing OCR libraries or model files set `metadata.ocr_unavailable`. Neither may become a service error.
- Embeddings: vectors are truncated to `embedding_dimensions` (1024) and re-normalized client-side; no `dimensions` field is sent. Only queries get the `Instruct: ...\nQuery:` prefix. `meta.embedding_identity` records endpoint, model and dims; when it changes, `embed_pending` deletes all vectors and re-embeds, and search stays FTS-only until then. A chunk the endpoint rejects (`EmbeddingError.rejected`: HTTP 400/413/422) is isolated by splitting its batch and stored in `embedding_failures`; other embedding errors stop the pass.
- FTS rows follow `chunks` through triggers; `fts_fold` (NFC + đ→d) plus `remove_diacritics 2` makes Vietnamese search accent-insensitive.
- Folder ancestry (migration 3): `folders` (Drive ID, name, parent ID, display path), `folder_sources` membership (same replace/keep rules as `document_sources`), `documents.parent_id`, and `document_folders` holding every ancestor folder of each document. `_refresh_ancestry` recomputes paths and `document_folders` from parent IDs at the end of each sync and on `source remove`, so a folder rename or move never re-extracts or re-embeds. When two listings report one folder, the non-empty parent wins; the stored parent is kept only when a source that held the folder failed or was incomplete, or the pass stopped. A pass stopped by `DriveSourceError` replaces neither `document_sources` nor `folder_sources`. A hit's `parent_folder_id` is null when that folder is not indexed. A file-kind source's parent folder is not in `folders`, so it is not listable.
- Search scope (`SearchEngine._scope`): `source`, `folder` (that folder and all descendants, via `document_folders`) and `files` combine as SQL conditions applied to both the FTS query and the allowed vector chunks before fusion. An unknown source, folder or file raises `ValueError` naming it. The 3-hits-per-document cap is soft: remaining slots are filled from the same files. Folder and file values go through `parse_drive_link`, so links and bare IDs both work; `read` accepts them too.
- MCP tools (`server.py`) are read-only: `search`, `read`, `list_sources`, `list_folders`, `sync_status`. `list_folders` pages folders then documents as one sequence (`offset` / `next_offset`, 100 per page, then the wire cap). Each returns `Annotated[CallToolResult, <Model>]` with the same JSON as text and structured content, trimmed until `wire_size` (both copies, after JSON escaping) is at most `MAX_WIRE_CHARS` (56,000), so a response stays under about 60,000 characters. Do not add admin tools over MCP; admin goes through the CLI.
- The server is stateless JSON-response Streamable HTTP with DNS-rebinding protection allowing only `127.0.0.1`, `localhost` and `[::1]` hosts (extend with `GDRAG_ALLOWED_HOSTS`). In Docker it listens on `0.0.0.0`; compose publishes only `127.0.0.1:8765`.

## Config and secrets

- Settings come from `<data_dir>/config.json` (mode 0600), overridden by `GDRAG_*` variables (`config.ENV_FIELDS`). Data dir: `--data-dir`, `GDRAG_DATA_DIR`, else `~/.gdrive-rag`; `/data` in Docker.
- Google files live in `<data_dir>/google/` with mode 0600, written by `config.write_private`. OAuth uses a paste-back flow (`auth.oauth_sign_in`).
- Never print, log or commit API keys, tokens, or credential file contents; `Settings.public()` is the only settings view for output. Tests use fakes only, never real Google or OpenRouter.

## Origin

Several modules are copied or adapted from local-rag-mcp (Apache-2.0); `NOTICE` lists them. Update `NOTICE` when copying more code.
