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

    Silent when the file is missing or the filesystem refuses the change: the
    caller is doing its own job and a mode that cannot be set is not a reason
    to fail it.
    """
    with contextlib.suppress(OSError):
        if path.exists():
            os.chmod(path, PRIVATE_FILE_MODE)


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
