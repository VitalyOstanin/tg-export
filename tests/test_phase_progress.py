"""Как двигаются признаки прогресса фаз между запусками экспорта.

Фаза 1 идёт от новых сообщений к старым, поэтому продвинутый на максимум
указатель last_msg_id означает «всё выше этого id обработано». Прерывание
посреди фазы 1 это условие нарушает, и пропущенный интервал уже никто не
заберёт: фаза 2 идёт вниз от oldest_msg_id и в него не заходит.

Признак full_history означает «ниже уже ничего нет» и навсегда отключает фазу
2, поэтому он вправе появиться только после исчерпанного итератора.
"""

from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import make_chat, make_message

from tg_export.exporter import Exporter, ExportStats
from tg_export.models import Chat, Message


def _message(msg_id: int, chat_id: int) -> Message:
    return make_message(
        id=msg_id, chat_id=chat_id, date=datetime(2026, 1, 1, 12, 0), from_id=1, from_name="Someone"
    )


def _chat(chat_id: int) -> Chat:
    return make_chat(id=chat_id)


def _exporter(state, ids_newest_first, *, stop_after=None, monkeypatch=None, date_of=None):
    """Экспортёр поверх поддельного чата, проходы которого отдают заданные идентификаторы.

    stop_after: поднять признак остановки после выдачи такого числа сообщений,
    изображая Ctrl+C посреди фазы 1.
    date_of: дата сообщения по его идентификатору -- для прогона, ограниченного
    значением date_from.
    """
    exporter_holder = {}

    async def fake_iter(chat_id, **kwargs):
        min_id = kwargs.get("min_id", 0)
        served = 0
        for msg_id in ids_newest_first:
            if msg_id <= min_id:
                continue
            when = date_of(msg_id) if date_of else datetime(2026, 1, 1, 12, 0)
            yield SimpleNamespace(id=msg_id, date=when)
            served += 1
            if stop_after is not None and served >= stop_after:
                exporter_holder["exporter"]._shutdown = True

    api = MagicMock()
    api.iter_messages = fake_iter
    api.client = MagicMock()
    api.client.get_messages = AsyncMock(return_value=MagicMock(total=len(ids_newest_first)))

    config = MagicMock()
    renderer = MagicMock()
    downloader = AsyncMock()
    exporter = Exporter(
        api=api,
        state=state,
        config=config,
        renderer=renderer,
        downloader=downloader,
        account="test",
    )
    exporter_holder["exporter"] = exporter

    if monkeypatch is not None:
        monkeypatch.setattr(
            "tg_export.exporter.convert_message",
            lambda tl_msg, chat_id: _message(tl_msg.id, chat_id),
        )
    return exporter


@pytest.mark.asyncio
async def test_phase1_interrupted_does_not_advance_last_msg_id(state, tmp_path, monkeypatch):
    # Прежний указатель 100, в чате есть 101..110. Прерывание после двух
    # сообщений (110, 109): интервал 101..108 не выгружен, поэтому указатель
    # обязан остаться на 100 -- иначе эти сообщения не заберёт уже никто.
    chat_id = 42
    await state.set_last_msg_id(chat_id, 100)

    exporter = _exporter(
        state,
        ids_newest_first=list(range(110, 100, -1)),
        stop_after=2,
        monkeypatch=monkeypatch,
    )
    chat_config = MagicMock()
    chat_config.date_from = None
    chat_config.date_to = None

    await exporter.export_chat(
        chat=_chat(chat_id),
        chat_config=chat_config,
        chat_dir=Path(tmp_path / "chat"),
        stats=ExportStats(),
    )

    assert await state.get_last_msg_id(chat_id) == 100


@pytest.mark.asyncio
async def test_phase1_completed_advances_last_msg_id(state, tmp_path, monkeypatch):
    # Контроль: без прерывания фаза 1 проходит целиком и указатель двигается.
    chat_id = 43
    await state.set_last_msg_id(chat_id, 100)

    exporter = _exporter(
        state,
        ids_newest_first=list(range(110, 100, -1)),
        monkeypatch=monkeypatch,
    )
    chat_config = MagicMock()
    chat_config.date_from = None
    chat_config.date_to = None

    await exporter.export_chat(
        chat=_chat(chat_id),
        chat_config=chat_config,
        chat_dir=Path(tmp_path / "chat"),
        stats=ExportStats(),
    )

    assert await state.get_last_msg_id(chat_id) == 110


@pytest.mark.asyncio
async def test_last_msg_id_never_moves_backwards(state):
    # Фаза 2 пишет last_msg_id из копии, прочитанной в начале export_chat.
    # Если за это время указатель ушёл вперёд (соседний процесс, фаза 1),
    # запись не должна откатывать его назад.
    chat_id = 44
    await state.set_last_msg_id(chat_id, 500)
    await state.commit_phase_progress(
        chat_id,
        last_msg_id=300,
        oldest_msg_id=10,
        full_history=False,
        messages_count=5,
    )

    assert await state.get_last_msg_id(chat_id) == 500


@pytest.mark.asyncio
async def test_reset_rewinds_the_pointer(state):
    # Монотонность обычной записи не должна мешать намеренному сбросу:
    # `state reset` обязан отматывать указатель назад.
    chat_id = 45
    await state.set_last_msg_id(chat_id, 900)

    await state.reset_chat_progress(chat_id)

    assert await state.get_last_msg_id(chat_id) == 0


@pytest.mark.asyncio
async def test_a_pass_stopped_by_date_from_does_not_close_the_history(state, tmp_path, monkeypatch):
    # В чате есть 110..101, и сообщения ниже 105 старше границы date_from:
    # фаза 2 останавливается на первом из них, хотя чат ниже границы не
    # исчерпан. Признак full_history=1 отключил бы фазу 2 навсегда, и снятие
    # границы уже не вернуло бы эти сообщения -- помог бы только `state reset`.
    chat_id = 46
    exporter = _exporter(
        state,
        ids_newest_first=list(range(110, 100, -1)),
        date_of=lambda msg_id: datetime(2026, 1, 1, 12, 0) if msg_id >= 105 else datetime(2025, 12, 1, 12, 0),
        monkeypatch=monkeypatch,
    )
    chat_config = MagicMock()
    chat_config.date_from = date(2026, 1, 1)
    chat_config.date_to = None

    await exporter.export_chat(
        chat=_chat(chat_id),
        chat_config=chat_config,
        chat_dir=Path(tmp_path / "chat"),
        stats=ExportStats(),
    )

    chat_state = await state.get_chat_state(chat_id)
    assert chat_state is not None
    assert chat_state["full_history"] == 0


@pytest.mark.asyncio
async def test_an_exhausted_pass_closes_the_history(state, tmp_path, monkeypatch):
    # Контроль: без границы по дате итератор доходит до конца, и только это
    # даёт право утверждать, что ниже в чате ничего нет.
    chat_id = 47
    exporter = _exporter(
        state,
        ids_newest_first=list(range(110, 100, -1)),
        monkeypatch=monkeypatch,
    )
    chat_config = MagicMock()
    chat_config.date_from = None
    chat_config.date_to = None

    await exporter.export_chat(
        chat=_chat(chat_id),
        chat_config=chat_config,
        chat_dir=Path(tmp_path / "chat"),
        stats=ExportStats(),
    )

    chat_state = await state.get_chat_state(chat_id)
    assert chat_state is not None
    assert chat_state["full_history"] == 1
