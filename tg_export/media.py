"""Downloading media and reusing what is already on disk.

The module holds the downloader itself, the registry that hands out
destination names, the readers of sibling accounts' state databases and the
progress records the status display reads.
"""

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
from collections.abc import Callable
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
from tg_export.waits import FLOOD_WAIT_REASON, WAITS

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
STAGING_PREFIX = ".tg-export-staging-"

# The same trick used to be written twice, with a prefix per caller: the
# download swept `.tg-export-download-*` in one directory, the verify pass
# swept `.tg-export-verify-*` across the tree, and neither touched the other's
# leftovers. One prefix now, and the old two are still swept -- they sit in the
# media directories of exports made by earlier versions.
_LEGACY_STAGING_PREFIXES = (".tg-export-download-", ".tg-export-verify-")
STAGING_PREFIXES = (STAGING_PREFIX, *_LEGACY_STAGING_PREFIXES)


def clean_staging(root: Path) -> None:
    """Remove staging directories left behind anywhere under `root`.

    A SIGKILL/SIGTERM in the middle of a download skips TemporaryDirectory
    cleanup. The leftovers are harmless but accumulate next to the media, so
    sweep them at the start of the next run.
    """
    for prefix in STAGING_PREFIXES:
        for path in root.rglob(f"{prefix}*"):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)


def clean_staging_in(target_dir: Path) -> None:
    """Remove staging directories of `target_dir` itself, without descending."""
    for entry in target_dir.iterdir():
        if entry.is_dir() and entry.name.startswith(STAGING_PREFIXES):
            shutil.rmtree(entry, ignore_errors=True)
            logger.debug("removed stale staging dir: %s", entry)


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


class TargetRegistry:
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
        # Where the search for a free name starts, per (directory, name). A
        # name handed out with reuse=False is occupied by the file written into
        # it, so scanning from zero on the next file of the same name cost k
        # stat() calls for the k-th of them -- and the scan runs on the event
        # loop. The cursor only moves forward: a name freed later leaves a gap
        # in the numbering, which costs nothing.
        self._search_from: dict[tuple[Path, str], int] = {}

    @contextlib.contextmanager
    def claim(
        self, target_dir: Path, name: str, expected_size: int, *, reuse: bool, replacing: Path | None = None
    ):
        """Yield `(path, reused)` -- a path only this claimant may write to.

        `reused` is True when the file is already there with the expected size
        and nothing needs to be written. With `reuse=False` an occupied name is
        never taken over: the next free one is picked instead, which is what a
        finished download needs.

        `replacing` names a file this claimant already owns -- the broken file
        a re-download replaces. Without it a re-download would step over its
        own file, see the name occupied and move to the next one, renaming the
        file on every pass.
        """
        path, reused = self._take(target_dir, name, expected_size, reuse=reuse, replacing=replacing)
        try:
            yield path, reused
        finally:
            with self._lock:
                self._claimed.discard(path)

    def _take(
        self, target_dir: Path, name: str, expected_size: int, *, reuse: bool, replacing: Path | None = None
    ) -> tuple[Path, bool]:
        base = target_dir / name
        stem, suffix = base.stem, base.suffix
        # The cursor is only for the plain "give me a free name" case: reuse
        # looks for an existing file to take over, and a re-download looks for
        # the very name it already owns -- both may be behind the cursor.
        cursor_applies = not reuse and replacing is None
        key = (target_dir, name)
        with self._lock:
            counter = self._search_from.get(key, 0) if cursor_applies else 0
            while True:
                candidate = base if counter == 0 else target_dir / f"{stem} ({counter}){suffix}"
                counter += 1
                if candidate in self._claimed:
                    continue
                if candidate == replacing:
                    self._claimed.add(candidate)
                    return candidate, False
                size = _size_or_none(candidate)
                if size is None:
                    self._claimed.add(candidate)
                    if cursor_applies:
                        self._search_from[key] = counter
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

    def drop_unclaimed(self, path: Path) -> None:
        """Delete `path`, unless another writer is holding that name.

        A re-download whose replacement landed under a different name has to
        remove the file it replaced. Checking the claims first is what keeps it
        from deleting a file another coroutine has just moved into place.
        """
        with self._lock:
            if path in self._claimed:
                return
            with contextlib.suppress(OSError):
                path.unlink()


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


async def download_with_retries(attempt_download, *, msg_id: int) -> str | None:
    """Run one download, repeating the failures a repeat can change.

    Both paths that fetch media -- the export and the re-download of a broken
    file -- go through here, so what counts as transient, which `errno` values
    a repeat cannot change, and how long a flood wait is worth waiting are
    described once. The re-download used to call the transport directly, and a
    dropped connection turned a file the pass exists to restore into a failure
    of that pass.

    `attempt_download` is called anew on every attempt and must carry its own
    arguments; the download it starts is expected to leave nothing behind when
    it fails, which staging directories give both callers.
    """
    for attempt in range(_MAX_DOWNLOAD_ATTEMPTS):
        try:
            return await attempt_download()
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
            with WAITS.waiting(reason=FLOOD_WAIT_REASON, what=f"msg {msg_id}", seconds=e.seconds):
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
                # The exception itself travels on to the caller; this line is
                # what ties the final failure to the attempts that led to it,
                # which were silent before.
                logger.warning(
                    "msg %d: download failed after %d attempts",
                    msg_id,
                    _MAX_DOWNLOAD_ATTEMPTS,
                    exc_info=True,
                )
                raise
            # Exponential backoff with jitter to desynchronise retries of many
            # parallel downloads after a shared network blip.
            delay = 2**attempt + random.uniform(0, _RETRY_JITTER_SECONDS)
            logger.debug(
                "msg %d: attempt %d failed (%s: %s), retrying in %.1fs",
                msg_id,
                attempt + 1,
                type(e).__name__,
                e,
                delay,
            )
            with WAITS.waiting(reason="retry after a failure", what=f"msg {msg_id}", seconds=delay):
                await asyncio.sleep(delay)
    # Unreachable while the attempt limit is positive: every iteration either
    # returns the downloaded path or raises on the last attempt. The line
    # exists so that a limit accidentally set to zero fails loudly instead of
    # returning "no file" for every media item.
    raise AssertionError(f"download attempt limit is not positive: {_MAX_DOWNLOAD_ATTEMPTS}")


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
    """Puts one media file of a message on disk, downloading it only if needed.

    Six mechanisms live here, and each guards a failure of its own:

    - a semaphore bounding how many downloads run at once;
    - per-file_id locks, so two messages carrying the same file do not both
      download it -- the second one links to what the first registered;
    - a registry of destination names, so two writers never get one path;
    - a cache of the free-space check, asked once per interval rather than per
      file;
    - read-only connections to the state databases of sibling accounts, kept
      open for the whole export;
    - a snapshot of the downloads in flight, read by the display thread.

    Reuse is tried before the network: another chat of this account, a sibling
    account's export, a Telegram Desktop export.
    """

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
        self._targets = TargetRegistry()

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
        self,
        tl_message,
        media: Media,
        chat_dir: Path,
        chat_id: int = 0,
        media_config: MediaConfig | None = None,
    ) -> tuple[Path | None, DownloadStatus]:
        """Download media file if needed. Returns (local_path, status).

        chat_id: canonical chat ID (positive, as used in messages table).
        The outcomes are the members of DownloadStatus.

        media_config: the settings the rules resolved for this chat -- types
        and size limit are read from it, not from `defaults.media`, so a
        `media` section written for a chat, a type or a folder decides what is
        downloaded there. Without it the defaults the downloader was built with
        apply.
        """
        config = media_config or self.config
        skip = check_skip_reason(media, config)
        if skip:
            # Why: previously skipped files left no DB trace, so verify/count
            # could not distinguish "intentionally skipped" from "missing".
            await self._register_skip(tl_message, media, chat_id, skip)
            return None, skip

        if media.file is None or not media.file.id:
            return await self._download_inner(tl_message, media, chat_dir, chat_id, config)

        # Serialise concurrent downloads of the same file_id (across chats).
        async with self._file_lock(media.file.id):
            return await self._download_inner(tl_message, media, chat_dir, chat_id, config)

    async def _download_inner(
        self, tl_message, media: Media, chat_dir: Path, chat_id: int, config: MediaConfig | None = None
    ) -> tuple[Path | None, DownloadStatus]:
        # Already downloaded?
        if media.file:
            existing = await self.state.get_file(media.file.id, chat_id)
            if existing and existing["status"] == FileStatus.done:
                return Path(existing["local_path"]), DownloadStatus.existing

        # Bytes already on this disk beat bytes over the network, whichever
        # export left them there. Each source is asked in turn, and what
        # follows a hit -- registering the file and reporting where it came
        # from -- is written once for all of them.
        # Callables, not coroutines: a coroutine built for a source that is
        # never reached is never awaited, and Python reports that as a warning.
        sources = (
            (DownloadStatus.reused_chat, lambda: self._try_link_intra_account(media, chat_dir, chat_id)),
            (
                DownloadStatus.reused_tdesktop,
                lambda: self._try_import_tdesktop(tl_message, media, chat_dir),
            ),
            (DownloadStatus.reused_sibling, lambda: self._try_link_sibling(media, chat_dir)),
        )
        for status, attempt in sources:
            reused = await attempt()
            if reused:
                await self._register(tl_message, media, reused, chat_id)
                return reused, status

        chat_dir.mkdir(parents=True, exist_ok=True)
        if not self._has_free_space(chat_dir):
            raise DiskSpaceError(f"Free space less than {self.min_free_bytes // 1024**3} GB")

        target_dir = self._media_target_dir(media, chat_dir)
        max_size = (config or self.config).max_file_size_bytes

        self._clean_stale_staging(target_dir)
        try:
            # TemporaryDirectory drops whatever the download left behind on any
            # exit path, and it can only contain this download's own files.
            # dir=target_dir keeps it on the same filesystem, so the move below
            # is a rename rather than a copy.
            with tempfile.TemporaryDirectory(dir=target_dir, prefix=STAGING_PREFIX) as staging:
                async with self.semaphore:
                    path = await self._download_with_retry(
                        tl_message, Path(staging), media, max_size=max_size
                    )

                if path is None:
                    return None, DownloadStatus.no_file

                local_path = self._move_into_place(Path(path), target_dir)

            await self._register(tl_message, media, local_path, chat_id)
            return local_path, DownloadStatus.downloaded
        except _FileTooLargeError as e:
            logger.debug(
                "file too large (real size %d > limit %d), msg %d",
                e.size,
                max_size,
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
        clean_staging_in(target_dir)

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

    @staticmethod
    def _media_target_dir(media: Media, chat_dir: Path) -> Path:
        """Subdirectory of the chat this media type goes to, created if absent."""
        target_dir = chat_dir / media_subdir(media.type)
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir

    def _place_in_chat_dir(
        self,
        src: Path,
        media: Media,
        chat_dir: Path,
        expected_size: int,
        hand_over: Callable[[Path, Path], bool],
    ) -> Path | None:
        """Put a file that is already on disk into the chat's media subdirectory.

        The three sources a file can be reused from -- another chat of this
        account, a sibling account's export, a tdesktop export -- differ only
        in where the file comes from and how it is handed over; the rest is
        the same for all of them. Blocking: it creates the directory, picks a
        free name by looking at the directory and hands the file over.
        """
        target_dir = self._media_target_dir(media, chat_dir)
        with self._targets.claim(target_dir, src.name, expected_size, reuse=True) as (dst, reused):
            if reused:
                return dst
            if hand_over(src, dst):
                return dst
        return None

    async def _try_link_intra_account(self, media: Media, chat_dir: Path, chat_id: int) -> Path | None:
        """Try to hardlink file from another chat within the same account."""
        if not media.file or not media.file.id:
            return None
        file_id = media.file.id

        existing = await self.state.get_file_any_chat(file_id)
        if existing is None or existing["chat_id"] == chat_id:
            return None

        src = Path(existing["local_path"])
        if not src.exists():
            return None

        def hand_over(source: Path, dst: Path) -> bool:
            if not _link_or_copy(source, dst):
                return False
            logger.debug("linked intra-account: file_id=%d %s -> %s", file_id, source, dst)
            return True

        # to_thread: os.link is a syscall, the copy fallback reads and writes
        # the whole file, and picking a free name looks at the directory. All
        # of it stalls every other download and the Telegram connection while
        # it runs in the loop thread.
        return await asyncio.to_thread(
            self._place_in_chat_dir, src, media, chat_dir, media.file.size or 0, hand_over
        )

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

            def hand_over(source: Path, dst: Path) -> bool:
                if not _link_or_copy(source, dst):
                    return False
                logger.debug("linked from sibling: file_id=%d %s -> %s", file_id, source, dst)
                return True

            placed = self._place_in_chat_dir(src, media, chat_dir, expected_size, hand_over)
            if placed is not None:
                return placed

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

            def hand_over(source: Path, dst: Path) -> bool:
                try:
                    shutil.copy2(source, dst)
                except OSError as e:
                    logger.debug("tdesktop copy failed: %s -> %s (%s) -- skipping", source, dst, e)
                    return False
                logger.debug("imported from tdesktop: msg %d -> %s", msg_id, dst)
                return True

            expected_size = media.file.size if media.file else 0
            placed = self._place_in_chat_dir(src, media, chat_dir, expected_size, hand_over)
            if placed is not None:
                return placed

        return None

    async def _download_with_retry(
        self, tl_message, target_dir: Path, media: Media | None = None, *, max_size: int | None = None
    ) -> str | None:
        msg_id = tl_message.id
        if max_size is None:
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
            return await download_with_retries(
                lambda: self.api.download_media(tl_message, target_dir, progress_cb=_progress_cb),
                msg_id=msg_id,
            )
        finally:
            with self._active_lock:
                self.active_downloads.pop(msg_id, None)
