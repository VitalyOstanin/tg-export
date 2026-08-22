"""Advisory locks that keep two tg-export processes off the same resource.

Both the state database and the Telegram session file tolerate exactly one
writer. SQLite alone answers contention with a bare "database is locked", and
Telethon answers it by corrupting the authorisation key in the session file, so
each resource gets an explicit lock with a message naming what is busy.

The lock file is never deleted. flock is tied to the inode, not to the name:
removing the file on release let a process that had already opened it keep the
lock on the orphaned inode while the next process created a fresh file and
locked that one, giving two processes the same resource.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from tg_export.errors import ProcessLockError

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None  # type: ignore[assignment]


class ProcessLock:
    """An advisory lock on `<path>.lock`, held for the life of the process.

    On platforms without flock the lock is a no-op: there is nothing portable
    to fall back on, and the failure mode it guards against is unchanged from
    before this existed.
    """

    def __init__(self, path: Path, busy_message: str):
        self._path = Path(f"{path}.lock")
        self._busy_message = busy_message
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if _fcntl is None or self._fd is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as e:
            os.close(fd)
            raise ProcessLockError(self._busy_message) from e
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is None or _fcntl is None:
            self._fd = None
            return
        with contextlib.suppress(OSError):
            _fcntl.flock(self._fd, _fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(self._fd)
        self._fd = None
