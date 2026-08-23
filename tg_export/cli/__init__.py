"""Command-line interface: the ``main`` group and the entry point.

The commands themselves live one module per group:

===============  ======================================================
``cli.auth``     credentials, login, session check
``cli.account``  list accounts, set the default one, remove one
``cli.takeout``  finish the Telegram Takeout session
``cli.tg``       direct API calls: info, messages, send, download
``cli.state``    inspect and reset the export state of a chat
``cli.export``   ``config``, ``list``, ``init``, ``run``, ``purge``, ``verify``
``cli.common``   output, exit, account and state plumbing they share
===============  ======================================================

Each module declares its own group or command and this module attaches them to
``main``, so no command module has to import the entry point back.

The flow behind ``run`` is ``cli -> config -> api -> exporter ->
state/media -> renderer``; see the "Устройство пакета" section of
CONTRIBUTING.md for what each module owns.

Imports of project modules are deliberately made inside the command bodies
rather than at module level: the entry point is a console script, and
importing telethon, jinja2 and the exporter on every ``--help`` costs about a
second of startup. Keep them where they are unless the module is needed by
the group definitions themselves. The standard library is the other way round
-- it is already loaded by the interpreter, so it belongs in the header of the
module; a test enforces that boundary.

The names re-exported below are what the rest of the project and the tests
address as ``tg_export.cli.<name>``; each one is owned by the module it comes
from.
"""

from __future__ import annotations

import logging

import click

# Modules, not the group objects inside them: `tg_export.cli.tg` then means the
# module in every context, and nothing shadows it at package level.
from tg_export.cli import account, auth, common, export, state, takeout, tg
from tg_export.cli.common import STATE_DB_NAME, _quiet_third_party_loggers, _resolve_log_level
from tg_export.errors import EXIT_FAILURE, EXIT_OK, EXIT_SIGINT, TgExportError

logger = logging.getLogger(__name__)

# Re-exports: the names above are addressed as tg_export.cli.<name> by the rest
# of the project and by the tests, though each is owned by the module it comes
# from. Patching one of them affects only this binding -- the owning module is
# the address for that.
__all__ = [
    "EXIT_FAILURE",
    "EXIT_OK",
    "EXIT_SIGINT",
    "STATE_DB_NAME",
    "TgExportError",
    "account",
    "auth",
    "common",
    "export",
    "main",
    "run_cli",
    "state",
    "takeout",
    "tg",
]


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="tg-export", prog_name="tg-export")
@click.option("--debug", is_flag=True, default=False, help="Enable debug logging (forces DEBUG level)")
@click.option(
    "--log-level",
    default=None,
    help=(
        "Log level (DEBUG/INFO/WARNING/ERROR/CRITICAL). Overrides the LOG_LEVEL env var; "
        "--debug overrides this. Append ':all' (DEBUG:all) to include telethon and aiosqlite."
    ),
)
@click.option(
    "--quiet",
    "-q",
    is_flag=True,
    default=False,
    help="Suppress progress and status output (errors and the final summary are still shown).",
)
def main(debug, log_level, quiet):
    """tg-export: Flexible Telegram data export tool."""
    from rich.logging import RichHandler

    from tg_export.console import console as export_console

    level, include_libraries = _resolve_log_level(debug, log_level)

    logging.basicConfig(
        level=level,
        format="%(name)s %(message)s",
        handlers=[RichHandler(console=export_console, rich_tracebacks=True, show_path=debug)],
    )
    _quiet_third_party_loggers(level, include_libraries=include_libraries)
    # One home for the two flags. They used to be written here twice -- into
    # these globals and into ctx.obj -- and read from both, while ctx.obj["debug"]
    # was read nowhere; run_cli reports errors outside any click context and
    # needs the module-level value anyway.
    common._QUIET = quiet
    common._DEBUG = debug


# Groups and top-level commands declare themselves in their own modules; the
# assembly happens here so that none of them imports the entry point back.
for _command in (
    auth.auth,
    account.account,
    takeout.takeout,
    tg.tg,
    state.state,
    export.show_config,
    export.list_chats,
    export.init_config,
    export.run_export,
    export.purge_chat,
    export.verify_files,
):
    main.add_command(_command)


def run_cli() -> None:
    """Console-script entry point.

    Wraps the Click app so that tg-export domain errors (TgExportError and its
    subclasses) are reported as a short message on stderr, instead of dumping a
    stack trace. The exit code comes from the error class itself -- the table
    lives in tg_export.errors -- and the full traceback is shown only when
    --debug is passed.
    """
    try:
        # standalone_mode=False: in standalone mode Click catches
        # KeyboardInterrupt itself, prints "Aborted!" and exits with 1, so the
        # handler below never ran and Ctrl+C outside the export loop was
        # indistinguishable from a failed command. What Click reports in that
        # mode is reproduced by the clauses below.
        result = main.main(standalone_mode=False)
    except click.Abort:
        # What Click raises for a Ctrl+C or a Ctrl+D on a prompt. Report it the
        # way `run` already reports an interrupted export: 128 + SIGINT.
        click.echo("Interrupted.", err=True)
        raise SystemExit(EXIT_SIGINT) from None
    except KeyboardInterrupt:
        # An interruption that arrived outside main.main -- while a lazy module
        # was being imported, or between the parse and the call.
        click.echo("Interrupted.", err=True)
        raise SystemExit(EXIT_SIGINT) from None
    except click.ClickException as e:
        # Usage errors and their kin: their own message and code, printed by
        # Click itself only while standalone mode is on.
        e.show()
        raise SystemExit(e.exit_code) from e
    except TgExportError as e:
        if common._DEBUG:
            raise
        click.echo(f"Error: {e}", err=True)
        # The code comes from the error class (see tg_export.errors), so a new
        # failure kind brings its own without touching this handler.
        raise SystemExit(e.exit_code) from e
    except Exception as e:
        # Everything outside the domain hierarchy -- telethon, sqlite, sockets.
        # Such an exception used to print a full traceback, and during an export
        # it landed on top of the live progress widget: a network failure read
        # as a broken tool.
        if common._DEBUG:
            raise
        click.echo(f"Error: {type(e).__name__}: {e}", err=True)
        click.echo("Run with --debug for the full traceback.", err=True)
        raise SystemExit(EXIT_FAILURE) from e

    # `ctx.exit(code)` -- how a command here reports a refusal -- is returned as
    # the code once standalone mode is off, instead of ending the process.
    if isinstance(result, int) and result != EXIT_OK:
        raise SystemExit(result)
