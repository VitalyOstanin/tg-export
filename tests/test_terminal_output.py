"""Тесты вывода в терминал: markup-эскейп и потокобезопасность.

Покрывают регрессионные сценарии из code review (см. tmp/code-review-2026-05-02.md):
- markup-инъекция через имена чатов, имена файлов, текст исключений;
- гонка чтения/мутации active_downloads между refresh-thread Live и event loop.
"""
from __future__ import annotations

import threading
import time

import pytest
from rich.text import Text


# ----- Markup escaping -----


def test_chat_export_line_escapes_markup_in_chat_name():
    from tg_export.exporter import chat_export_line

    line = chat_export_line(chat_name="[bold red]EVIL[/]", chat_type="group", folder=None)
    plain = Text.from_markup(line).plain
    assert "[bold red]EVIL[/]" in plain


def test_chat_export_line_escapes_markup_in_folder():
    from tg_export.exporter import chat_export_line

    line = chat_export_line(chat_name="Normal", chat_type="group", folder="[private]")
    plain = Text.from_markup(line).plain
    assert "[private]" in plain


def test_chat_export_line_keeps_chat_type_visible():
    from tg_export.exporter import chat_export_line

    line = chat_export_line(chat_name="Normal", chat_type="group", folder=None)
    plain = Text.from_markup(line).plain
    assert "Normal" in plain
    assert "(group)" in plain


def test_chat_progress_description_escapes_markup():
    from tg_export.exporter import chat_progress_description

    desc = chat_progress_description("[bold]X[/]")
    plain = Text.from_markup(desc).plain
    assert "[bold]X[/]" in plain


def test_chat_error_line_escapes_markup_in_name_and_error():
    from tg_export.exporter import chat_error_line

    err = ValueError("[link]nasty[/link]")
    line = chat_error_line("[bold]chat[/]", err)
    plain = Text.from_markup(line).plain
    assert "[bold]chat[/]" in plain
    assert "[link]nasty[/link]" in plain


def test_disk_space_error_line_escapes_markup():
    from tg_export.exporter import disk_space_error_line

    err = OSError("insufficient [bold]space[/]")
    line = disk_space_error_line(err)
    plain = Text.from_markup(line).plain
    assert "[bold]space[/]" in plain


def test_file_progress_description_escapes_markup():
    """Имя файла из Telegram идёт в Progress как description, который рендерится как markup."""
    from tg_export.exporter import file_progress_description

    desc = file_progress_description("photo_[bold]EVIL[/].jpg")
    plain = Text.from_markup(desc).plain
    assert "photo_[bold]EVIL[/].jpg" in plain


# ----- Concurrency safety in _build_status_table -----


@pytest.mark.asyncio
async def test_build_status_table_handles_concurrent_active_downloads_mutation():
    """Гонка: refresh-thread читает active_downloads, event loop его мутирует."""
    from unittest.mock import AsyncMock, MagicMock

    from rich.progress import Progress

    from tg_export.exporter import ExportStats, Exporter
    from tg_export.media import DownloadProgress

    api = AsyncMock()
    state = AsyncMock()
    config = MagicMock()
    renderer = MagicMock()
    downloader = MagicMock()
    downloader.active_downloads = {}

    exporter = Exporter(
        api=api, state=state, config=config,
        renderer=renderer, downloader=downloader, account="test",
    )

    from rich.progress import TaskID

    progress = Progress()
    main_task = progress.add_task("test", total=100)
    file_progress = Progress()
    file_tasks: dict[int, TaskID] = {}
    stats = ExportStats()
    stats.begin_chat(messages_in_db=0, messages_total=0)

    stop = threading.Event()
    errors: list[BaseException] = []

    def mutate():
        i = 0
        while not stop.is_set():
            try:
                downloader.active_downloads[i] = DownloadProgress(filename=f"f{i}.bin")
                if i > 5:
                    downloader.active_downloads.pop(i - 5, None)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
            i += 1
            time.sleep(0)

    t = threading.Thread(target=mutate, daemon=True)
    t.start()
    try:
        for _ in range(2000):
            try:
                exporter._build_status_table(
                    progress=progress, main_task=main_task,
                    file_progress=file_progress, file_tasks=file_tasks,
                    stats=stats, line1="line1", line2="line2",
                )
            except RuntimeError as e:
                errors.append(e)
                break
    finally:
        stop.set()
        t.join(timeout=1)

    assert not errors, f"Concurrent mutation broke _build_status_table: {errors!r}"


# ----- Behavioural smoke: exporter still works end-to-end with escape -----


@pytest.mark.asyncio
async def test_exporter_dry_run_with_markup_in_chat_name_does_not_corrupt_output(monkeypatch):
    """Полный путь: dry-run с именем чата, содержащим markup -- литерал должен дойти до вывода."""
    from io import StringIO
    from unittest.mock import AsyncMock, MagicMock

    from rich.console import Console

    from tg_export import exporter as exporter_mod
    from tg_export.exporter import Exporter
    from tg_export.models import Chat, ChatType

    test_console = Console(
        file=StringIO(), force_terminal=False, width=200, record=True,
    )
    monkeypatch.setattr(exporter_mod, "console", test_console)

    api = AsyncMock()
    state = AsyncMock()
    config = MagicMock()
    config.output.path = "/tmp/test"
    config.left_channels_action = "include"
    config.archived_action = "include"
    config.defaults.date_from = None
    config.defaults.date_to = None
    config.resolve_chat_config.return_value = MagicMock()
    renderer = MagicMock()
    downloader = MagicMock()
    downloader.active_downloads = {}

    exporter = Exporter(
        api=api, state=state, config=config,
        renderer=renderer, downloader=downloader, account="test",
    )

    chat = Chat(
        id=1, name="[bold red]EVIL[/] chat", type=ChatType.private_group,
        username=None, folder=None, members_count=None,
        last_message_date=None, messages_count=0,
        is_left=False, is_archived=False, is_forum=False,
        migrated_to_id=None, migrated_from_id=None, is_monoforum=False,
    )
    await exporter.run(dry_run=True, chat_list=[chat])

    output = test_console.export_text()
    assert "[bold red]EVIL[/] chat" in output, (
        f"имя чата с markup исчезло из вывода dry-run: {output!r}"
    )
