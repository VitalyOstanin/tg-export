"""Base class for tg-export domain errors.

All errors raised by tg-export's own logic derive from :class:`TgExportError`.
This lets the CLI entry point catch domain failures and report them as short
messages (full traceback only under ``--debug``) instead of dumping a stack
trace, and lets callers catch the whole family with a single ``except``.
"""

from __future__ import annotations


class TgExportError(Exception):
    """Base class for all tg-export domain errors."""
