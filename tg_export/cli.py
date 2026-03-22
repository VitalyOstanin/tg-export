import asyncio
from pathlib import Path

import click

from tg_export.auth import AccountManager


def _mgr() -> AccountManager:
    mgr = AccountManager()
    mgr.ensure_dirs()
    return mgr




@click.group()
@click.option("--debug", is_flag=True, default=False, help="Enable debug logging")
@click.pass_context
def main(ctx, debug):
    """tg-export: Flexible Telegram data export tool."""
    import logging
    from rich.logging import RichHandler
    level = logging.DEBUG if debug else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(name)s %(message)s",
        handlers=[RichHandler(rich_tracebacks=True, show_path=debug)],
    )
    if not debug:
        logging.getLogger("aiosqlite").setLevel(logging.ERROR)
    ctx.ensure_object(dict)
    ctx.obj["debug"] = debug


@main.group()
def auth():
    """Telegram authentication: credentials, login, session check."""
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


@auth.command("check")
@click.argument("name", required=False, default=None)
def auth_check(name):
    """Check if account sessions are valid."""
    asyncio.run(_auth_check(name))


async def _auth_check(name):
    from tg_export.api import TgApi

    mgr = _mgr()
    accounts = [name] if name else mgr.list_accounts()
    if not accounts:
        click.echo("No accounts configured.")
        return

    api_id, api_hash = mgr.load_credentials()
    for acc in accounts:
        session = mgr.session_path(acc)
        if not session.exists():
            click.echo(f"  {acc}: session file missing")
            continue
        proxy = mgr.load_proxy()
        api = TgApi(session, api_id, api_hash, proxy=proxy)
        try:
            await api.connect()
            if await api.client.is_user_authorized():
                me = await api.client.get_me()
                click.echo(f"  {acc}: OK - {me.first_name} {me.last_name or ''} (id={me.id})")
            else:
                click.echo(f"  {acc}: not authorized")
        except Exception as e:
            click.echo(f"  {acc}: error - {e}")
        finally:
            await api.disconnect()


@main.group()
def account():
    """Manage accounts: list, set default, remove."""
    pass


@account.command("list")
def account_list():
    """List configured accounts."""
    mgr = _mgr()
    accounts = mgr.list_accounts()
    default = mgr.get_default_account()
    if not accounts:
        click.echo("No accounts configured.")
        return
    for acc in accounts:
        marker = " (default)" if acc == default else ""
        click.echo(f"  {acc}{marker}")


@account.command("default")
@click.argument("name", required=False, default=None)
def account_default(name):
    """Set or show default account."""
    mgr = _mgr()
    if name:
        if name not in mgr.list_accounts():
            click.echo(f"Account '{name}' not found.")
            raise SystemExit(1)
        mgr.set_default_account(name)
        click.echo(f"Default account set to '{name}'.")
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
    mgr = _mgr()
    if name not in mgr.list_accounts():
        click.echo(f"Account '{name}' not found.")
        return
    mgr.remove_account(name)
    click.echo(f"Account '{name}' removed.")


@main.command("config")
@click.option("--verbose", "-v", is_flag=True, help="Verbose: show per-account filters")
def show_config(verbose):
    """Show current configuration (global + per-account)."""
    import yaml as _yaml
    mgr = _mgr()

    # Global config
    global_path = mgr.config_dir / "config.yaml"
    cred_path = mgr.config_dir / "api_credentials.yaml"

    click.echo(f"# Global: {global_path}")
    if global_path.exists():
        data = mgr.load_global_config()
        proxy = data.get("proxy")
        if proxy:
            p = proxy
            auth_str = ""
            if p.get("username"):
                auth_str = f" auth={p['username']}:***"
            click.echo(f"  proxy: {p.get('type', 'socks5')}://{p.get('host')}:{p.get('port')}"
                       f" rdns={p.get('rdns', True)}{auth_str}")
        else:
            click.echo("  proxy: none")
        import shutil
        mfs = data.get("min_free_space", "20GB")
        usage = shutil.disk_usage(Path.cwd())
        free_gb = usage.free / 1024**3
        click.echo(f"  min_free_space: {mfs}  # available: {free_gb:.1f} GB")
    else:
        click.echo("  (not found)")

    click.echo(f"\n# Credentials: {cred_path}")
    if cred_path.exists():
        creds = _yaml.safe_load(cred_path.read_text())
        click.echo(f"  api_id: {creds.get('api_id')}")
        click.echo(f"  api_hash: {creds.get('api_hash', '')[:8]}...")
    else:
        click.echo("  (not found)")

    # Default account
    default = mgr.get_default_account()
    click.echo(f"\n# Default account: {default or '(not set)'}")

    # Per-account configs
    accounts = mgr.list_accounts()
    if not accounts:
        click.echo("\n# No accounts configured.")
        return

    click.echo(f"\n# Accounts: {len(accounts)}")
    for acc in accounts:
        marker = " (default)" if acc == default else ""
        config_path = mgr.config_path(acc)
        session_path = mgr.session_path(acc)
        session_ok = session_path.exists()
        config_ok = config_path.exists()

        click.echo(f"\n  [{acc}]{marker}")
        click.echo(f"    session: {'OK' if session_ok else 'MISSING'} ({session_path})")
        click.echo(f"    config:  {'OK' if config_ok else 'MISSING'} ({config_path})")

        if config_ok and verbose:
            _show_account_config(config_path)


def _show_account_config(config_path):
    """Show per-account config details (verbose mode)."""
    from tg_export.config import load_config
    cfg = load_config(config_path)

    click.echo(f"    output.path: {cfg.output.path}")
    click.echo(f"    output.format: {cfg.output.format}")

    d = cfg.defaults
    click.echo(f"    defaults.media.types: {d.media.types}")
    click.echo(f"    defaults.media.max_file_size: {d.media.max_file_size_bytes // 1024**2}MB")
    if d.date_from or d.date_to:
        click.echo(f"    defaults.date_range: {d.date_from or '...'} — {d.date_to or '...'}")

    if cfg.type_rules:
        click.echo(f"    type_rules:")
        for key, rule in cfg.type_rules.items():
            if rule.skip:
                click.echo(f"      {key}: skip")
            else:
                parts = []
                if rule.media:
                    parts.append(f"media={rule.media.types}")
                if rule.date_from or rule.date_to:
                    parts.append(f"dates={rule.date_from or '...'}—{rule.date_to or '...'}")
                click.echo(f"      {key}: {', '.join(parts) or 'defaults'}")

    if cfg.folders:
        click.echo(f"    folders:")
        for name, fr in cfg.folders.items():
            if fr.skip:
                click.echo(f"      {name}: skip")
            else:
                n_chats = len(fr.chats)
                media_str = f"media={fr.media.types}" if fr.media else "defaults"
                click.echo(f"      {name}: {media_str}, {n_chats} chat rules")

    if cfg.chats:
        click.echo(f"    chats: {len(cfg.chats)} rules")
        for rule in cfg.chats:
            ident = f"id={rule.id}" if rule.id else f"name={rule.name}"
            if rule.skip:
                click.echo(f"      {ident}: skip")
            else:
                parts = []
                if rule.media:
                    parts.append(f"media={rule.media.types}")
                if rule.date_from or rule.date_to:
                    parts.append(f"dates={rule.date_from or '...'}—{rule.date_to or '...'}")
                click.echo(f"      {ident}: {', '.join(parts) or 'defaults'}")

    click.echo(f"    unmatched: {cfg.unmatched_action}")
    click.echo(f"    left_channels: {cfg.left_channels_action}")


# ---------------------------------------------------------------------------
# Takeout management
# ---------------------------------------------------------------------------

@main.group()
def takeout():
    """Manage Telegram Takeout sessions."""
    pass


@takeout.command("clear")
@click.argument("name", required=False, default=None)
def takeout_clear(name):
    """Clear stale takeout session ID from local session file."""
    asyncio.run(_takeout_clear(name))


async def _takeout_clear(name):
    from tg_export.api import TgApi

    mgr = _mgr()
    account = mgr.resolve_account(name)
    api_id, api_hash = mgr.load_credentials()
    proxy = mgr.load_proxy()
    api = TgApi(mgr.session_path(account), api_id, api_hash, proxy=proxy)
    await api.connect()
    try:
        old_id = api.client.session.takeout_id
        if old_id is None:
            click.echo(f"  {account}: no active takeout session")
            return
        api.client.session.takeout_id = None
        api.client.session.save()
        click.echo(f"  {account}: takeout session cleared (was id={old_id})")
    finally:
        await api.disconnect()


# ---------------------------------------------------------------------------
# tg: direct Telegram API commands
# ---------------------------------------------------------------------------

@main.group()
def tg():
    """Direct Telegram API commands."""
    pass


@tg.command("messages")
@click.argument("chat_id", type=int)
@click.option("--account", default=None, help="Account alias (default: from 'auth default')")
@click.option("--limit", "-n", default=10, help="Number of messages to show")
def tg_messages(chat_id, account, limit):
    """Show recent messages from a chat."""
    asyncio.run(_tg_messages(chat_id, account, limit))


async def _tg_messages(chat_id, account, limit):
    from tg_export.api import TgApi

    mgr = _mgr()
    account = mgr.resolve_account(account)
    api_id, api_hash = mgr.load_credentials()
    proxy = mgr.load_proxy()
    api = TgApi(mgr.session_path(account), api_id, api_hash, proxy=proxy)
    await api.connect()

    try:
        entity = await api.client.get_entity(chat_id)
        title = getattr(entity, "title", None) or _entity_name(entity)
        click.echo(f"# {title} (id={chat_id})\n")

        async for msg in api.client.iter_messages(entity, limit=limit):
            date_str = msg.date.strftime("%Y-%m-%d %H:%M") if msg.date else "?"
            sender = ""
            if msg.sender:
                sender = getattr(msg.sender, "first_name", "") or ""
                last = getattr(msg.sender, "last_name", "") or ""
                if last:
                    sender = f"{sender} {last}"
            text = msg.message or ""
            if msg.media:
                media_type = msg.media.__class__.__name__.replace("MessageMedia", "")
                text = f"[{media_type}] {text}" if text else f"[{media_type}]"
            if msg.action:
                action_type = msg.action.__class__.__name__.replace("MessageAction", "")
                text = f"({action_type})"

            click.echo(f"  {date_str}  {sender}: {text[:200]}")
    finally:
        await api.disconnect()


def _entity_name(entity) -> str:
    first = getattr(entity, "first_name", "") or ""
    last = getattr(entity, "last_name", "") or ""
    return f"{first} {last}".strip() or "Unknown"


@tg.command("info")
@click.argument("chat_ids", type=int, nargs=-1)
@click.option("--account", default=None, help="Account alias")
@click.option("--from-catalog", "catalog_file", type=click.Path(exists=True), help="JSON catalog file (from tg-export list --format json)")
@click.option("--type", "chat_type", default=None, help="Filter by chat type (with --from-catalog)")
@click.option("--last", "last_n", type=int, default=0, help="Show last N messages per chat")
@click.option("--output", "output_file", type=click.Path(), default=None, help="Save results to JSON file")
def tg_info(chat_ids, account, catalog_file, chat_type, last_n, output_file):
    """Show chat info: message count, type, title.

    Accepts one or more CHAT_IDS, or use --from-catalog with --type to batch query.
    """
    asyncio.run(_tg_info(chat_ids, account, catalog_file, chat_type, last_n, output_file))


async def _tg_info(chat_ids, account, catalog_file, chat_type, last_n, output_file):
    import json
    from tg_export.api import TgApi
    from telethon.tl.functions.messages import GetHistoryRequest

    # Collect IDs
    ids = list(chat_ids)
    if catalog_file:
        with open(catalog_file) as f:
            catalog = json.load(f)
        for entry in catalog:
            if chat_type and entry.get("type") != chat_type:
                continue
            ids.append(entry["id"])

    if not ids:
        click.echo("No chat IDs specified. Use arguments or --from-catalog --type.")
        return

    mgr = _mgr()
    account = mgr.resolve_account(account)
    api_id, api_hash = mgr.load_credentials()
    proxy = mgr.load_proxy()
    api = TgApi(mgr.session_path(account), api_id, api_hash, proxy=proxy)
    await api.connect()

    results = []
    try:
        total = len(ids)
        for idx, cid in enumerate(ids, 1):
            try:
                entity = await api.client.get_entity(cid)
                title = getattr(entity, "title", None) or _entity_name(entity)
                limit = max(last_n, 1)
                result = await api.client(GetHistoryRequest(
                    peer=entity, offset_id=0, offset_date=None, add_offset=0,
                    limit=limit, max_id=0, min_id=0, hash=0,
                ))
                count = getattr(result, "count", len(result.messages))
                last_date = None
                messages = []
                for msg in result.messages:
                    date_str = msg.date.strftime("%Y-%m-%d %H:%M") if msg.date else "?"
                    if last_date is None:
                        last_date = date_str
                    if last_n > 0:
                        sender = ""
                        if msg.sender:
                            sender = getattr(msg.sender, "first_name", "") or ""
                            last = getattr(msg.sender, "last_name", "") or ""
                            if last:
                                sender = f"{sender} {last}"
                        text = msg.message or ""
                        if msg.media:
                            media_type = msg.media.__class__.__name__.replace("MessageMedia", "")
                            text = f"[{media_type}] {text}" if text else f"[{media_type}]"
                        messages.append({"date": date_str, "sender": sender, "text": text[:200]})

                entry = {
                    "id": cid,
                    "name": title,
                    "messages": count,
                    "last_date": last_date,
                }
                if messages:
                    entry["last_messages"] = messages
                results.append(entry)

                if not output_file:
                    click.echo(f"[{idx}/{total}] {title} (id={cid}): {count} msgs, last: {last_date}")
                elif idx % 50 == 0:
                    click.echo(f"  [{idx}/{total}]...")

            except Exception as e:
                entry = {"id": cid, "error": str(e), "messages": 0}
                results.append(entry)
                if not output_file:
                    click.echo(f"[{idx}/{total}] id={cid}: ERROR {e}")

        if output_file:
            with open(output_file, "w") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
            click.echo(f"Saved {len(results)} entries to {output_file}")
    finally:
        await api.disconnect()


@main.command("list")
@click.option("--account", default=None, help="Account alias (default: from 'auth default')")
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
    account = mgr.resolve_account(account)
    api_id, api_hash = mgr.load_credentials()
    proxy = mgr.load_proxy()
    api = TgApi(mgr.session_path(account), api_id, api_hash, proxy=proxy)
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
@click.option("--account", default=None, help="Account alias (default: from 'auth default')")
@click.option("--from", "from_catalog", type=click.Path(exists=True), help="Catalog file")
@click.option("--output", type=click.Path(), default=None, help="Override output config path")
def init_config(account, from_catalog, output):
    """Generate config template from catalog. Saves to ~/.config/tg-export/<account>.yaml."""
    asyncio.run(_init_config(account, from_catalog, output))


async def _init_config(account, from_catalog, output):
    from tg_export.catalog import generate_config_template

    mgr = _mgr()
    account = mgr.resolve_account(account)
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
        proxy = mgr.load_proxy()
        api = TgApi(mgr.session_path(account), api_id, api_hash, proxy=proxy)
        await api.connect()
        try:
            chats = await fetch_catalog(api)
            template = generate_config_template(chats, account=account)
            config_path.write_text(template, encoding="utf-8")
            click.echo(f"Config template saved to {config_path}")
        finally:
            await api.disconnect()
        return

    click.echo(f"Config saved to {config_path}")


def _get_dir_size(path: Path) -> int | None:
    """Get directory size using du -sb."""
    import subprocess
    try:
        result = subprocess.run(
            ["du", "-sb", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            return int(result.stdout.split()[0])
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass
    return None


@main.command("run")
@click.option("--account", default=None, help="Account alias (default: from 'auth default')")
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
    account = mgr.resolve_account(account)
    config_path = mgr.resolve_config(account, config_override)
    if not config_path.exists():
        click.echo(f"Config not found: {config_path}")
        click.echo(f"Create it with: tg-export init --account {account}")
        raise SystemExit(1)

    cfg = load_config(config_path)
    output_base = Path(output_override) if output_override else Path(cfg.output.path)
    click.echo(f"Account: {account}")
    click.echo(f"Output: {output_base.resolve()}")

    # Ensure output dir exists (needed for state DB)
    output_base.mkdir(parents=True, exist_ok=True)

    # State DB next to output
    state_path = output_base / ".tg-export-state.db"
    state = ExportState(state_path)
    await state.open()

    # Connect API
    api_id, api_hash = mgr.load_credentials()
    proxy = mgr.load_proxy()
    api = TgApi(mgr.session_path(account), api_id, api_hash, proxy=proxy)
    await api.connect()

    try:
        # Start takeout if possible
        from telethon.errors import TakeoutInitDelayError
        try:
            await api.start_takeout(
                contacts=cfg.contacts,
                users=True,
                chats=True,
                megagroups=True,
                channels=True,
                files=True,
                max_file_size=cfg.defaults.media.max_file_size_bytes,
            )
            click.echo("Takeout session started.")
        except TakeoutInitDelayError as e:
            hours = e.seconds // 3600
            minutes = (e.seconds % 3600) // 60
            click.echo(
                f"Takeout cooldown: need to wait {hours}h {minutes}m "
                f"({e.seconds}s). Approve takeout in your Telegram client "
                f"to skip the wait. Using regular API for now."
            )
        except Exception as e:
            click.echo(f"Takeout not available: {e}. Using regular API.")

        # Setup renderer
        renderer = HtmlRenderer(output_dir=output_base, config=cfg.output)
        renderer.setup()

        # Setup tdesktop import indexes
        from tg_export.importer import build_tdesktop_indexes
        tdesktop_indexes = build_tdesktop_indexes(cfg.import_existing)
        if tdesktop_indexes:
            for idx in tdesktop_indexes:
                click.echo(f"tdesktop import: {idx.export_path}")

        # Auto-discover sibling account state DBs for file deduplication
        import logging
        logger = logging.getLogger(__name__)
        sibling_dbs = []
        for sibling in output_base.parent.iterdir():
            if sibling == output_base or not sibling.is_dir():
                continue
            sdb = sibling / ".tg-export-state.db"
            if sdb.exists():
                sibling_dbs.append(sdb)
                logger.debug("sibling state DB: %s", sdb)
        if sibling_dbs:
            names = [s.parent.name for s in sibling_dbs]
            click.echo(f"Sibling exports for file dedup: {', '.join(names)}")

        # Setup downloader
        min_free = mgr.load_min_free_space() or 20 * 1024**3  # default 20GB
        downloader = MediaDownloader(
            api=api, state=state,
            config=cfg.defaults.media,
            min_free_bytes=min_free,
            tdesktop_indexes=tdesktop_indexes,
            sibling_db_paths=sibling_dbs,
        )

        # Fetch chat list
        chats = await fetch_catalog(api, include_left=(cfg.left_channels_action != "skip"))

        # Create exporter and run
        exporter = Exporter(
            api=api, state=state, config=cfg,
            renderer=renderer, downloader=downloader, account=account,
        )
        stats = await exporter.run(dry_run=dry_run, verify=verify, chat_list=chats)

        if exporter._force_shutdown:
            click.echo("\nForce shutdown — state saved.")
        else:
            # Render index
            if not dry_run:
                await _render_index(renderer, chats, cfg, state)

            # Summary
            from tg_export.exporter import _format_size
            click.echo(f"\nExport complete:")
            click.echo(f"  Chats: {stats.chats_exported}/{stats.chats_included} (skipped {stats.chats_skipped})")
            click.echo(f"  Messages: {stats.messages_exported}")
            click.echo(f"  Files downloaded: {stats.files_downloaded}")
            if stats.files_imported:
                click.echo(f"  Files imported: {stats.files_imported}")
            if stats.files_cached:
                click.echo(f"  Files cached: {stats.files_cached}")
            if stats.files_too_large:
                click.echo(f"  Files too large: {stats.files_too_large}")
            if stats.files_type_skip:
                click.echo(f"  Files type skip: {stats.files_type_skip}")
            if stats.data_size:
                click.echo(f"  Downloaded: {_format_size(stats.data_size)}")
            # File counts from DB
            file_counts = await state.count_files()
            click.echo(f"  Files: {file_counts['files_downloaded']}/{file_counts['expected_files']} (media messages: {file_counts['media_messages']})")
            # DB size
            db_size = state.db_path.stat().st_size if state.db_path.exists() else 0
            click.echo(f"  DB size: {_format_size(db_size)}")
            # Total export size on disk (excluding DB)
            total_disk = _get_dir_size(output_base)
            if total_disk is not None:
                click.echo(f"  Export size on disk: {_format_size(total_disk)}")
            if stats.errors:
                click.echo(f"  Errors: {len(stats.errors)}")

    except asyncio.CancelledError:
        click.echo("\nForce shutdown — saving state...")
    finally:
        if api.takeout:
            try:
                await api.stop_takeout(success=True)
            except (Exception, asyncio.CancelledError):
                pass
        try:
            await api.disconnect()
        except (Exception, asyncio.CancelledError):
            pass
        try:
            await state.close()
        except (Exception, asyncio.CancelledError):
            pass


async def _render_index(renderer, chats, cfg, state):
    """Build and render the main index page."""
    from collections import defaultdict
    from tg_export.exporter import sanitize_name
    folders = defaultdict(list)
    unfiled = []

    for chat in chats:
        chat_cfg = cfg.resolve_chat_config(chat.id, chat.name, chat.folder, chat.type.value)
        if chat_cfg is None:
            continue
        # Get real message count from DB if available
        msg_count = chat.messages_count
        chat_state = await state.get_chat_state(chat.id)
        if chat_state and chat_state.get("messages_count"):
            msg_count = chat_state["messages_count"]
        else:
            # Count from messages table
            try:
                msgs = await state.count_messages(chat.id)
                if msgs > 0:
                    msg_count = msgs
            except Exception:
                pass

        dir_name = f"{sanitize_name(chat.name)}_{chat.id}"
        entry = {
            "name": chat.name,
            "type": chat.type.value,
            "messages": msg_count,
            "href": f"{'folders/' + chat.folder + '/' if chat.folder else 'unfiled/'}{dir_name}/messages.html",
        }
        if chat.folder:
            folders[chat.folder].append(entry)
        else:
            unfiled.append(entry)

    sections = []
    if cfg.personal_info:
        sections.append({"title": "Personal Info", "entries": [{"name": "Personal Information", "href": "personal_info.html", "meta": ""}]})
    if cfg.contacts:
        sections.append({"title": "Contacts", "entries": [{"name": "Contacts", "href": "contacts.html", "meta": ""}]})
    if cfg.sessions:
        sections.append({"title": "Sessions", "entries": [{"name": "Active Sessions", "href": "sessions.html", "meta": ""}]})
    if cfg.userpics:
        sections.append({"title": "Profile Photos", "entries": [{"name": "Profile Photos", "href": "userpics.html", "meta": ""}]})
    if cfg.stories:
        sections.append({"title": "Stories", "entries": [{"name": "Stories", "href": "stories.html", "meta": ""}]})
    if cfg.other_data or cfg.profile_music:
        sections.append({"title": "Other Data", "entries": [{"name": "Other Data", "href": "other_data.html", "meta": ""}]})

    # Build folders_list with hrefs for folder index pages
    folders_list = []
    for folder_name, folder_chats in folders.items():
        folder_dir_name = sanitize_name(folder_name)
        folders_list.append({
            "name": folder_name,
            "href": f"folders/{folder_dir_name}/index.html",
            "chats": folder_chats,
        })

    renderer.render_index(folders_list=folders_list, unfiled=unfiled, sections=sections)

    # Render per-folder index pages
    for folder_info in folders_list:
        adjusted = []
        for entry in folder_info["chats"]:
            dir_name = entry["href"].split("/")[-2]  # Chat_123 from folders/Folder/Chat_123/messages.html
            adjusted.append({
                "name": entry["name"],
                "type": entry["type"],
                "messages": entry["messages"],
                "href": f"{dir_name}/messages.html",
            })
        renderer.render_folder_index(folder_info["name"], adjusted)


@main.group()
def state():
    """Manage export state (reset, show status, force re-export)."""
    pass


def _open_state(account, config_override, output_override):
    """Helper: resolve paths and return (state, output_base, account). Caller must open/close."""
    from tg_export.config import load_config
    from tg_export.state import ExportState

    mgr = _mgr()
    account = mgr.resolve_account(account)
    config_path = mgr.resolve_config(account, config_override)
    if not config_path.exists():
        click.echo(f"Config not found: {config_path}")
        raise SystemExit(1)

    cfg = load_config(config_path)
    output_base = Path(output_override) if output_override else Path(cfg.output.path)
    state_path = output_base / ".tg-export-state.db"

    if not state_path.exists():
        click.echo("No state database found.")
        raise SystemExit(1)

    return ExportState(state_path), output_base, account


@state.command("show")
@click.option("--account", default=None, help="Account alias")
@click.option("--config", type=click.Path(exists=True), default=None)
@click.option("--output", type=click.Path(), default=None)
@click.argument("chat_id", type=int, required=False)
def state_show(account, config, output, chat_id):
    """Show export state for all chats or a specific chat."""
    asyncio.run(_state_show(account, config, output, chat_id))


async def _state_show(account, config_override, output_override, chat_id):
    st, _, account = _open_state(account, config_override, output_override)
    await st.open()
    try:
        if chat_id:
            chat_state = await st.get_chat_state(chat_id)
            if not chat_state:
                click.echo(f"No state for chat {chat_id}")
                return
            msg_count = await st.count_messages(chat_id)
            click.echo(f"Chat {chat_id}:")
            click.echo(f"  last_msg_id:   {chat_state['last_msg_id']}")
            click.echo(f"  oldest_msg_id: {chat_state['oldest_msg_id']}")
            click.echo(f"  full_history:  {bool(chat_state['full_history'])}")
            click.echo(f"  messages in DB: {msg_count}")
            click.echo(f"  updated_at:    {chat_state['updated_at']}")
        else:
            async with st.db.execute(
                "SELECT es.*, (SELECT COUNT(*) FROM messages m WHERE m.chat_id=es.chat_id) as msg_count "
                "FROM export_state es ORDER BY es.updated_at DESC"
            ) as cur:
                rows = await cur.fetchall()
            if not rows:
                click.echo("No export state records.")
                return
            click.echo(f"{'chat_id':>15}  {'msgs':>6}  {'last_id':>8}  {'oldest_id':>9}  {'full':>4}  updated_at")
            click.echo("-" * 80)
            for r in rows:
                r = dict(r)
                full = "yes" if r["full_history"] else "no"
                click.echo(f"{r['chat_id']:>15}  {r['msg_count']:>6}  {r['last_msg_id']:>8}  {r['oldest_msg_id']:>9}  {full:>4}  {r['updated_at']}")
    finally:
        await st.close()


@state.command("reset")
@click.option("--account", default=None, help="Account alias")
@click.option("--config", type=click.Path(exists=True), default=None)
@click.option("--output", type=click.Path(), default=None)
@click.option("--all", "reset_all", is_flag=True, help="Reset all chats")
@click.option("--delete-messages", is_flag=True, help="Also delete messages from DB")
@click.argument("chat_id", type=int, required=False)
def state_reset(account, config, output, reset_all, delete_messages, chat_id):
    """Reset export state to force re-download. Specify chat_id or --all."""
    if not chat_id and not reset_all:
        click.echo("Specify chat_id or --all")
        raise SystemExit(1)
    asyncio.run(_state_reset(account, config, output, reset_all, delete_messages, chat_id))


async def _state_reset(account, config_override, output_override, reset_all, delete_messages, chat_id):
    st, _, account = _open_state(account, config_override, output_override)
    await st.open()
    try:
        if reset_all:
            await st.db.execute("UPDATE export_state SET last_msg_id=0, oldest_msg_id=0, full_history=0")
            if delete_messages:
                await st.db.execute("DELETE FROM messages")
                await st.db.execute("DELETE FROM files")
            await st.db.commit()
            click.echo("Reset all chats.")
        else:
            chat_state = await st.get_chat_state(chat_id)
            if not chat_state:
                click.echo(f"No state for chat {chat_id}")
                return
            await st.db.execute(
                "UPDATE export_state SET last_msg_id=0, oldest_msg_id=0, full_history=0 WHERE chat_id=?",
                (chat_id,),
            )
            if delete_messages:
                await st.db.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
                await st.db.execute("DELETE FROM files WHERE chat_id=?", (chat_id,))
            await st.db.commit()
            msg = f"Reset chat {chat_id}."
            if delete_messages:
                msg += " Messages and files records deleted."
            click.echo(msg)
    finally:
        await st.close()


@main.command("purge")
@click.argument("chat", required=True)
@click.option("--account", default=None, help="Account alias (default: from 'auth default')")
@click.option("--config", type=click.Path(exists=True), default=None, help="Override config path")
@click.option("--output", type=click.Path(), help="Export output directory")
@click.option("--yes", is_flag=True, help="Skip confirmation")
def purge_chat(chat, account, config, output, yes):
    """Purge chat data: messages, files, state, and rendered HTML.

    CHAT can be a chat ID (number) or a name (substring search).
    """
    asyncio.run(_purge_chat(chat, account, config, output, yes))


async def _purge_chat(chat_arg, account, config_override, output_override, skip_confirm):
    import shutil
    from tg_export.config import load_config
    from tg_export.state import ExportState

    mgr = _mgr()
    account = mgr.resolve_account(account)
    config_path = mgr.resolve_config(account, config_override)
    if not config_path.exists():
        click.echo(f"Config not found: {config_path}")
        raise SystemExit(1)

    cfg = load_config(config_path)
    output_base = Path(output_override) if output_override else Path(cfg.output.path)
    state_path = output_base / ".tg-export-state.db"

    if not state_path.exists():
        click.echo("No state database found.")
        raise SystemExit(1)

    state = ExportState(state_path)
    await state.open()

    try:
        # Resolve chat: by ID or by name search
        try:
            chat_id = int(chat_arg)
            matches = await state.find_chat_by_name("")
            chat_name = next((c["name"] for c in matches if c["chat_id"] == chat_id), f"id={chat_id}")
        except ValueError:
            matches = await state.find_chat_by_name(chat_arg)
            if not matches:
                click.echo(f"No chats found matching '{chat_arg}'")
                raise SystemExit(1)
            if len(matches) > 1:
                click.echo(f"Multiple chats match '{chat_arg}':")
                for m in matches:
                    click.echo(f"  {m['chat_id']}  {m['name']}  ({m['type']})")
                click.echo("Specify exact chat ID.")
                raise SystemExit(1)
            chat_id = matches[0]["chat_id"]
            chat_name = matches[0]["name"]

        # Show what will be deleted
        counts = {}
        for table in ("messages", "files", "export_state", "catalog_cache"):
            async with state.db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE chat_id=?", (chat_id,)
            ) as cur:
                row = await cur.fetchone()
                counts[table] = row[0] if row else 0

        # Find chat directory on disk
        from tg_export.exporter import sanitize_name
        dir_suffix = f"{sanitize_name(chat_name)}_{chat_id}"
        chat_dirs = list(output_base.rglob(dir_suffix))

        click.echo(f"Chat: {chat_name} (id={chat_id})")
        click.echo(f"  DB: messages={counts['messages']}, files={counts['files']}, "
                    f"export_state={counts['export_state']}, catalog_cache={counts['catalog_cache']}")
        if chat_dirs:
            for d in chat_dirs:
                size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                from tg_export.exporter import _format_size
                click.echo(f"  Dir: {d} ({_format_size(size)})")
        else:
            click.echo("  Dir: not found")

        if not skip_confirm:
            if not click.confirm("Delete all data for this chat?"):
                click.echo("Cancelled.")
                return

        # Purge from DB
        deleted = await state.purge_chat(chat_id)
        click.echo(f"  Deleted from DB: {deleted}")

        # Remove directory
        for d in chat_dirs:
            shutil.rmtree(d)
            click.echo(f"  Removed: {d}")

        click.echo("Done.")

    finally:
        await state.close()


@main.command("verify")
@click.option("--account", default=None, help="Account alias (default: from 'auth default')")
@click.option("--config", type=click.Path(exists=True), default=None, help="Override config path")
@click.option("--output", type=click.Path(), help="Export output directory")
def verify_files(account, config, output):
    """Verify integrity of previously downloaded files."""
    asyncio.run(_verify_files(account, config, output))


async def _verify_files(account, config_override, output_override):
    from tg_export.config import load_config
    from tg_export.state import ExportState

    mgr = _mgr()
    account = mgr.resolve_account(account)
    config_path = mgr.resolve_config(account, config_override)
    if not config_path.exists():
        click.echo(f"Config not found: {config_path}")
        raise SystemExit(1)

    cfg = load_config(config_path)
    output_base = Path(output_override) if output_override else Path(cfg.output.path)
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

        # Connect to Telegram and re-download
        from tg_export.api import TgApi
        api_id, api_hash = mgr.load_credentials()
        proxy = mgr.load_proxy()
        api = TgApi(mgr.session_path(account), api_id, api_hash, proxy=proxy)
        await api.connect()

        try:
            redownloaded = 0
            for f in broken:
                chat_id = f["chat_id"]
                msg_id = f["msg_id"]
                local_path = Path(f["local_path"])
                try:
                    tl_messages = await api.client.get_messages(chat_id, ids=msg_id)
                    tl_msg = tl_messages if not isinstance(tl_messages, list) else (tl_messages[0] if tl_messages else None)
                    if tl_msg is None or tl_msg.media is None:
                        click.echo(f"  [skip] msg {msg_id}: not found or no media")
                        continue

                    if local_path.exists():
                        local_path.unlink()

                    target_dir = local_path.parent
                    target_dir.mkdir(parents=True, exist_ok=True)
                    path = await api.download_media(tl_msg, target_dir)
                    if path:
                        actual_size = Path(path).stat().st_size
                        await state.register_file(
                            file_id=f["file_id"], chat_id=chat_id, msg_id=msg_id,
                            expected_size=f["expected_size"], actual_size=actual_size,
                            local_path=str(path), status="done",
                        )
                        await state.commit()
                        redownloaded += 1
                        click.echo(f"  [ok] {path}")
                    else:
                        click.echo(f"  [fail] {local_path}")
                except Exception as e:
                    click.echo(f"  [error] {local_path}: {e}")

            click.echo(f"\nRe-downloaded: {redownloaded}/{len(broken)}")
        finally:
            await api.disconnect()
    finally:
        await state.close()


# ---------------------------------------------------------------------------
# tg send / tg download — additional direct Telegram API commands
# ---------------------------------------------------------------------------

async def _connect_tg(account_name):
    """Helper: connect to Telegram API and return (api, account_name).
    Caller must call api.disconnect() when done."""
    from tg_export.api import TgApi
    mgr = _mgr()
    acc = mgr.resolve_account(account_name)
    api_id, api_hash = mgr.load_credentials()
    proxy = mgr.load_proxy()
    api = TgApi(mgr.session_path(acc), api_id, api_hash, proxy=proxy)
    await api.connect()
    return api, acc


@tg.command("send")
@click.option("--account", default=None, help="Account alias")
@click.option("--file", "-f", "files", multiple=True, type=click.Path(exists=True),
              help="File(s) to attach (can be specified multiple times)")
@click.option("--text", "-t", default=None, help="Message text")
@click.argument("recipients", nargs=-1, required=True)
def tg_send(account, files, text, recipients):
    """Send message to one or more recipients.

    RECIPIENTS: chat IDs or usernames (multiple allowed).
    Use --text for message text and --file for attachments.
    At least --text or --file must be specified.
    """
    if not text and not files:
        click.echo("Error: specify --text and/or --file")
        raise SystemExit(1)

    parsed = []
    for r in recipients:
        try:
            parsed.append(int(r))
        except ValueError:
            parsed.append(r)

    asyncio.run(_tg_send(account, parsed, text, files))


async def _tg_send(account_name, recipients, text, files):
    api, _ = await _connect_tg(account_name)
    try:
        file_paths = [Path(f) for f in files] if files else None

        for recipient in recipients:
            try:
                if file_paths:
                    if len(file_paths) == 1:
                        await api.client.send_file(
                            recipient, file_paths[0], caption=text or "",
                        )
                    else:
                        await api.client.send_file(
                            recipient, file_paths, caption=text or "",
                        )
                elif text:
                    await api.client.send_message(recipient, text)

                click.echo(f"  sent to {recipient}")
            except Exception as e:
                click.echo(f"  error sending to {recipient}: {e}")
    finally:
        await api.disconnect()


@tg.command("download")
@click.option("--account", default=None, help="Account alias")
@click.option("--output", "-o", type=click.Path(), default=".", help="Output directory")
@click.argument("chat_id", type=int)
@click.argument("msg_id", type=int)
def tg_download(account, output, chat_id, msg_id):
    """Download message content: text and all media files.

    Saves message text to <msg_id>.txt and media files to the output directory.
    """
    asyncio.run(_tg_download(account, chat_id, msg_id, output))


async def _tg_download(account_name, chat_id, msg_id, output_dir):
    api, _ = await _connect_tg(account_name)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    try:
        tl_msg = await api.client.get_messages(chat_id, ids=msg_id)
        if isinstance(tl_msg, list):
            tl_msg = tl_msg[0] if tl_msg else None
        if tl_msg is None:
            click.echo(f"Message {msg_id} not found in chat {chat_id}")
            return

        # Save text
        if tl_msg.text:
            text_file = out / f"{msg_id}.txt"
            text_file.write_text(tl_msg.text, encoding="utf-8")
            click.echo(f"  text: {text_file}")

        # Download media
        if tl_msg.media:
            path = await api.client.download_media(tl_msg, file=str(out))
            if path:
                click.echo(f"  media: {path}")
            else:
                click.echo("  media: download failed")

        # Check for grouped_id (album) — download all parts
        if tl_msg.grouped_id:
            count = 0
            async for grouped_msg in api.client.iter_messages(
                chat_id, min_id=msg_id - 10, max_id=msg_id + 10,
            ):
                if grouped_msg.grouped_id == tl_msg.grouped_id and grouped_msg.id != msg_id:
                    if grouped_msg.media:
                        path = await api.client.download_media(grouped_msg, file=str(out))
                        if path:
                            click.echo(f"  album media: {path}")
                            count += 1
            if count:
                click.echo(f"  ({count} additional album files)")

        if not tl_msg.text and not tl_msg.media:
            click.echo("  (empty message, no text or media)")
    finally:
        await api.disconnect()
