"""``account`` group: list accounts, set the default one, remove one."""

from __future__ import annotations

import json
import logging

import click

from tg_export.cli import common
from tg_export.cli.common import fail

logger = logging.getLogger(__name__)


@click.group()
def account():
    """Manage accounts: list, set default, remove."""


@account.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as machine-readable JSON")
def account_list(as_json):
    """List configured accounts."""
    mgr = common.account_manager()
    accounts = mgr.list_accounts()
    default = mgr.get_default_account()
    if as_json:
        payload = [{"name": acc, "default": acc == default} for acc in accounts]
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if not accounts:
        common.diag("No accounts configured.")
        return
    for acc in accounts:
        marker = " (default)" if acc == default else ""
        click.echo(f"  {acc}{marker}")


@account.command("default")
@click.argument("name", required=False, default=None)
@click.option("--json", "as_json", is_flag=True, help="Output as machine-readable JSON")
def account_default(name, as_json):
    """Set or show default account."""
    mgr = common.account_manager()
    if name:
        if name not in mgr.list_accounts():
            fail(f"Account '{name}' not found.")
        mgr.set_default_account(name)
        common.diag(f"Default account set to '{name}'.")
        return
    default = mgr.get_default_account()
    if as_json:
        click.echo(json.dumps({"default": default}, ensure_ascii=False, indent=2))
        return
    # stdout carries the value alone: `NAME=$(tg-export account default)` used
    # to receive the caption too, and "No default account set." as the value
    # when there was none. The caption goes to stderr like every other one.
    if default:
        click.echo(default)
        common.diag(f"Default account: {default}")
    else:
        common.diag("No default account set.")


@account.command("remove")
@click.argument("name")
def account_remove(name):
    """Remove a Telegram account."""
    mgr = common.account_manager()
    if name not in mgr.list_accounts():
        fail(f"Account '{name}' not found.")
    mgr.remove_account(name)
    common.diag(f"Account '{name}' removed.")
