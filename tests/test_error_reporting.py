"""Ошибки должны быть видны: в логе, в сводке и в тексте сообщения.

Раньше сбой отдельного файла попадал только в счётчик, а причина не
восстанавливалась ни при каком уровне логирования: исключение перехватывалось
и превращалось в строку списка, минуя logging.
"""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from tg_export.cli import common as cli_common
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

    monkeypatch.setattr(cli_common, "_DEBUG", False)
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

    try:
        level, include_libraries = cli_common._resolve_log_level(debug=True, log_level=None)
        assert (level, include_libraries) == (logging.DEBUG, False)

        cli_common._quiet_third_party_loggers(level, include_libraries=include_libraries)
        assert logging.getLogger("telethon").level == logging.WARNING
        assert logging.getLogger("aiosqlite").level == logging.WARNING
    finally:
        for name in cli_common._THIRD_PARTY_LOGGERS:
            logging.getLogger(name).setLevel(logging.NOTSET)


def test_the_all_suffix_lifts_the_libraries_too():
    """Полный вывод библиотек должен оставаться доступным -- отдельным словом."""

    try:
        level, include_libraries = cli_common._resolve_log_level(debug=False, log_level="DEBUG:all")
        assert (level, include_libraries) == (logging.DEBUG, True)

        cli_common._quiet_third_party_loggers(level, include_libraries=include_libraries)
        assert logging.getLogger("telethon").level == logging.DEBUG
    finally:
        for name in cli_common._THIRD_PARTY_LOGGERS:
            logging.getLogger(name).setLevel(logging.NOTSET)


@pytest.mark.asyncio
async def test_pages_are_not_rebuilt_when_the_chat_did_not_change(tmp_path):
    """Рендер шёл безусловно: страницы всех месяцев перерисовывались каждый запуск.

    Инкрементальный запуск, не добавивший ни одного сообщения и ни одного файла,
    всё равно удалял готовые страницы и собирал их заново -- по загрузке месяца
    из SQLite и разбору JSON каждого сообщения на каждую страницу.
    """
    from tg_export.exporter import ExportStats

    state = MagicMock()
    state.list_message_months = AsyncMock(return_value=["2024-01"])
    exporter = _exporter(state=state)

    chat_dir = tmp_path / "chat"
    chat_dir.mkdir()
    (chat_dir / "messages_2024-01.html").write_text("<html>", encoding="utf-8")

    stats = ExportStats()
    stats.begin_chat(messages_in_db=10, messages_total=10)
    chat = MagicMock(id=1, name="Chat")

    await exporter._render_chat_html(chat, chat_dir, stats)

    state.list_message_months.assert_not_awaited()


@pytest.mark.asyncio
async def test_pages_are_rebuilt_when_a_message_arrived(tmp_path):
    """Новое сообщение обязано попасть на страницу -- пропуск рендера тут недопустим."""
    from tg_export.exporter import ExportStats

    state = MagicMock()
    state.list_message_months = AsyncMock(return_value=[])
    exporter = _exporter(state=state)

    chat_dir = tmp_path / "chat"
    chat_dir.mkdir()
    (chat_dir / "messages_2024-01.html").write_text("<html>", encoding="utf-8")

    stats = ExportStats()
    stats.begin_chat(messages_in_db=10, messages_total=10)
    stats.messages_exported += 1
    chat = MagicMock(id=1, name="Chat")

    await exporter._render_chat_html(chat, chat_dir, stats)

    state.list_message_months.assert_awaited_once()


@pytest.mark.asyncio
async def test_pages_are_built_when_they_are_missing(tmp_path):
    """Пустой каталог чата -- страницы нужно собрать, даже если ничего не пришло."""
    from tg_export.exporter import ExportStats

    state = MagicMock()
    state.list_message_months = AsyncMock(return_value=[])
    exporter = _exporter(state=state)

    stats = ExportStats()
    stats.begin_chat(messages_in_db=10, messages_total=10)

    await exporter._render_chat_html(MagicMock(id=1, name="Chat"), tmp_path / "absent", stats)

    state.list_message_months.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_full_disk_stops_the_run_instead_of_being_counted_per_file(tmp_path):
    """Кончившееся место -- причина остановить прогон, а не отказ одного файла.

    Пока DiskSpaceError забирал общий `except Exception`, ветка остановки в
    _export_chat_entry была недостижима: прогон шёл по всем оставшимся
    сообщениям, ничего не скачивая, и складывал в сводку десятки тысяч
    одинаковых строк «Media error ...».
    """
    from tg_export.media import DiskSpaceError

    downloader = MagicMock()
    downloader.download = AsyncMock(side_effect=DiskSpaceError("Free space less than 5 GB"))
    exporter = _exporter(downloader=downloader)

    msg = MagicMock()
    msg.id = 42
    msg.media = MagicMock()
    stats = ExportStats()

    with pytest.raises(DiskSpaceError):
        await exporter._process_media(msg, MagicMock(), tmp_path, stats, chat_id=555)

    assert not stats.errors, f"нехватка места учтена как отказ одного файла: {stats.errors}"


@pytest.mark.asyncio
async def test_the_chat_loop_is_told_to_stop_when_the_disk_is_full(tmp_path):
    """Возврат False из _export_chat_entry -- то, чем прерывается цикл по чатам."""
    from tg_export.media import DiskSpaceError

    state = MagicMock()
    state.cache_catalog = AsyncMock()
    exporter = _exporter(state=state)
    exporter.export_chat = AsyncMock(side_effect=DiskSpaceError("Free space less than 5 GB"))
    exporter._cleanup_orphaned_files = AsyncMock()
    exporter.downloader.tdesktop_indexes = []

    chat = MagicMock(id=1, folder=None, is_left=False, is_archived=False)
    # `name` -- служебный аргумент конструктора MagicMock, задаётся отдельно.
    chat.name = "Chat"
    chat.type.value = "private"
    chat.messages_count = 1
    stats = ExportStats()

    keep_going = await exporter._export_chat_entry(
        chat, MagicMock(), tmp_path, stats, MagicMock(), MagicMock()
    )

    assert keep_going is False, "цикл по чатам продолжился при заполненном диске"
    assert stats.errors and "Free space" in stats.errors[0], f"причина остановки не записана: {stats.errors}"


@pytest.mark.asyncio
async def test_a_failed_global_section_is_recorded_and_the_rest_go_on():
    """Отказ подвыгрузки обязан попасть в сводку и в код возврата.

    Код возврата и блок «Errors:» считаются по stats.errors, а шесть блоков
    фазы глобальных данных только писали в журнал: прогон, где не выгрузились
    ни контакты, ни сессии, ни аватары, завершался нулём и печатал
    «Export complete».
    """
    from tg_export.config import Config

    config = Config(
        personal_info=False,
        contacts=True,
        sessions=True,
        userpics=False,
        stories=False,
        profile_music=False,
    )
    exporter = _exporter(config=config)
    exporter._export_contacts = AsyncMock(side_effect=RuntimeError("no connection"))
    exporter._export_sessions = AsyncMock()
    stats = ExportStats()

    await exporter.export_global_data(stats)

    exporter._export_sessions.assert_awaited_once(), "отказ одной секции прервал остальные"
    assert stats.errors, "отказ подвыгрузки не попал в сводку и не изменит код возврата"
    assert "contacts" in stats.errors[0] and "RuntimeError" in stats.errors[0], (
        f"по записи не видно, что именно не выгрузилось: {stats.errors}"
    )


@pytest.mark.asyncio
async def test_a_failure_of_the_global_phase_itself_is_recorded_too():
    """Перехват вокруг всей фазы считался по тому же списку, но в него не писал."""
    exporter = _exporter()
    exporter.export_global_data = AsyncMock(side_effect=RuntimeError("takeout expired"))
    stats = ExportStats()

    await exporter._run_global_data_phase(stats)

    assert stats.errors and "RuntimeError" in stats.errors[0], (
        f"отказ фазы не отражён ни в сводке, ни в коде возврата: {stats.errors}"
    )
