import asyncio

import click

from tg_export.auth import AccountManager


def _mgr() -> AccountManager:
    mgr = AccountManager()
    mgr.ensure_dirs()
    return mgr


@click.group()
def main():
    """tg-export: Flexible Telegram data export tool."""
    pass


@main.group()
def auth():
    """Manage Telegram accounts."""
    pass


@auth.command("add")
@click.option("--name", prompt="Account alias", help="Account alias")
def auth_add(name):
    """Add a new Telegram account (interactive login)."""
    mgr = _mgr()
    asyncio.run(mgr.add_account(name))
    click.echo(f"Account '{name}' added successfully.")


@auth.command("list")
def auth_list():
    """List configured accounts."""
    mgr = _mgr()
    accounts = mgr.list_accounts()
    if not accounts:
        click.echo("No accounts configured.")
        return
    for acc in accounts:
        click.echo(f"  {acc}")


@auth.command("remove")
@click.argument("name")
def auth_remove(name):
    """Remove a Telegram account."""
    mgr = _mgr()
    if name not in mgr.list_accounts():
        click.echo(f"Account '{name}' not found.")
        return
    mgr.remove_account(name)
    click.echo(f"Account '{name}' removed.")


@main.command("list")
@click.option("--account", required=True, help="Account alias")
@click.option("--output", type=click.Path(), help="Output file path")
@click.option("--format", "fmt", type=click.Choice(["yaml", "json"]), default="yaml")
@click.option("--include-left", is_flag=True, help="Include left channels")
def list_chats(account, output, fmt, include_left):
    """Export chat/folder catalog."""
    click.echo("Not implemented yet")


@main.command("init")
@click.option("--account", required=True, help="Account alias")
@click.option("--from", "from_catalog", type=click.Path(exists=True), help="Catalog file")
@click.option("--output", type=click.Path(), default=None, help="Override output config path")
def init_config(account, from_catalog, output):
    """Generate config template from catalog. Saves to ~/.config/tg-export/<account>.yaml."""
    click.echo("Not implemented yet")


@main.command("run")
@click.option("--account", required=True, help="Account alias (loads ~/.config/tg-export/<account>.yaml)")
@click.option("--config", type=click.Path(exists=True), default=None, help="Override config path")
@click.option("--output", type=click.Path(), help="Override output directory")
@click.option("--verify", is_flag=True, help="Verify file integrity after export")
@click.option("--dry-run", is_flag=True, help="Show what would be exported")
def run_export(account, config, output, verify, dry_run):
    """Run export according to config. Config resolved by account name convention."""
    mgr = _mgr()
    config_path = mgr.resolve_config(account, config)
    if not config_path.exists():
        click.echo(f"Config not found: {config_path}")
        click.echo(f"Create it with: tg-export init --account {account}")
        raise SystemExit(1)
    click.echo("Not implemented yet")


@main.command("verify")
@click.option("--account", required=True, help="Account alias")
@click.option("--config", type=click.Path(exists=True), default=None, help="Override config path")
@click.option("--output", type=click.Path(), help="Export output directory")
def verify_files(account, config, output):
    """Verify integrity of previously downloaded files."""
    click.echo("Not implemented yet")
