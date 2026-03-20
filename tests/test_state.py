import pytest
import pytest_asyncio
from pathlib import Path
from datetime import datetime
from tg_export.state import ExportState


@pytest_asyncio.fixture
async def state(tmp_path):
    s = ExportState(tmp_path / "state.db")
    await s.open()
    yield s
    await s.close()


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
        file_id=100, chat_id=123, msg_id=1,
        expected_size=5000, actual_size=5000,
        local_path="photos/photo.jpg", status="done",
    )
    info = await state.get_file(file_id=100, chat_id=123)
    assert info["expected_size"] == 5000
    assert info["status"] == "done"


@pytest.mark.asyncio
async def test_message_store_and_load(state):
    await state.store_message(chat_id=123, msg_id=1, data='{"id": 1}')
    await state.store_message(chat_id=123, msg_id=2, data='{"id": 2}')
    messages = await state.load_messages(chat_id=123)
    assert len(messages) == 2
    assert messages[0] == '{"id": 1}'


@pytest.mark.asyncio
async def test_verify_files_finds_partial(state):
    await state.register_file(
        file_id=100, chat_id=123, msg_id=1,
        expected_size=5000, actual_size=3000,
        local_path="photos/photo.jpg", status="partial",
    )
    broken = await state.get_files_to_verify()
    assert len(broken) == 1
    assert broken[0]["file_id"] == 100
