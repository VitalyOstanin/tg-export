"""``takeout`` group: finish the Telegram Takeout session of an account."""

from __future__ import annotations

import asyncio
import logging

import click

from tg_export.cli import common

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Takeout management
# ---------------------------------------------------------------------------


@click.group()
def takeout():
    """Manage Telegram Takeout sessions."""


@takeout.command("clear")
@click.argument("name", required=False, default=None)
def takeout_clear(name):
    """Clear stale takeout session ID from local session file."""
    asyncio.run(_takeout_clear(name))


async def _takeout_clear(name):
    async with common._connected_api(name) as (api, account):
        session = api.client.session
        if session is None:
            common._diag(f"  {account}: no session available")
            return
        old_id = session.takeout_id
        if old_id is None:
            common._diag(f"  {account}: no active takeout session")
            return
        # Finish it on the server too: an export keeps the session alive on
        # purpose, so wiping only the local id would leave a takeout running
        # with no way to reach it.
        finished = False
        try:
            finished = await api.client.end_takeout(success=True)
        except Exception as e:
            common._error(f"  {account}: could not finish takeout on the server ({e}); clearing locally")
        if session.takeout_id is not None:
            session.takeout_id = None
            session.save()
        state = "finished" if finished else "cleared locally"
        common._diag(f"  {account}: takeout session {state} (was id={old_id})")
