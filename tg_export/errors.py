"""Base class for tg-export domain errors.

All errors raised by tg-export's own logic derive from :class:`TgExportError`.
This lets the CLI entry point catch domain failures and report them as short
messages (full traceback only under ``--debug``) instead of dumping a stack
trace, and lets callers catch the whole family with a single ``except``.
"""

from __future__ import annotations


class TgExportError(Exception):
    """Base class for all tg-export domain errors."""


class TakeoutUnavailableError(TgExportError):
    """Takeout could not be started while it was required.

    Raised only when the caller asked for Takeout explicitly (``run
    --require-takeout``): without that flag an unavailable Takeout is reported
    and the export falls back to the regular API.
    """


class ProcessLockError(TgExportError, RuntimeError):
    """Another process holds the lock on a resource this one needs.

    The state database and the Telegram session file each tolerate one writer;
    the lock turns a corrupted file or a bare "database is locked" into a clear
    statement of what is busy.
    """
