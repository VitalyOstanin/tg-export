"""Ошибки должны быть видны: в логе, в сводке и в тексте сообщения.

Раньше сбой отдельного файла попадал только в счётчик, а причина не
восстанавливалась ни при каком уровне логирования: исключение перехватывалось
и превращалось в строку списка, минуя logging.
"""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from tg_export.exporter import Exporter, ExportStats, chat_error_line


def _exporter(**over) -> Exporter:
    """Exporter на подставных зависимостях: тестируется только обработка ошибок."""
    parts = {
        "api": MagicMock(),
        "state": MagicMock(),
        "config": MagicMock(),
        "renderer": MagicMock(),
        "downloader": MagicMock(),
        "account": "acc",
        "quiet": True,
    }
    parts.update(over)
    return Exporter(**parts)  # pyright: ignore[reportArgumentType]


@pytest.mark.asyncio
async def test_a_media_failure_reaches_the_log_with_its_context(tmp_path, caplog):
    """«Errors: 137» без единой записи в логе не поддаётся расследованию."""
    downloader = MagicMock()
    downloader.download = AsyncMock(side_effect=OSError("connection dropped"))
    exporter = _exporter(downloader=downloader)

    msg = MagicMock()
    msg.id = 42
    msg.media = MagicMock()
    stats = ExportStats()

    with caplog.at_level(logging.WARNING, logger="tg_export.exporter"):
        await exporter._process_media(msg, MagicMock(), tmp_path, stats, chat_id=555)

    records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert records, "ошибка скачивания нигде не залогирована"
    text = records[0].getMessage()
    assert "42" in text and "555" in text, f"нет идентификаторов чата и сообщения: {text}"
    assert records[0].exc_info is not None, "трассировка не сохранена"
    assert stats.errors and "OSError" in stats.errors[0], f"тип ошибки потерян: {stats.errors}"


def test_the_error_line_names_the_exception_type():
    """У TimeoutError и ValueError пустой str(), и строка обрывалась на двоеточии."""
    assert "TimeoutError" in chat_error_line("Chat", TimeoutError(), chat_id=1)
    assert "KeyError" in chat_error_line("Chat", KeyError("peer_id"), chat_id=1)
    line = chat_error_line("Chat", ConnectionResetError(104, "Connection reset"), chat_id=1)
    assert "ConnectionResetError" in line and "Connection reset" in line


@pytest.mark.asyncio
async def test_failed_userpics_are_counted_next_to_the_saved_ones(tmp_path):
    """`exported: 10 profile photos` при 50 несохранённых выглядит как полнота."""
    printed = []

    async def iter_userpics():
        for _ in range(3):
            yield MagicMock()

    api = MagicMock()
    api.iter_userpics = iter_userpics
    api.client.download_media = AsyncMock(
        side_effect=[str(tmp_path / "photo_1.jpg"), OSError("no"), OSError("no")]
    )
    renderer = MagicMock()
    renderer.output_dir = tmp_path
    exporter = _exporter(api=api, renderer=renderer, quiet=False)
    exporter._status_print = lambda *args, **kwargs: printed.append(str(args[0]))

    await exporter._export_userpics()

    assert printed, "строка итога не напечатана"
    assert "1 profile photos" in printed[-1]
    assert "2 failed" in printed[-1], f"неудачи не показаны: {printed[-1]}"


def test_an_unexpected_error_is_reported_as_a_line_not_a_traceback(monkeypatch, capsys):
    """Трассировка ложится поверх незакрытого прогресс-бара и читается как поломка."""
    from tg_export import cli

    monkeypatch.setattr(cli, "_DEBUG", False)
    monkeypatch.setattr(cli.main, "main", MagicMock(side_effect=RuntimeError("socket closed")))

    with pytest.raises(SystemExit) as exit_info:
        cli.run_cli()

    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "socket closed" in err, err
    assert "--debug" in err, "нет подсказки, как получить трассировку"
    assert "Traceback" not in err


def test_debug_does_not_turn_on_the_libraries_own_logging():
    """--debug заявлен как «показать всё своё», а топил собственные записи чужими.

    telethon логирует каждый пакет MTProto, aiosqlite -- каждый запрос; на их
    фоне три десятка собственных debug-записей не найти.
    """
    from tg_export import cli

    try:
        level, include_libraries = cli._resolve_log_level(debug=True, log_level=None)
        assert (level, include_libraries) == (logging.DEBUG, False)

        cli._quiet_third_party_loggers(level, include_libraries=include_libraries)
        assert logging.getLogger("telethon").level == logging.WARNING
        assert logging.getLogger("aiosqlite").level == logging.WARNING
    finally:
        for name in cli._THIRD_PARTY_LOGGERS:
            logging.getLogger(name).setLevel(logging.NOTSET)


def test_the_all_suffix_lifts_the_libraries_too():
    """Полный вывод библиотек должен оставаться доступным -- отдельным словом."""
    from tg_export import cli

    try:
        level, include_libraries = cli._resolve_log_level(debug=False, log_level="DEBUG:all")
        assert (level, include_libraries) == (logging.DEBUG, True)

        cli._quiet_third_party_loggers(level, include_libraries=include_libraries)
        assert logging.getLogger("telethon").level == logging.DEBUG
    finally:
        for name in cli._THIRD_PARTY_LOGGERS:
            logging.getLogger(name).setLevel(logging.NOTSET)
