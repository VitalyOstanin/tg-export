"""Тесты видимости пауз: flood wait и повтор после сбоя.

Экспорт останавливается не только по своей воле: Telegram называет задержку,
и до её конца ни одно сообщение не приходит. Прогресс при этом замирает, а
единственная запись об ожидании шла в журнал telethon уровня INFO, который при
штатном запуске придавлен до WARNING. Со стороны это выглядело как зависание.

Проверяется три вещи: доска ожиданий отдаёт остаток и сама забывает истёкшее,
статусная строка показывает этот остаток, а запись telethon о сне доходит до
доски и остаётся видимой, не вытаскивая за собой остальной журнал библиотеки.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from rich.text import Text


def test_a_noted_wait_reports_the_time_left():
    from tg_export.waits import WaitBoard

    board = WaitBoard()
    board.note(reason="flood wait", what="GetHistoryRequest", seconds=18, now=100.0)
    pending = board.pending(now=105.0)
    assert len(pending) == 1
    assert pending[0].remaining(105.0) == pytest.approx(13.0)


def test_a_wait_that_ran_out_is_forgotten():
    from tg_export.waits import WaitBoard

    board = WaitBoard()
    board.note(reason="flood wait", what="GetHistoryRequest", seconds=18, now=100.0)
    assert board.pending(now=118.5) == []


def test_the_longest_wait_comes_first():
    from tg_export.waits import WaitBoard

    board = WaitBoard()
    board.note(reason="flood wait", what="short", seconds=5, now=100.0)
    board.note(reason="flood wait", what="long", seconds=30, now=100.0)
    assert [w.what for w in board.pending(now=101.0)] == ["long", "short"]


def test_the_status_line_shows_how_long_the_export_is_held():
    import time

    from tg_export.exporter import ExportStats, StatusView
    from tg_export.waits import WaitBoard

    board = WaitBoard()
    board.note(reason="flood wait", what="GetHistoryRequest", seconds=18)
    stats = ExportStats()
    status = StatusView(stats, start_time=time.monotonic(), waits=board)
    plain = Text.from_markup(status.line1(stats.chat_view())).plain
    assert "waiting" in plain
    assert "flood wait" in plain
    assert "17s" in plain or "18s" in plain


def test_the_status_line_says_nothing_while_the_export_runs():
    import time

    from tg_export.exporter import ExportStats, StatusView
    from tg_export.waits import WaitBoard

    stats = ExportStats()
    status = StatusView(stats, start_time=time.monotonic(), waits=WaitBoard())
    assert "waiting" not in Text.from_markup(status.line1(stats.chat_view())).plain


def _flood_record(seconds: int, request: str) -> logging.LogRecord:
    """Запись, какую telethon делает перед сном на flood wait."""
    import datetime

    return logging.LogRecord(
        name="telethon.client.users",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Sleeping%s for %ds (%s) on %s flood wait",
        args=("", seconds, datetime.timedelta(seconds=seconds), request),
        exc_info=None,
    )


def test_a_flood_wait_of_telethon_reaches_the_board_and_stays_visible():
    from tg_export.waits import FloodWaitNotices, WaitBoard

    board = WaitBoard()
    notices = FloodWaitNotices(board, level=logging.WARNING)
    assert notices.filter(_flood_record(18, "GetHistoryRequest")) is True
    pending = board.pending()
    assert len(pending) == 1
    assert pending[0].what == "GetHistoryRequest"
    assert pending[0].seconds == 18


def test_the_rest_of_the_telethon_log_stays_hidden():
    from tg_export.waits import FloodWaitNotices, WaitBoard

    board = WaitBoard()
    notices = FloodWaitNotices(board, level=logging.WARNING)
    record = logging.LogRecord(
        name="telethon.client.users",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Connecting to %s...",
        args=("149.154.167.51",),
        exc_info=None,
    )
    assert notices.filter(record) is False
    assert board.pending() == []


def test_a_warning_of_telethon_passes_as_before():
    from tg_export.waits import FloodWaitNotices, WaitBoard

    notices = FloodWaitNotices(WaitBoard(), level=logging.WARNING)
    record = logging.LogRecord(
        name="telethon.client.users",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="Telegram is having internal issues %s",
        args=("ServerError",),
        exc_info=None,
    )
    assert notices.filter(record) is True


@pytest.mark.asyncio
async def test_a_flood_wait_of_a_download_is_on_the_board_while_it_sleeps(monkeypatch):
    """Пауза загрузчика видна ровно пока она длится, и снимается после."""
    import asyncio

    from telethon.errors import FloodWaitError

    from tg_export import media
    from tg_export.waits import WaitBoard

    board = WaitBoard()
    monkeypatch.setattr(media, "WAITS", board)
    seen: list[str] = []

    async def sleep(_seconds):
        seen.extend(f"{w.reason}:{w.what}" for w in board.pending())

    monkeypatch.setattr(asyncio, "sleep", sleep)
    attempts = 0

    async def attempt_download():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise FloodWaitError(request=None, capture=10)
        return "file.jpg"

    assert await media.download_with_retries(attempt_download, msg_id=7) == "file.jpg"
    assert seen == ["flood wait:msg 7"]
    assert board.pending() == []


def test_the_default_run_lifts_the_flood_wait_notices_of_telethon():
    """При штатном запуске запись telethon о сне доходит до вывода."""
    from click.testing import CliRunner

    from tg_export.cli import main
    from tg_export.waits import FLOOD_WAIT_LOGGER

    CliRunner().invoke(main, ["state"])
    telethon_logger = logging.getLogger(FLOOD_WAIT_LOGGER)
    assert telethon_logger.level == logging.INFO
    assert telethon_logger.filter(_flood_record(18, "GetHistoryRequest")) is True
    assert (
        telethon_logger.filter(
            logging.LogRecord(
                name=FLOOD_WAIT_LOGGER,
                level=logging.INFO,
                pathname=__file__,
                lineno=1,
                msg="Connecting to %s...",
                args=("dc",),
                exc_info=None,
            )
        )
        is False
    )


def test_asking_for_the_library_log_still_gives_all_of_it():
    """С ':all' модуль telethon не остаётся придавленным до INFO."""
    from click.testing import CliRunner

    from tg_export.cli import main
    from tg_export.waits import FLOOD_WAIT_LOGGER

    CliRunner().invoke(main, ["--log-level", "DEBUG:all", "state"])
    telethon_logger = logging.getLogger(FLOOD_WAIT_LOGGER)
    assert telethon_logger.level == logging.DEBUG
    assert (
        telethon_logger.filter(
            logging.LogRecord(
                name=FLOOD_WAIT_LOGGER,
                level=logging.DEBUG,
                pathname=__file__,
                lineno=1,
                msg="Sending %s",
                args=("GetHistoryRequest",),
                exc_info=None,
            )
        )
        is True
    )


def test_the_log_line_is_dropped_while_the_countdown_is_on_screen():
    """При живом статусе запись telethon о сне не дублирует отсчёт."""
    from tg_export.waits import FloodWaitNotices, WaitBoard

    board = WaitBoard()
    notices = FloodWaitNotices(board, level=logging.WARNING)
    with board.shown_in_status():
        assert notices.filter(_flood_record(18, "GetHistoryRequest")) is False
        assert len(board.pending()) == 1


def test_the_log_line_returns_when_the_status_is_gone():
    """Без живого статуса запись остаётся единственным следом паузы."""
    from tg_export.waits import FloodWaitNotices, WaitBoard

    board = WaitBoard()
    notices = FloodWaitNotices(board, level=logging.WARNING)
    with board.shown_in_status():
        pass
    assert notices.filter(_flood_record(18, "GetHistoryRequest")) is True


def test_asking_for_the_library_log_keeps_the_line_even_with_a_countdown():
    """Явно запрошенный журнал библиотеки живой статус не отменяет."""
    from tg_export.waits import FloodWaitNotices, WaitBoard

    board = WaitBoard()
    notices = FloodWaitNotices(board, level=logging.INFO)
    with board.shown_in_status():
        assert notices.filter(_flood_record(18, "GetHistoryRequest")) is True


def test_a_wait_of_the_package_is_not_logged_while_the_status_shows_it(caplog):
    """Свою паузу пакет тоже не пишет в журнал, пока виден отсчёт."""
    from tg_export.waits import WaitBoard

    board = WaitBoard()
    with caplog.at_level(logging.WARNING, logger="tg_export.waits"):
        with board.shown_in_status(), board.waiting(reason="flood wait", what="msg 7", seconds=120):
            pass
        assert caplog.records == []
        with board.waiting(reason="flood wait", what="msg 7", seconds=120):
            pass
        assert len(caplog.records) == 1


def test_the_live_display_takes_over_the_reporting_of_waits(monkeypatch):
    """Пока Live на экране, доска знает, что пауза уже показана."""
    from rich.console import Console

    from tg_export import exporter as exporter_module
    from tg_export.waits import WAITS

    fake_console = Console(quiet=True)
    monkeypatch.setattr(type(fake_console), "is_terminal", property(lambda self: True))
    monkeypatch.setattr(exporter_module, "console", fake_console)
    exporter = exporter_module.Exporter(
        api=MagicMock(),
        state=MagicMock(),
        config=MagicMock(),
        renderer=MagicMock(),
        downloader=MagicMock(),
        account="acc",
    )
    assert WAITS.status_visible is False
    with exporter._live_display(lambda: None):
        assert WAITS.status_visible is True
    assert WAITS.status_visible is False
