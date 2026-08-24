"""``state`` group: inspect and reset the export progress of a chat."""

from __future__ import annotations

import asyncio
import json
import logging

import click

from tg_export.cli import common
from tg_export.cli.common import fail
from tg_export.console import confirm
from tg_export.errors import (
    EXIT_FAILURE,
    EXIT_OK,
)

logger = logging.getLogger(__name__)


@click.group()
def state() -> None:
    """Manage export state (reset, show status, force re-export)."""


# One place per column: the header, every row and the rule under the header are
# built from this. The widths used to be written twice and the length of the
# rule a third time, as the unrelated number 80 -- which matched neither the
# header (62 characters) nor a row with an ISO date (77).
_STATE_TABLE_COLUMNS = (
    ("chat_id", 15),
    ("msgs", 6),
    ("last_id", 8),
    ("oldest_id", 9),
    ("full", 4),
    ("updated_at", 0),
)


def _state_table_row(values) -> str:
    """One line of the `state show` table, columns as wide as the header says."""
    return "  ".join(
        f"{value:>{width}}" if width else str(value)
        for value, (_, width) in zip(values, _STATE_TABLE_COLUMNS, strict=True)
    )


_STATE_TABLE_HEADER = _state_table_row([title for title, _ in _STATE_TABLE_COLUMNS])


@state.command("show")
@common.over_an_export
@click.option("--json", "as_json", is_flag=True, help="Output as machine-readable JSON")
@click.argument("chat_id", type=int, required=False)
def state_show(account, config, output, as_json, chat_id) -> None:
    """Show export state for all chats or a specific chat."""
    asyncio.run(_state_show(account, config, output, chat_id, as_json))


async def _state_show(account, config_override, output_override, chat_id, as_json=False) -> None:
    """Show the export state: one chat when it is named, the whole account otherwise."""
    async with common.opened_state(account, config_override, output_override) as (st, _, account):
        if chat_id:
            chat_state = await st.get_chat_state(chat_id)
            if not chat_state:
                if as_json:
                    click.echo(json.dumps(None))
                else:
                    common.diag(f"No state for chat {chat_id}")
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
                common.diag("No export state records.")
                return
            header = _STATE_TABLE_HEADER
            click.echo(header)
            click.echo("-" * len(header))
            for r in rows:
                r = dict(r)
                values = (
                    r["chat_id"],
                    r["msg_count"],
                    r["last_msg_id"],
                    r["oldest_msg_id"],
                    "yes" if r["full_history"] else "no",
                    r["updated_at"],
                )
                click.echo(_state_table_row(values))


@state.command("reset")
@common.over_an_export
@click.option("--all", "reset_all", is_flag=True, help="Reset all chats")
@click.option("--delete-messages", is_flag=True, help="Also delete messages from DB")
@click.option("--yes", is_flag=True, help="Skip confirmation for destructive resets")
@click.argument("chat_id", type=int, required=False)
def state_reset(account, config, output, reset_all, delete_messages, yes, chat_id) -> None:
    """Reset export state to force re-download. Specify chat_id or --all."""
    if chat_id is None and not reset_all:
        raise click.UsageError("Specify chat_id or --all")
    if chat_id is not None and reset_all:
        # The two ask for different things, and the chat_id used to be dropped
        # without a word: `state reset --all 123` rewound the whole account
        # while reading as a request about one chat. The neighbouring commands
        # refuse such a pair the same way.
        raise click.UsageError("Specify either chat_id or --all, not both")
    exit_code = asyncio.run(
        _state_reset(account, config, output, reset_all, delete_messages, chat_id, skip_confirm=yes)
    )
    if exit_code:
        fail(code=exit_code)


async def _state_reset(
    account,
    config_override,
    output_override,
    reset_all,
    delete_messages,
    chat_id,
    *,
    skip_confirm: bool = False,
) -> int:
    async with common.opened_state(account, config_override, output_override) as (st, _, account):
        if reset_all:
            # essential: the question below authorises rewinding -- and with
            # --delete-messages, emptying -- the whole account. A prompt whose
            # subject was hidden by --quiet is a prompt nobody can answer.
            counts = await st.count_all_rows()
            common.diag(f"Account: {account}", essential=True)
            common.diag(common.db_rows_line(counts), essential=True)
            what = (
                "Delete every message and file record and rewind all chats?"
                if delete_messages
                else ("Rewind export progress of all chats?")
            )
            if not skip_confirm and not confirm(what, without_an_answer="--yes"):
                common.diag("Cancelled.", essential=True)
                return EXIT_OK
            await st.reset_chat_progress(delete_messages=delete_messages)
            common.diag("Reset all chats.")
        else:
            chat_state = await st.get_chat_state(chat_id)
            if not chat_state:
                common.diag(f"No state for chat {chat_id}", essential=True)
                return EXIT_FAILURE
            if delete_messages:
                # essential: for the same reason as the branch above -- the
                # messages and file records of the chat go for good, and the
                # question about it must not be hidden by --quiet.
                counts = await st.count_chat_rows(chat_id)
                common.diag(f"Chat: {chat_id}", essential=True)
                common.diag(common.db_rows_line(counts), essential=True)
                if not skip_confirm and not confirm(
                    "Delete every message and file record of this chat?", without_an_answer="--yes"
                ):
                    common.diag("Cancelled.", essential=True)
                    return EXIT_OK
            await st.reset_chat_progress(chat_id, delete_messages=delete_messages)
            msg = f"Reset chat {chat_id}."
            if delete_messages:
                msg += " Messages and files records deleted."
            common.diag(msg)
        return EXIT_OK
