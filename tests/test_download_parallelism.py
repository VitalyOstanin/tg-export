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
from conftest import make_chat, make_message

from tg_export.exporter import Exporter, ExportStats
from tg_export.models import Chat, FileInfo, MediaType, Message, PhotoMedia


def _chat(chat_id: int) -> Chat:
    return make_chat(id=chat_id)


def _message_with_media(msg_id: int, chat_id: int) -> Message:
    return make_message(
        id=msg_id,
        chat_id=chat_id,
        date=datetime(2026, 1, 1, 12, 0),
        from_id=1,
        from_name="Someone",
        media=PhotoMedia(
            type=MediaType.photo,
            file=FileInfo(id=msg_id, size=4, name=f"p{msg_id}.jpg", mime_type="image/jpeg", local_path=None),
            width=1,
            height=1,
        ),
    )


def _message_without_media(msg_id: int, chat_id: int) -> Message:
    """Обычное текстовое сообщение: скачивать нечего."""
    msg = _message_with_media(msg_id, chat_id)
    msg.media = None
    return msg


class _RecordingDownloader:
    """Загрузчик, замеряющий, сколько загрузок идёт одновременно."""

    def __init__(self, limit: int):
        self.config = MagicMock()
        self.config.concurrent_downloads = limit
        self.in_flight = 0
        self.peak = 0
        self.order = []

    async def download(
        self, tl_msg, media, chat_dir, chat_id=0, media_config=None
    ) -> tuple[Path | None, str]:
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
    paths = []

    class _Downloader(_RecordingDownloader):
        async def download(
            self, tl_msg, media, chat_dir, chat_id=0, media_config=None
        ) -> tuple[Path | None, str]:
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
        # Путь появляется только после завершения загрузки, поэтому его
        # наличие в момент попадания в пачку -- и есть проверяемый инвариант.
        # Один состав идентификаторов к нему нечувствителен: порядок задаёт
        # очередь и он тот же, дождались загрузки или нет.
        paths.extend(m.media.file.local_path for m in batch if m.media and m.media.file)
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
    assert paths and all(paths), f"в базу ушли записи без пути к файлу: {paths}"


@pytest.mark.asyncio
async def test_downloads_still_overlap_when_media_is_sparse(state, tmp_path, monkeypatch):
    """Окно конвейера обязано считать загрузки, а не сообщения.

    Задача заводилась на каждое сообщение, включая текстовые, и занимала место
    в окне. Как только очередь набирала `concurrent_downloads` элементов, цикл
    вставал на ожидании самой старой задачи, а за ней в окне стояли уже
    завершённые текстовые сообщения -- то есть в это время не шло ни одной
    другой загрузки. При медиа реже, чем одно на `concurrent_downloads`
    сообщений (обычный текстовый чат с редкими фотографиями), параллельность
    вырождалась в единицу.
    """
    chat_id = 79
    every = 8
    ids = list(range(200, 100, -1))
    await state.set_last_msg_id(chat_id, 100)

    async def fake_iter(cid, **kwargs):
        if "min_id" not in kwargs:
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
        lambda tl_msg, chat_id: (
            _message_with_media(tl_msg.id, chat_id)
            if tl_msg.id % every == 0
            else _message_without_media(tl_msg.id, chat_id)
        ),
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

    assert downloader.peak == 4, f"одновременных загрузок при разреженном медиа: {downloader.peak}"


@pytest.mark.asyncio
async def test_the_window_is_measured_without_walking_the_queue(tmp_path):
    """Число загрузок в полёте считалось обходом всей очереди на каждое сообщение.

    Пока головная загрузка идёт, сообщения без медиа копятся за ней, и каждое
    следующее стоило обхода накопленной очереди -- то есть квадратичной работы
    на серии таких сообщений.
    """
    from collections import deque
    from unittest.mock import MagicMock

    from tg_export.exporter import _MediaPipeline

    class CountingDeque(deque):
        """Очередь, считающая, сколько элементов посетили обходы."""

        visited = 0

        def __iter__(self):
            for item in super().__iter__():
                type(self).visited += 1
                yield item

    pipeline = _MediaPipeline(
        MagicMock(), tmp_path, MagicMock(), chat_id=1, limit=3, media_config=MagicMock()
    )
    head = asyncio.create_task(asyncio.sleep(3600))
    pipeline._pending = CountingDeque([(head, MagicMock())])

    messages = 300
    for _ in range(messages):
        msg = MagicMock()
        msg.media = None
        await pipeline.submit(msg, MagicMock())

    head.cancel()

    assert CountingDeque.visited <= 3 * messages, (
        f"обходов очереди {CountingDeque.visited} на {messages} сообщений"
    )


@pytest.mark.asyncio
async def test_profile_photos_are_downloaded_within_the_configured_window(monkeypatch):
    """Профильные фото и истории качались строго по одному.

    Экспорт чатов ради той же задачи держит окно `concurrent_downloads`, а
    фаза общих данных ждала каждый файл до перехода к следующему: сотня
    историй превращалась в сотню обменов подряд, хотя настройка разрешает
    несколько сразу.
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from tg_export.exporter import Exporter

    running = 0
    peak = 0

    async def download_media(media, file):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        try:
            await asyncio.sleep(0.01)
            return f"{file}.jpg"
        finally:
            running -= 1

    api = MagicMock()
    api.client.download_media = download_media

    async def userpics():
        for _ in range(6):
            yield MagicMock(date=None)

    api.iter_userpics = userpics

    config = MagicMock()
    downloader = MagicMock()
    downloader.config.concurrent_downloads = 3

    exporter = Exporter(
        api=api,
        state=AsyncMock(),
        config=config,
        renderer=MagicMock(),
        downloader=downloader,
        account="acc",
        quiet=True,
    )
    exporter.renderer.output_dir = Path("/tmp")
    monkeypatch.setattr(Path, "mkdir", lambda *args, **kwargs: None)

    await exporter._export_userpics()

    assert peak > 1, "фото по-прежнему качаются по одному"
    assert peak <= 3, f"окно шире настроенного: {peak}"
