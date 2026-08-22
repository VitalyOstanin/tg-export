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
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from telethon.errors import FloodWaitError, ServerError, TimedOutError

from tg_export.config import MediaConfig
from tg_export.errors import TgExportError
from tg_export.models import Media, MediaType
from tg_export.state import DB_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

# Download retry policy: transient errors (network/OS) are retried with
# exponential backoff plus jitter to avoid synchronised retry bursts when many
# parallel downloads fail at once.
_MAX_DOWNLOAD_ATTEMPTS = 3
_RETRY_JITTER_SECONDS = 1.0

# Telegram answers a transient server-side failure with an RPC error, not with
# a socket error: ServerError and TimedOutError derive from RPCError, so the
# network-only tuple below never covered them and a single one of them lost the
# file. FloodWaitError is different in kind -- the server states how long to
# wait -- and is handled separately, so it is not listed here.
_TRANSIENT_DOWNLOAD_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    OSError,
    ServerError,
    TimedOutError,
)

# A flood wait longer than this is not worth holding the export for: the file
# is reported as failed and the run goes on. Shorter waits Telethon absorbs
# itself (flood_sleep_threshold), so what reaches here is already the long tail.
_MAX_FLOOD_WAIT_SECONDS = 300

# Every download goes to its own directory under the target one and is moved
# into place only once complete. A shared target directory cannot tell whose
# partial file is whose: the previous cleanup deleted everything that appeared
# since a snapshot taken before the download started, which with concurrent
# downloads means deleting the files a neighbour had just finished writing.
DOWNLOAD_STAGING_PREFIX = ".tg-export-download-"

# How long a successful free-space check is trusted before asking the
# filesystem again.
_DISK_SPACE_CHECK_INTERVAL = 5.0


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


def _link_or_copy(src: Path, dst: Path) -> bool:
    """Hardlink src to dst, copying instead across filesystems. Blocking."""
    try:
        os.link(src, dst)
        return True
    except OSError:
        try:
            shutil.copy2(src, dst)
            return True
        except OSError:
            return False


def _reusable_target(dst: Path, expected_size: int) -> Path | None:
    """Return ``dst`` when it already holds the expected bytes, else None.

    A name match alone is no proof of content: an interrupted run leaves a
    truncated file under the same name, and reusing it registers a partial
    download as complete. A leftover of the wrong size is dropped so the
    caller can link or copy the file again.
    """
    try:
        actual_size = dst.stat().st_size
    except OSError:
        return None
    if expected_size and actual_size != expected_size:
        logger.debug("stale target %s: %d bytes, expected %d -- replacing", dst, actual_size, expected_size)
        with contextlib.suppress(OSError):
            dst.unlink()
        return None
    return dst


def _lookup_file_in_db(db_path: Path, file_id: int) -> str | None:
    """Look up file_id in a sibling state DB (synchronous, read-only).

    Why timeout: a busy writer in the sibling can hold a SHARED lock for
    seconds during a batch commit; default 5s is short for big batches.
    """
    # quote: the path goes into a URI, so a `?` in it would start the query
    # string and silently drop mode=ro, opening the sibling database for writing.
    db = sqlite3.connect(f"file:{quote(str(db_path))}?mode=ro", uri=True, timeout=DB_TIMEOUT_SECONDS)
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
        # Monotonic deadline until which free space is taken on trust.
        self._space_ok_until: float = 0.0

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
        imported = await self._try_import_tdesktop(tl_message, media, chat_dir)
        if imported:
            await self._register(tl_message, media, imported, chat_id)
            return imported, "reused_tdesktop"

        # Try to hardlink from sibling account export
        linked = await self._try_link_sibling(media, chat_dir)
        if linked:
            await self._register(tl_message, media, linked, chat_id)
            return linked, "reused_sibling"

        # Disk space check
        chat_dir.mkdir(parents=True, exist_ok=True)
        if not self._has_free_space(chat_dir):
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

    def _has_free_space(self, path: Path) -> bool:
        """Disk space check, asked at most once per interval.

        Free space falls by the size of what is being downloaded, not in jumps,
        so querying the filesystem for every single file buys nothing. A failed
        check is never cached: once the disk is full the caller must keep
        seeing that on every file.
        """
        now = time.monotonic()
        if self._space_ok_until > now:
            return True
        if not check_disk_space(path, self.min_free_bytes):
            self._space_ok_until = 0.0
            return False
        self._space_ok_until = now + _DISK_SPACE_CHECK_INTERVAL
        return True

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
            reused = _reusable_target(dst, media.file.size or 0)
            if reused is not None:
                return reused

        # to_thread: os.link is a syscall and the copy fallback reads and writes
        # the whole file. Both stall every other download and the Telegram
        # connection while they run in the loop thread.
        if await asyncio.to_thread(_link_or_copy, src, dst):
            logger.debug("linked intra-account: file_id=%d %s -> %s", media.file.id, src, dst)
            return dst
        return None

    async def _try_link_sibling(self, media: Media, chat_dir: Path) -> Path | None:
        """Try to hardlink file from a sibling account's export by file_id.

        Runs off the loop: the lookup opens the sibling database with plain
        sqlite3 and waits up to 30 seconds for a busy writer there, and the
        fallback copies the whole file.
        """
        if not self.sibling_db_paths or not media.file or not media.file.id:
            return None
        return await asyncio.to_thread(self._link_sibling_blocking, media, chat_dir)

    def _link_sibling_blocking(self, media: Media, chat_dir: Path) -> Path | None:
        """Synchronous body of _try_link_sibling; never call it on the loop.

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
            except OSError as e:
                logger.debug("sibling file %s is unreadable (%s) -- skipping", src, e)
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
                reused = _reusable_target(dst, expected_size)
                if reused is not None:
                    return reused

            if _link_or_copy(src, dst):
                logger.debug("linked from sibling: file_id=%d %s -> %s", file_id, src, dst)
                return dst
            continue

        return None

    async def _try_import_tdesktop(self, tl_message, media: Media, chat_dir: Path) -> Path | None:
        """Try to copy file from tdesktop export. Returns local path or None.

        Runs off the loop: the index is searched on disk and the file is copied
        in full.
        """
        if not self.tdesktop_indexes:
            return None
        return await asyncio.to_thread(self._import_tdesktop_blocking, tl_message, media, chat_dir)

    def _import_tdesktop_blocking(self, tl_message, media: Media, chat_dir: Path) -> Path | None:
        """Synchronous body of _try_import_tdesktop; never call it on the loop."""
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
            # Reuse an existing file only when its size matches; a leftover of
            # the wrong size is replaced by the copy below.
            if dst.exists():
                reused = _reusable_target(dst, media.file.size if media.file else 0)
                if reused is not None:
                    return reused
            try:
                shutil.copy2(src, dst)
            except OSError as e:
                logger.debug("tdesktop copy failed: %s -> %s (%s) -- skipping", src, dst, e)
                continue

            logger.debug("imported from tdesktop: msg %d -> %s", msg_id, dst)
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
                except FloodWaitError as e:
                    if e.seconds > _MAX_FLOOD_WAIT_SECONDS or attempt == _MAX_DOWNLOAD_ATTEMPTS - 1:
                        logger.warning(
                            "msg %d: flood wait of %ds, giving up on the file",
                            msg_id,
                            e.seconds,
                            exc_info=True,
                        )
                        raise
                    logger.debug("msg %d: attempt %d failed, flood wait %ds", msg_id, attempt + 1, e.seconds)
                    await asyncio.sleep(e.seconds)
                except _TRANSIENT_DOWNLOAD_ERRORS as e:
                    if attempt == _MAX_DOWNLOAD_ATTEMPTS - 1:
                        # The exception itself travels on to the caller; this
                        # line is what ties the final failure to the attempts
                        # that led to it, which were silent before.
                        logger.warning(
                            "msg %d: download failed after %d attempts",
                            msg_id,
                            _MAX_DOWNLOAD_ATTEMPTS,
                            exc_info=True,
                        )
                        raise
                    # Exponential backoff with jitter to desynchronise retries
                    # of many parallel downloads after a shared network blip.
                    delay = 2**attempt + random.uniform(0, _RETRY_JITTER_SECONDS)
                    logger.debug(
                        "msg %d: attempt %d failed (%s: %s), retrying in %.1fs",
                        msg_id,
                        attempt + 1,
                        type(e).__name__,
                        e,
                        delay,
                    )
                    await asyncio.sleep(delay)
            return None
        finally:
            with self._active_lock:
                self.active_downloads.pop(msg_id, None)
