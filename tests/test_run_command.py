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
from tg_export.cli import common as cli_common
from tg_export.cli import export as cli_export
from tg_export.cli import main
from tg_export.exporter import ExportStats


@pytest.fixture
def run_env(tmp_path, monkeypatch):
    """Окружение для run: аккаунт, конфиг, подставные Telegram и экспортёр."""
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
    monkeypatch.setattr(cli_common, "account_manager", lambda: AccountManager(config_dir=cfg_dir))

    api = MagicMock()
    api.connect = AsyncMock()
    api.disconnect = AsyncMock()
    api.start_takeout = AsyncMock()
    api.stop_takeout = AsyncMock()
    api.takeout = None

    # TgApi используется как контекстменеджер, поэтому подставной объект должен
    # проводить вход и выход через те же connect/disconnect.
    async def _aenter(*_args):
        await api.connect()
        return api

    async def _aexit(*_exc):
        await api.disconnect()
        return False

    api.__aenter__ = _aenter
    api.__aexit__ = _aexit
    monkeypatch.setattr("tg_export.api.TgApi", lambda *a, **k: api)
    monkeypatch.setattr("tg_export.catalog.fetch_catalog", AsyncMock(return_value=[]))
    monkeypatch.setattr("tg_export.html.renderer.HtmlRenderer", MagicMock())
    monkeypatch.setattr("tg_export.media.MediaDownloader", MagicMock())

    stats = ExportStats()
    exporter = MagicMock()
    exporter.run = AsyncMock(return_value=stats)
    exporter.force_shutdown = False
    exporter.shutdown_requested = False
    exporter.shutdown_signal = None
    monkeypatch.setattr("tg_export.exporter.Exporter", lambda *a, **k: exporter)
    monkeypatch.setattr(cli_export, "_render_index", AsyncMock())

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

    run_env.exporter.shutdown_signal = signal.SIGINT
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


def test_run_keeps_the_takeout_session_for_the_next_run(run_env):
    """Завершение takeout после экспорта стоит суток ожидания на следующем
    запуске: Telegram отвечает на новый InitTakeoutSessionRequest откатом до
    24 часов. Команда обязана отпустить сессию, а не финализировать её.

    Отпускает её выход из контекста подключения (TgApi.disconnect), поэтому
    здесь проверяется, что команда закрывает соединение и ни при каких
    обстоятельствах не завершает takeout сама; что именно делает disconnect,
    проверено в test_disconnect_releases_takeout_without_finishing_it.
    """
    run_env.api.takeout = MagicMock()

    result = CliRunner().invoke(main, ["run"])

    assert result.exit_code == 0, result.output
    run_env.api.disconnect.assert_awaited_once_with()
    assert run_env.api.stop_takeout.await_args_list == []


def test_takeout_clear_finishes_the_session_on_the_server(tmp_path, monkeypatch):
    """Раз экспорт намеренно оставляет сессию живой, clear должен закрыть её и
    на сервере, иначе takeout остаётся запущенным без способа к нему обратиться."""
    cfg_dir = tmp_path / "config"
    mgr = AccountManager(config_dir=cfg_dir)
    mgr.ensure_dirs()
    mgr.save_credentials(1, "hash")
    mgr.set_default_account("acc")
    mgr.session_path("acc").write_bytes(b"")
    monkeypatch.setattr(cli_common, "account_manager", lambda: AccountManager(config_dir=cfg_dir))

    api = MagicMock()
    api.connect = AsyncMock()
    api.disconnect = AsyncMock()
    api.client.session.takeout_id = 4242
    api.client.end_takeout = AsyncMock(return_value=True)
    monkeypatch.setattr("tg_export.api.TgApi", lambda *a, **k: api)

    result = CliRunner().invoke(main, ["takeout", "clear"])

    assert result.exit_code == 0, result.output
    api.client.end_takeout.assert_awaited_once_with(success=True)
    assert "4242" in result.output


def test_service_messages_are_skipped_when_the_option_says_so(tmp_path):
    """`export_service_messages: false` не влияла ни на что.

    Опция разбиралась, прокидывалась в каждый ChatExportConfig и нигде не
    читалась: сообщения со служебным действием сохранялись наравне с
    обычными.
    """
    from datetime import datetime

    from tg_export.exporter import Exporter
    from tg_export.models import ActionPinMessage, Message

    def _msg(msg_id, action=None):
        return Message(
            id=msg_id,
            chat_id=1,
            date=datetime(2024, 1, 1),
            edited=None,
            from_id=None,
            from_name="",
            text=[],
            media=None,
            action=action,
            reply_to_msg_id=None,
            reply_to_peer_id=None,
            forwarded_from=None,
            reactions=[],
            is_outgoing=False,
            signature=None,
            via_bot_id=None,
            saved_from_chat_id=None,
            inline_buttons=None,
            topic_id=None,
            grouped_id=None,
        )

    exporter = Exporter.__new__(Exporter)
    kept = exporter._keep_message(_msg(1), export_service_messages=False)
    dropped = exporter._keep_message(
        _msg(2, ActionPinMessage(type="ActionPinMessage")), export_service_messages=False
    )
    both = exporter._keep_message(
        _msg(3, ActionPinMessage(type="ActionPinMessage")), export_service_messages=True
    )

    assert kept is True
    assert dropped is False
    assert both is True


def test_the_summary_shows_what_the_errors_were(run_env):
    """«Errors: 137» без единой строки причины не поддаётся расследованию."""
    run_env.stats.errors.extend([f"Media error msg {i}: OSError: connection dropped" for i in range(3)])

    result = CliRunner().invoke(main, ["run"])

    assert result.exit_code == 1
    assert "Errors: 3" in result.output
    assert result.output.count("connection dropped") == 3, result.output


def test_a_long_list_of_errors_is_cut_with_a_count_of_the_rest(run_env):
    """Полный список на сотни строк смыл бы саму сводку."""
    run_env.stats.errors.extend([f"error {i}" for i in range(25)])

    result = CliRunner().invoke(main, ["run"])

    assert "Errors: 25" in result.output
    assert "error 0" in result.output
    assert "error 24" not in result.output
    assert "15 more" in result.output, result.output


def test_no_takeout_skips_the_request_entirely(monkeypatch, run_env):
    """`--no-takeout` не должен даже спрашивать Takeout.

    Запрос стоит обращения, на которое Telegram отвечает кулдауном до суток, и
    экспорт всё равно переходит на обычный API. Флаг существует ровно для того,
    чтобы этого обращения не было.
    """
    asked = []

    async def _never(api, cfg, *, require):
        asked.append(require)
        return True

    monkeypatch.setattr(cli_export, "_start_takeout", _never)

    result = CliRunner().invoke(main, ["run", "--no-takeout", "--dry-run"])

    assert asked == [], "Takeout запрошен вопреки --no-takeout"
    assert result.exit_code == 0, result.output


def test_require_takeout_and_no_takeout_together_are_refused(run_env):
    """Два флага просят противоположного: молча предпочесть один из них нельзя.

    Отказ по форме вызова -- это код 2 и блок usage, как у любой другой
    непригодной комбинации аргументов; код 1 занят отказом по состоянию
    системы (нет аккаунта, нет конфигурации, нет чата в базе).
    """
    result = CliRunner().invoke(main, ["run", "--no-takeout", "--require-takeout"])

    assert result.exit_code == 2
    assert "--no-takeout" in result.output
    assert "Usage:" in result.output


def test_takeout_clear_keeps_the_id_when_the_server_could_not_be_reached(tmp_path, monkeypatch):
    """Единственная ссылка на takeout-сессию стиралась и при обрыве связи.

    Обработчик не различал «сервер отказал по существу» и «запрос не дошёл»,
    а стирал `takeout_id` в обоих случаях: сессия оставалась висеть на сервере
    до собственного истечения, и повторить `takeout clear` было уже нечем.
    """
    cfg_dir = tmp_path / "config"
    mgr = AccountManager(config_dir=cfg_dir)
    mgr.ensure_dirs()
    mgr.save_credentials(1, "hash")
    mgr.set_default_account("acc")
    mgr.session_path("acc").write_bytes(b"")
    monkeypatch.setattr(cli_common, "account_manager", lambda: AccountManager(config_dir=cfg_dir))

    api = MagicMock()
    api.connect = AsyncMock()
    api.disconnect = AsyncMock()
    api.client.session.takeout_id = 4242
    api.client.end_takeout = AsyncMock(side_effect=TimeoutError("no answer"))
    monkeypatch.setattr("tg_export.api.TgApi", lambda *a, **k: api)

    result = CliRunner().invoke(main, ["takeout", "clear"])

    assert result.exit_code == 1, result.output
    assert api.client.session.takeout_id == 4242
    api.client.session.save.assert_not_called()


def test_takeout_clear_drops_the_id_when_the_server_refused_on_the_merits(tmp_path, monkeypatch):
    """Отказ по существу означает, что сессии на сервере уже нет: id можно стереть."""
    cfg_dir = tmp_path / "config"
    mgr = AccountManager(config_dir=cfg_dir)
    mgr.ensure_dirs()
    mgr.save_credentials(1, "hash")
    mgr.set_default_account("acc")
    mgr.session_path("acc").write_bytes(b"")
    monkeypatch.setattr(cli_common, "account_manager", lambda: AccountManager(config_dir=cfg_dir))

    api = MagicMock()
    api.connect = AsyncMock()
    api.disconnect = AsyncMock()
    api.client.session.takeout_id = 4242
    api.client.end_takeout = AsyncMock(side_effect=ValueError("takeout not found"))
    monkeypatch.setattr("tg_export.api.TgApi", lambda *a, **k: api)

    result = CliRunner().invoke(main, ["takeout", "clear"])

    assert result.exit_code == 0, result.output
    assert api.client.session.takeout_id is None


def test_an_interrupted_run_does_not_report_itself_as_complete(run_env):
    """После Ctrl+C сводка названа прерванной, а не завершённой.

    Мягкое прерывание сохраняет состояние и печатает те же цифры, поэтому
    заголовок -- единственное, что отличает прогон, дошедший до конца, от
    прогона, остановленного на 214-м чате из 243.
    """
    import signal

    run_env.exporter.shutdown_requested = True
    run_env.exporter.shutdown_signal = signal.SIGINT
    result = CliRunner().invoke(main, ["run"])
    assert "Export complete:" not in result.output
    assert "Export interrupted" in result.output
