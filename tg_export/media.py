"""Media downloader with filtering and disk space check."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from tg_export.models import Media, MediaType
from tg_export.config import MediaConfig


MEDIA_SUBDIRS = {
    MediaType.photo: "photos",
    MediaType.video: "videos",
    MediaType.document: "files",
    MediaType.voice: "voice_messages",
    MediaType.video_note: "video_messages",
    MediaType.sticker: "stickers",
    MediaType.gif: "gifs",
}


def media_subdir(media_type: MediaType) -> str:
    return MEDIA_SUBDIRS.get(media_type, "files")


def should_download(media: Media, config: MediaConfig) -> bool:
    if media.file is None:
        return False
    if media.type.value not in config.types and "all" not in config.types:
        return False
    if media.file.size > config.max_file_size_bytes:
        return False
    return True


def check_disk_space(path: Path, min_free_bytes: int) -> bool:
    usage = shutil.disk_usage(path)
    return usage.free >= min_free_bytes


class DiskSpaceError(Exception):
    pass


class MediaDownloader:
    def __init__(self, api, state, config: MediaConfig, min_free_bytes: int):
        self.api = api
        self.state = state
        self.config = config
        self.min_free_bytes = min_free_bytes
        self.semaphore = asyncio.Semaphore(config.concurrent_downloads)

    async def download(self, tl_message, media: Media, chat_dir: Path) -> Path | None:
        """Download media file if needed. Returns local path or None."""
        if not should_download(media, self.config):
            return None

        # Already downloaded?
        if media.file:
            existing = await self.state.get_file(media.file.id, tl_message.chat_id if hasattr(tl_message, 'chat_id') else 0)
            if existing and existing["status"] == "done":
                return Path(existing["local_path"])

        # Disk space check
        chat_dir.mkdir(parents=True, exist_ok=True)
        if not check_disk_space(chat_dir, self.min_free_bytes):
            raise DiskSpaceError(
                f"Free space less than {self.min_free_bytes // 1024**3} GB"
            )

        # Download with semaphore
        subdir = media_subdir(media.type)
        target_dir = chat_dir / subdir
        target_dir.mkdir(parents=True, exist_ok=True)

        async with self.semaphore:
            path = await self._download_with_retry(tl_message, target_dir)

        if path is None:
            return None

        local_path = Path(path)
        actual_size = local_path.stat().st_size if local_path.exists() else 0
        expected_size = media.file.size if media.file else 0
        status = "done" if actual_size == expected_size or expected_size == 0 else "partial"

        chat_id = tl_message.chat_id if hasattr(tl_message, 'chat_id') else 0
        await self.state.register_file(
            file_id=media.file.id if media.file else 0,
            chat_id=chat_id,
            msg_id=tl_message.id,
            expected_size=expected_size,
            actual_size=actual_size,
            local_path=str(local_path),
            status=status,
        )

        return local_path

    async def _download_with_retry(self, tl_message, target_dir: Path) -> str | None:
        for attempt in range(3):
            try:
                return await self.api.download_media(tl_message, target_dir)
            except (ConnectionError, TimeoutError, OSError):
                if attempt == 2:
                    raise
                await asyncio.sleep(2 ** attempt)
        return None
