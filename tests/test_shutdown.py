"""Поведение при завершении: отмена задач, защищённые операции, сигналы.

Второй Ctrl+C отменял все задачи цикла событий разом, включая внутреннюю
задачу asyncio.shield -- то есть ровно ту защиту, ради которой shield и
поставлен. Сам shield при этом не удерживает вызывающего: тот получает отмену
немедленно и доходит до закрытия соединения, под которым ещё идёт commit.
"""

import asyncio
import signal
import time
from unittest.mock import MagicMock

import pytest

from tg_export.exporter import Exporter
from tg_export.state import ExportState


def _exporter() -> Exporter:
    return Exporter(
        api=MagicMock(),
        state=MagicMock(),
        config=MagicMock(),
        renderer=MagicMock(),
        downloader=MagicMock(),
        account="test",
        quiet=True,
    )


@pytest.mark.asyncio
async def test_commit_completes_before_the_caller_gives_up(tmp_path, monkeypatch):
    """shield защищает саму операцию, но не удерживает вызывающего: тот уходит
    в finally и закрывает соединение, а идущий commit падает в неотловленной
    задаче с ValueError: Connection closed."""
    state = ExportState(tmp_path / "state.db")
    await state.open()
    try:
        finished = []

        async def slow_commit():
            await asyncio.sleep(0.05)
            finished.append(True)

        monkeypatch.setattr(state._db, "commit", slow_commit)

        task = asyncio.create_task(state.commit())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert finished == [True], "вызывающий ушёл раньше, чем завершился commit"
    finally:
        await state.close()


@pytest.mark.asyncio
async def test_force_shutdown_cancels_only_the_export():
    """Принудительное завершение должно снимать экспорт, а не всё подряд:
    задача, которую создал shield, обязана доработать."""
    exporter = _exporter()
    protected_done = []
    export_cancelled = []

    async def protected():
        await asyncio.sleep(0.05)
        protected_done.append(True)

    async def export():
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            export_cancelled.append(True)
            raise

    background = asyncio.create_task(protected())
    main = asyncio.create_task(export())
    await asyncio.sleep(0)

    exporter._main_task = main
    exporter._shutdown = True
    exporter._first_signal_time = time.monotonic()
    exporter._handle_shutdown(signal.SIGINT)

    try:
        await asyncio.gather(main, return_exceptions=True)
        await background
    except asyncio.CancelledError:
        pytest.fail("отменены посторонние задачи, а не только экспорт")

    assert export_cancelled == [True]
    assert protected_done == [True]


@pytest.mark.asyncio
async def test_run_survives_a_platform_without_signal_handlers(monkeypatch):
    """loop.add_signal_handler не реализован на Windows: без защиты экспорт
    падал бы там сразу на старте."""
    exporter = _exporter()
    loop = asyncio.get_running_loop()

    def unsupported(*args, **kwargs):
        raise NotImplementedError

    monkeypatch.setattr(loop, "add_signal_handler", unsupported)

    stats = await exporter.run(chat_list=None)

    assert stats is not None


@pytest.mark.asyncio
async def test_cancellation_is_not_mistaken_for_a_failed_chat(tmp_path):
    """Отмена -- не отказ одного чата, и гасить её на этом уровне нельзя.

    `_export_chat_entry` перехватывал CancelledError и возвращал False. Цикл по
    чатам выходил через break, флаг принудительной остановки оставался снятым,
    и `run` тут же запускал фазу глобальных данных: контакты, сессии, истории,
    аватары -- целую новую порцию сетевой работы уже после того, как прогон
    попросили остановить.
    """
    from unittest.mock import AsyncMock

    from tg_export.exporter import ExportStats

    state = MagicMock()
    state.cache_catalog = AsyncMock()
    exporter = _exporter()
    exporter.state = state
    exporter.export_chat = AsyncMock(side_effect=asyncio.CancelledError)
    exporter._cleanup_orphaned_files = AsyncMock()
    exporter.downloader.tdesktop_indexes = []

    chat = MagicMock(id=1, folder=None, is_left=False, is_archived=False)
    # `name` -- служебный аргумент конструктора MagicMock, задаётся отдельно.
    chat.name = "Chat"
    chat.type.value = "private"
    chat.messages_count = 1

    with pytest.raises(asyncio.CancelledError):
        await exporter._export_chat_entry(
            chat, MagicMock(), tmp_path, ExportStats(), MagicMock(), MagicMock()
        )


def test_a_second_ctrl_c_forces_the_exit_only_inside_the_window():
    """Документация обещала немедленный выход по любому повторному Ctrl+C.

    Принудительное завершение включается только внутри окна: позже второй
    сигнал -- новая просьба остановиться мягко, иначе случайное двойное
    нажатие отменяло бы чат, который в этот момент сохраняется.
    """
    from unittest.mock import MagicMock

    from tg_export.exporter import FORCE_SHUTDOWN_WINDOW_SECONDS, Exporter

    exporter = MagicMock()
    exporter._shutdown = False
    exporter._force_shutdown = False
    exporter._main_task = None
    exporter._first_signal_time = 0.0

    with pytest.MonkeyPatch.context() as mp:
        now = [1000.0]
        mp.setattr("tg_export.exporter.time.monotonic", lambda: now[0])
        Exporter._handle_shutdown(exporter, 2)
        assert exporter._shutdown and not exporter._force_shutdown

        now[0] += FORCE_SHUTDOWN_WINDOW_SECONDS + 1
        Exporter._handle_shutdown(exporter, 2)
        assert not exporter._force_shutdown, "второй сигнал вне окна прервал экспорт"

        now[0] += FORCE_SHUTDOWN_WINDOW_SECONDS - 1
        Exporter._handle_shutdown(exporter, 2)
        assert exporter._force_shutdown, "второй сигнал внутри окна не прервал экспорт"
