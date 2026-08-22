from pathlib import Path
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
    import time

    from tg_export.exporter import ExportStats, StatusView

    stats = ExportStats()
    stats.begin_chat(messages_in_db=0, messages_total=0)
    stats._chat_started_at = time.monotonic() - 10
    stats.data_size = 50 * 1024 * 1024
    stats.messages_exported = 100

    line = StatusView(stats, start_time=time.monotonic() - 10_000).line1()

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
