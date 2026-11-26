# Baseline for tg-export-plus

Date: 2026-11-26

## Upstream revision

- Repository: `https://github.com/VitalyOstanin/tg-export.git`
- Branch: `master`
- Commit: `8d81bc6cf95be4f04f5daa44cb38a92ead32f61b`
- Commit date: 2026-11-21 12:33:22 +0300
- Local development branch: `feature/media-variants`
- Package version from `pyproject.toml`: `2.2.0`
- Dependency source: committed `uv.lock`; installation used `uv sync --frozen`
- Python: `3.12.14`; uv: `0.13.9`; Telethon from lock: `1.45.0`

## Baseline checks

| Check | Result |
| --- | --- |
| `uv run --frozen tg-export --help` | Passed |
| `uv run --frozen python -m pytest --cov-report=term` | 1011 passed; total coverage 88.79% |
| `scripts/check.sh` | Passed: `uv lock --check`, Ruff lint and format, Pyright (0 errors), 1011 tests, and coverage floors for 36 modules |

The commands were run on Linux with Python 3.12.14. Unit tests use mocks and do not prove Telegram connectivity or Takeout RPC behavior. No Telegram account was connected.

The first test run had one environment-related failure: the checkout gave `config.example.yaml` mode `0600`, while `test_the_example_config_keeps_the_permissions_of_a_published_file` expects a readable example. Changing only that file's local mode to `0644` made the full suite pass. The mode change does not appear in Git status. A file I/O test also hung inside the command sandbox; the same test passed outside it. The passing full suite and CI script were run outside that sandbox.

## Initial audit observations

- `TgApi.download_media` delegates to Telethon without a `thumb` argument. The downloader and broken-file recovery use this path.
- `exporter.py` tracks `full_history` for the history pass, while the existing `files` schema records one local file per `(file_id, chat_id)`.
- No use of `GetSplitRanges`, `InvokeWithMessagesRange`, or `InputTakeoutFileLocation` was found in `tg_export` at this revision.

These observations define the starting point for the media-variant and strict Takeout work in the technical design. They are not an implementation of those features.
