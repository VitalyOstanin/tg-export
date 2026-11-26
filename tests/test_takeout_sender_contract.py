"""Проверка границы sender Telethon без аккаунта и сети."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from telethon import TelegramClient
from telethon.errors import FileMigrateError
from telethon.sessions import MemorySession
from telethon.tl import functions, types


class RecordingSender:
    def __init__(self, *, migrate_once: bool = False) -> None:
        self.requests: list[Any] = []
        self.migrate_once = migrate_once

    def send(self, request: Any, ordered: bool = False) -> asyncio.Future[Any]:
        self.requests.append(request)
        result: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        if self.migrate_once:
            self.migrate_once = False
            result.set_exception(FileMigrateError(request, capture=2))
        elif isinstance(request, functions.InvokeWithTakeoutRequest):
            result.set_result(object())
        else:
            result.set_result(types.upload.File(type=types.storage.FileJpeg(), mtime=0, bytes=b"x"))
        return result


def _client_and_senders(monkeypatch: pytest.MonkeyPatch):
    session = MemorySession()
    session.set_dc(1, "127.0.0.1", 443)
    session.takeout_id = 123
    client = TelegramClient(session, 12345, "a" * 32)
    main = RecordingSender()
    media = RecordingSender()
    client._sender = cast(Any, main)
    borrowed: list[int] = []
    returned: list[RecordingSender] = []

    async def borrow(dc_id: int) -> RecordingSender:
        borrowed.append(dc_id)
        return media

    async def release(sender: RecordingSender) -> None:
        returned.append(sender)

    monkeypatch.setattr(client, "_borrow_exported_sender", borrow)
    monkeypatch.setattr(client, "_return_exported_sender", release)
    return client, main, media, borrowed, returned


@pytest.mark.asyncio
@pytest.mark.parametrize("dc_id", [1, 2], ids=["main-dc", "media-dc"])
async def test_takeout_proxy_sends_get_file_without_wrapper(monkeypatch: pytest.MonkeyPatch, dc_id: int):
    """Публичный файловый API обходит __call__ proxy на обоих sender."""
    client, main, media, borrowed, returned = _client_and_senders(monkeypatch)
    takeout = client.takeout(finalize=False)

    # Control: a normal proxy call really does add the takeout envelope.
    await takeout(functions.updates.GetStateRequest())
    assert len(main.requests) == 1
    assert isinstance(main.requests[0], functions.InvokeWithTakeoutRequest)
    assert main.requests[0].takeout_id == 123
    assert isinstance(main.requests[0].query, functions.updates.GetStateRequest)

    location = types.InputDocumentFileLocation(id=1, access_hash=2, file_reference=b"ref", thumb_size="m")
    assert await takeout.download_file(location, bytes, file_size=1, dc_id=dc_id) == b"x"

    file_sender = main if dc_id == 1 else media
    assert len(main.requests) == (2 if dc_id == 1 else 1)
    assert len(media.requests) == (0 if dc_id == 1 else 1)
    file_request = file_sender.requests[-1]
    assert isinstance(file_request, functions.upload.GetFileRequest)
    assert isinstance(file_request.location, types.InputDocumentFileLocation)
    assert file_request.location.thumb_size == "m"
    assert borrowed == ([] if dc_id == 1 else [2])
    assert returned == ([] if dc_id == 1 else [media])


@pytest.mark.asyncio
async def test_takeout_proxy_file_migration_still_sends_unwrapped_rpc(monkeypatch: pytest.MonkeyPatch):
    client, main, media, borrowed, returned = _client_and_senders(monkeypatch)
    main.migrate_once = True
    takeout = client.takeout(finalize=False)
    location = types.InputDocumentFileLocation(id=1, access_hash=2, file_reference=b"ref", thumb_size="m")

    assert await takeout.download_file(location, bytes, file_size=1, dc_id=1) == b"x"
    assert len(main.requests) == len(media.requests) == 1
    assert all(
        isinstance(request, functions.upload.GetFileRequest) for request in [*main.requests, *media.requests]
    )
    assert borrowed == [2]
    assert returned == [media]


@pytest.mark.asyncio
@pytest.mark.parametrize("dc_id", [1, 2], ids=["main-dc", "media-dc"])
async def test_download_media_thumbnail_uses_same_sender_path(monkeypatch: pytest.MonkeyPatch, dc_id: int):
    """Высокоуровневый media API отправляет тот же сырой GetFile."""
    client, main, media, _, _ = _client_and_senders(monkeypatch)
    if dc_id == 2:
        main.migrate_once = True
    document = types.Document(
        id=1,
        access_hash=2,
        file_reference=b"ref",
        date=None,
        mime_type="video/mp4",
        size=10_000,
        dc_id=dc_id,
        attributes=[],
        thumbs=[types.PhotoSize(type="m", w=10, h=10, size=1)],
    )

    takeout = cast(Any, client.takeout(finalize=False))
    result = await takeout.download_media(document, file=bytes, thumb="m")

    assert result == b"x"
    assert len(main.requests) == 1
    assert len(media.requests) == (0 if dc_id == 1 else 1)
    for request in [*main.requests, *media.requests]:
        assert isinstance(request, functions.upload.GetFileRequest)
        assert isinstance(request.location, types.InputDocumentFileLocation)
        assert request.location.thumb_size == "m"
