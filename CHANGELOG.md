# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/). The install guide for the owner is `README.md` (Vietnamese).

## [0.3.0] - 2026-11-15

Tóm tắt: thêm bộ công cụ quản trị từ Claude (tắt sẵn, bật bằng `GDRAG_ADMIN_TOOLS=1`), quy tắc hạ thấp hoặc ẩn tệp theo đường dẫn khi tìm, tiến độ đọc Drive trực tiếp trong `sync_status`, danh sách tệp bị bỏ qua, và tìm được ngay trong lúc nguồn mới đang được đọc lần đầu. Cập nhật không đọc lại tệp nào và không tính lại dữ liệu ý nghĩa.

### Added

- Admin MCP tools, off unless `GDRAG_ADMIN_TOOLS=1` is set in the environment: `add_source`, `remove_source`, `set_path_rules`, `google_auth_status`, `set_google_service_account`, `google_auth_start`, `google_auth_finish`, `remove_google_auth`, `set_embedding`, `remove_embedding`. Destructive ones preview first and apply only with `confirm: true`. File arguments must be names inside `GDRAG_SECRETS_DIR` (`/secrets` in Docker). Results, errors and logs never contain keys or tokens.
- CLI commands sharing the same rules: `source rules`, `auth remove`, `embedding set` / `embedding remove`, `config import-env`, `status --skipped`.
- Per-source search path rules: `demote` multiplies a matching file's fused score by 0.5, and `exclude` leaves the file out of search results. The file stays indexed and readable by ID, and a file named in `files` is never excluded. Patterns are globs over the result `path` (`*`, `?`, `**`; an unanchored pattern matches at any depth), compared without case or accents. Set them with the `set_path_rules` tool or `gdrive-rag source rules`. `list_sources` shows them as `path_rules`, and `search --explain` shows `path_rule` and `fused_before_rules`. Rules are applied at query time only. With no rules, ranking is unchanged.
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
