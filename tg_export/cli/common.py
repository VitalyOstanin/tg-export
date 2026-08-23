"""Helpers every command group shares: output, exit, account and state plumbing.

The two verbosity flags live here as module state because ``main()`` sets them
from the global options and every group reads them afterwards; they are read
through the module (``common._QUIET``) so that a change is seen by the code
that already imported this module.
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
from typing import Any, NoReturn

import click

from tg_export.auth import AccountManager
from tg_export.errors import EXIT_FAILURE, EXIT_OK

logger = logging.getLogger(__name__)


# Set by main() from the global --debug flag; used by run_cli() to decide
# whether to show a full traceback or a short message for domain errors.
_DEBUG = False


def _mgr() -> AccountManager:
    mgr = AccountManager()
    mgr.ensure_dirs()
    return mgr


# Set by main() from the global --quiet flag. When True, non-essential status
# and progress messages are suppressed; errors and final summaries still print.
_QUIET = False


def _diag(message: str, *, essential: bool = False, **kwargs) -> None:
    """Print a diagnostic / status / error message to stderr.

    stdout is reserved for machine-readable output of query commands
    (list / state show / tg info / tg messages), so everything that is not
    that data -- progress, confirmations, error notices -- goes to stderr.

    Under --quiet only ``essential`` messages (errors, final summaries) are
    printed; routine status/progress lines are suppressed.
    """
    if _QUIET and not essential:
        return
    click.echo(message, err=True, **kwargs)


def _error(message: str, **kwargs) -> None:
    """Print a message the user must see even under --quiet.

    Every reason a command refuses to do its job goes through here, so that a
    non-zero exit code is never the only thing the user gets. Marking such
    messages one by one on ``_diag`` is not enough: the flag is easy to forget
    exactly where the refusal happens.
    """
    _diag(message, essential=True, **kwargs)


# EXIT_OK / EXIT_FAILURE / EXIT_SIGINT come from tg_export.errors, where they
# sit next to the exception classes they belong to; they are imported above and
# re-exported here because commands read them by these names.


def _db_rows_line(counts: dict[str, int]) -> str:
    """The row counts a destructive command is about to remove, as one line.

    Printed by `purge` and by both branches of `state reset --delete-messages`,
    always as an essential line: a prompt whose subject --quiet hid is a prompt
    nobody can answer. The tables come from the count itself, so a table added
    to CHAT_TABLES shows up in the warning without a second edit here.
    """
    return "  DB: " + ", ".join(f"{table}={number}" for table, number in counts.items())


def _fail(message: str | None = None, code: int = EXIT_FAILURE) -> NoReturn:
    """Report a refusal and end the command with ``code``.

    One place decides how a command stops, instead of each site pairing its own
    message with its own exception. ``ctx.exit`` is Click's documented way out;
    the class it raises is not part of the public namespace, so reaching for it
    directly tied the code to an implementation detail of the dependency.

    Outside a Click invocation (a helper called straight from a test) there is
    no context to exit, and SystemExit carries the same code.

    Never call this from code running under a broad ``except Exception``: the
    class ``ctx.exit`` raises inherits from RuntimeError, so such a handler
    would turn the exit into an ordinary error and swallow the code. That is
    why refusals live in the command layer, not inside the export loop, where
    broad handlers are the rule.

    NoReturn is part of the contract: both branches raise, and without the
    annotation the code after a call reads as reachable -- `_resolve_output`
    went on to load a config file it had just refused over.
    """
    if message:
        _error(message)
    ctx = click.get_current_context(silent=True)
    if ctx is not None:
        ctx.exit(code)
    raise SystemExit(code)


def _export_exit_code(*, signum: int | None, error_count: int) -> int:
    """Turn the outcome of an export into an exit code.

    An interrupted run outranks a failed one: the signal is what the caller
    asked for, and 128 + signum tells the shell which one arrived.
    """
    if signum:
        return 128 + signum
    return EXIT_FAILURE if error_count else EXIT_OK


# The state database lives inside the output directory, so its name is part of
# the export layout rather than a local detail of any one command.
STATE_DB_NAME = ".tg-export-state.db"


@contextlib.asynccontextmanager
async def _connected_api(account_name):
    """Connect to Telegram for one account; yield ``(api, account)``.

    Every command needs the same prologue -- resolve the account, load the
    credentials and the proxy, build TgApi, connect -- and the same guarantee
    that the socket is closed afterwards. Returning a connected object instead
    left that guarantee to a line of docstring.
    """
    from tg_export.api import TgApi

    mgr = _mgr()
    account = mgr.resolve_account(account_name)
    api_id, api_hash = mgr.load_credentials()
    proxy = mgr.load_proxy()
    api = TgApi(mgr.session_path(account), api_id, api_hash, proxy=proxy)
    async with api:
        yield api, account


# The way out of a missing config, named by every command that needs one.
MISSING_CONFIG_HINT = "Create it with: tg-export init --account {account}"


def _resolve_output(
    account: str | None,
    config_override: Path | None,
    output_override: Path | None,
    *,
    missing_config_hint: str | None = MISSING_CONFIG_HINT,
) -> tuple[str, Any, Path]:
    """Resolve ``(account, config, output_base)`` from the command options.

    Reported as a failure when the config file is missing: without it there is
    neither an output directory nor a state database to work on. The refusal
    names the way out by default -- it used to be passed by ``run`` alone, and
    the four other commands over a config stopped at the same wall without
    saying what to do next. ``{account}`` in the hint is filled in.
    """
    from tg_export.config import load_config

    mgr = _mgr()
    account = mgr.resolve_account(account)
    config_path = mgr.resolve_config(account, config_override)
    if not config_path.exists():
        _error(f"Config not found: {config_path}")
        if missing_config_hint:
            _error(missing_config_hint.format(account=account))
        _fail()

    cfg = load_config(config_path)
    if output_override:
        output_base = output_override.expanduser()
    else:
        output_base = _account_output_dir(Path(cfg.output.path), account)
    return account, cfg, output_base


def _account_output_dir(base: Path, account: str) -> Path:
    """Return the export directory of one account under the configured base.

    The documented layout is ``{output.path}/{alias}``: accounts sharing one
    directory would share one state database, and the dedup scan would take a
    neighbour's files for its own. Two shapes are left alone -- a base whose
    last component is already the alias, and a base that already holds an
    export -- because configs written by earlier versions named the account
    directory itself, and appending the alias there would silently restart the
    export in an empty ``{path}/{alias}/{alias}``.
    """
    if base.name == account or (base / STATE_DB_NAME).exists():
        return base
    return base / account


@contextlib.asynccontextmanager
async def _opened_state(account, config_override, output_override, *, required: bool = True):
    """Open the state database of an account; yield ``(state, output_base, account)``.

    With ``required=False`` a missing database yields ``(None, ...)`` instead of
    exiting: `verify` has nothing to check on a fresh output directory, which is
    not a failure.
    """
    from tg_export.state import ExportState

    account, _, output_base = _resolve_output(account, config_override, output_override)
    state_path = output_base / STATE_DB_NAME

    if not state_path.exists():
        if required:
            _fail("No state database found.")
        yield None, output_base, account
        return

    async with ExportState(state_path) as state:
        yield state, output_base, account


_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# Default cut length for message text in `tg messages`; 0 disables the cut.
DEFAULT_MESSAGE_TEXT_LENGTH = 200

# Help for every --account option: one wording, and the command it names is real.
ACCOUNT_HELP = "Account alias (default: the one set by 'account default')"


# Libraries whose own debug stream would bury ours: telethon logs every MTProto
# packet, aiosqlite every statement. --debug means "show me everything tg-export
# knows", so their level is set independently of ours; LOG_LEVEL=DEBUG:all (or
# --log-level DEBUG:all) is what turns the libraries on as well.
_THIRD_PARTY_LOGGERS = ("telethon", "aiosqlite")
_ALL_SUFFIX = ":ALL"


def _resolve_log_level(debug: bool, log_level: str | None) -> tuple[int, bool]:
    """Resolve the effective log level and whether libraries share it.

    Priority (highest first): --debug flag, --log-level flag, LOG_LEVEL env var,
    default WARNING. --debug always wins so it stays a quick "show me everything"
    switch regardless of the environment. A trailing ``:all`` (``DEBUG:all``)
    additionally lifts the libraries off their WARNING floor.
    """

    raw = log_level or os.environ.get("LOG_LEVEL") or ""
    include_libraries = raw.strip().upper().endswith(_ALL_SUFFIX)
    if debug:
        return logging.DEBUG, include_libraries
    if not raw:
        return logging.WARNING, False
    name = raw.strip().upper()
    if include_libraries:
        name = name[: -len(_ALL_SUFFIX)]
    if name not in _LOG_LEVELS:
        raise click.BadParameter(
            f"unknown log level {raw!r}; expected one of {', '.join(_LOG_LEVELS)}"
            f" (append '{_ALL_SUFFIX.lower()}' to include the libraries)",
            param_hint="--log-level",
        )
    return getattr(logging, name), include_libraries


def _quiet_third_party_loggers(level: int, *, include_libraries: bool = False) -> None:
    """Hold third-party loggers at WARNING unless the caller asked for them."""
    for name in _THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(level if include_libraries else max(level, logging.WARNING))
