import click


@click.group()
def main():
    """tg-export: Flexible Telegram data export tool."""
    pass


@main.group()
def auth():
    """Manage Telegram accounts."""
    pass


@auth.command("add")
@click.option("--name", help="Account alias")
def auth_add(name):
    """Add a new Telegram account (interactive login)."""
    click.echo("Not implemented yet")


@auth.command("list")
def auth_list():
    """List configured accounts."""
    click.echo("Not implemented yet")


@auth.command("remove")
@click.argument("name")
def auth_remove(name):
    """Remove a Telegram account."""
    click.echo("Not implemented yet")


@main.command("list")
@click.option("--account", help="Account name")
@click.option("--output", type=click.Path(), help="Output file path")
@click.option("--format", "fmt", type=click.Choice(["yaml", "json"]), default="yaml")
@click.option("--include-left", is_flag=True, help="Include left channels")
def list_chats(account, output, fmt, include_left):
    """Export chat/folder catalog."""
    click.echo("Not implemented yet")


@main.command("init")
@click.option("--from", "from_catalog", type=click.Path(exists=True), help="Catalog file")
@click.option("--output", type=click.Path(), default="config.yaml")
def init_config(from_catalog, output):
    """Generate config template from catalog."""
    click.echo("Not implemented yet")


@main.command("run")
@click.option("--config", type=click.Path(exists=True), default="config.yaml")
@click.option("--account", help="Account name")
@click.option("--output", type=click.Path(), help="Output directory")
@click.option("--verify", is_flag=True, help="Verify file integrity after export")
@click.option("--dry-run", is_flag=True, help="Show what would be exported")
def run_export(config, account, output, verify, dry_run):
    """Run export according to config."""
    click.echo("Not implemented yet")


@main.command("verify")
@click.option("--config", type=click.Path(exists=True), default="config.yaml")
@click.option("--output", type=click.Path(), help="Export output directory")
def verify_files(config, output):
    """Verify integrity of previously downloaded files."""
    click.echo("Not implemented yet")
