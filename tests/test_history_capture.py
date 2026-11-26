"""Согласованная запись истории, Raw и очереди миниатюр."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import make_chat, make_message
from telethon import types

from tg_export.history_capture import capture_message
from tg_export.state import ExportState


def _tl(msg_id: int, *, bad_raw: bool = False):
    photo = types.Photo(
        id=99,
        access_hash=20,
        file_reference=b"reference",
        date=None,
        sizes=[
            types.PhotoSize(type="m", w=160, h=160, size=12_000),
            types.PhotoSize(type="x", w=800, h=800, size=100_000),
        ],
        dc_id=2,
    )
    return SimpleNamespace(
        id=msg_id,
        media=SimpleNamespace(photo=photo),
        to_dict=lambda: {
            "_": "Message",
            "id": msg_id,
            "date": datetime(2024, 1, 1, tzinfo=UTC),
            "bytes": object() if bad_raw else b"\x00\xff",
        },
    )


async def _count(state, table: str) -> int:
    async with state.db.execute(f"SELECT count(*) FROM {table}") as cur:
        row = await cur.fetchone()
    assert row is not None
    return row[0]


def _record(msg_id: int, tl=None):
    tl = _tl(msg_id) if tl is None else tl
    return make_message(id=msg_id, chat_id=7), capture_message(tl, account_id=42, chat_id=7)


@pytest.mark.asyncio
async def test_history_batch_writes_raw_sources_requests_and_one_static_job(tmp_path):
    """Два сообщения об одном фото дают один asset, variant и job."""
    async with ExportState(tmp_path / "state.db") as state:
        records = [_record(n) for n in (11, 12)]
        await state.store_history_batch(records)
        await state.store_history_batch(records)
        for table, expected in (
            ("messages", 2),
            ("message_raw", 2),
            ("media_assets", 1),
            ("media_sources", 2),
            ("media_variants", 1),
            ("media_requests", 2),
            ("media_jobs", 1),
        ):
            assert await _count(state, table) == expected
        async with state.db.execute("SELECT variant_key,role,thumb_type FROM media_variants") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert tuple(row) == ("static:PhotoSize:m:160x160", "static", "m")
        async with state.db.execute("SELECT raw_json FROM message_raw WHERE msg_id=11") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert json.loads(row[0])["bytes"] == {"$type": "bytes", "base64": "AP8="}
        async with state.db.execute("PRAGMA foreign_key_check") as cur:
            assert await cur.fetchall() == []


@pytest.mark.asyncio
async def test_history_batch_failure_rolls_back_all_rows(tmp_path, monkeypatch):
    """Сбой после первой записи откатывает сообщения и весь каталог."""
    async with ExportState(tmp_path / "state.db") as state:
        original = state._store_capture
        seen = 0

        async def fail_second(item):
            nonlocal seen
            seen += 1
            if seen == 2:
                raise RuntimeError("injected failure")
            await original(item)

        monkeypatch.setattr(state, "_store_capture", fail_second)
        with pytest.raises(RuntimeError, match="injected failure"):
            await state.store_history_batch([_record(n) for n in (11, 12)])
        for table in (
            "messages",
            "message_raw",
            "media_assets",
            "media_sources",
            "media_variants",
            "media_requests",
            "media_jobs",
        ):
            assert await _count(state, table) == 0


@pytest.mark.asyncio
async def test_file_registration_cannot_commit_half_written_history(tmp_path, monkeypatch):
    """Параллельный legacy commit ждёт завершения пакета Raw и каталога."""
    path = tmp_path / "state.db"
    async with ExportState(path) as state:
        entered = asyncio.Event()
        release = asyncio.Event()
        original = state._store_capture

        async def pause(item):
            entered.set()
            await release.wait()
            await original(item)

        monkeypatch.setattr(state, "_store_capture", pause)
        batch_task = asyncio.create_task(state.store_history_batch([_record(11)]))
        await asyncio.wait_for(entered.wait(), timeout=2)
        file_task = asyncio.create_task(state.register_file(99, 7, 11, 10, 10, "photo.jpg"))
        await asyncio.sleep(0)
        assert not file_task.done()
        with contextlib.closing(sqlite3.connect(path)) as reader:
            assert reader.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
        release.set()
        await asyncio.gather(batch_task, file_task)
        assert await _count(state, "messages") == 1
        assert await _count(state, "message_raw") == 1
        assert await _count(state, "files") == 1


@pytest.mark.asyncio
async def test_raw_serialization_failure_is_explicit_and_history_survives(tmp_path):
    """Неизвестное поле Raw сохраняется как raw_failed без потери сообщения."""
    capture = capture_message(_tl(11, bad_raw=True), account_id=42, chat_id=7)
    assert capture.raw_json is None and capture.error_code == "raw_serialization_failed"
    async with ExportState(tmp_path / "state.db") as state:
        await state.store_history_batch([_record(11, _tl(11, bad_raw=True))])
        assert await _count(state, "messages") == 1
        async with state.db.execute("SELECT raw_status,error_code FROM message_raw") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert tuple(row) == ("raw_failed", "raw_serialization_failed")


@pytest.mark.asyncio
async def test_no_safe_thumbnail_creates_no_download_job(tmp_path):
    """Единственный размер фото считается оригиналом и не попадает в очередь."""
    tl = _tl(11)
    tl.media.photo.sizes = [types.PhotoSize(type="x", w=800, h=800, size=100_000)]
    async with ExportState(tmp_path / "state.db") as state:
        await state.store_history_batch([_record(11, tl)])
        assert await _count(state, "media_variants") == 0
        assert await _count(state, "media_jobs") == 0
        async with state.db.execute("SELECT result,variant_id FROM media_requests") as cur:
            row = await cur.fetchone()
        assert row is not None and tuple(row) == ("no_thumbnail", None)


@pytest.mark.asyncio
async def test_history_walker_passes_preconversion_raw_to_batch(tmp_path, monkeypatch):
    """Обход истории снимает Raw до конвертации и передаёт его в пакет."""
    import tg_export.exporter as exporter_module
    from tg_export.exporter import Exporter, ExportStats

    tl = SimpleNamespace(id=11, date=datetime(2024, 1, 1, tzinfo=UTC), media=None)
    tl.to_dict = lambda: {"_": "Message", "id": 11}

    async def messages(*_args, **_kwargs):
        yield tl

    api = MagicMock()
    api.iter_messages = messages
    state = MagicMock()
    state.store_history_batch = AsyncMock()
    downloader = MagicMock()
    exporter = Exporter(api, state, MagicMock(), MagicMock(), downloader, "acc", account_id=42)

    def convert(value, chat_id):
        assert value is tl and chat_id == 7
        assert captured_before_conversion[0]
        return make_message(id=11, chat_id=7)

    original_capture = exporter_module.capture_message
    captured_before_conversion = [False]

    def capture(*args, **kwargs):
        captured_before_conversion[0] = True
        return original_capture(*args, **kwargs)

    monkeypatch.setattr(exporter_module, "capture_message", capture)
    monkeypatch.setattr(exporter_module, "convert_message", convert)
    stats = ExportStats()
    await exporter._walk_messages(
        make_chat(id=7, name="Chat"),
        tmp_path,
        stats,
        iter_kwargs={},
        before_date_from=lambda _: False,
        progress_line=lambda: "",
        keep_service=True,
        media_config=MagicMock(),
        note_message=lambda _: None,
        stored_line=lambda: "",
    )
    state.store_history_batch.assert_awaited_once()
    saved = state.store_history_batch.await_args.args[0]
    assert saved[0][1].raw_json == '{"_":"Message","id":11}'
