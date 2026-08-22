"""Media downloader with filtering and disk space check."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import shutil
import sqlite3
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from tg_export.config import MediaConfig
from tg_export.errors import TgExportError
from tg_export.models import Media, MediaType

logger = logging.getLogger(__name__)

# Download retry policy: transient errors (network/OS) are retried with
# exponential backoff plus jitter to avoid synchronised retry bursts when many
# parallel downloads fail at once.
_MAX_DOWNLOAD_ATTEMPTS = 3
_RETRY_JITTER_SECONDS = 1.0

# Every download goes to its own directory under the target one and is moved
# into place only once complete. A shared target directory cannot tell whose
# partial file is whose: the previous cleanup deleted everything that appeared
# since a snapshot taken before the download started, which with concurrent
# downloads means deleting the files a neighbour had just finished writing.
DOWNLOAD_STAGING_PREFIX = ".tg-export-download-"


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
        return "no_file"
    if media.type.value not in config.types and "all" not in config.types:
        return "skipped_by_type"
    if media.file.size > config.max_file_size_bytes:
        return "skipped_by_size"
    return None


def check_disk_space(path: Path, min_free_bytes: int) -> bool:
    usage = shutil.disk_usage(path)
    return usage.free >= min_free_bytes


def _lookup_file_in_db(db_path: Path, file_id: int) -> str | None:
    """Look up file_id in a sibling state DB (synchronous, read-only).

    Why timeout: a busy writer in the sibling can hold a SHARED lock for
    seconds during a batch commit; default 5s is short for big batches.
    """
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
    try:
        row = db.execute(
            "SELECT local_path FROM files WHERE file_id=? AND status='done' LIMIT 1",
            (file_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        db.close()


def _is_sibling_path_safe(local_path: Path, db_path: Path) -> bool:
    """Validate that a sibling-DB local_path stays inside the sibling's tree.

    Why: a tampered sibling DB could point to /etc/passwd or any file readable
    by the user. We only accept paths under the sibling's parent directory.
    """
    try:
        resolved = local_path.resolve()
        sibling_root = db_path.parent.resolve()
        return resolved.is_relative_to(sibling_root)
    except OSError:
        return False


class DiskSpaceError(TgExportError):
    pass


class _FileTooLargeError(TgExportError):
    """Raised inside progress callback when real file size exceeds limit."""

    def __init__(self, size: int):
        self.size = size
        super().__init__(f"File too large: {size} bytes")


@dataclass
class DownloadProgress:
    """Tracks progress of a single file download."""

    filename: str
    received: int = 0
    total: int = 0


class MediaDownloader:
    def __init__(
        self,
        api,
        state,
        config: MediaConfig,
        min_free_bytes: int,
        tdesktop_indexes: list | None = None,
        sibling_db_paths: list[Path] | None = None,
    ):
        self.api = api
        self.state = state
        self.config = config
        self.min_free_bytes = min_free_bytes
        self.semaphore = asyncio.Semaphore(config.concurrent_downloads)
        self.tdesktop_indexes = tdesktop_indexes or []
        self.sibling_db_paths = sibling_db_paths or []
        self.active_downloads: dict[int, DownloadProgress] = {}  # msg_id -> progress
        # active_downloads is mutated by the event loop and read by the Rich
        # Live refresh thread; this lock guards both sides against
        # "dictionary changed size during iteration". Use snapshot_active_downloads()
        # to read a consistent copy from another thread.
        self._active_lock = threading.Lock()
        self._next_dl_id = 0
        # Why: two coroutines processing different messages with the same
        # file_id (cross-chat duplicates) would otherwise both download the
        # same content; per-file-id locks serialise them so the second one
        # picks up the registered file via _try_link_intra_account.
        self._file_id_locks: dict[int, asyncio.Lock] = {}
        # Reference count per file_id so a lock can be dropped from the dict
        # once no coroutine holds or waits on it. Without this the dict grows
        # monotonically for the whole export (one Lock per unique file_id).
        self._file_id_lock_users: dict[int, int] = {}
        # Target directories whose leftover staging dirs were already removed.
        # Anything found on the first visit within this process belongs to a
        # previous run that was killed mid-download.
        self._staging_cleaned: set[Path] = set()

    def snapshot_active_downloads(self) -> dict[int, DownloadProgress]:
        """Return a consistent copy of active_downloads (thread-safe).

        Called from the Rich Live refresh thread; copying under the lock
        prevents a race with the event loop mutating the dict.
        """
        with self._active_lock:
            return dict(self.active_downloads)

    @contextlib.asynccontextmanager
    async def _file_lock(self, file_id: int):
        lock = self._file_id_locks.get(file_id)
        if lock is None:
            lock = asyncio.Lock()
            self._file_id_locks[file_id] = lock
        self._file_id_lock_users[file_id] = self._file_id_lock_users.get(file_id, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._file_id_lock_users[file_id] -= 1
            if self._file_id_lock_users[file_id] == 0:
                # No other coroutine holds or waits on this lock — release it.
                del self._file_id_lock_users[file_id]
                del self._file_id_locks[file_id]

    async def download(
        self, tl_message, media: Media, chat_dir: Path, chat_id: int = 0
    ) -> tuple[Path | None, str]:
        """Download media file if needed. Returns (local_path, status).

        chat_id: canonical chat ID (positive, as used in messages table).
        status: "downloaded", "existing", "reused_chat", "reused_tdesktop",
                "reused_sibling", "skipped_by_type", "skipped_by_size", "no_file"
        """
        skip = check_skip_reason(media, self.config)
        if skip:
            # Why: previously skipped files left no DB trace, so verify/count
            # could not distinguish "intentionally skipped" from "missing".
            await self._register_skip(tl_message, media, chat_id, skip)
            return None, skip

        if media.file is None or not media.file.id:
            return await self._download_inner(tl_message, media, chat_dir, chat_id)

        # Serialise concurrent downloads of the same file_id (across chats).
        async with self._file_lock(media.file.id):
            return await self._download_inner(tl_message, media, chat_dir, chat_id)

    async def _download_inner(
        self, tl_message, media: Media, chat_dir: Path, chat_id: int
    ) -> tuple[Path | None, str]:
        # Already downloaded?
        if media.file:
            existing = await self.state.get_file(media.file.id, chat_id)
            if existing and existing["status"] == "done":
                return Path(existing["local_path"]), "existing"

        # Try to hardlink from another chat within this account
        if media.file:
            linked = await self._try_link_intra_account(media, chat_dir, chat_id)
            if linked:
                await self._register(tl_message, media, linked, chat_id)
                return linked, "reused_chat"

        # Try to copy from tdesktop export instead of downloading
        imported = self._try_import_tdesktop(tl_message, media, chat_dir)
        if imported:
            await self._register(tl_message, media, imported, chat_id)
            return imported, "reused_tdesktop"

        # Try to hardlink from sibling account export
        linked = self._try_link_sibling(media, chat_dir)
        if linked:
            await self._register(tl_message, media, linked, chat_id)
            return linked, "reused_sibling"

        # Disk space check
        chat_dir.mkdir(parents=True, exist_ok=True)
        if not check_disk_space(chat_dir, self.min_free_bytes):
            raise DiskSpaceError(f"Free space less than {self.min_free_bytes // 1024**3} GB")

        # Download with semaphore
        subdir = media_subdir(media.type)
        target_dir = chat_dir / subdir
        target_dir.mkdir(parents=True, exist_ok=True)

        self._clean_stale_staging(target_dir)
        try:
            # TemporaryDirectory drops whatever the download left behind on any
            # exit path, and it can only contain this download's own files.
            # dir=target_dir keeps it on the same filesystem, so the move below
            # is a rename rather than a copy.
            with tempfile.TemporaryDirectory(dir=target_dir, prefix=DOWNLOAD_STAGING_PREFIX) as staging:
                async with self.semaphore:
                    path = await self._download_with_retry(tl_message, Path(staging), media)

                if path is None:
                    return None, "no_file"

                local_path = self._move_into_place(Path(path), target_dir)

            await self._register(tl_message, media, local_path, chat_id)
            return local_path, "downloaded"
        except _FileTooLargeError as e:
            logger.debug(
                "file too large (real size %d > limit %d), msg %d",
                e.size,
                self.config.max_file_size_bytes,
                tl_message.id,
            )
            await self._register_skip(tl_message, media, chat_id, "skipped_by_size")
            return None, "skipped_by_size"

    def _clean_stale_staging(self, target_dir: Path) -> None:
        """Drop staging directories left by a run that was killed mid-download."""
        if target_dir in self._staging_cleaned:
            return
        self._staging_cleaned.add(target_dir)
        for entry in target_dir.iterdir():
            if entry.is_dir() and entry.name.startswith(DOWNLOAD_STAGING_PREFIX):
                shutil.rmtree(entry, ignore_errors=True)
                logger.debug("removed stale staging dir: %s", entry)

    @staticmethod
    def _move_into_place(downloaded: Path, target_dir: Path) -> Path:
        """Move a finished download out of its staging directory.

        Picks a free name the way Telethon does when it writes straight into
        the target directory: downloading into an empty staging directory means
        Telethon always chooses the base name, so a same-named neighbour would
        be overwritten without this.
        """
        final_path = target_dir / downloaded.name
        if final_path.exists():
            stem, suffix = final_path.stem, final_path.suffix
            counter = 1
            while final_path.exists():
                final_path = target_dir / f"{stem} ({counter}){suffix}"
                counter += 1
        os.replace(downloaded, final_path)
        return final_path

    async def _register_skip(self, tl_message, media: Media, chat_id: int, status: str):
        """Record a skipped file in DB (no actual file on disk)."""
        if media.file is None or not media.file.id:
            return
        await self.state.register_file(
            file_id=media.file.id,
            chat_id=chat_id,
            msg_id=tl_message.id,
            expected_size=media.file.size or 0,
            actual_size=0,
            local_path=f"<{status}>",
            status=status,
        )

    async def _register(self, tl_message, media: Media, local_path: Path, chat_id: int = 0):
        """Register downloaded/imported file in state DB."""
        actual_size = local_path.stat().st_size if local_path.exists() else 0
        expected_size = media.file.size if media.file else 0
        status = "done" if actual_size == expected_size or expected_size == 0 else "partial"
        await self.state.register_file(
            file_id=media.file.id if media.file else 0,
            chat_id=chat_id,
            msg_id=tl_message.id,
            expected_size=expected_size,
            actual_size=actual_size,
            local_path=str(local_path),
            status=status,
        )

    async def _try_link_intra_account(self, media: Media, chat_dir: Path, chat_id: int) -> Path | None:
        """Try to hardlink file from another chat within the same account."""
        if not media.file or not media.file.id:
            return None

        existing = await self.state.get_file_any_chat(media.file.id)
        if existing is None or existing["chat_id"] == chat_id:
            return None

        src = Path(existing["local_path"])
        if not src.exists():
            return None

        subdir = media_subdir(media.type)
        target_dir = chat_dir / subdir
        target_dir.mkdir(parents=True, exist_ok=True)
        dst = target_dir / src.name

        if dst.exists():
            return dst

        try:
            os.link(src, dst)
            logger.debug("hardlinked intra-account: file_id=%d %s -> %s", media.file.id, src, dst)
            return dst
        except OSError:
            try:
                shutil.copy2(src, dst)
                logger.debug("copied intra-account: file_id=%d %s -> %s", media.file.id, src, dst)
                return dst
            except OSError:
                return None

    def _try_link_sibling(self, media: Media, chat_dir: Path) -> Path | None:
        """Try to hardlink file from a sibling account's export by file_id.

        Why size+path checks: sibling DBs are external input; we validate that
        the referenced path is inside the sibling's directory tree and that
        the file size matches what Telegram says.
        """
        if not self.sibling_db_paths or not media.file:
            return None

        file_id = media.file.id
        if not file_id:
            return None

        expected_size = media.file.size or 0

        for db_path in self.sibling_db_paths:
            try:
                src_path = _lookup_file_in_db(db_path, file_id)
            except sqlite3.Error as e:
                logger.debug("sibling DB lookup failed (%s): %s", db_path, e)
                continue
            if src_path is None:
                continue

            src = Path(src_path)
            if not src.exists():
                continue

            if not _is_sibling_path_safe(src, db_path):
                logger.warning(
                    "sibling DB %s points to a path outside its tree: %s -- skipping",
                    db_path,
                    src,
                )
                continue

            try:
                actual_size = src.stat().st_size
            except OSError:
                continue
            if expected_size and actual_size != expected_size:
                logger.debug(
                    "sibling file size mismatch: file_id=%d expected=%d actual=%d -- skipping",
                    file_id,
                    expected_size,
                    actual_size,
                )
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

            logging.getLogger(__name__).debug("imported from tdesktop: msg %d -> %s", msg_id, dst)
            return dst

        return None

    async def _download_with_retry(
        self, tl_message, target_dir: Path, media: Media | None = None
    ) -> str | None:
        msg_id = tl_message.id
        max_size = self.config.max_file_size_bytes

        # Determine filename for progress display
        filename = ""
        if hasattr(tl_message, "file") and tl_message.file:
            filename = getattr(tl_message.file, "name", "") or ""
        if not filename and media and media.file:
            # Use file name from our model, or type + extension
            filename = media.file.name or ""
        if not filename:
            # Fallback: media type + msg_id
            ext = ""
            if hasattr(tl_message, "file") and tl_message.file:
                ext = getattr(tl_message.file, "ext", "") or ""
            type_str = media.type.value if media else "file"
            filename = f"{type_str}_{msg_id}{ext}"
        # Truncate long filenames for progress display
        if len(filename) > 40:
            filename = filename[:37] + "..."

        dl_progress = DownloadProgress(filename=filename)
        with self._active_lock:
            self.active_downloads[msg_id] = dl_progress

        def _progress_cb(received: int, total: int):
            dl_progress.received = received
            dl_progress.total = total
            # Cancel download if real size exceeds limit (file.size was 0)
            if total > max_size:
                raise _FileTooLargeError(total)

        try:
            for attempt in range(_MAX_DOWNLOAD_ATTEMPTS):
                try:
                    return await self.api.download_media(tl_message, target_dir, progress_cb=_progress_cb)
                except _FileTooLargeError:
                    raise
                except (ConnectionError, TimeoutError, OSError):
                    if attempt == _MAX_DOWNLOAD_ATTEMPTS - 1:
                        raise
                    # Exponential backoff with jitter to desynchronise retries
                    # of many parallel downloads after a shared network blip.
                    delay = 2**attempt + random.uniform(0, _RETRY_JITTER_SECONDS)
                    await asyncio.sleep(delay)
            return None
        finally:
            with self._active_lock:
                self.active_downloads.pop(msg_id, None)
