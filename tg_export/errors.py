"""Domain errors and the exit codes they map to.

All errors raised by tg-export's own logic derive from :class:`TgExportError`.
This lets the CLI entry point catch domain failures and report them as short
messages (full traceback only under ``--debug``) instead of dumping a stack
trace, and lets callers catch the whole family with a single ``except``.

The exit code belongs here as well, next to the class it describes. It used to
be decided at the point of catching, so the entry point knew one code for the
whole hierarchy and a failure needing a different one could not be introduced
without editing it:

===========================  ====  =====================================
error                        code  meaning
===========================  ====  =====================================
(no error)                   0     the command did what it was asked
TgExportError and subclasses 1     the command refused or failed
(click's own UsageError)     2     the arguments do not form a command
terminated by signal N       128+N the shell convention, 130 for SIGINT
===========================  ====  =====================================

A subclass that needs its own code overrides :attr:`TgExportError.exit_code`.
"""

from __future__ import annotations

# 0 -- success, 1 -- the command reported a failure, 2 -- Click's own usage
# error, 128 + N -- terminated by signal N (130 for SIGINT, 143 for SIGTERM),
# the convention every shell already understands.
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_SIGINT = 130


class TgExportError(Exception):
    """Base class for all tg-export domain errors."""

    #: What the process returns when this error reaches the entry point.
    exit_code: int = EXIT_FAILURE


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
