"""Перекачивание битых файлов не должно уничтожать то, что уже есть.

Прежний порядок был «удалить, потом качать»: обрыв связи или Ctrl+C между
этими шагами оставлял файл удалённым, а запись в БД — со старым путём и
статусом partial.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


def _entry(path: Path) -> dict:
    return {
        "file_id": 1,
        "chat_id": 100,
        "msg_id": 200,
        "expected_size": 1000,
        "actual_size": 10,
        "local_path": str(path),
        "status": "partial",
    }


@pytest.mark.asyncio
async def test_failed_redownload_keeps_the_existing_file(tmp_path):
    from tg_export.cli import _redownload_broken_file

    existing = tmp_path / "photo.jpg"
    existing.write_bytes(b"partial")

    api = MagicMock()
    api.client.get_messages = AsyncMock(return_value=MagicMock(media=object()))
    api.download_media = AsyncMock(side_effect=RuntimeError("connection lost"))
    state = AsyncMock()

    with pytest.raises(RuntimeError):
        await _redownload_broken_file(api, state, _entry(existing))

    assert existing.read_bytes() == b"partial"
    state.register_file.assert_not_called()


@pytest.mark.asyncio
async def test_empty_download_keeps_the_existing_file(tmp_path):
    from tg_export.cli import _redownload_broken_file

    existing = tmp_path / "photo.jpg"
    existing.write_bytes(b"partial")

    api = MagicMock()
    api.client.get_messages = AsyncMock(return_value=MagicMock(media=object()))
    api.download_media = AsyncMock(return_value=None)
    state = AsyncMock()

    assert await _redownload_broken_file(api, state, _entry(existing)) is False
    assert existing.read_bytes() == b"partial"
    state.register_file.assert_not_called()


@pytest.mark.asyncio
async def test_successful_redownload_replaces_the_file(tmp_path):
    from tg_export.cli import _redownload_broken_file

    existing = tmp_path / "photo.jpg"
    existing.write_bytes(b"partial")

    async def fake_download(tl_msg, target_dir, *args, **kwargs):
        out = Path(target_dir) / "photo.jpg"
        out.write_bytes(b"complete-content")
        return str(out)

    api = MagicMock()
    api.client.get_messages = AsyncMock(return_value=MagicMock(media=object()))
    api.download_media = AsyncMock(side_effect=fake_download)
    state = AsyncMock()

    assert await _redownload_broken_file(api, state, _entry(existing)) is True
    assert existing.read_bytes() == b"complete-content"
    assert state.register_file.await_count == 1
    kwargs = state.register_file.await_args.kwargs
    assert kwargs["status"] == "done"
    assert kwargs["actual_size"] == len(b"complete-content")
    # После замены во временном каталоге ничего не остаётся.
    assert [p.name for p in tmp_path.iterdir()] == ["photo.jpg"]


@pytest.mark.asyncio
async def test_missing_message_is_skipped_without_touching_the_file(tmp_path):
    from tg_export.cli import _redownload_broken_file

    existing = tmp_path / "photo.jpg"
    existing.write_bytes(b"partial")

    api = MagicMock()
    api.client.get_messages = AsyncMock(return_value=None)
    api.download_media = AsyncMock()
    state = AsyncMock()

    assert await _redownload_broken_file(api, state, _entry(existing)) is False
    assert existing.read_bytes() == b"partial"
    api.download_media.assert_not_called()


def test_stale_staging_dirs_are_cleaned(tmp_path):
    from tg_export.cli import _clean_verify_staging

    stale = tmp_path / "chat" / ".tg-export-verify-abc"
    stale.mkdir(parents=True)
    (stale / "leftover.bin").write_bytes(b"x")
    keep = tmp_path / "chat" / "photo.jpg"
    keep.write_bytes(b"y")

    _clean_verify_staging(tmp_path)

    assert not stale.exists()
    assert keep.exists()
