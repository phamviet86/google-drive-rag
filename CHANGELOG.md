# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/). The install guide for the owner is `README.md` (Vietnamese).

## [Unreleased]

Tóm tắt: chỉ sửa tài liệu, không đổi cách dịch vụ chạy. README giờ hướng dẫn kết nối cho Claude Code, Codex và Antigravity, và lời văn không còn gắn riêng với Claude.

### Added

- `read` (MCP tool, `Service.read`) takes an optional `source` (id, name or link, as in `search`). The document must belong to that source, otherwise it is refused with `document <id> is not in source <source>` (the same words for an unknown or ignored document, so nothing leaks about IDs outside the source), so a client confined to one source cannot read a document that left it but is still indexed through another. Without `source` nothing changes. New CLI command `gdrive-rag read <id> [--source S]`.

### Changed

- Docs: setup for three MCP clients (Claude Code, OpenAI Codex, Google Antigravity) in README "Kết nối với agent", including the SSH tunnel for an agent on another computer; README, package description, `.env.example` and `docker-compose.yml` comments are agent-neutral. No behaviour change.

## [0.4.0] - 2026-11-19

Tóm tắt: thêm nguồn là thư mục trên chính VPS (`local:`), thêm quy tắc `ignore` để loại thư mục hoặc tệp khỏi chỉ mục, ba công cụ nguồn (`add_source`, `remove_source`, `set_path_rules`) luôn có không cần `GDRAG_ADMIN_TOOLS`, và sửa các lỗi an toàn của bản 0.3.0. Cập nhật thêm migration 5 mà không đọc lại tệp nào và không tính lại dữ liệu ý nghĩa.

### Added

- Local folder sources: a folder of the server, mounted read-only as `/local` (`GDRAG_LOCAL_HOST_DIR` in `.env`, default `./local` next to `docker-compose.yml`; the container reads it through the env-only `GDRAG_LOCAL_ROOT`, set to `/local` by the Dockerfile).
  - Add with `add_source local:<path inside it>` (an absolute path inside the root also works) or `gdrive-rag source add local:<path>`. IDs are `local:<relative path>` and the root is `local:.`; reading, scopes and `list_folders` work as for Drive.
  - Names starting with `.` and `~$` lock files are ignored. Symlinks are followed inside the root; one that leaves the root, points to a folder, a hidden file or a file the same source already lists, or is broken, is listed as skipped with a reason.
  - The service's data folder and secrets folder are never read, even when they sit inside the mounted folder, and a file that looks like a private key or a Google token is refused at download. Names are matched in Unicode NFC, so Vietnamese names copied from a Mac are found as typed.
  - A source folder that suddenly lists nothing while it holds documents (an unplugged disk) deletes nothing.
  - A missing, unreadable or empty root, or a mount that disappears mid-sync, deletes nothing. A source path that is gone while the root is fine clears that source.
  - Each source uses the backend of its kind, so a Google or a local failure stops only its own side. `GDRAG_DRIVE_LINKS` may hold `local:` entries. `sync_status.local_folder` shows `configured`, `ok` and `error`.
  - A renamed file is read again but, with the same chunk text, not embedded again (a scanned PDF is OCR'd again). Files must be readable by uid 10001 (mode 644, folders 755).
- Path rule effect `ignore`: keeps files and folders out of the index. An entry is a glob, a Drive link, `id:<ID>`, `local:<path>` or a bare ID that the source already knows (any other bare value is a glob, and the preview warns about entries that match nothing); folder entries follow renames and moves. After listing and before any download, matching items are dropped and recorded as skipped (`ignored by rule: <entry>`); the next complete listing removes what was indexed, and an incomplete one or a `DriveSourceError` deletes nothing. A file ignored by any source is dropped from every source that lists it. Search and `list_folders` hide ignored items at once and `read` refuses them; a file moved out of an ignored folder is searchable again even if later listings are incomplete. The preview counts documents and folders per entry and refuses entries that would empty a source. Changing `ignore` queues a full sync; `demote` and `exclude` changes queue nothing. CLI: `source rules --ignore` (applied at the server's next sync, or run `gdrive-rag sync`).

### Changed

- `add_source`, `remove_source` and `set_path_rules` are always registered; the other seven admin tools still need `GDRAG_ADMIN_TOOLS=1`. `set_path_rules` is annotated destructive, because `ignore` removes indexed documents.
- `gdrive-rag source add` no longer stores a Drive link that Google has not confirmed; the server reads it at the next scheduled sync, or run `gdrive-rag sync`.
- MCP `set_embedding`: `base_url` and `model` are optional and keep the current value, as in the CLI.
- Demote with a full candidate pool: when demoted chunks fill the first text or vector candidates, text search runs again without them and vector candidates come from one cosine order, so other documents are not pushed out of the list.
- A demoted hit's score is capped just below the hit it was placed under. When path rules exist, hits beyond the 3-per-file cap come after all capped hits, so scores never increase down the list and a smaller `limit` is a prefix of a larger one. Without rules the order is unchanged.

### Fixed

- Shared address space (`100.64.0.0/10`: CGNAT, Tailscale, the Alibaba metadata address) is no longer treated as public; an embedding endpoint there needs `GDRAG_ALLOW_LOCAL_EMBEDDINGS=1`. A destination is public only if it is globally routable.
- Every read-merge-write of `config.json` runs under a file lock (`<data_dir>/.config.lock`), so the CLI and the server cannot overwrite each other's changes.
- Pattern `**??` and similar names that match every path are refused as match-all patterns.

### Security

- A stored embedding key is bound to the origin (scheme, host, port) it was set for (`embedding_key_origin`). If the address later has another origin, the key is not used: search is full-text only, and `sync_status` and `status` show a warning. `GDRAG_EMBEDDING_API_KEY` is not bound. A key stored by an earlier version binds to the address in effect at first load, once; `set_embedding` without a new key is refused while a stored key is unbound.
- Schema migration 5 rebuilds `path_rules` to allow `ignore`; every row is kept, and no document, chunk or vector row is touched.

## [0.3.0] - 2026-11-15

Tóm tắt: thêm bộ công cụ quản trị từ Claude (tắt sẵn, bật bằng `GDRAG_ADMIN_TOOLS=1`), quy tắc hạ thấp hoặc ẩn tệp theo đường dẫn khi tìm, tiến độ đọc Drive trực tiếp trong `sync_status`, danh sách tệp bị bỏ qua, và tìm được ngay trong lúc nguồn mới đang được đọc lần đầu. Cập nhật không đọc lại tệp nào và không tính lại dữ liệu ý nghĩa.

### Added

- Admin MCP tools, off unless `GDRAG_ADMIN_TOOLS=1` is set in the environment: `add_source`, `remove_source`, `set_path_rules`, `google_auth_status`, `set_google_service_account`, `google_auth_start`, `google_auth_finish`, `remove_google_auth`, `set_embedding`, `remove_embedding`. Destructive ones preview first and apply only with `confirm: true`. File arguments must be names inside `GDRAG_SECRETS_DIR` (`/secrets` in Docker). Results, errors and logs never contain keys or tokens.
- CLI commands sharing the same rules: `source rules`, `auth remove`, `embedding set` / `embedding remove`, `config import-env`, `status --skipped`.
- Per-source search path rules: `demote` moves every hit of a matching file below the best hit of the next 10 other files, and `exclude` leaves the file out of search results. Demoted scores are lowered to just below the hit above them, so scores stay in order. The file stays indexed and readable by ID, and a file named in `files` is never excluded. Patterns are globs over the result `path` (`*`, `?`, `**`; an unanchored pattern matches at any depth), compared without case or accents. Each source holds at most 20 patterns per effect, each at most 100 characters. Matching runs in linear time. A pattern that matches every path (`*`, `**/*`) is refused, and so are exclude patterns that would hide every document of the source. A document held by two overlapping sources has one stored path, from the source that listed it first: an anchored pattern of the other source may not match it, and either source's exclude hides it. Set them with the `set_path_rules` tool or `gdrive-rag source rules`. `list_sources` shows them as `path_rules`, and `search --explain` shows `path_rule` and `fused_before_rules`. Rules are applied at query time only. With no rules, ranking is unchanged.
- `sync_status.sync.progress` while a sync runs (also one started from the CLI):
  - `phase`: `listing`, `extracting` or `embedding`;
  - per-source `files_done` of `files_total`;
  - `embedding.done` of `embedding.pending`.

  `last_report` is unchanged.
- Skipped files: files listed in Drive that no extractor handles are recorded per source with path, MIME type, size and reason. Examples are archives, unknown binary types, Drive shortcuts and Google Forms. `sync_status.skipped.total` and `list_sources[].skipped` give counts. `sync_status(include_skipped=true)` and `status --skipped` list the first 100.
- First-sync visibility:
  - A folder source's root folder is known as soon as the source is added.
  - Subfolders are known once listed.
  - Files are linked to their source and folders as each one is indexed during the first sync.
  - A search scoped to such a source or folder returns what is indexed so far, plus a warning such as `source 'X' is still on its first sync (N of M files indexed)`. Before, it returned an error or a silent empty result.
- `list_sources[].first_sync_done`.
- Hardened embedding endpoint checks:
  - Only https, except a literal local address with `GDRAG_ALLOW_LOCAL_EMBEDDINGS=1`.
  - Every resolved address must be public; the check runs when the endpoint is set and on every request.
  - Requests are pinned to the checked IP and never follow redirects.
- Container memory limit `GDRAG_MEMORY_LIMIT` (default `8g`).

### Changed

- Schema migration 4 is additive: `sources.first_synced_at`, `path_rules` and `skipped_files`. Sources that already synced are marked first-synced. No document, chunk or vector row is touched.
- `gdrive-rag setup --yes` no longer copies `.env` values into `config.json`; use `gdrive-rag config import-env`.
- A source listed in `GDRAG_DRIVE_LINKS` cannot be removed with `source remove`, because it would come back at the next start.

## [0.2.0] - 2026-11-14

### Added

- MCP tool `sync_now` for an on-demand background sync, limited to one request per minute.

### Changed

- Ranking: weighted reciprocal-rank fusion of full-text, vector, title and phrase signals, tuned on real data.

## [0.1.1] - 2026-11-14

### Changed

- OCR uses Tesseract 5 with Vietnamese and English data.
- CI covers lint, types and tests on Python 3.12 and 3.13, plus an x86_64 Docker build.

## [0.1.0] - 2026-11-14

- First release: an MCP server and a background sync over Google Drive, with hybrid search, folder and file scopes, and Docker packaging.
