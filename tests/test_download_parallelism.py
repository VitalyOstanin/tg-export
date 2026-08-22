"""Параллельность загрузок медиа внутри чата.

Настройка `concurrent_downloads` и docs/configuration.md обещают параллельные
загрузки, но `_process_media` ожидался прямо в теле цикла по сообщениям:
фактический параллелизм всегда был равен единице, а между файлами добавлялся
полный цикл «запрос -- ожидание -- ответ» к серверу Telegram. На время
скачивания итератор сообщений тоже стоял.
"""

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from tg_export.exporter import Exporter, ExportStats
from tg_export.models import Chat, ChatType, FileInfo, MediaType, Message, PhotoMedia


def _chat(chat_id: int) -> Chat:
    return Chat(
        id=chat_id,
        name="Chat",
        type=ChatType.personal,
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


def _message_with_media(msg_id: int, chat_id: int) -> Message:
    return Message(
        id=msg_id,
        chat_id=chat_id,
        date=datetime(2026, 1, 1, 12, 0),
        edited=None,
        from_id=1,
        from_name="Someone",
        text=[],
        media=PhotoMedia(
            type=MediaType.photo,
            file=FileInfo(id=msg_id, size=4, name=f"p{msg_id}.jpg", mime_type="image/jpeg", local_path=None),
            width=1,
            height=1,
        ),
        action=None,
        reply_to_msg_id=None,
        reply_to_peer_id=None,
        forwarded_from=None,
        reactions=[],
        is_outgoing=False,
        signature=None,
        via_bot_id=None,
        saved_from_chat_id=None,
        inline_buttons=None,
        topic_id=None,
        grouped_id=None,
    )


class _RecordingDownloader:
    """Загрузчик, замеряющий, сколько загрузок идёт одновременно."""

    def __init__(self, limit: int):
        self.config = MagicMock()
        self.config.concurrent_downloads = limit
        self.in_flight = 0
        self.peak = 0
        self.order = []

    async def download(self, tl_msg, media, chat_dir, chat_id=0):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        # Две уступки циклу событий: за это время соседние загрузки успевают
        # начаться, если параллельность действительно есть.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.in_flight -= 1
        self.order.append(tl_msg.id)
        return None, "no_file"


@pytest.mark.asyncio
async def test_media_downloads_overlap_within_a_chat(state, tmp_path, monkeypatch):
    chat_id = 77
    ids = list(range(110, 100, -1))
    await state.set_last_msg_id(chat_id, 100)

    async def fake_iter(cid, **kwargs):
        if "min_id" not in kwargs:
            # Фаза 2 идёт вниз от oldest_msg_id; здесь она не нужна.
            return
        min_id = kwargs["min_id"]
        for msg_id in ids:
            if msg_id <= min_id:
                continue
            yield SimpleNamespace(id=msg_id, date=datetime(2026, 1, 1, 12, 0))

    api = MagicMock()
    api.iter_messages = fake_iter
    api.client = MagicMock()
    api.client.get_messages = AsyncMock(return_value=MagicMock(total=len(ids)))

    downloader = _RecordingDownloader(limit=4)
    monkeypatch.setattr(
        "tg_export.exporter.convert_message",
        lambda tl_msg, chat_id: _message_with_media(tl_msg.id, chat_id),
    )

    exporter = Exporter(
        api=api,
        state=state,
        config=MagicMock(),
        renderer=MagicMock(),
        downloader=cast(Any, downloader),
        account="test",
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

    assert downloader.peak == 4, f"одновременных загрузок: {downloader.peak}"
    assert sorted(downloader.order) == sorted(ids)


@pytest.mark.asyncio
async def test_every_message_is_stored_despite_parallel_downloads(state, tmp_path, monkeypatch):
    """Сообщение попадает в пачку только после своей загрузки: иначе в базу
    уйдёт запись без пути к файлу."""
    chat_id = 78
    ids = list(range(110, 100, -1))
    await state.set_last_msg_id(chat_id, 100)

    async def fake_iter(cid, **kwargs):
        if "min_id" not in kwargs:
            # Фаза 2 идёт вниз от oldest_msg_id; здесь она не нужна.
            return
        min_id = kwargs["min_id"]
        for msg_id in ids:
            if msg_id <= min_id:
                continue
            yield SimpleNamespace(id=msg_id, date=datetime(2026, 1, 1, 12, 0))

    api = MagicMock()
    api.iter_messages = fake_iter
    api.client = MagicMock()
    api.client.get_messages = AsyncMock(return_value=MagicMock(total=len(ids)))

    stored = []

    class _Downloader(_RecordingDownloader):
        async def download(self, tl_msg, media, chat_dir, chat_id=0):
            await asyncio.sleep(0)
            media.file.local_path = f"/tmp/p{tl_msg.id}.jpg"
            return Path(f"/tmp/p{tl_msg.id}.jpg"), "existing"

    monkeypatch.setattr(
        "tg_export.exporter.convert_message",
        lambda tl_msg, chat_id: _message_with_media(tl_msg.id, chat_id),
    )

    exporter = Exporter(
        api=api,
        state=state,
        config=MagicMock(),
        renderer=MagicMock(),
        downloader=cast(Any, _Downloader(limit=4)),
        account="test",
    )
    original_store = state.store_messages_batch

    async def spy(batch):
        stored.extend(m.id for m in batch)
        return await original_store(batch)

    monkeypatch.setattr(state, "store_messages_batch", spy)

    chat_config = MagicMock()
    chat_config.date_from = None
    chat_config.date_to = None

    await exporter.export_chat(
        chat=_chat(chat_id),
        chat_config=chat_config,
        chat_dir=Path(tmp_path / "chat"),
        stats=ExportStats(),
    )

    assert stored == ids, stored
