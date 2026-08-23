from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tg_export.exporter import Exporter, resolve_chat_dir, sanitize_name


@pytest.mark.asyncio
async def test_exporter_dry_run_no_downloads(tmp_path):
    api = AsyncMock()
    state = AsyncMock()
    config = MagicMock()
    config.output.path = str(tmp_path / "out")
    renderer = MagicMock()
    downloader = AsyncMock()

    exporter = Exporter(
        api=api, state=state, config=config, renderer=renderer, downloader=downloader, account="test"
    )
    await exporter.run(dry_run=True, chat_list=[])
    downloader.download.assert_not_called()


def test_resolve_chat_dir():
    result = resolve_chat_dir(
        base=Path("/output"),
        chat_name="Рабочий чат",
        chat_id=1234567890,
        folder="Работа",
        is_left=False,
    )
    assert result == Path("/output/folders/Работа/Рабочий_чат_1234567890")


def test_resolve_chat_dir_unfiled():
    result = resolve_chat_dir(
        base=Path("/output"),
        chat_name="Иван Иванов",
        chat_id=9876543210,
        folder=None,
        is_left=False,
    )
    assert result == Path("/output/unfiled/Иван_Иванов_9876543210")


def test_resolve_chat_dir_left():
    result = resolve_chat_dir(
        base=Path("/output"),
        chat_name="Старый канал",
        chat_id=111,
        folder=None,
        is_left=True,
    )
    assert result == Path("/output/left/Старый_канал_111")


def test_resolve_chat_dir_archived():
    result = resolve_chat_dir(
        base=Path("/output"),
        chat_name="Старый чат",
        chat_id=222,
        folder=None,
        is_left=False,
        is_archived=True,
    )
    assert result == Path("/output/archived/Старый_чат_222")


def test_sanitize_name():
    assert sanitize_name("Рабочий чат") == "Рабочий_чат"
    assert sanitize_name("file/with:special<chars>") == "file_with_special_chars_"
    assert sanitize_name("  spaces  ") == "spaces"


def test_sanitize_name_handles_path_traversal_and_unsafe_chars():
    assert sanitize_name("..") == "_"
    assert sanitize_name(".") == "_"
    assert sanitize_name("") == "_"
    # Управляющие символы заменяются на _
    assert "\x00" not in sanitize_name("foo\x00bar")
    assert "\n" not in sanitize_name("a\nb")
    # RTL override (U+202E) удаляется
    assert "‮" not in sanitize_name("file‮gpj.txt")
    # NFKC нормализация: "ﬁle" (U+FB01) -> "file"
    assert sanitize_name("ﬁle") == "file"
    # Длина ограничена 200 байтами
    long_name = "a" * 500
    assert len(sanitize_name(long_name).encode("utf-8")) <= 200


def test_speed_is_measured_over_the_current_chat_not_the_whole_run():
    """Числитель относился к текущему чату, а знаменатель -- ко всему прогону.

    Счётчик байтов сбрасывается на каждом чате, а время продолжает расти, и
    через час экспорта показанная скорость занижена в десятки раз.
    """
    import dataclasses
    import time

    from tg_export.exporter import ExportStats, StatusView

    stats = ExportStats()
    stats.begin_chat(messages_in_db=0, messages_total=0)
    stats._chat_start = dataclasses.replace(stats._chat_start, started_at=time.monotonic() - 10)
    stats.data_size = 50 * 1024 * 1024
    stats.messages_exported = 100

    line = StatusView(stats, start_time=time.monotonic() - 10_000).line1(stats.chat_view())

    # 50 МБ за 10 секунд -- около 5 МБ/с, а не 5 КБ/с от общего времени прогона.
    assert "5.0 MB/s" in line, line
    assert "10/s" in line, line


def test_phase_two_continues_from_where_phase_one_stopped():
    """Без нижней границы фаза 2 шла с самого свежего сообщения.

    Прерывание между фазами оставляет `oldest_msg_id == 0` при непустом
    `last_msg_id`: чат перебирался целиком заново вместо продолжения.
    """
    from tg_export.exporter import phase_two_kwargs

    base = {"offset_date": "2024-01-01"}

    assert phase_two_kwargs(base, oldest_msg_id=500, last_msg_id=900) == {"offset_id": 500}
    assert phase_two_kwargs(base, oldest_msg_id=0, last_msg_id=900) == {"offset_id": 900}
    # Первый запуск: нижней границы нет и верхней тоже -- идём от самых свежих.
    assert phase_two_kwargs(base, oldest_msg_id=0, last_msg_id=0) == base


def test_every_per_chat_counter_exists_on_the_run_counters():
    """Снимок счётчиков строится по именам полей ChatCounters.

    Прежде имена были записаны третий раз строковыми ключами словаря-снимка и
    читались через `.get(..., 0)`: опечатка в ключе давала не ошибку, а тихий
    ноль -- счётчик по чату навсегда оставался равным общему.
    """
    from dataclasses import fields

    from tg_export.exporter import ChatCounters, ExportStats

    run_fields = {f.name for f in fields(ExportStats)}
    missing = [f.name for f in fields(ChatCounters) if f.name not in run_fields]
    assert not missing, f"нет таких счётчиков в ExportStats: {missing}"


def test_per_chat_counts_only_what_the_current_chat_added():
    from tg_export.exporter import ExportStats

    stats = ExportStats()
    stats.files_downloaded = 7
    stats.data_size = 500
    stats.begin_chat(messages_in_db=0, messages_total=0)
    stats.files_downloaded += 2
    stats.data_size += 100

    assert stats.per_chat.files_downloaded == 2
    assert stats.per_chat.data_size == 100
    assert stats.files_downloaded == 9


@pytest.mark.asyncio
async def test_message_count_is_recounted_only_when_something_was_written():
    """COUNT(*) по чату линеен по его размеру, а прогон без записей его не меняет.

    Счётчик снимается в начале каждого чата; повторный подсчёт в конце нужен
    только там, где сообщения действительно добавились.
    """
    from tg_export.exporter import ExportStats

    state = AsyncMock()
    state.count_messages = AsyncMock(return_value=999)
    exporter = Exporter(
        api=AsyncMock(),
        state=state,
        config=MagicMock(),
        renderer=MagicMock(),
        downloader=AsyncMock(),
        account="test",
    )

    stats = ExportStats()
    stats.begin_chat(messages_in_db=42, messages_total=42)

    assert await exporter._chat_message_count(10, stats) == 42
    state.count_messages.assert_not_called()

    stats.messages_exported += 3
    assert await exporter._chat_message_count(10, stats) == 999
    state.count_messages.assert_awaited_once_with(10)


def test_an_unknown_download_status_is_reported_not_swallowed(caplog):
    """Исход загрузки, не описанный в таблице счётчиков, обязан быть заметен.

    Раньше статусы разбирались цепочкой elif без завершающей ветки: новый исход
    в MediaDownloader.download проходил мимо статистики, и сводка молча
    расходилась с тем, что лежит на диске.
    """
    import logging

    from tg_export.exporter import ExportStats

    stats = ExportStats()
    with caplog.at_level(logging.WARNING):
        Exporter._count_download("teleported", None, stats)

    assert "teleported" in caplog.text
    assert stats.files_downloaded == 0


def test_every_download_status_has_a_place_in_the_summary():
    """Каждый член DownloadStatus описан в таблице счётчиков экспортёра."""
    from tg_export.media import DownloadStatus

    missing = [s for s in DownloadStatus if s not in Exporter._DOWNLOAD_COUNTERS]
    assert not missing, f"исходы загрузки без места в сводке: {missing}"

    from tg_export.exporter import ExportStats

    counters = {name for name, _ in Exporter._DOWNLOAD_COUNTERS.values() if name}
    stats = ExportStats()
    unknown = [name for name in counters if not hasattr(stats, name)]
    assert not unknown, f"счётчиков с такими именами в ExportStats нет: {unknown}"


async def _exported_story(tmp_path, filename: str) -> dict:
    """Одна выгруженная история, названная скачанным файлом `filename`."""
    story = SimpleNamespace(id=1, media=object(), caption="", date=None)
    api = MagicMock()
    api.get_stories = AsyncMock(return_value=(SimpleNamespace(stories=[story]), SimpleNamespace(stories=[])))
    api.client = MagicMock()
    api.client.download_media = AsyncMock(return_value=str(tmp_path / "stories" / filename))

    renderer = MagicMock()
    renderer.output_dir = tmp_path
    exporter = Exporter(
        api=api,
        state=AsyncMock(),
        config=MagicMock(),
        renderer=renderer,
        downloader=AsyncMock(),
        account="test",
    )

    await exporter._export_stories()

    (stories,), _ = renderer.render_stories.call_args
    return stories[0]


@pytest.mark.asyncio
async def test_a_story_file_without_an_extension_is_rendered_as_a_photo(tmp_path):
    """Проверялось вхождение суффикса в строку расширения, а не равенство.

    Пустая строка входит в любую (`"" in ".mp4"`), поэтому файл без расширения
    всегда попадал в `video_path` и подставлялся в `<video>` вместо `<img>`.
    То же было у любого суффикса-префикса: `.m`, `.mo`, `.av`.
    """
    story = await _exported_story(tmp_path, "story_0")

    assert story["photo_path"] == "stories/story_0"
    assert story["video_path"] is None


@pytest.mark.asyncio
async def test_a_story_video_is_still_rendered_as_a_video(tmp_path):
    # Контроль: расширение из списка по-прежнему даёт видео, и регистр не мешает.
    story = await _exported_story(tmp_path, "story_0.MP4")

    assert story["video_path"] == "stories/story_0.MP4"
    assert story["photo_path"] is None
