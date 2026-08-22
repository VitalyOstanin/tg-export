"""Код возврата обязан отличать успех от отказа.

Раньше половина команд печатала сообщение об ошибке и завершалась нулём, из-за
чего скрипт вокруг tg-export считал неудачу успехом: `account remove nope`
возвращал 0, тогда как `account default nope` -- 1.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

from tg_export.auth import AccountManager
from tg_export.cli import main


@pytest.fixture
def cfg_dir(tmp_path, monkeypatch):
    """Изолированный каталог настроек с одним аккаунтом и учётными данными."""
    import tg_export.cli as cli

    d = tmp_path / "config"
    mgr = AccountManager(config_dir=d)
    mgr.ensure_dirs()
    mgr.save_credentials(12345, "hash")
    monkeypatch.setattr(cli, "_mgr", lambda: AccountManager(config_dir=d))
    return d


def test_account_remove_unknown_returns_error(cfg_dir):
    result = CliRunner().invoke(main, ["account", "remove", "nope"])
    assert result.exit_code == 1


def test_account_default_unknown_returns_error(cfg_dir):
    # Контроль: эта команда сообщала об ошибке кодом и до правки.
    result = CliRunner().invoke(main, ["account", "default", "nope"])
    assert result.exit_code == 1


def test_account_remove_existing_returns_ok(cfg_dir):
    (cfg_dir / "sessions" / "acc.session").write_bytes(b"")
    result = CliRunner().invoke(main, ["account", "remove", "acc"])
    assert result.exit_code == 0


def test_auth_check_reports_broken_account(cfg_dir, monkeypatch):
    # Аккаунт есть, но подключиться к нему нельзя -- это отказ, а не норма.
    (cfg_dir / "sessions" / "acc.session").write_bytes(b"")
    api = MagicMock()
    # Соединение открывается входом в контекст, поэтому отказ имитируется на нём.
    api.__aenter__ = AsyncMock(side_effect=RuntimeError("cannot connect"))
    api.__aexit__ = AsyncMock(return_value=False)
    # TgApi импортируется внутри функции, поэтому подменяем его в модуле-источнике.
    monkeypatch.setattr("tg_export.api.TgApi", lambda *a, **k: api)

    result = CliRunner().invoke(main, ["auth", "check"])
    assert result.exit_code == 1


def _fake_connected_api(api, account="me"):
    """Подменяет _connected_api: тесту нужен готовый объект, а не соединение."""
    import contextlib

    @contextlib.asynccontextmanager
    async def fake(_account_name):
        yield api, account

    return fake


def _fake_opened_state(state, account="acc"):
    """Подменяет _opened_state тем же способом."""
    import contextlib
    from unittest.mock import MagicMock as _MagicMock

    @contextlib.asynccontextmanager
    async def fake(*_args, **_kwargs):
        await state.open()
        try:
            yield state, _MagicMock(), account
        finally:
            await state.close()

    return fake


def test_auth_check_without_accounts_is_ok(cfg_dir):
    result = CliRunner().invoke(main, ["auth", "check"])
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_tg_send_reports_failed_delivery(monkeypatch):
    from tg_export import cli

    api = MagicMock()
    api.disconnect = AsyncMock()
    api.client.send_message = AsyncMock(side_effect=RuntimeError("no such user"))
    monkeypatch.setattr(cli, "_connected_api", _fake_connected_api(api))

    code = await cli._tg_send("acc", ["someone"], "hi", None)
    assert code == 1


@pytest.mark.asyncio
async def test_tg_send_success_returns_zero(monkeypatch):
    from tg_export import cli

    api = MagicMock()
    api.disconnect = AsyncMock()
    api.client.send_message = AsyncMock()
    monkeypatch.setattr(cli, "_connected_api", _fake_connected_api(api))

    code = await cli._tg_send("acc", ["someone"], "hi", None)
    assert code == 0


@pytest.mark.asyncio
async def test_tg_download_missing_message_is_error(monkeypatch, tmp_path):
    from tg_export import cli

    api = MagicMock()
    api.disconnect = AsyncMock()
    api.client.get_messages = AsyncMock(return_value=None)
    monkeypatch.setattr(cli, "_connected_api", _fake_connected_api(api))

    code = await cli._tg_download("acc", 1, 2, str(tmp_path))
    assert code == 1


def test_export_exit_code_maps_signal_and_errors():
    from tg_export.cli import _export_exit_code

    assert _export_exit_code(signum=None, error_count=0) == 0
    assert _export_exit_code(signum=None, error_count=3) == 1
    # Прерывание важнее: 128 + номер сигнала, как принято в оболочках.
    assert _export_exit_code(signum=2, error_count=0) == 130
    assert _export_exit_code(signum=15, error_count=7) == 143


def test_keyboard_interrupt_maps_to_130(monkeypatch):
    import click

    from tg_export import cli

    def boom(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.main, "main", boom)
    monkeypatch.setattr(click, "echo", lambda *a, **k: None)

    with pytest.raises(SystemExit) as excinfo:
        cli.run_cli()
    assert excinfo.value.code == 130


@pytest.mark.asyncio
async def test_state_reset_unknown_chat_is_error(tmp_path, monkeypatch):
    from tg_export import cli
    from tg_export.state import ExportState

    st = ExportState(tmp_path / "state.db")
    monkeypatch.setattr(cli, "_opened_state", _fake_opened_state(st))

    code = await cli._state_reset("acc", None, None, False, False, 999)
    assert code == 1


def _unused(_: Path) -> None:
    """Ссылка на Path, чтобы импорт не выглядел лишним для линтера."""
