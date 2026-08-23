"""The one console the whole tool prints to.

Progress, status tables, per-chat lines and diagnostics go to stderr so that
stdout stays reserved for the machine-readable output of the query commands
(list / state show / tg info / tg messages).

Why a module of its own: a command that prints a progress bar -- ``tg send``
among them, which has nothing to do with exporting -- depends on this module
and rich, not on the exporter with all its imports.
"""

from __future__ import annotations

from rich.console import Console

console = Console(stderr=True)
