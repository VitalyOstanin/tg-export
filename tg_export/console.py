"""The one console the whole tool prints to.

Progress, status tables, per-chat lines and diagnostics go to stderr so that
stdout stays reserved for the machine-readable output of the query commands
(list / state show / tg info / tg messages).

Why a module of its own: a command that prints a progress bar -- ``tg send``
among them, which has nothing to do with exporting -- depends on this module
and rich, not on the exporter with all its imports.
"""

from __future__ import annotations

import click
from rich.console import Console

console = Console(stderr=True)


def ask(question: str, **kwargs):
    """Ask for a value, with the question on stderr.

    `click.prompt` writes the prompt to stdout by default, so a command whose
    stdout is redirected asked its question into the redirect and looked hung.
    Every question of the tool goes through here, so the stream is decided
    once rather than at each call site.
    """
    return click.prompt(question, err=True, **kwargs)


def confirm(question: str, **kwargs) -> bool:
    """Ask a yes/no question, with the question on stderr. See `ask`."""
    return click.confirm(question, err=True, **kwargs)
