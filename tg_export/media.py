"""Media downloader with filtering and disk space check."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import os
import random
import shutil
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import quote

from telethon.errors import FloodWaitError, ServerError, TimedOutError

from tg_export.config import MediaConfig
from tg_export.errors import TgExportError
from tg_export.importer import TdesktopIndex
from tg_export.models import FileStatus, Media, MediaType
from tg_export.state import DB_TIMEOUT_SECONDS, READER_PRAGMAS

logger = logging.getLogger(__name__)

# Download retry policy: transient errors (network/OS) are retried with
# exponential backoff plus jitter to avoid synchronised retry bursts when many
# parallel downloads fail at once.
_MAX_DOWNLOAD_ATTEMPTS = 3
_RETRY_JITTER_SECONDS = 1.0

# Width of the file name in the progress table. The tail is replaced by an
# ellipsis, so the cut is computed from the width instead of being a second
# number to keep in step with it.
_PROGRESS_NAME_WIDTH = 40
_PROGRESS_NAME_ELLIPSIS = "..."

# Telegram answers a transient server-side failure with an RPC error, not with
# a socket error: ServerError and TimedOutError derive from RPCError, so the
# network-only tuple below never covered them and a single one of them lost the
# file. FloodWaitError is different in kind -- the server states how long to
# wait -- and is handled separately, so it is not listed here.
# OSError covers the network, but also the filesystem, and there a repeat
# changes nothing: no space, no permission, a read-only filesystem or a name
# too long answer the same way on every attempt. Retrying them cost each file
# the full 2**attempt backoff before the error reached the caller, which turned
# a full disk into a long idle pass over the whole catalog.
_PERMANENT_OS_ERRORS = frozenset(
    {
        errno.ENOSPC,
        errno.EACCES,
        errno.EPERM,
        errno.EROFS,
        errno.ENAMETOOLONG,
        errno.EDQUOT,
    }
)

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


# Extensions a downloaded story is shown as a video by. Membership in a set,
# not a substring test: "" is a substring of every extension, so a file without
# one used to be taken for a video and rendered inside a <video> element.
STORY_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".avi"})

MEDIA_SUBDIRS = {
    MediaType.photo: "photos",
    MediaType.video: "videos",
    MediaType.document: "files",
    MediaType.voice: "voice_messages",
    MediaType.video_note: "video_messages",
    MediaType.sticker: "stickers",
    MediaType.gif: "gifs",
}

# The same names as a set, for the callers that ask "is this path component a
# media subdirectory": the renderer used to build this set again for every
# message it repaired a path for.
MEDIA_SUBDIR_NAMES = frozenset(MEDIA_SUBDIRS.values())


class DownloadStatus(StrEnum):
    """What happened to one media file.

    A StrEnum rather than bare strings: the set of outcomes is what the caller
    branches on, and until it was written down a new outcome could be added
    here and silently ignored there. Values are unchanged, so what is stored in
    the database and compared in tests stays the same.
    """

    downloaded = "downloaded"
    existing = "existing"
    reused_chat = "reused_chat"
    reused_tdesktop = "reused_tdesktop"
    reused_sibling = "reused_sibling"
    skipped_by_type = "skipped_by_type"
    skipped_by_size = "skipped_by_size"
    no_file = "no_file"


def media_subdir(media_type: MediaType) -> str:
    return MEDIA_SUBDIRS.get(media_type, "files")


def check_skip_reason(media: Media, config: MediaConfig) -> DownloadStatus | None:
    """Return skip reason or None if file should be downloaded."""
    if media.file is None:
        return DownloadStatus.no_file
    if media.type.value not in config.types and "all" not in config.types:
        return DownloadStatus.skipped_by_type
    if media.file.size > config.max_file_size_bytes:
        return DownloadStatus.skipped_by_size
    return None


def check_disk_space(path: Path, min_free_bytes: int) -> bool:
    usage = shutil.disk_usage(path)
    return usage.free >= min_free_bytes


def _link_or_copy(src: Path, dst: Path) -> bool:
    """Hardlink src to dst, copying instead across filesystems. Blocking.

    A refusal here is not "there was nothing to reuse" but "reusing it did not
    work" -- no permission on the target directory, no space, an unreadable
    source -- and its consequence is a full download instead of a link. Both
    failures are logged, as the neighbouring branches of the caller do.
    """
    try:
        os.link(src, dst)
        return True
    except OSError as link_error:
        try:
            shutil.copy2(src, dst)
            return True
        except OSError as copy_error:
            logger.debug("reuse failed for %s -> %s: link (%s), copy (%s)", src, dst, link_error, copy_error)
            return False


def _progress_name(filename: str) -> str:
    """Fit a file name into the width of the progress table."""
    if len(filename) <= _PROGRESS_NAME_WIDTH:
        return filename
    keep = _PROGRESS_NAME_WIDTH - len(_PROGRESS_NAME_ELLIPSIS)
    return filename[:keep] + _PROGRESS_NAME_ELLIPSIS


def _size_or_none(path: Path) -> int | None:
    """Size of `path`, or None when it does not exist or cannot be read."""
    try:
        return path.stat().st_size
    except OSError:
        return None


class _TargetRegistry:
    """Hands out destination paths so that no two writers get the same one.

    The resource two downloads compete for is the name in the target
    directory, not the file_id they are locked by: Telegram hands out
    `photo.jpg` and `document.pdf` en masse, and the per-file_id lock lets two
    different files with the same source name reach the same `dst`. Both would
    then see it free, both would write, and the run would end with one file on
    disk and two rows in the database pointing at it.

    A claim is held for the duration of one write and dropped afterwards: by
    then the file is in place, so the next claimant sees its size and either
    reuses it or moves on to the next free name. Claims live in this process
    only -- a second tg-export over the same output directory is kept away by
    the lock on the state database.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._claimed: set[Path] = set()

    @contextlib.contextmanager
    def claim(self, target_dir: Path, name: str, expected_size: int, *, reuse: bool):
        """Yield `(path, reused)` -- a path only this claimant may write to.

        `reused` is True when the file is already there with the expected size
        and nothing needs to be written. With `reuse=False` an occupied name is
        never taken over: the next free one is picked instead, which is what a
        finished download needs.
        """
        path, reused = self._take(target_dir, name, expected_size, reuse=reuse)
        try:
            yield path, reused
        finally:
            with self._lock:
                self._claimed.discard(path)

    def _take(self, target_dir: Path, name: str, expected_size: int, *, reuse: bool) -> tuple[Path, bool]:
        base = target_dir / name
        stem, suffix = base.stem, base.suffix
        with self._lock:
            counter = 0
            while True:
                candidate = base if counter == 0 else target_dir / f"{stem} ({counter}){suffix}"
                counter += 1
                if candidate in self._claimed:
                    continue
                size = _size_or_none(candidate)
                if size is None:
                    self._claimed.add(candidate)
                    return candidate, False
                if not reuse:
                    continue
                if not expected_size or size == expected_size:
                    self._claimed.add(candidate)
                    return candidate, True
                # A name match alone is no proof of content: an interrupted run
                # leaves a truncated file behind. Nobody in this run holds the
                # name, so the leftover is that -- drop it and write again.
                logger.debug(
                    "stale target %s: %d bytes, expected %d -- replacing", candidate, size, expected_size
                )
                with contextlib.suppress(OSError):
                    candidate.unlink()
                self._claimed.add(candidate)
                return candidate, False


class _SiblingReaders:
    """Read-only connections to sibling state databases, one per database.

    A connection used to be opened and closed for every file looked up, on
    every sibling export -- the cost the renderer already refused to pay per
    month (see state.month_reader): a file open, a schema read and a page
    cache that dies cold. Here the lookup happens per file, so the cost is
    multiplied by the number of files and of siblings.

    The lookups run in arbitrary threads of the default executor, so the
    connections are opened with ``check_same_thread=False`` and serialised by
    a lock. What they serialise is a point lookup by an indexed column, which
    is cheaper than reopening the database each time -- and closing them from
    the thread that owns the downloader is only possible this way.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conns: dict[Path, sqlite3.Connection] = {}

    def lookup(self, db_path: Path, file_id: int) -> str | None:
        """The path of a completed download of `file_id` in that sibling, if any.

        Why timeout: a busy writer in the sibling can hold a SHARED lock for
        seconds during a batch commit; default 5s is short for big batches.
        """
        with self._lock:
            db = self._conns.get(db_path)
            if db is None:
                db = self._open(db_path)
                self._conns[db_path] = db
            row = db.execute(
                "SELECT local_path FROM files WHERE file_id=? AND status='done' LIMIT 1",
                (file_id,),
            ).fetchone()
        return row[0] if row else None

    @staticmethod
    def _open(db_path: Path) -> sqlite3.Connection:
        # quote: the path goes into a URI, so a `?` in it would start the query
        # string and silently drop mode=ro, opening the sibling database for writing.
        db = sqlite3.connect(
            f"file:{quote(str(db_path))}?mode=ro",
            uri=True,
            timeout=DB_TIMEOUT_SECONDS,
            check_same_thread=False,
        )
        for pragma in READER_PRAGMAS:
            db.execute(pragma)
        db.execute("PRAGMA query_only = ON")
        return db

    def close(self) -> None:
        """Close every connection; the object stays usable and reopens on demand."""
        with self._lock:
            for db in self._conns.values():
                with contextlib.suppress(sqlite3.Error):
                    db.close()
            self._conns.clear()


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


@dataclass(frozen=True)
class DownloadProgress:
    """What one file download has transferred so far.

    Immutable, and republished as a whole on every update: the Live thread
    reads the fields of a snapshot outside the lock, so a record edited in
    place could be read half-updated -- `received` from the latest callback
    next to a `total` still holding its initial zero, which reaches rich as
    `completed` without a `total`.
    """

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
        tdesktop_indexes: list[TdesktopIndex] | None = None,
        sibling_db_paths: list[Path] | None = None,
    ):
        self.api = api
        self.state = state
        self.config = config
        self.min_free_bytes = min_free_bytes
        self.semaphore = asyncio.Semaphore(config.concurrent_downloads)
        self.tdesktop_indexes = tdesktop_indexes or []
        self.sibling_db_paths = sibling_db_paths or []
        self._siblings = _SiblingReaders()
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
        # Destination names handed out to the downloads currently in flight.
        self._targets = _TargetRegistry()

    def snapshot_active_downloads(self) -> dict[int, DownloadProgress]:
        """Return a consistent copy of active_downloads (thread-safe).

        Called from the Rich Live refresh thread; copying under the lock
        prevents a race with the event loop mutating the dict, and the records
        themselves are immutable, so what the caller reads afterwards is what
        was published at the moment of the snapshot.
        """
        with self._active_lock:
            return dict(self.active_downloads)

    def _publish_progress(self, msg_id: int, progress: DownloadProgress) -> None:
        """Make the state of one download visible to the Live thread."""
        with self._active_lock:
            self.active_downloads[msg_id] = progress

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
    ) -> tuple[Path | None, DownloadStatus]:
        """Download media file if needed. Returns (local_path, status).

        chat_id: canonical chat ID (positive, as used in messages table).
        The outcomes are the members of DownloadStatus.
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
    ) -> tuple[Path | None, DownloadStatus]:
        # Already downloaded?
        if media.file:
            existing = await self.state.get_file(media.file.id, chat_id)
            if existing and existing["status"] == FileStatus.done:
                return Path(existing["local_path"]), DownloadStatus.existing

        # Try to hardlink from another chat within this account
        if media.file:
            linked = await self._try_link_intra_account(media, chat_dir, chat_id)
            if linked:
                await self._register(tl_message, media, linked, chat_id)
                return linked, DownloadStatus.reused_chat

        # Try to copy from tdesktop export instead of downloading
        imported = await self._try_import_tdesktop(tl_message, media, chat_dir)
        if imported:
            await self._register(tl_message, media, imported, chat_id)
            return imported, DownloadStatus.reused_tdesktop

        # Try to hardlink from sibling account export
        linked = await self._try_link_sibling(media, chat_dir)
        if linked:
            await self._register(tl_message, media, linked, chat_id)
            return linked, DownloadStatus.reused_sibling

        chat_dir.mkdir(parents=True, exist_ok=True)
        if not self._has_free_space(chat_dir):
            raise DiskSpaceError(f"Free space less than {self.min_free_bytes // 1024**3} GB")

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
                    return None, DownloadStatus.no_file

                local_path = self._move_into_place(Path(path), target_dir)

            await self._register(tl_message, media, local_path, chat_id)
            return local_path, DownloadStatus.downloaded
        except _FileTooLargeError as e:
            logger.debug(
                "file too large (real size %d > limit %d), msg %d",
                e.size,
                self.config.max_file_size_bytes,
                tl_message.id,
            )
            await self._register_skip(tl_message, media, chat_id, DownloadStatus.skipped_by_size)
            return None, DownloadStatus.skipped_by_size

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

    def _move_into_place(self, downloaded: Path, target_dir: Path) -> Path:
        """Move a finished download out of its staging directory.

        Picks a free name the way Telethon does when it writes straight into
        the target directory: downloading into an empty staging directory means
        Telethon always chooses the base name, so a same-named neighbour would
        be overwritten without this. The name is taken from the registry rather
        than probed here, so a file another coroutine is about to link into the
        same directory is not overwritten either.
        """
        with self._targets.claim(target_dir, downloaded.name, 0, reuse=False) as (final_path, _reused):
            os.replace(downloaded, final_path)
            return final_path

    async def _register_skip(self, tl_message, media: Media, chat_id: int, status: FileStatus | str):
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
        status = FileStatus.done if actual_size == expected_size or expected_size == 0 else FileStatus.partial
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
        with self._targets.claim(target_dir, src.name, media.file.size or 0, reuse=True) as (dst, reused):
            if reused:
                return dst
            # to_thread: os.link is a syscall and the copy fallback reads and
            # writes the whole file. Both stall every other download and the
            # Telegram connection while they run in the loop thread.
            if await asyncio.to_thread(_link_or_copy, src, dst):
                logger.debug("linked intra-account: file_id=%d %s -> %s", media.file.id, src, dst)
                return dst
        return None

    def close(self) -> None:
        """Release what the downloader keeps open for the whole export."""
        self._siblings.close()

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
                src_path = self._siblings.lookup(db_path, file_id)
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
            with self._targets.claim(target_dir, src.name, expected_size, reuse=True) as (dst, reused):
                if reused:
                    return dst
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
            expected_size = media.file.size if media.file else 0
            with self._targets.claim(target_dir, src.name, expected_size, reuse=True) as (dst, reused):
                if reused:
                    return dst
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
        filename = _progress_name(filename)

        self._publish_progress(msg_id, DownloadProgress(filename=filename))

        def _progress_cb(received: int, total: int):
            self._publish_progress(msg_id, DownloadProgress(filename, received, total))
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
                    if isinstance(e, OSError) and e.errno in _PERMANENT_OS_ERRORS:
                        logger.debug(
                            "msg %d: %s is not a failure a retry can change, giving up on the file",
                            msg_id,
                            errno.errorcode.get(e.errno, e.errno),
                        )
                        raise
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
            # Unreachable while the attempt limit is positive: every iteration
            # either returns the downloaded path or raises on the last attempt.
            # The line exists so that a limit accidentally set to zero fails
            # loudly instead of returning "no file" for every media item.
            raise AssertionError(f"download attempt limit is not positive: {_MAX_DOWNLOAD_ATTEMPTS}")
        finally:
            with self._active_lock:
                self.active_downloads.pop(msg_id, None)
