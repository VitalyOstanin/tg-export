"""``takeout`` group: finish the Telegram Takeout session of an account."""

from __future__ import annotations

import asyncio
import logging

import click

from tg_export.cli import common
from tg_export.cli.common import fail
from tg_export.errors import EXIT_FAILURE, EXIT_OK

logger = logging.getLogger(__name__)


@click.group()
def takeout() -> None:
    """Manage Telegram Takeout sessions."""


@takeout.command("clear")
@click.argument("name", required=False, default=None)
@click.option("--account", default=None, help=common.ACCOUNT_HELP)
def takeout_clear(name, account) -> None:
    """Finish the takeout session on the server and clear its local id.

    The server side is the point: an export keeps the session alive on purpose,
    so wiping only the local id would leave a takeout running with no way to
    reach it. When the server cannot be reached, the id is kept and the command
    fails, so it can be repeated.
    """
    exit_code = asyncio.run(_takeout_clear(common.one_account(name, account)))
    if exit_code:
        fail(code=exit_code)


# What tells "the server refused" from "the request never arrived": only the
# second one leaves a takeout that may still be running there.
_UNREACHABLE_SERVER = (ConnectionError, TimeoutError, OSError)


async def _takeout_clear(name) -> int:
    async with common.connected_api(name) as (api, account):
        session = api.client.session
        if session is None:
            common.diag(f"  {account}: no session available")
            return EXIT_OK
        old_id = session.takeout_id
        if old_id is None:
            common.diag(f"  {account}: no active takeout session")
            return EXIT_OK
        # Finish it on the server too: an export keeps the session alive on
        # purpose, so wiping only the local id would leave a takeout running
        # with no way to reach it.
        finished = False
        try:
            finished = await api.client.end_takeout(success=True)
        except _UNREACHABLE_SERVER as e:
            # The request never got an answer, so the session may well be alive
            # on the server. The stored id is the only way back to it: wiped
            # here, it would leave the takeout running until it expires, with
            # nothing left to repeat the command with.
            common.error(
                f"  {account}: could not reach the server to finish takeout ({e}); "
                f"the local id is kept -- repeat the command when the connection is back"
            )
            return EXIT_FAILURE
        except Exception as e:
            # A refusal on the merits: whatever the server thinks of the
            # takeout, it will not finish it, and keeping the id changes
            # nothing.
            common.error(f"  {account}: could not finish takeout on the server ({e}); clearing locally")
        if session.takeout_id is not None:
            session.takeout_id = None
            session.save()
        state = "finished" if finished else "cleared locally"
        common.diag(f"  {account}: takeout session {state} (was id={old_id})")
        return EXIT_OK
