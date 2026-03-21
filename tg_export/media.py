"""Media downloader with filtering and disk space check."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sqlite3
from pathlib import Path

from tg_export.models import Media, MediaType
from tg_export.config import MediaConfig

logger = logging.getLogger(__name__)


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


def check_skip_reason(media: Media, config: MediaConfig) -> str | None:
    """Return skip reason or None if file should be downloaded."""
    if media.file is None:
        return None  # no file to download
    if media.type.value not in config.types and "all" not in config.types:
        return "type_skip"
    if media.file.size > config.max_file_size_bytes:
        return "too_large"
    return None


def check_disk_space(path: Path, min_free_bytes: int) -> bool:
    usage = shutil.disk_usage(path)
    return usage.free >= min_free_bytes


def _lookup_file_in_db(db_path: Path, file_id: int) -> str | None:
    """Look up file_id in a sibling state DB (synchronous, read-only)."""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = db.execute(
            "SELECT local_path FROM files WHERE file_id=? AND status='done' LIMIT 1",
            (file_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        db.close()


class DiskSpaceError(Exception):
    pass


class MediaDownloader:
    def __init__(self, api, state, config: MediaConfig, min_free_bytes: int,
                 tdesktop_indexes: list | None = None,
                 sibling_db_paths: list[Path] | None = None):
        self.api = api
        self.state = state
        self.config = config
        self.min_free_bytes = min_free_bytes
        self.semaphore = asyncio.Semaphore(config.concurrent_downloads)
        self.tdesktop_indexes = tdesktop_indexes or []
        self.sibling_db_paths = sibling_db_paths or []

    async def download(self, tl_message, media: Media, chat_dir: Path) -> tuple[Path | None, str]:
        """Download media file if needed. Returns (local_path, status).

        status: "downloaded", "cached", "imported", "type_skip", "too_large", "no_file"
        """
        skip = check_skip_reason(media, self.config)
        if skip:
            return None, skip

        # Already downloaded?
        if media.file:
            existing = await self.state.get_file(media.file.id, tl_message.chat_id if hasattr(tl_message, 'chat_id') else 0)
            if existing and existing["status"] == "done":
                return Path(existing["local_path"]), "cached"

        # Try to copy from tdesktop export instead of downloading
        imported = self._try_import_tdesktop(tl_message, media, chat_dir)
        if imported:
            await self._register(tl_message, media, imported)
            return imported, "imported"

        # Try to hardlink from sibling account export
        linked = self._try_link_sibling(media, chat_dir)
        if linked:
            await self._register(tl_message, media, linked)
            return linked, "imported"

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
            return None, "no_file"

        local_path = Path(path)
        await self._register(tl_message, media, local_path)
        return local_path, "downloaded"

    async def _register(self, tl_message, media: Media, local_path: Path):
        """Register downloaded/imported file in state DB."""
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

    def _try_link_sibling(self, media: Media, chat_dir: Path) -> Path | None:
        """Try to hardlink file from a sibling account's export by file_id."""
        if not self.sibling_db_paths or not media.file:
            return None

        file_id = media.file.id
        if not file_id:
            return None

        for db_path in self.sibling_db_paths:
            try:
                src_path = _lookup_file_in_db(db_path, file_id)
            except Exception:
                continue
            if src_path is None:
                continue

            src = Path(src_path)
            if not src.exists():
                continue

            subdir = media_subdir(media.type)
            target_dir = chat_dir / subdir
            target_dir.mkdir(parents=True, exist_ok=True)
            dst = target_dir / src.name

            if dst.exists():
                return dst

            try:
                os.link(src, dst)
                logger.debug("hardlinked from sibling: file_id=%d %s -> %s", file_id, src, dst)
                return dst
            except OSError:
                # Different filesystem or not supported — fall back to copy
                try:
                    shutil.copy2(src, dst)
                    logger.debug("copied from sibling: file_id=%d %s -> %s", file_id, src, dst)
                    return dst
                except OSError:
                    continue

        return None

    def _try_import_tdesktop(self, tl_message, media: Media, chat_dir: Path) -> Path | None:
        """Try to copy file from tdesktop export. Returns local path or None."""
        if not self.tdesktop_indexes:
            return None

        msg_id = tl_message.id
        for idx in self.tdesktop_indexes:
            src = idx.find_file(msg_id)
            if src is None:
                continue

            # Copy to tg-export directory structure
            subdir = media_subdir(media.type)
            target_dir = chat_dir / subdir
            target_dir.mkdir(parents=True, exist_ok=True)
            dst = target_dir / src.name
            # Avoid overwriting if already exists
            if dst.exists():
                return dst
            try:
                shutil.copy2(src, dst)
            except OSError:
                continue

            import logging
            logging.getLogger(__name__).debug(
                "imported from tdesktop: msg %d -> %s", msg_id, dst)
            return dst

        return None

    async def _download_with_retry(self, tl_message, target_dir: Path) -> str | None:
        for attempt in range(3):
            try:
                return await self.api.download_media(tl_message, target_dir)
            except (ConnectionError, TimeoutError, OSError):
                if attempt == 2:
                    raise
                await asyncio.sleep(2 ** attempt)
        return None
