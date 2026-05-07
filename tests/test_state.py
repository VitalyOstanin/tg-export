from datetime import datetime

import pytest

from tg_export.models import (
    FileInfo,
    MediaType,
    Message,
    PhotoMedia,
    TextPart,
    TextType,
)


def _make_msg(msg_id=1, chat_id=123, text="Hello", from_id=100, from_name="Test", media=None, date=None):
    return Message(
        id=msg_id,
        chat_id=chat_id,
        date=date or datetime(2024, 1, 1),
        edited=None,
        from_id=from_id,
        from_name=from_name,
        text=[TextPart(type=TextType.text, text=text)] if text else [],
        media=media,
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


@pytest.mark.asyncio
async def test_export_state_roundtrip(state):
    await state.set_last_msg_id(chat_id=123, msg_id=456)
    result = await state.get_last_msg_id(chat_id=123)
    assert result == 456


@pytest.mark.asyncio
async def test_export_state_returns_none_for_unknown_chat(state):
    result = await state.get_last_msg_id(chat_id=999)
    assert result is None


@pytest.mark.asyncio
async def test_file_registration(state):
    await state.register_file(
        file_id=100,
        chat_id=123,
        msg_id=1,
        expected_size=5000,
        actual_size=5000,
        local_path="photos/photo.jpg",
        status="done",
    )
    info = await state.get_file(file_id=100, chat_id=123)
    assert info["expected_size"] == 5000
    assert info["status"] == "done"


@pytest.mark.asyncio
async def test_message_store_and_load(state):
    msg = _make_msg(msg_id=1, chat_id=123, text="Привет мир", from_name="Иван")
    await state.store_message(msg)
    messages = await state.load_messages(chat_id=123)
    assert len(messages) == 1
    assert messages[0].from_name == "Иван"
    assert messages[0].text[0].text == "Привет мир"


@pytest.mark.asyncio
async def test_message_store_multiple_and_order(state):
    await state.store_message(_make_msg(msg_id=2, text="Second"))
    await state.store_message(_make_msg(msg_id=1, text="First"))
    messages = await state.load_messages(chat_id=123)
    assert len(messages) == 2
    assert messages[0].text[0].text == "First"
    assert messages[1].text[0].text == "Second"


@pytest.mark.asyncio
async def test_message_with_media_roundtrip(state):
    media = PhotoMedia(
        type=MediaType.photo,
        file=FileInfo(id=1, size=1000, name="p.jpg", mime_type="image/jpeg", local_path=None),
        width=800,
        height=600,
    )
    msg = _make_msg(msg_id=1, media=media, text="")
    await state.store_message(msg)
    loaded = (await state.load_messages(chat_id=123))[0]
    assert isinstance(loaded.media, PhotoMedia)
    assert loaded.media.width == 800
    assert loaded.media.file is not None
    assert loaded.media.file.name == "p.jpg"


@pytest.mark.asyncio
async def test_search_by_text(state):
    await state.store_message(_make_msg(msg_id=1, text="Привет"))
    await state.store_message(_make_msg(msg_id=2, text="Мир"))
    await state.store_message(_make_msg(msg_id=3, text="Привет мир"))
    results = await state.search_messages(chat_id=123, text_query="Привет")
    assert len(results) == 2


@pytest.mark.asyncio
async def test_search_by_media_type(state):
    media = PhotoMedia(
        type=MediaType.photo,
        file=FileInfo(id=1, size=1000, name="p.jpg", mime_type="image/jpeg", local_path=None),
        width=800,
        height=600,
    )
    await state.store_message(_make_msg(msg_id=1, media=media, text=""))
    await state.store_message(_make_msg(msg_id=2, text="no media"))
    results = await state.search_messages(chat_id=123, media_type="photo")
    assert len(results) == 1
    assert results[0].id == 1


@pytest.mark.asyncio
async def test_search_by_from_id(state):
    await state.store_message(_make_msg(msg_id=1, from_id=100, text="A"))
    await state.store_message(_make_msg(msg_id=2, from_id=200, text="B"))
    results = await state.search_messages(chat_id=123, from_id=200)
    assert len(results) == 1
    assert results[0].text[0].text == "B"


@pytest.mark.asyncio
async def test_state_lock_prevents_second_open(tmp_path):
    from tg_export.state import ExportState, StateLockError

    db = tmp_path / "lock_test.db"
    s1 = ExportState(db_path=db)
    await s1.open()
    s2 = ExportState(db_path=db)
    try:
        with pytest.raises(StateLockError):
            await s2.open()
    finally:
        await s1.close()
    # После close() второй экземпляр должен открыться без ошибки.
    s3 = ExportState(db_path=db)
    await s3.open()
    await s3.close()


@pytest.mark.asyncio
async def test_verify_files_finds_partial(state):
    await state.register_file(
        file_id=100,
        chat_id=123,
        msg_id=1,
        expected_size=5000,
        actual_size=3000,
        local_path="photos/photo.jpg",
        status="partial",
    )
    broken = await state.get_files_to_verify()
    assert len(broken) == 1
    assert broken[0]["file_id"] == 100
