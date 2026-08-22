"""``account`` group: list accounts, set the default one, remove one."""

from __future__ import annotations

import json
import logging

import click

from tg_export.cli import common
from tg_export.cli.common import _fail

logger = logging.getLogger(__name__)


@click.group()
def account():
    """Manage accounts: list, set default, remove."""


@account.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as machine-readable JSON")
def account_list(as_json):
    """List configured accounts."""
    mgr = common._mgr()
    accounts = mgr.list_accounts()
    default = mgr.get_default_account()
    if as_json:
        payload = [{"name": acc, "default": acc == default} for acc in accounts]
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if not accounts:
        common._diag("No accounts configured.")
        return
    for acc in accounts:
        marker = " (default)" if acc == default else ""
        click.echo(f"  {acc}{marker}")


@account.command("default")
@click.argument("name", required=False, default=None)
def account_default(name):
    """Set or show default account."""
    mgr = common._mgr()
    if name:
        if name not in mgr.list_accounts():
            _fail(f"Account '{name}' not found.")
        mgr.set_default_account(name)
        common._diag(f"Default account set to '{name}'.")
    else:
        default = mgr.get_default_account()
        if default:
            click.echo(f"Default account: {default}")
        else:
            click.echo("No default account set.")


@account.command("remove")
@click.argument("name")
def account_remove(name):
    """Remove a Telegram account."""
    mgr = common._mgr()
    if name not in mgr.list_accounts():
        _fail(f"Account '{name}' not found.")
    mgr.remove_account(name)
    common._diag(f"Account '{name}' removed.")
