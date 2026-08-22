"""The one console the whole tool prints to.

Progress, status tables, per-chat lines and diagnostics go to stderr so that
stdout stays reserved for the machine-readable output of the query commands
(list / state show / tg info / tg messages).

Why a module of its own: the declaration used to live in ``exporter``, and
every command that prints a progress bar -- ``tg send`` among them, which has
nothing to do with exporting -- pulled in the exporter with all its imports to
reach it. Here the dependency is one small module and rich.
"""

from __future__ import annotations

from rich.console import Console

console = Console(stderr=True)
