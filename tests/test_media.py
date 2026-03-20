import pytest
from pathlib import Path
from tg_export.media import MediaDownloader, should_download, media_subdir, check_disk_space
from tg_export.models import MediaType, PhotoMedia, DocumentMedia, FileInfo
from tg_export.config import MediaConfig


def test_should_download_allowed_type():
    media = PhotoMedia(type=MediaType.photo, file=FileInfo(id=1, size=1000, name="photo.jpg", mime_type="image/jpeg", local_path=None), width=100, height=100)
    cfg = MediaConfig(types=["photo", "video"], max_file_size_bytes=50 * 1024**2, concurrent_downloads=3)
    assert should_download(media, cfg) is True


def test_should_download_disallowed_type():
    media = PhotoMedia(type=MediaType.photo, file=FileInfo(id=1, size=1000, name="photo.jpg", mime_type="image/jpeg", local_path=None), width=100, height=100)
    cfg = MediaConfig(types=["document"], max_file_size_bytes=50 * 1024**2, concurrent_downloads=3)
    assert should_download(media, cfg) is False


def test_should_download_file_too_large():
    media = DocumentMedia(
        type=MediaType.document,
        file=FileInfo(id=1, size=100 * 1024**2, name="big.zip", mime_type="application/zip", local_path=None),
        name="big.zip", mime_type="application/zip",
    )
    cfg = MediaConfig(types=["document"], max_file_size_bytes=50 * 1024**2, concurrent_downloads=3)
    assert should_download(media, cfg) is False


def test_should_download_all_types():
    media = PhotoMedia(type=MediaType.photo, file=FileInfo(id=1, size=1000, name="p.jpg", mime_type="image/jpeg", local_path=None), width=100, height=100)
    cfg = MediaConfig(types=["all"], max_file_size_bytes=50 * 1024**2, concurrent_downloads=3)
    assert should_download(media, cfg) is True


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
