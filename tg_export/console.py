"""The one console the whole tool prints to.

Progress, status tables, per-chat lines and diagnostics go to stderr so that
stdout stays reserved for the machine-readable output of the query commands
(list / state show / tg info / tg messages).

Why a module of its own: a command that prints a progress bar -- ``tg send``
among them, which has nothing to do with exporting -- depends on this module
and rich, not on the exporter with all its imports.
"""

from __future__ import annotations

import sys
from typing import NoReturn

import click
from rich.console import Console


def make_console(file=None, *, is_terminal: bool) -> Console:
    """The console the tool prints to, told whether anyone is watching.

    Outside a terminal rich still assumes 80 columns: it wrapped every line to
    that width and padded it with spaces, so a path in a diagnostic message was
    torn in the middle and one message became four lines of a log file. Soft
    wrapping leaves the line as it is -- a log reader wraps it itself, and
    `grep` finds the path.
    """
    return Console(file=file, stderr=file is None, soft_wrap=not is_terminal)


console = make_console(is_terminal=sys.stderr.isatty())


def _no_answer_is_coming(without_an_answer: str) -> NoReturn:
    """Refuse a question nobody is there to answer, naming the way around it.

    Click raises the same Abort for a Ctrl+C and for the end of the input, and
    the tool reported both as an interruption: exit code 130, which a
    supervisor reads as "killed by a signal". A run without a terminal on the
    input -- cron, a systemd timer, CI, `< /dev/null` -- got that code with no
    signal involved and without being told which flag answers the question
    beforehand. A real Ctrl+C keeps the interrupt path: it arrives at a
    terminal, where there was something to read.
    """
    raise click.ClickException(
        f"the question cannot be answered: stdin is at its end (not a terminal). "
        f"Pass {without_an_answer} to run without the question."
    )


def ask(question: str, *, without_an_answer: str, **kwargs):
    """Ask for a value, with the question on stderr.

    `click.prompt` writes the prompt to stdout by default, so a command whose
    stdout is redirected asked its question into the redirect and looked hung.
    Every question of the tool goes through here, so the stream is decided
    once rather than at each call site.

    `without_an_answer` names the option that supplies the value without
    asking; it is what the refusal points at when there is nothing to read.
    """
    try:
        return click.prompt(question, err=True, **kwargs)
    except click.Abort:
        if sys.stdin.isatty():
            raise
        _no_answer_is_coming(without_an_answer)


def confirm(question: str, *, without_an_answer: str, **kwargs) -> bool:
    """Ask a yes/no question, with the question on stderr. See `ask`."""
    try:
        return click.confirm(question, err=True, **kwargs)
    except click.Abort:
        if sys.stdin.isatty():
            raise
        _no_answer_is_coming(without_an_answer)
