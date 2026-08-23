"""``state`` group: inspect and reset the export progress of a chat."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import click

from tg_export.cli import common
from tg_export.cli.common import _fail
from tg_export.console import confirm
from tg_export.errors import (
    EXIT_FAILURE,
    EXIT_OK,
)

logger = logging.getLogger(__name__)


@click.group()
def state():
    """Manage export state (reset, show status, force re-export)."""


@state.command("show")
@click.option("--account", default=None, help=common.ACCOUNT_HELP)
@click.option(
    "--config", type=click.Path(exists=True, path_type=Path), default=None, help="Override config path"
)
@click.option(
    "--output",
    type=click.Path(path_type=Path),
    default=None,
    help="Export output directory, overriding the config",
)
@click.option("--json", "as_json", is_flag=True, help="Output as machine-readable JSON")
@click.argument("chat_id", type=int, required=False)
def state_show(account, config, output, as_json, chat_id):
    """Show export state for all chats or a specific chat."""
    asyncio.run(_state_show(account, config, output, chat_id, as_json))


async def _state_show(account, config_override, output_override, chat_id, as_json=False):
    """Show the export state: one chat when it is named, the whole account otherwise."""
    async with common._opened_state(account, config_override, output_override) as (st, _, account):
        if chat_id:
            chat_state = await st.get_chat_state(chat_id)
            if not chat_state:
                if as_json:
                    click.echo(json.dumps(None))
                else:
                    common._diag(f"No state for chat {chat_id}")
                return
            msg_count = await st.count_messages(chat_id)
            if as_json:
                payload = {
                    "chat_id": chat_id,
                    "last_msg_id": chat_state["last_msg_id"],
                    "oldest_msg_id": chat_state["oldest_msg_id"],
                    "full_history": bool(chat_state["full_history"]),
                    "messages_in_db": msg_count,
                    "updated_at": chat_state["updated_at"],
                }
                click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
                return
            click.echo(f"Chat {chat_id}:")
            click.echo(f"  last_msg_id:   {chat_state['last_msg_id']}")
            click.echo(f"  oldest_msg_id: {chat_state['oldest_msg_id']}")
            click.echo(f"  full_history:  {bool(chat_state['full_history'])}")
            click.echo(f"  messages in DB: {msg_count}")
            click.echo(f"  updated_at:    {chat_state['updated_at']}")
        else:
            rows = await st.list_chat_states()
            if as_json:
                payload = [
                    {
                        "chat_id": d["chat_id"],
                        "messages": d["msg_count"],
                        "last_msg_id": d["last_msg_id"],
                        "oldest_msg_id": d["oldest_msg_id"],
                        "full_history": bool(d["full_history"]),
                        "updated_at": d["updated_at"],
                    }
                    for d in (dict(r) for r in rows)
                ]
                click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
                return
            if not rows:
                common._diag("No export state records.")
                return
            click.echo(
                f"{'chat_id':>15}  {'msgs':>6}  {'last_id':>8}  {'oldest_id':>9}  {'full':>4}  updated_at"
            )
            click.echo("-" * 80)
            for r in rows:
                r = dict(r)
                full = "yes" if r["full_history"] else "no"
                click.echo(
                    f"{r['chat_id']:>15}  {r['msg_count']:>6}  {r['last_msg_id']:>8}  {r['oldest_msg_id']:>9}  {full:>4}  {r['updated_at']}"
                )


@state.command("reset")
@click.option("--account", default=None, help=common.ACCOUNT_HELP)
@click.option(
    "--config", type=click.Path(exists=True, path_type=Path), default=None, help="Override config path"
)
@click.option(
    "--output",
    type=click.Path(path_type=Path),
    default=None,
    help="Export output directory, overriding the config",
)
@click.option("--all", "reset_all", is_flag=True, help="Reset all chats")
@click.option("--delete-messages", is_flag=True, help="Also delete messages from DB")
@click.option("--yes", is_flag=True, help="Skip confirmation for destructive resets")
@click.argument("chat_id", type=int, required=False)
def state_reset(account, config, output, reset_all, delete_messages, yes, chat_id):
    """Reset export state to force re-download. Specify chat_id or --all."""
    if chat_id is None and not reset_all:
        raise click.UsageError("Specify chat_id or --all")
    exit_code = asyncio.run(
        _state_reset(account, config, output, reset_all, delete_messages, chat_id, skip_confirm=yes)
    )
    if exit_code:
        _fail(code=exit_code)


async def _state_reset(
    account,
    config_override,
    output_override,
    reset_all,
    delete_messages,
    chat_id,
    *,
    skip_confirm: bool = False,
):
    async with common._opened_state(account, config_override, output_override) as (st, _, account):
        if reset_all:
            # essential: the question below authorises rewinding -- and with
            # --delete-messages, emptying -- the whole account. A prompt whose
            # subject was hidden by --quiet is a prompt nobody can answer.
            counts = await st.count_all_rows()
            common._diag(f"Account: {account}", essential=True)
            common._diag(common._db_rows_line(counts), essential=True)
            what = (
                "Delete every message and file record and rewind all chats?"
                if delete_messages
                else ("Rewind export progress of all chats?")
            )
            if not skip_confirm and not confirm(what):
                common._diag("Cancelled.", essential=True)
                return EXIT_OK
            await st.reset_chat_progress(delete_messages=delete_messages)
            common._diag("Reset all chats.")
        else:
            chat_state = await st.get_chat_state(chat_id)
            if not chat_state:
                common._diag(f"No state for chat {chat_id}", essential=True)
                return EXIT_FAILURE
            if delete_messages:
                # essential: for the same reason as the branch above -- the
                # messages and file records of the chat go for good, and the
                # question about it must not be hidden by --quiet.
                counts = await st.count_chat_rows(chat_id)
                common._diag(f"Chat: {chat_id}", essential=True)
                common._diag(common._db_rows_line(counts), essential=True)
                if not skip_confirm and not confirm("Delete every message and file record of this chat?"):
                    common._diag("Cancelled.", essential=True)
                    return EXIT_OK
            await st.reset_chat_progress(chat_id, delete_messages=delete_messages)
            msg = f"Reset chat {chat_id}."
            if delete_messages:
                msg += " Messages and files records deleted."
            common._diag(msg)
        return EXIT_OK
