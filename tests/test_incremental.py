import pytest
import pytest_asyncio
from pathlib import Path
from datetime import datetime
from tg_export.state import ExportState
from tg_export.importer import scan_tdesktop_export


@pytest_asyncio.fixture
async def state(tmp_path):
    s = ExportState(tmp_path / "state.db")
    await s.open()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_incremental_uses_last_msg_id(state):
    await state.set_last_msg_id(chat_id=123, msg_id=500)
    last_id = await state.get_last_msg_id(chat_id=123)
    assert last_id == 500


@pytest.mark.asyncio
async def test_file_verification_detects_partial(state):
    await state.register_file(
        file_id=1, chat_id=123, msg_id=1,
        expected_size=10000, actual_size=5000,
        local_path="photos/photo.jpg", status="partial",
    )
    broken = await state.get_files_to_verify()
    assert len(broken) == 1


def test_scan_tdesktop_export(tmp_path):
    chat_dir = tmp_path / "chats" / "chat_001"
    photos_dir = chat_dir / "photos"
    photos_dir.mkdir(parents=True)
    (photos_dir / "photo_1@01-01-2024_10-00-00.jpg").write_bytes(b"x" * 1000)
    (photos_dir / "photo_2@01-01-2024_10-05-00.jpg").write_bytes(b"x" * 2000)

    files = scan_tdesktop_export(tmp_path)
    assert len(files) == 2
    assert files[0]["size"] == 1000
    assert files[1]["size"] == 2000
