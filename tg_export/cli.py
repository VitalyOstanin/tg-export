import asyncio
from pathlib import Path

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


@auth.command("credentials")
@click.option("--api-id", prompt="API ID (from https://my.telegram.org)", type=int, help="Telegram API ID")
@click.option("--api-hash", prompt="API Hash", help="Telegram API Hash")
def auth_credentials(api_id, api_hash):
    """Set Telegram API credentials (api_id and api_hash)."""
    mgr = _mgr()
    mgr.save_credentials(api_id=api_id, api_hash=api_hash)
    click.echo("Credentials saved.")


@auth.command("add")
@click.option("--name", prompt="Account alias", help="Account alias")
def auth_add(name):
    """Add a new Telegram account (interactive login)."""
    mgr = _mgr()
    cred_path = mgr.config_dir / "api_credentials.yaml"
    if not cred_path.exists():
        click.echo("No API credentials found. Run 'tg-export auth credentials' first.")
        raise SystemExit(1)
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
    asyncio.run(_list_chats(account, output, fmt, include_left))


async def _list_chats(account, output, fmt, include_left):
    from tg_export.api import TgApi
    from tg_export.catalog import fetch_catalog, format_catalog_yaml, format_catalog_json

    mgr = _mgr()
    api_id, api_hash = mgr.load_credentials()
    api = TgApi(mgr.session_path(account), api_id, api_hash)
    await api.connect()

    try:
        chats = await fetch_catalog(api, include_left=include_left)
        if fmt == "json":
            result = format_catalog_json(chats)
        else:
            result = format_catalog_yaml(chats)

        if output:
            Path(output).write_text(result, encoding="utf-8")
            click.echo(f"Catalog saved to {output}")
        else:
            click.echo(result)
    finally:
        await api.disconnect()


@main.command("init")
@click.option("--account", required=True, help="Account alias")
@click.option("--from", "from_catalog", type=click.Path(exists=True), help="Catalog file")
@click.option("--output", type=click.Path(), default=None, help="Override output config path")
def init_config(account, from_catalog, output):
    """Generate config template from catalog. Saves to ~/.config/tg-export/<account>.yaml."""
    asyncio.run(_init_config(account, from_catalog, output))


async def _init_config(account, from_catalog, output):
    from tg_export.catalog import generate_config_template

    mgr = _mgr()
    config_path = Path(output) if output else mgr.config_path(account)

    if from_catalog:
        import yaml
        with open(from_catalog) as f:
            catalog = yaml.safe_load(f)
        # Simple passthrough — generate template
        click.echo(f"Generating config from catalog: {from_catalog}")
    else:
        # Fetch from API
        from tg_export.api import TgApi
        from tg_export.catalog import fetch_catalog
        api_id, api_hash = mgr.load_credentials()
        api = TgApi(mgr.session_path(account), api_id, api_hash)
        await api.connect()
        try:
            chats = await fetch_catalog(api)
            template = generate_config_template(chats)
            config_path.write_text(template, encoding="utf-8")
            click.echo(f"Config template saved to {config_path}")
        finally:
            await api.disconnect()
        return

    click.echo(f"Config saved to {config_path}")


@main.command("run")
@click.option("--account", required=True, help="Account alias (loads ~/.config/tg-export/<account>.yaml)")
@click.option("--config", type=click.Path(exists=True), default=None, help="Override config path")
@click.option("--output", type=click.Path(), help="Override output directory")
@click.option("--verify", is_flag=True, help="Verify file integrity after export")
@click.option("--dry-run", is_flag=True, help="Show what would be exported")
def run_export(account, config, output, verify, dry_run):
    """Run export according to config. Config resolved by account name convention."""
    asyncio.run(_run_export(account, config, output, verify, dry_run))


async def _run_export(account, config_override, output_override, verify, dry_run):
    from tg_export.api import TgApi
    from tg_export.catalog import fetch_catalog
    from tg_export.config import load_config
    from tg_export.exporter import Exporter
    from tg_export.html.renderer import HtmlRenderer
    from tg_export.media import MediaDownloader
    from tg_export.state import ExportState

    mgr = _mgr()
    config_path = mgr.resolve_config(account, config_override)
    if not config_path.exists():
        click.echo(f"Config not found: {config_path}")
        click.echo(f"Create it with: tg-export init --account {account}")
        raise SystemExit(1)

    cfg = load_config(config_path)

    # Output directory: {config.output.path}/{account}/
    output_base = Path(output_override) if output_override else Path(cfg.output.path) / account

    # State DB next to output
    state_path = output_base / ".tg-export-state.db"
    state = ExportState(state_path)
    await state.open()

    # Connect API
    api_id, api_hash = mgr.load_credentials()
    api = TgApi(mgr.session_path(account), api_id, api_hash)
    await api.connect()

    try:
        # Start takeout if possible
        try:
            await api.start_takeout(
                contacts=cfg.contacts,
                message_users=True,
                message_chats=True,
                message_megagroups=True,
                message_channels=True,
                files=True,
            )
            click.echo("Takeout session started.")
        except Exception as e:
            click.echo(f"Takeout not available: {e}. Using regular API.")

        # Setup renderer
        renderer = HtmlRenderer(output_dir=output_base, config=cfg.output)
        renderer.setup()

        # Setup downloader
        downloader = MediaDownloader(
            api=api, state=state,
            config=cfg.defaults.media,
            min_free_bytes=cfg.output.min_free_space_bytes,
        )

        # Fetch chat list
        chats = await fetch_catalog(api, include_left=(cfg.left_channels_action != "skip"))

        # Create exporter and run
        exporter = Exporter(
            api=api, state=state, config=cfg,
            renderer=renderer, downloader=downloader, account=account,
        )
        stats = await exporter.run(dry_run=dry_run, verify=verify, chat_list=chats)

        # Render index
        if not dry_run:
            _render_index(renderer, chats, cfg)

        # Summary
        click.echo(f"\nExport complete:")
        click.echo(f"  Chats: {stats.chats_exported}")
        click.echo(f"  Messages: {stats.messages_exported}")
        click.echo(f"  Files downloaded: {stats.files_downloaded}")
        click.echo(f"  Files skipped: {stats.files_skipped}")
        if stats.errors:
            click.echo(f"  Errors: {len(stats.errors)}")

    finally:
        await api.disconnect()
        await state.close()


def _render_index(renderer, chats, cfg):
    """Build and render the main index page."""
    from collections import defaultdict
    folders = defaultdict(list)
    unfiled = []

    for chat in chats:
        chat_cfg = cfg.resolve_chat_config(chat.id, chat.name, chat.folder)
        if chat_cfg is None:
            continue
        entry = {
            "name": chat.name,
            "type": chat.type.value,
            "messages": chat.messages_count,
            "href": f"{'folders/' + chat.folder + '/' if chat.folder else 'unfiled/'}{chat.name}_{chat.id}/messages.html",
        }
        if chat.folder:
            folders[chat.folder].append(entry)
        else:
            unfiled.append(entry)

    sections = []
    if cfg.personal_info:
        sections.append({"title": "Personal Info", "items": [{"name": "Personal Information", "href": "personal_info.html", "meta": ""}]})
    if cfg.contacts:
        sections.append({"title": "Contacts", "items": [{"name": "Contacts", "href": "contacts.html", "meta": ""}]})

    renderer.render_index(folders=dict(folders), unfiled=unfiled, sections=sections)


@main.command("verify")
@click.option("--account", required=True, help="Account alias")
@click.option("--config", type=click.Path(exists=True), default=None, help="Override config path")
@click.option("--output", type=click.Path(), help="Export output directory")
def verify_files(account, config, output):
    """Verify integrity of previously downloaded files."""
    asyncio.run(_verify_files(account, config, output))


async def _verify_files(account, config_override, output_override):
    from tg_export.config import load_config
    from tg_export.state import ExportState

    mgr = _mgr()
    config_path = mgr.resolve_config(account, config_override)
    if not config_path.exists():
        click.echo(f"Config not found: {config_path}")
        raise SystemExit(1)

    cfg = load_config(config_path)
    output_base = Path(output_override) if output_override else Path(cfg.output.path) / account
    state_path = output_base / ".tg-export-state.db"

    if not state_path.exists():
        click.echo("No state database found. Nothing to verify.")
        return

    state = ExportState(state_path)
    await state.open()

    try:
        broken = await state.get_files_to_verify()
        if not broken:
            click.echo("All files OK.")
            return
        click.echo(f"Found {len(broken)} files with issues:")
        for f in broken:
            click.echo(f"  {f['local_path']} - status: {f['status']}, "
                       f"expected: {f['expected_size']}, actual: {f['actual_size']}")
    finally:
        await state.close()
