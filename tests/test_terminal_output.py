"""Тесты вывода в терминал: markup-эскейп и потокобезопасность.

Два класса отказов, каждый из которых уже случался:

- markup-инъекция: имя чата, имя файла или текст исключения попадают в строку,
  которую rich разбирает как разметку. Квадратные скобки в имени -- обычное дело,
  и `[draft]report.txt` печатался как `report.txt`, а незакрытая скобка роняла
  вывод целиком;
- гонка потоков: живой статус перерисовывается отдельным потоком rich.Live,
  тогда как счётчики и словарь активных загрузок меняет event loop. Чтение без
  снимка давало то `RuntimeError: dictionary changed size during iteration`, то
  строку статуса, где итог одного чата смешан с началом следующего.
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


def test_chat_error_line_includes_chat_id_when_provided():
    # Why: при NOT NULL constraint failed (или иной БД-ошибке) нужно знать
    # chat_id, чтобы вручную поправить запись в export_state.
    from tg_export.exporter import chat_error_line

    err = ValueError("boom")
    line = chat_error_line("Alice", err, chat_id=12345)
    plain = Text.from_markup(line).plain
    assert "Alice" in plain
    assert "12345" in plain


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

    from tg_export.exporter import Exporter, ExportStats, StatusView
    from tg_export.media import DownloadProgress

    api = AsyncMock()
    state = AsyncMock()
    config = MagicMock()
    renderer = MagicMock()
    downloader = MagicMock()
    downloader.active_downloads = {}

    exporter = Exporter(
        api=api,
        state=state,
        config=config,
        renderer=renderer,
        downloader=downloader,
        account="test",
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
            except Exception as e:  # поток отрисовки не должен падать
                errors.append(e)
            i += 1
            time.sleep(0)

    t = threading.Thread(target=mutate, daemon=True)
    t.start()
    try:
        for _ in range(2000):
            try:
                exporter._build_status_table(
                    progress=progress,
                    main_task=main_task,
                    file_progress=file_progress,
                    file_tasks=file_tasks,
                    stats=stats,
                    status=StatusView(stats, start_time=time.monotonic()),
                )
            except RuntimeError as e:
                errors.append(e)
                break
    finally:
        stop.set()
        t.join(timeout=1)

    assert not errors, f"Одновременное изменение сломало _build_status_table: {errors!r}"


# ----- Behavioural smoke: exporter still works end-to-end with escape -----


@pytest.mark.asyncio
async def test_exporter_dry_run_with_markup_in_chat_name_does_not_corrupt_output(monkeypatch, tmp_path):
    """Полный путь: dry-run с именем чата, содержащим markup -- литерал должен дойти до вывода."""
    from io import StringIO
    from unittest.mock import AsyncMock, MagicMock

    from rich.console import Console

    from tg_export import exporter as exporter_mod
    from tg_export.exporter import Exporter
    from tg_export.models import Chat, ChatType

    test_console = Console(
        file=StringIO(),
        force_terminal=False,
        width=200,
        record=True,
    )
    monkeypatch.setattr(exporter_mod, "console", test_console)

    api = AsyncMock()
    state = AsyncMock()
    config = MagicMock()
    config.output.path = str(tmp_path / "out")
    config.left_channels_action = "include"
    config.archived_action = "include"
    config.defaults.date_from = None
    config.defaults.date_to = None
    config.resolve_chat_config.return_value = MagicMock()
    renderer = MagicMock()
    downloader = MagicMock()
    downloader.active_downloads = {}

    exporter = Exporter(
        api=api,
        state=state,
        config=config,
        renderer=renderer,
        downloader=downloader,
        account="test",
    )

    chat = Chat(
        id=1,
        name="[bold red]EVIL[/] chat",
        type=ChatType.private_group,
        username=None,
        folder=None,
        members_count=None,
        last_message_date=None,
        messages_count=0,
        is_left=False,
        is_archived=False,
        is_forum=False,
        migrated_to_id=None,
        migrated_from_id=None,
        is_monoforum=False,
    )
    await exporter.run(dry_run=True, chat_list=[chat])

    output = test_console.export_text()
    assert "[bold red]EVIL[/] chat" in output, f"имя чата с markup исчезло из вывода dry-run: {output!r}"


def test_main_progress_never_mixes_a_new_chat_with_the_previous_snapshot():
    """Поток отрисовки видел новый messages_in_db рядом со старым снимком.

    `begin_chat` присваивал поля по одному: сначала счётчики начала чата,
    потом снимок предыдущих значений. Между этими присваиваниями поток
    обновления Live читал новое число уже выгруженных сообщений вместе со
    снимком прошлого чата и показывал `completed` в разы больше `total`.

    Смена чата воспроизводится ровно в окне гонки — между двумя чтениями
    снимка внутри `chat_view` — вместо перебора наудачу в течение двух секунд:
    вердикт перебора зависел от планировщика, а на исправной сборке он всегда
    стоил полные две секунды и занятое ядро.
    """
    from tg_export.exporter import ChatCounters, ChatStart, ExportStats

    class _StatsSwitchingChatMidRead(ExportStats):
        """Счётчики, у которых чат сменяется между чтениями снимка.

        Первое чтение отдаёт снимок прошлого чата, каждое следующее -- снимок
        нового: это и есть состояние, которое поток отрисовки застаёт, попав
        между публикацией снимка и чтением счётчиков.
        """

        def arm(self, following: ChatStart) -> None:
            self._following = following
            self._reads = 0

        # Поле dataclass подменяется свойством намеренно: именно так
        # воспроизводится смена снимка между двумя его чтениями.
        @property
        def _chat_start(self) -> ChatStart:
            following = getattr(self, "_following", None)
            if following is None:
                return self._published
            self._reads += 1
            return self._published if self._reads == 1 else following

        @_chat_start.setter
        def _chat_start(self, value: ChatStart) -> None:  # pyright: ignore[reportIncompatibleVariableOverride]
            self._published = value

    stats = _StatsSwitchingChatMidRead()
    stats.begin_chat(messages_in_db=0, messages_total=1000)
    stats.messages_exported = 1000
    stats.arm(
        ChatStart(
            counters=ChatCounters(messages_exported=1000),
            messages_in_db=0,
            messages_total=10,
            started_at=time.monotonic(),
        )
    )

    view = stats.chat_view()

    assert (view.messages_total, view.counters.messages_exported) == (10, 0), (
        f"в одном виде смешаны разные чаты: {view}"
    )


def test_one_frame_of_the_live_display_reads_one_snapshot_of_the_chat():
    """Кадр собирался тремя независимыми снимками -- по одному на часть.

    Снимок `ChatStart` заведён ради того, чтобы читатель из потока Live не
    увидел счётчики нового чата рядом со счётчиками предыдущего. Внутри одного
    `chat_view()` это выполнялось, но кадр складывался из трёх вызовов: полоса
    прогресса, первая строка состояния и вторая. Между ними `begin_chat`
    публиковал новый чат, и в одном кадре оказывались полоса нового чата и
    счётчики старого -- та же рассогласованность уровнем выше.
    """
    from unittest.mock import AsyncMock, MagicMock

    from rich.progress import Progress, TaskID

    from tg_export.exporter import Exporter, ExportStats, StatusView

    downloader = MagicMock()
    downloader.snapshot_active_downloads = MagicMock(return_value={})
    exporter = Exporter(
        api=AsyncMock(),
        state=AsyncMock(),
        config=MagicMock(),
        renderer=MagicMock(),
        downloader=downloader,
        account="test",
    )

    stats = ExportStats()
    stats.begin_chat(messages_in_db=0, messages_total=10)
    views = []
    original = stats.chat_view

    def counted():
        view = original()
        views.append(view)
        return view

    stats.chat_view = counted

    progress = Progress()
    exporter._build_status_table(
        progress=progress,
        main_task=progress.add_task("test", total=10),
        file_progress=Progress(),
        file_tasks=dict[int, TaskID](),
        stats=stats,
        status=StatusView(stats, start_time=time.monotonic()),
    )

    assert len(views) == 1, f"кадр собран из {len(views)} снимков чата"


def test_a_long_line_stays_one_line_when_the_output_is_not_a_terminal():
    """Вне терминала rich брал ширину 80 и всё равно переносил строки.

    Диагностика и журнал идут в stderr, а штатный запуск по расписанию --
    `tg-export run 2>> export.log`. Путь в сообщении рвался по границе 80
    колонок посередине имени файла, строка добивалась пробелами и превращалась
    в четыре: `grep` по пути ничего не находил, а сборщик журналов получал
    четыре записи вместо одной.
    """
    import io

    from tg_export.console import make_console

    buffer = io.StringIO()
    console = make_console(buffer, is_terminal=False)
    path = "/home/user/.config/tg-export/" + "sub/" * 20 + "account.yaml"

    console.print(f"{path} has too-permissive mode 664")

    printed = buffer.getvalue()
    assert printed.count("\n") == 1, f"строка разбита на части: {printed!r}"
    assert path in printed, f"путь разорван переносом: {printed!r}"


def test_the_log_outside_a_terminal_is_written_line_per_record():
    """`RichHandler` вне терминала оформляет запись журнала в колонки.

    Колонки времени и имени логгера съедают ширину, остаток переносится, и
    одна запись журнала занимает несколько строк файла. Вне терминала журнал
    пишется обычным обработчиком: запись -- одна строка любой длины.
    """
    import logging

    from rich.logging import RichHandler

    from tg_export.cli import log_handler

    assert isinstance(log_handler(is_terminal=True), RichHandler)

    plain = log_handler(is_terminal=False)
    assert isinstance(plain, logging.StreamHandler)
    assert not isinstance(plain, RichHandler)
