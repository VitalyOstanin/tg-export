"""Код возврата обязан отличать успех от отказа.

Раньше половина команд печатала сообщение об ошибке и завершалась нулём, из-за
чего скрипт вокруг tg-export считал неудачу успехом: `account remove nope`
возвращал 0, тогда как `account default nope` -- 1.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import click
import pytest
from click.testing import CliRunner

from tg_export.auth import AccountManager
from tg_export.cli import common as cli_common
from tg_export.cli import main
from tg_export.cli import state as cli_state
from tg_export.cli import tg as cli_tg
from tg_export.errors import EXIT_USAGE


@pytest.fixture
def cfg_dir(tmp_path, monkeypatch):
    """Изолированный каталог настроек с одним аккаунтом и учётными данными."""
    d = tmp_path / "config"
    mgr = AccountManager(config_dir=d)
    mgr.ensure_dirs()
    mgr.save_credentials(12345, "hash")
    monkeypatch.setattr(cli_common, "account_manager", lambda: AccountManager(config_dir=d))
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
    """Подменяет connected_api: тесту нужен готовый объект, а не соединение."""
    import contextlib

    @contextlib.asynccontextmanager
    async def fake(_account_name):
        yield api, account

    return fake


def _fake_opened_state(state, account="acc"):
    """Подменяет opened_state тем же способом."""
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
    api = MagicMock()
    api.disconnect = AsyncMock()
    api.client.send_message = AsyncMock(side_effect=RuntimeError("no such user"))
    monkeypatch.setattr(cli_common, "connected_api", _fake_connected_api(api))

    code = await cli_tg._tg_send("acc", ["someone"], "hi", None)
    assert code == 1


@pytest.mark.asyncio
async def test_tg_send_success_returns_zero(monkeypatch):
    api = MagicMock()
    api.disconnect = AsyncMock()
    api.client.send_message = AsyncMock()
    monkeypatch.setattr(cli_common, "connected_api", _fake_connected_api(api))

    code = await cli_tg._tg_send("acc", ["someone"], "hi", None)
    assert code == 0


@pytest.mark.asyncio
async def test_tg_download_missing_message_is_error(monkeypatch, tmp_path):
    api = MagicMock()
    api.disconnect = AsyncMock()
    api.client.get_messages = AsyncMock(return_value=None)
    monkeypatch.setattr(cli_common, "connected_api", _fake_connected_api(api))

    code = await cli_tg._tg_download("acc", 1, 2, tmp_path)
    assert code == 1


def test_export_exit_code_maps_signal_and_errors():
    from tg_export.cli.common import export_exit_code

    assert export_exit_code(signum=None, error_count=0) == 0
    assert export_exit_code(signum=None, error_count=3) == 1
    # Прерывание важнее: 128 + номер сигнала, как принято в оболочках.
    assert export_exit_code(signum=2, error_count=0) == 130
    assert export_exit_code(signum=15, error_count=7) == 143


def test_a_ctrl_c_inside_a_command_ends_the_process_with_130(monkeypatch, capsys):
    """Прерывание обязано возбуждаться внутри команды, а не вместо click.

    Прежний тест подменял `cli.main.main` -- то есть убирал ровно тот код
    click, который перехватывает KeyboardInterrupt, печатает «Aborted!» и
    завершается кодом 1. Такой тест зелёный при любой поломке поведения.
    """
    from tg_export import cli

    def interrupted():
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_common, "account_manager", interrupted)
    monkeypatch.setattr(sys, "argv", ["tg-export", "account", "list"])

    with pytest.raises(SystemExit) as excinfo:
        cli.run_cli()

    assert excinfo.value.code == 130
    assert "Interrupted." in capsys.readouterr().err


def test_an_unknown_command_still_ends_with_the_usage_code(monkeypatch, capsys):
    """Сообщение и код 2 для неверного набора аргументов печатает сам click."""
    from tg_export import cli

    monkeypatch.setattr(sys, "argv", ["tg-export", "no-such-command"])

    with pytest.raises(SystemExit) as excinfo:
        cli.run_cli()

    assert excinfo.value.code == EXIT_USAGE
    assert "no-such-command" in capsys.readouterr().err


def test_a_refusal_inside_a_command_still_ends_with_one(cfg_dir, monkeypatch, capsys):
    """`ctx.exit(code)` -- то, чем команды сообщают об отказе."""
    from tg_export import cli

    monkeypatch.setattr(sys, "argv", ["tg-export", "account", "default", "nope"])

    with pytest.raises(SystemExit) as excinfo:
        cli.run_cli()

    assert excinfo.value.code == 1
    assert "nope" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_state_reset_unknown_chat_is_error(tmp_path, monkeypatch):
    from tg_export.state import ExportState

    st = ExportState(tmp_path / "state.db")
    monkeypatch.setattr(cli_common, "opened_state", _fake_opened_state(st))

    code = await cli_state._state_reset("acc", None, None, False, False, 999)
    assert code == 1


def _unused(_: Path) -> None:
    """Ссылка на Path, чтобы импорт не выглядел лишним для линтера."""


def test_domain_error_carries_its_own_exit_code(monkeypatch):
    """Код возврата берётся из класса ошибки, а не назначается на месте перехвата.

    Пока run_cli подставляет единицу сам, добавить ошибку с другим кодом
    (например, «занято другим процессом») нельзя, не правя точку входа.
    """
    import click

    from tg_export import cli
    from tg_export.errors import TgExportError

    class BusyError(TgExportError):
        exit_code = 7

    def boom(*args, **kwargs):
        raise BusyError("resource is busy")

    monkeypatch.setattr(cli.main, "main", boom)
    monkeypatch.setattr(click, "echo", lambda *a, **k: None)

    with pytest.raises(SystemExit) as excinfo:
        cli.run_cli()
    assert excinfo.value.code == 7


def test_exit_codes_are_defined_once():
    """Константы кодов возврата живут рядом с иерархией ошибок, а не в CLI."""
    from tg_export import cli, errors

    assert (errors.EXIT_OK, errors.EXIT_FAILURE, errors.EXIT_SIGINT) == (0, 1, 130)
    # cli продолжает их экспортировать, но повторно не объявляет.
    assert (cli.EXIT_OK, cli.EXIT_FAILURE, cli.EXIT_SIGINT) == (
        errors.EXIT_OK,
        errors.EXIT_FAILURE,
        errors.EXIT_SIGINT,
    )


def test_a_refusal_over_the_call_shape_gives_code_two_with_usage():
    """Один класс ситуаций -- один способ отказать.

    «Аргументы не складываются в вызов» разбиралось тремя способами: код 2 с
    блоком usage, код 1 без него и код 0. Таблица кодов возврата закрепляет за
    2 именно эту ситуацию, а за 1 -- отказ по состоянию системы.
    """
    from click.testing import CliRunner

    from tg_export.cli import main

    for args in (["state", "reset"], ["tg", "send", "123"], ["tg", "info"]):
        result = CliRunner().invoke(main, args)
        assert result.exit_code == EXIT_USAGE, (args, result.output)
        assert "Usage:" in result.output, (args, result.output)


def test_a_question_with_nothing_to_read_is_a_refusal_not_an_interruption(monkeypatch, capsys):
    """Пустой stdin -- это не Ctrl+C, и код 130 о нём лжёт.

    `click.Abort` поднимается и при прерывании, и при конце ввода, поэтому
    любой запуск без терминала на входе (cron, systemd, CI, `< /dev/null`)
    отчитывался строкой «Interrupted.» и кодом 130, который супервизор читает
    как «процесс убит сигналом». Настоящего сигнала при этом не было, а выход
    из положения -- флаг, которым вопрос обходится, -- не назывался.
    """
    from tg_export import cli
    from tg_export.console import confirm

    def unanswered(*args, **kwargs):
        raise click.Abort

    monkeypatch.setattr(click, "confirm", unanswered)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    with pytest.raises(click.ClickException) as excinfo:
        confirm("Delete everything?", without_an_answer="--yes")

    assert excinfo.value.exit_code == 1
    assert "--yes" in excinfo.value.message
    assert cli.EXIT_SIGINT == 130


def test_a_question_interrupted_at_a_terminal_is_still_an_interruption(monkeypatch):
    """Ctrl+C у настоящего терминала остаётся прерыванием с кодом 130."""
    from tg_export.console import ask, confirm

    def interrupted(*args, **kwargs):
        raise click.Abort

    monkeypatch.setattr(click, "confirm", interrupted)
    monkeypatch.setattr(click, "prompt", interrupted)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    with pytest.raises(click.Abort):
        confirm("Delete everything?", without_an_answer="--yes")
    with pytest.raises(click.Abort):
        ask("Account alias", without_an_answer="--name")


def test_a_value_with_nothing_to_read_names_the_option_that_supplies_it(monkeypatch):
    """То же для вопроса о значении: отказ называет опцию, а не «Interrupted.»."""
    from tg_export.console import ask

    def unanswered(*args, **kwargs):
        raise click.Abort

    monkeypatch.setattr(click, "prompt", unanswered)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    with pytest.raises(click.ClickException) as excinfo:
        ask("Account alias", without_an_answer="--name")

    assert excinfo.value.exit_code == 1
    assert "--name" in excinfo.value.message


def test_a_closed_pipe_is_not_reported_as_a_refusal(monkeypatch):
    """`state show --json | head -20` завершался кодом 1 и без единого слова.

    Click отвечает на закрытый stdout подменой потоков и `sys.exit(1)`, минуя
    разбор исходов: обёртка с `set -o pipefail` читала обычный ранний выход
    читателя как отказ экспорта. Соглашение оболочек для этого случая --
    завершение сигналом (`141` = 128 + SIGPIPE), а не код отказа.
    """
    import contextlib
    import signal

    from tg_export import cli

    if not hasattr(signal, "SIGPIPE"):
        pytest.skip("SIGPIPE есть только на POSIX")

    previous = signal.getsignal(signal.SIGPIPE)
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    monkeypatch.setattr(sys, "argv", ["tg-export", "--version"])
    try:
        with contextlib.suppress(SystemExit):
            cli.run_cli()
        assert signal.getsignal(signal.SIGPIPE) is signal.SIG_DFL
    finally:
        signal.signal(signal.SIGPIPE, previous)


def test_state_reset_refuses_a_chat_id_together_with_all():
    """`state reset --all 123` молча отбрасывал идентификатор и сбрасывал весь аккаунт.

    Соседние команды такую пару отвергают кодом 2 с блоком usage; здесь запрос
    читался как «сбросить один чат», а выполнялся как «сбросить все».
    """
    from click.testing import CliRunner

    from tg_export.cli import main

    result = CliRunner().invoke(main, ["state", "reset", "--all", "123"])

    assert result.exit_code == 2, result.output
    assert "not both" in result.output, result.output
