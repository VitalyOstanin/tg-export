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
