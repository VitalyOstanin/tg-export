"""Keeping files that carry secrets or private data out of other users' reach.

The session file holds an authorisation key -- with it another local user
enters the account without a password or a second factor. The export tree and
the state database hold the text of every message, phone numbers and the
addresses of active sessions. The global config holds the proxy login and
password. All of them were created with the process umask, which on a typical
system leaves them world-readable.
"""

from __future__ import annotations

import contextlib
import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

# Owner only: rw for files, rwx for directories.
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700


def restrict_file(path: Path) -> None:
    """Drop group and other permissions from a file that holds a secret.

    A last resort for files somebody else created: the ones this package
    creates itself are born private -- see ``create_private_file`` and
    ``write_private_text``. Missing file is not an event; a refused change is,
    and it leaves the secret open, so it goes into the log rather than being
    swallowed. Neither case fails the caller, which is doing its own job.
    """
    if not path.exists():
        return
    try:
        os.chmod(path, PRIVATE_FILE_MODE)
    except OSError as e:
        logger.warning("%s keeps its permissions: %s", path, e)


def create_private_file(path: Path) -> None:
    """Make sure the file exists and only its owner can read it.

    For a file a library creates for us -- Telethon's session, the SQLite
    database: they open it with the process umask, which normally leaves it
    readable by everyone, and tightening it afterwards leaves a window in
    which another local user can open a descriptor that survives the change.
    An empty file created first takes the mode away from the umask, and the
    library then opens what is already there.
    """
    with contextlib.suppress(FileExistsError, OSError):
        os.close(os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, PRIVATE_FILE_MODE))


def write_private_text(path: Path, text: str) -> None:
    """Write text into a file that only its owner can read, private from the start."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    # An existing file keeps the mode it was created with, so say it once more.
    restrict_file(path)


def tighten_if_loose(path: Path) -> None:
    """Restrict a file that is readable beyond its owner, saying so once.

    Used for files the user edits by hand: silently changing the mode of
    something they wrote is worth a line in the log.
    """
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return
    if mode & 0o077 == 0:
        return
    logger.warning("%s has too-permissive mode %o; tightening to 0o600", path, mode & 0o777)
    restrict_file(path)


def ensure_private_dir(path: Path) -> None:
    """Create a directory only the owner can enter, leaving an existing one alone.

    An existing directory keeps its mode: the user may have opened it on
    purpose -- to serve the export over HTTP, for instance -- and that decision
    is theirs, not ours.
    """
    if path.exists():
        return
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, PRIVATE_DIR_MODE)
