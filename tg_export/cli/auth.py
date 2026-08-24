"""``auth`` group: API credentials, interactive login, session check."""

from __future__ import annotations

import asyncio
import json
import logging

import click

from tg_export.cli import common
from tg_export.cli.common import fail
from tg_export.console import ask
from tg_export.errors import (
    EXIT_FAILURE,
    EXIT_OK,
)
from tg_export.format import display_name

logger = logging.getLogger(__name__)


@click.group()
def auth() -> None:
    """Telegram authentication: credentials, login, session check."""


@auth.command("credentials")
@click.option("--api-id", type=int, default=None, help="Telegram API ID")
@click.option(
    "--api-hash",
    default=None,
    help="Telegram API Hash (input is hidden; passing it as an option puts it in the shell history)",
)
def auth_credentials(api_id, api_hash) -> None:
    """Set Telegram API credentials (api_id and api_hash)."""
    if api_id is None:
        api_id = ask("API ID (from https://my.telegram.org)", type=int, without_an_answer="--api-id")
    if api_hash is None:
        # hide_input: the hash authenticates the application the same way a
        # password does; typed in the open it stays in the shell history.
        api_hash = ask("API Hash", hide_input=True, without_an_answer="--api-hash")
    mgr = common.account_manager()
    mgr.save_credentials(api_id=api_id, api_hash=api_hash)
    common.diag("Credentials saved.")


@auth.command("add")
@click.option("--name", default=None, help="Account alias")
def auth_add(name) -> None:
    """Add a new Telegram account (interactive login)."""
    if name is None:
        name = ask("Account alias", without_an_answer="--name")
    mgr = common.account_manager()
    cred_path = mgr.config_dir / "api_credentials.yaml"
    if not cred_path.exists():
        fail("No API credentials found. Run 'tg-export auth credentials' first.")
    asyncio.run(mgr.add_account(name))
    common.diag(f"Account '{name}' added successfully.")


@auth.command("check")
@click.argument("name", required=False, default=None)
@click.option("--account", default=None, help=common.ACCOUNT_HELP)
@click.option("--json", "as_json", is_flag=True, help="Output as machine-readable JSON")
def auth_check(name, account, as_json) -> None:
    """Check if account sessions are valid."""
    name = common.one_account(name, account)
    exit_code = asyncio.run(_auth_check(name, as_json))
    if exit_code:
        fail(code=exit_code)


async def _auth_check(name, as_json=False) -> int | None:
    """Connect as each account in turn and report whether it is usable."""
    from tg_export.api import TgApi

    mgr = common.account_manager()
    accounts = [name] if name else mgr.list_accounts()
    if not accounts:
        if as_json:
            click.echo(json.dumps([], ensure_ascii=False, indent=2))
        else:
            common.diag("No accounts configured.")
        return

    api_id, api_hash = mgr.load_credentials()
    results = []
    for acc in accounts:
        session = mgr.session_path(acc)
        if not session.exists():
            results.append({"account": acc, "status": "session_missing"})
            if not as_json:
                common.error(f"  {acc}: session file missing")
            continue
        proxy = mgr.load_proxy()
        try:
            async with TgApi(session, api_id, api_hash, proxy=proxy) as api:
                if await api.client.is_user_authorized():
                    me = await api.client.get_me()
                    me_id = getattr(me, "id", None)
                    name = display_name(me)
                    results.append(
                        {
                            "account": acc,
                            "status": "ok",
                            "name": name,
                            "id": me_id,
                        }
                    )
                    if not as_json:
                        common.diag(f"  {acc}: OK - {name} (id={me_id})")
                else:
                    results.append({"account": acc, "status": "not_authorized"})
                    if not as_json:
                        common.error(f"  {acc}: not authorized")
        except Exception as e:
            results.append({"account": acc, "status": "error", "error": str(e)})
            if not as_json:
                common.error(f"  {acc}: error - {e}")

    if as_json:
        click.echo(json.dumps(results, ensure_ascii=False, indent=2))

    # An account that cannot be used is a failure, whatever the output format:
    # a script wrapping `auth check` has no other way to learn about it.
    return EXIT_FAILURE if any(r["status"] != "ok" for r in results) else EXIT_OK
