from pathlib import Path

import pytest

from tg_export.config import MediaConfig
from tg_export.media import check_disk_space, check_skip_reason, media_subdir
from tg_export.models import DocumentMedia, FileInfo, MediaType, PhotoMedia


def test_check_skip_allowed_type():
    media = PhotoMedia(
        type=MediaType.photo,
        file=FileInfo(id=1, size=1000, name="photo.jpg", mime_type="image/jpeg", local_path=None),
        width=100,
        height=100,
    )
    cfg = MediaConfig(types=["photo", "video"], max_file_size_bytes=50 * 1024**2, concurrent_downloads=3)
    assert check_skip_reason(media, cfg) is None


def test_check_skip_disallowed_type():
    media = PhotoMedia(
        type=MediaType.photo,
        file=FileInfo(id=1, size=1000, name="photo.jpg", mime_type="image/jpeg", local_path=None),
        width=100,
        height=100,
    )
    cfg = MediaConfig(types=["document"], max_file_size_bytes=50 * 1024**2, concurrent_downloads=3)
    assert check_skip_reason(media, cfg) == "skipped_by_type"


def test_check_skip_file_too_large():
    media = DocumentMedia(
        type=MediaType.document,
        file=FileInfo(id=1, size=100 * 1024**2, name="big.zip", mime_type="application/zip", local_path=None),
        name="big.zip",
        mime_type="application/zip",
    )
    cfg = MediaConfig(types=["document"], max_file_size_bytes=50 * 1024**2, concurrent_downloads=3)
    assert check_skip_reason(media, cfg) == "skipped_by_size"


def test_check_skip_all_types():
    media = PhotoMedia(
        type=MediaType.photo,
        file=FileInfo(id=1, size=1000, name="p.jpg", mime_type="image/jpeg", local_path=None),
        width=100,
        height=100,
    )
    cfg = MediaConfig(types=["all"], max_file_size_bytes=50 * 1024**2, concurrent_downloads=3)
    assert check_skip_reason(media, cfg) is None


def test_media_subdir():
    assert media_subdir(MediaType.photo) == "photos"
    assert media_subdir(MediaType.video) == "videos"
    assert media_subdir(MediaType.document) == "files"
    assert media_subdir(MediaType.voice) == "voice_messages"
    assert media_subdir(MediaType.video_note) == "video_messages"
    assert media_subdir(MediaType.sticker) == "stickers"
    assert media_subdir(MediaType.gif) == "gifs"


def test_check_disk_space():
    assert check_disk_space(Path("/tmp"), min_free_bytes=1) is True
    assert check_disk_space(Path("/tmp"), min_free_bytes=10**18) is False


def _downloader(api, tmp_path):
    """MediaDownloader на подставных api и состоянии."""
    from unittest.mock import AsyncMock, MagicMock

    from tg_export.media import MediaDownloader

    state = MagicMock()
    state.get_file = AsyncMock(return_value=None)
    state.register_file = AsyncMock()
    state.find_file_by_id = AsyncMock(return_value=None)
    state.get_file_any_chat = AsyncMock(return_value=None)
    cfg = MediaConfig(types=["all"], max_file_size_bytes=50 * 1024**2, concurrent_downloads=3)
    dl = MediaDownloader(api, state, cfg, min_free_bytes=0)
    return dl


def _photo(file_id=1, name="photo.jpg"):
    return PhotoMedia(
        type=MediaType.photo,
        file=FileInfo(id=file_id, size=4, name=name, mime_type="image/jpeg", local_path=None),
        width=1,
        height=1,
    )


@pytest.mark.asyncio
async def test_failed_download_keeps_the_files_of_other_downloads(tmp_path, monkeypatch):
    """Уборка после отказа удаляла всё новое в общем каталоге чата, а снимок
    каталога снимался ещё до захвата семафора. С параллельными загрузками это
    означает удаление файлов, которые в это же время дописал сосед.
    """
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setattr("tg_export.media._MAX_DOWNLOAD_ATTEMPTS", 1)

    chat_dir = tmp_path / "chat"
    neighbour = chat_dir / "photos" / "neighbour.jpg"

    async def download_media(tl_message, target_dir, progress_cb=None):
        # Пока идёт эта загрузка, соседняя завершает свою и кладёт файл рядом.
        neighbour.parent.mkdir(parents=True, exist_ok=True)
        neighbour.write_bytes(b"kept")
        raise OSError("connection dropped")

    api = MagicMock()
    api.download_media = AsyncMock(side_effect=download_media)
    dl = _downloader(api, tmp_path)

    msg = MagicMock()
    msg.id = 10
    msg.file = None

    with pytest.raises(OSError):
        await dl.download(msg, _photo(), chat_dir, chat_id=1)

    assert neighbour.exists(), "уборка после отказа снесла файл соседней загрузки"


@pytest.mark.asyncio
async def test_failed_download_leaves_no_partial_of_its_own(tmp_path, monkeypatch):
    """Свой недокачанный файл после отказа остаться не должен."""
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setattr("tg_export.media._MAX_DOWNLOAD_ATTEMPTS", 1)

    chat_dir = tmp_path / "chat"

    async def download_media(tl_message, target_dir, progress_cb=None):
        (Path(target_dir) / "partial.jpg").write_bytes(b"half")
        raise OSError("connection dropped")

    api = MagicMock()
    api.download_media = AsyncMock(side_effect=download_media)
    dl = _downloader(api, tmp_path)

    msg = MagicMock()
    msg.id = 11
    msg.file = None

    with pytest.raises(OSError):
        await dl.download(msg, _photo(), chat_dir, chat_id=1)

    leftovers = list((chat_dir / "photos").rglob("*"))
    assert leftovers == [], leftovers


@pytest.mark.asyncio
async def test_successful_download_does_not_overwrite_a_different_file(tmp_path):
    """Совпадение имён не должно затирать чужой файл: до правки Telethon сам
    подбирал свободное имя, потому что видел целевой каталог."""
    from unittest.mock import AsyncMock, MagicMock

    chat_dir = tmp_path / "chat"
    photos = chat_dir / "photos"
    photos.mkdir(parents=True)
    (photos / "photo.jpg").write_bytes(b"older")

    async def download_media(tl_message, target_dir, progress_cb=None):
        path = Path(target_dir) / "photo.jpg"
        path.write_bytes(b"newer")
        return str(path)

    api = MagicMock()
    api.download_media = AsyncMock(side_effect=download_media)
    dl = _downloader(api, tmp_path)

    msg = MagicMock()
    msg.id = 12
    msg.file = None

    path, status = await dl.download(msg, _photo(), chat_dir, chat_id=1)

    assert status == "downloaded"
    assert (photos / "photo.jpg").read_bytes() == b"older"
    assert path is not None and Path(path).read_bytes() == b"newer"
