import pytest

from tg_export.importer import TdesktopIndex


@pytest.mark.asyncio
async def test_incremental_uses_last_msg_id(state):
    await state.set_last_msg_id(chat_id=123, msg_id=500)
    last_id = await state.get_last_msg_id(chat_id=123)
    assert last_id == 500


@pytest.mark.asyncio
async def test_file_verification_detects_partial(state):
    await state.register_file(
        file_id=1,
        chat_id=123,
        msg_id=1,
        expected_size=10000,
        actual_size=5000,
        local_path="photos/photo.jpg",
        status="partial",
    )
    broken = await state.get_files_to_verify()
    assert len(broken) == 1


def test_tdesktop_index(tmp_path):
    # Create tdesktop-like structure
    chat_dir = tmp_path / "chats" / "chat_001"
    photos_dir = chat_dir / "photos"
    photos_dir.mkdir(parents=True)
    (photos_dir / "photo_1@01-01-2024_10-00-00.jpg").write_bytes(b"x" * 1000)
    (photos_dir / "photo_2@01-01-2024_10-05-00.jpg").write_bytes(b"x" * 2000)

    # Create messages.html with chat name and media links
    html = """<div class="page_header"><div class="text bold">
Test Chat
</div></div>
<div class="message default clearfix" id="message100">
<div class="media_wrap clearfix">
<a class="photo_wrap clearfix pull_left" href="../../chats/chat_001/photos/photo_1@01-01-2024_10-00-00.jpg">
</a></div></div>
<div class="message default clearfix" id="message200">
<div class="media_wrap clearfix">
<a class="photo_wrap clearfix pull_left" href="../../chats/chat_001/photos/photo_2@01-01-2024_10-05-00.jpg">
</a></div></div>"""
    (chat_dir / "messages.html").write_text(html)

    idx = TdesktopIndex(tmp_path)
    assert idx.find_chat_dir("Test Chat") == chat_dir
    assert idx.load_chat_index("Test Chat")

    f1 = idx.find_file(100)
    assert f1 is not None
    assert f1.name == "photo_1@01-01-2024_10-00-00.jpg"
    assert f1.stat().st_size == 1000

    f2 = idx.find_file(200)
    assert f2 is not None
    assert f2.stat().st_size == 2000

    assert idx.find_file(999) is None

    idx.unload_chat_index()
    assert idx.find_file(100) is None
