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
