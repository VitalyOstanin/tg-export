"""Сквозные проверки команды run.

Основная команда инструмента раньше не вызывалась ни одним тестом: из всех
команд CLI через CliRunner прогонялись только --help, --version и tg messages.
Здесь run проходит целиком на подставных Telegram, экспортёре и рендерере, так
что проверяются именно решения самой команды: код возврата, режим Takeout,
поведение под --quiet.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

from tg_export.auth import AccountManager
from tg_export.cli import main
from tg_export.exporter import ExportStats


@pytest.fixture
def run_env(tmp_path, monkeypatch):
    """Окружение для run: аккаунт, конфиг, подставные Telegram и экспортёр."""
    import tg_export.cli as cli

    cfg_dir = tmp_path / "config"
    mgr = AccountManager(config_dir=cfg_dir)
    mgr.ensure_dirs()
    mgr.save_credentials(1, "hash")
    mgr.set_default_account("acc")
    mgr.session_path("acc").write_bytes(b"")
    out_dir = tmp_path / "out"
    (cfg_dir / "acc.yaml").write_text(
        "output:\n"
        f"  path: {out_dir}\n"
        "defaults:\n"
        "  media:\n"
        "    types: [photo]\n"
        "    max_file_size: 50MB\n"
        "    concurrent_downloads: 3\n"
    )
    monkeypatch.setattr(cli, "_mgr", lambda: AccountManager(config_dir=cfg_dir))

    api = MagicMock()
    api.connect = AsyncMock()
    api.disconnect = AsyncMock()
    api.start_takeout = AsyncMock()
    api.stop_takeout = AsyncMock()
    api.takeout = None
    monkeypatch.setattr("tg_export.api.TgApi", lambda *a, **k: api)
    monkeypatch.setattr("tg_export.catalog.fetch_catalog", AsyncMock(return_value=[]))
    monkeypatch.setattr("tg_export.html.renderer.HtmlRenderer", MagicMock())
    monkeypatch.setattr("tg_export.media.MediaDownloader", MagicMock())

    stats = ExportStats()
    exporter = MagicMock()
    exporter.run = AsyncMock(return_value=stats)
    exporter._force_shutdown = False
    exporter._shutdown = False
    exporter._shutdown_signal = None
    monkeypatch.setattr("tg_export.exporter.Exporter", lambda *a, **k: exporter)
    monkeypatch.setattr(cli, "_render_index", AsyncMock())

    return MagicMock(api=api, exporter=exporter, stats=stats, out_dir=out_dir, cfg_dir=cfg_dir)


def test_run_succeeds_and_reports_takeout(run_env):
    result = CliRunner().invoke(main, ["run"])
    assert result.exit_code == 0, result.output
    run_env.api.start_takeout.assert_awaited_once()
    assert "API: takeout" in result.output


def test_run_reports_errors_with_nonzero_code(run_env):
    run_env.stats.errors.append(("chat", "boom"))
    result = CliRunner().invoke(main, ["run"])
    assert result.exit_code == 1
    assert "Errors: 1" in result.output


def test_run_maps_signal_to_exit_code(run_env):
    import signal

    run_env.exporter._shutdown_signal = signal.SIGINT
    result = CliRunner().invoke(main, ["run"])
    assert result.exit_code == 128 + signal.SIGINT


def test_run_falls_back_to_regular_api_visibly(run_env):
    from telethon.errors import RPCError

    run_env.api.start_takeout = AsyncMock(side_effect=RPCError(request=None, message="denied"))
    result = CliRunner().invoke(main, ["run"])
    assert result.exit_code == 0
    assert "regular API" in result.output
    assert "API: regular (no takeout)" in result.output


def test_run_require_takeout_fails_instead_of_falling_back(run_env):
    from telethon.errors import RPCError

    run_env.api.start_takeout = AsyncMock(side_effect=RPCError(request=None, message="denied"))
    result = CliRunner().invoke(main, ["run", "--require-takeout"])
    assert result.exit_code != 0
    assert "regular API" not in result.output


def test_run_quiet_keeps_errors_and_summary(run_env):
    run_env.stats.errors.append(("chat", "boom"))
    result = CliRunner().invoke(main, ["--quiet", "run"])
    assert result.exit_code == 1
    # Под --quiet статусные строки пропадают, а сводка и ошибки остаются.
    assert "Account: acc" not in result.output
    assert "Export complete:" in result.output
    assert "Errors: 1" in result.output


def test_run_without_config_reports_error(run_env):
    (run_env.cfg_dir / "acc.yaml").unlink()
    result = CliRunner().invoke(main, ["run"])
    assert result.exit_code == 1
    assert "Config not found" in result.output
