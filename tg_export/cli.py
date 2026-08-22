"""Command-line interface: every tg-export command lives here.

Command groups, in the order they appear below:

===========  ======================================================
``auth``     credentials, login, session check
``account``  list accounts, set the default one, remove one
``takeout``  inspect and clear the Telegram Takeout session
``tg``       direct API calls: info, messages, send, download
``state``    inspect and reset the export state of a chat
top level    ``config``, ``list``, ``init``, ``run``, ``purge``, ``verify``
===========  ======================================================

The flow behind ``run`` is ``cli -> config -> api -> exporter ->
state/media -> renderer``; see the "Устройство пакета" section of
CONTRIBUTING.md for what each module owns.

Imports of project modules are deliberately made inside the command bodies
rather than at module level: the entry point is a console script, and
importing telethon, jinja2 and the exporter on every ``--help`` costs about a
second of startup. Keep them where they are unless the module is needed by
the group definitions themselves.
"""

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

import aiosqlite
import click
import yaml

from tg_export.auth import AccountManager
from tg_export.errors import TakeoutUnavailableError, TgExportError
from tg_export.privacy import ensure_private_dir

logger = logging.getLogger(__name__)

# Set by main() from the global --debug flag; used by run_cli() to decide
# whether to show a full traceback or a short message for domain errors.
_DEBUG = False


def _mgr() -> AccountManager:
    mgr = AccountManager()
    mgr.ensure_dirs()
    return mgr


# Set by main() from the global --quiet flag. When True, non-essential status
# and progress messages are suppressed; errors and final summaries still print.
_QUIET = False


def _diag(message: str, *, essential: bool = False, **kwargs) -> None:
    """Print a diagnostic / status / error message to stderr.

    stdout is reserved for machine-readable output of query commands
    (list / state show / tg info / tg messages), so everything that is not
    that data -- progress, confirmations, error notices -- goes to stderr.

    Under --quiet only ``essential`` messages (errors, final summaries) are
    printed; routine status/progress lines are suppressed.
    """
    if _QUIET and not essential:
        return
    click.echo(message, err=True, **kwargs)


def _error(message: str, **kwargs) -> None:
    """Print a message the user must see even under --quiet.

    Every reason a command refuses to do its job goes through here. Marking
    such messages one by one on _diag proved unreliable: of 94 call sites only
    20 carried the flag and 17 of those were summary lines, so `--quiet` turned
    a refusal into an empty output with a non-zero exit code.
    """
    _diag(message, essential=True, **kwargs)


# Exit codes. 0 -- success, 1 -- the command reported a failure, 2 -- Click's
# own usage error, 128 + N -- terminated by signal N (130 for SIGINT, 143 for
# SIGTERM), the convention every shell already understands.
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_SIGINT = 130


def _export_exit_code(*, signum: int | None, error_count: int) -> int:
    """Turn the outcome of an export into an exit code.

    An interrupted run outranks a failed one: the signal is what the caller
    asked for, and 128 + signum tells the shell which one arrived.
    """
    if signum:
        return 128 + signum
    return EXIT_FAILURE if error_count else EXIT_OK


# The state database lives inside the output directory, so its name is part of
# the export layout rather than a local detail of any one command.
STATE_DB_NAME = ".tg-export-state.db"


@contextlib.asynccontextmanager
async def _connected_api(account_name):
    """Connect to Telegram for one account; yield ``(api, account)``.

    Every command needs the same prologue -- resolve the account, load the
    credentials and the proxy, build TgApi, connect -- and the same guarantee
    that the socket is closed afterwards. Returning a connected object instead
    left that guarantee to a line of docstring.
    """
    from tg_export.api import TgApi

    mgr = _mgr()
    account = mgr.resolve_account(account_name)
    api_id, api_hash = mgr.load_credentials()
    proxy = mgr.load_proxy()
    api = TgApi(mgr.session_path(account), api_id, api_hash, proxy=proxy)
    async with api:
        yield api, account


def _resolve_output(account, config_override, output_override, *, missing_config_hint=None):
    """Resolve ``(account, config, output_base)`` from the command options.

    Reported as a failure when the config file is missing: without it there is
    neither an output directory nor a state database to work on. ``run`` passes
    a hint telling how to create the file; ``{account}`` in it is filled in.
    """
    from tg_export.config import load_config

    mgr = _mgr()
    account = mgr.resolve_account(account)
    config_path = mgr.resolve_config(account, config_override)
    if not config_path.exists():
        _error(f"Config not found: {config_path}")
        if missing_config_hint:
            _error(missing_config_hint.format(account=account))
        raise click.exceptions.Exit(EXIT_FAILURE)

    cfg = load_config(config_path)
    output_base = Path(output_override) if output_override else Path(cfg.output.path)
    return account, cfg, output_base


@contextlib.asynccontextmanager
async def _opened_state(account, config_override, output_override, *, required: bool = True):
    """Open the state database of an account; yield ``(state, output_base, account)``.

    With ``required=False`` a missing database yields ``(None, ...)`` instead of
    exiting: `verify` has nothing to check on a fresh output directory, which is
    not a failure.
    """
    from tg_export.state import ExportState

    account, _, output_base = _resolve_output(account, config_override, output_override)
    state_path = output_base / STATE_DB_NAME

    if not state_path.exists():
        if required:
            _error("No state database found.")
            raise click.exceptions.Exit(EXIT_FAILURE)
        yield None, output_base, account
        return

    async with ExportState(state_path) as state:
        yield state, output_base, account


_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# Default cut length for message text in `tg messages`; 0 disables the cut.
DEFAULT_MESSAGE_TEXT_LENGTH = 200


def _resolve_log_level(debug: bool, log_level: str | None) -> int:
    """Resolve the effective log level.

    Priority (highest first): --debug flag, --log-level flag, LOG_LEVEL env var,
    default WARNING. --debug always wins so it stays a quick "show me everything"
    switch regardless of the environment.
    """
    import os

    if debug:
        return logging.DEBUG
    raw = log_level or os.environ.get("LOG_LEVEL")
    if not raw:
        return logging.WARNING
    name = raw.strip().upper()
    if name not in _LOG_LEVELS:
        raise click.BadParameter(
            f"unknown log level {raw!r}; expected one of {', '.join(_LOG_LEVELS)}",
            param_hint="--log-level",
        )
    return getattr(logging, name)


@click.group()
@click.version_option(package_name="tg-export", prog_name="tg-export")
@click.option("--debug", is_flag=True, default=False, help="Enable debug logging (forces DEBUG level)")
@click.option(
    "--log-level",
    default=None,
    help="Log level (DEBUG/INFO/WARNING/ERROR/CRITICAL). Overrides the LOG_LEVEL env var; --debug overrides this.",
)
@click.option(
    "--quiet",
    "-q",
    is_flag=True,
    default=False,
    help="Suppress progress and status output (errors and the final summary are still shown).",
)
@click.pass_context
def main(ctx, debug, log_level, quiet):
    """tg-export: Flexible Telegram data export tool."""
    from rich.logging import RichHandler

    from tg_export.exporter import console as export_console

    level = _resolve_log_level(debug, log_level)

    logging.basicConfig(
        level=level,
        format="%(name)s %(message)s",
        handlers=[RichHandler(console=export_console, rich_tracebacks=True, show_path=debug)],
    )
    if level > logging.DEBUG:
        logging.getLogger("aiosqlite").setLevel(logging.ERROR)
    global _QUIET, _DEBUG
    _QUIET = quiet
    _DEBUG = debug
    ctx.ensure_object(dict)
    ctx.obj["debug"] = debug
    ctx.obj["quiet"] = quiet


@main.group()
def auth():
    """Telegram authentication: credentials, login, session check."""
    pass


@auth.command("credentials")
@click.option("--api-id", prompt="API ID (from https://my.telegram.org)", type=int, help="Telegram API ID")
@click.option(
    "--api-hash",
    prompt="API Hash",
    # hide_input: the hash authenticates the application the same way a
    # password does; typed in the open it stays in the shell history.
    hide_input=True,
    help="Telegram API Hash (input is hidden; passing it as an option puts it in the shell history)",
)
def auth_credentials(api_id, api_hash):
    """Set Telegram API credentials (api_id and api_hash)."""
    mgr = _mgr()
    mgr.save_credentials(api_id=api_id, api_hash=api_hash)
    _diag("Credentials saved.")


@auth.command("add")
@click.option("--name", prompt="Account alias", help="Account alias")
def auth_add(name):
    """Add a new Telegram account (interactive login)."""
    mgr = _mgr()
    cred_path = mgr.config_dir / "api_credentials.yaml"
    if not cred_path.exists():
        _error("No API credentials found. Run 'tg-export auth credentials' first.")
        raise click.exceptions.Exit(1)
    asyncio.run(mgr.add_account(name))
    _diag(f"Account '{name}' added successfully.")


@auth.command("check")
@click.argument("name", required=False, default=None)
@click.option("--json", "as_json", is_flag=True, help="Output as machine-readable JSON")
def auth_check(name, as_json):
    """Check if account sessions are valid."""
    exit_code = asyncio.run(_auth_check(name, as_json))
    if exit_code:
        raise click.exceptions.Exit(exit_code)


async def _auth_check(name, as_json=False):
    import json

    from tg_export.api import TgApi

    mgr = _mgr()
    accounts = [name] if name else mgr.list_accounts()
    if not accounts:
        if as_json:
            click.echo(json.dumps([], ensure_ascii=False, indent=2))
        else:
            _diag("No accounts configured.")
        return

    api_id, api_hash = mgr.load_credentials()
    results = []
    for acc in accounts:
        session = mgr.session_path(acc)
        if not session.exists():
            results.append({"account": acc, "status": "session_missing"})
            if not as_json:
                _error(f"  {acc}: session file missing")
            continue
        proxy = mgr.load_proxy()
        try:
            async with TgApi(session, api_id, api_hash, proxy=proxy) as api:
                if await api.client.is_user_authorized():
                    me = await api.client.get_me()
                    first = getattr(me, "first_name", "")
                    last = getattr(me, "last_name", None) or ""
                    me_id = getattr(me, "id", None)
                    results.append(
                        {
                            "account": acc,
                            "status": "ok",
                            "name": f"{first} {last}".strip(),
                            "id": me_id,
                        }
                    )
                    if not as_json:
                        _diag(f"  {acc}: OK - {first} {last} (id={me_id})")
                else:
                    results.append({"account": acc, "status": "not_authorized"})
                    if not as_json:
                        _error(f"  {acc}: not authorized")
        except Exception as e:
            results.append({"account": acc, "status": "error", "error": str(e)})
            if not as_json:
                _error(f"  {acc}: error - {e}")

    if as_json:
        click.echo(json.dumps(results, ensure_ascii=False, indent=2))

    # An account that cannot be used is a failure, whatever the output format:
    # a script wrapping `auth check` has no other way to learn about it.
    return EXIT_FAILURE if any(r["status"] != "ok" for r in results) else EXIT_OK


@main.group()
def account():
    """Manage accounts: list, set default, remove."""
    pass


@account.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as machine-readable JSON")
def account_list(as_json):
    """List configured accounts."""
    mgr = _mgr()
    accounts = mgr.list_accounts()
    default = mgr.get_default_account()
    if as_json:
        import json

        payload = [{"name": acc, "default": acc == default} for acc in accounts]
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if not accounts:
        _diag("No accounts configured.")
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
            _error(f"Account '{name}' not found.")
            raise click.exceptions.Exit(1)
        mgr.set_default_account(name)
        _diag(f"Default account set to '{name}'.")
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
        _diag(f"Account '{name}' not found.", essential=True)
        raise click.exceptions.Exit(EXIT_FAILURE)
    mgr.remove_account(name)
    _diag(f"Account '{name}' removed.")


@main.command("config")
@click.option("--verbose", "-v", is_flag=True, help="Verbose: show per-account filters")
def show_config(verbose):
    """Show current configuration (global + per-account)."""
    mgr = _mgr()

    # Global config
    global_path = mgr.config_dir / "config.yaml"
    cred_path = mgr.config_dir / "api_credentials.yaml"

    click.echo(f"# Global: {global_path}")
    if global_path.exists():
        data = mgr.load_global_config()
        proxy = data.get("proxy")
        if proxy:
            auth_str = ""
            if proxy.get("username"):
                auth_str = f" auth={proxy['username']}:***"
            click.echo(
                f"  proxy: {proxy.get('type', 'socks5')}://{proxy.get('host')}:{proxy.get('port')}"
                f" rdns={proxy.get('rdns', True)}{auth_str}"
            )
        else:
            click.echo("  proxy: none")
        import shutil

        mfs = data.get("min_free_space", "20GB")
        # Check free space on the output partition, not cwd
        disk_check_path = Path.cwd()
        default_name = mgr.get_default_account()
        if default_name:
            try:
                cfg_path = mgr.config_path(default_name)
                if cfg_path.exists():
                    acc_cfg = yaml.safe_load(cfg_path.read_text()) or {}
                    output_path = Path(acc_cfg.get("output", {}).get("path", "."))
                    if output_path.exists():
                        disk_check_path = output_path
            except (OSError, yaml.YAMLError) as e:
                logger.debug("show config: cannot read account config %s: %s", default_name, e)
        usage = shutil.disk_usage(disk_check_path)
        free_gb = usage.free / 1024**3
        click.echo(f"  min_free_space: {mfs}  # available: {free_gb:.1f} GB (on {disk_check_path})")
    else:
        click.echo("  (not found)")

    click.echo(f"\n# Credentials: {cred_path}")
    if cred_path.exists():
        creds = yaml.safe_load(cred_path.read_text())
        click.echo(f"  api_id: {creds.get('api_id')}")
        click.echo(f"  api_hash: {creds.get('api_hash', '')[:4]}...")
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
        click.echo("    type_rules:")
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
        click.echo("    folders:")
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
    async with _connected_api(name) as (api, account):
        session = api.client.session
        if session is None:
            _diag(f"  {account}: no session available")
            return
        old_id = session.takeout_id
        if old_id is None:
            _diag(f"  {account}: no active takeout session")
            return
        # Finish it on the server too: an export keeps the session alive on
        # purpose, so wiping only the local id would leave a takeout running
        # with no way to reach it.
        finished = False
        try:
            finished = await api.client.end_takeout(success=True)
        except Exception as e:
            _error(f"  {account}: could not finish takeout on the server ({e}); clearing locally")
        if session.takeout_id is not None:
            session.takeout_id = None
            session.save()
        state = "finished" if finished else "cleared locally"
        _diag(f"  {account}: takeout session {state} (was id={old_id})")


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
@click.option(
    "--truncate",
    type=int,
    default=None,
    help=f"Cut message text to N characters (0 = no cut, default: {DEFAULT_MESSAGE_TEXT_LENGTH})",
)
@click.option("--no-truncate", is_flag=True, default=False, help="Print message text in full")
def tg_messages(chat_id, account, limit, truncate, no_truncate):
    """Show recent messages from a chat."""
    if no_truncate:
        if truncate:
            raise click.BadParameter("--no-truncate cannot be combined with --truncate")
        truncate = 0
    elif truncate is None:
        truncate = DEFAULT_MESSAGE_TEXT_LENGTH
    elif truncate < 0:
        raise click.BadParameter("--truncate must be 0 or greater")
    asyncio.run(_tg_messages(chat_id, account, limit, truncate))


async def _tg_messages(chat_id, account, limit, truncate=DEFAULT_MESSAGE_TEXT_LENGTH):

    async with _connected_api(account) as (api, account):
        entity = await api.client.get_entity(chat_id)
        title = getattr(entity, "title", None) or _entity_name(entity)
        click.echo(f"# {title} (id={chat_id})\n")

        async for msg in api.client.iter_messages(entity, limit=limit):  # pyright: ignore[reportArgumentType]
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

            if truncate:
                text = text[:truncate]
            click.echo(f"  {date_str}  [{msg.id}]  {sender}: {text}")


def _entity_name(entity) -> str:
    first = getattr(entity, "first_name", "") or ""
    last = getattr(entity, "last_name", "") or ""
    return f"{first} {last}".strip() or "Unknown"


@tg.command("info")
@click.argument("chat_ids", type=int, nargs=-1)
@click.option("--account", default=None, help="Account alias")
@click.option(
    "--from-catalog",
    "catalog_file",
    type=click.Path(exists=True),
    help="JSON catalog file (from tg-export list --format json)",
)
@click.option("--type", "chat_type", default=None, help="Filter by chat type (with --from-catalog)")
@click.option("--last", "last_n", type=int, default=0, help="Show last N messages per chat")
@click.option("--output", "output_file", type=click.Path(), default=None, help="Save results to JSON file")
def tg_info(chat_ids, account, catalog_file, chat_type, last_n, output_file):
    """Show chat info: message count, type, title.

    Accepts one or more CHAT_IDS, or use --from-catalog with --type to batch query.
    """
    exit_code = asyncio.run(_tg_info(chat_ids, account, catalog_file, chat_type, last_n, output_file))
    if exit_code:
        raise click.exceptions.Exit(exit_code)


async def _tg_info(chat_ids, account, catalog_file, chat_type, last_n, output_file):
    import json

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
        _diag("No chat IDs specified. Use arguments or --from-catalog --type.")
        return

    results = []
    async with _connected_api(account) as (api, account):
        total = len(ids)
        for idx, cid in enumerate(ids, 1):
            try:
                entity = await api.client.get_entity(cid)
                title = getattr(entity, "title", None) or _entity_name(entity)
                limit = max(last_n, 1)
                result: Any = await api.client(
                    GetHistoryRequest(
                        peer=entity,  # pyright: ignore[reportArgumentType]
                        offset_id=0,
                        offset_date=None,
                        add_offset=0,
                        limit=limit,
                        max_id=0,
                        min_id=0,
                        hash=0,
                    )
                )
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
                    _diag(f"  [{idx}/{total}]...")

            except Exception as e:
                entry = {"id": cid, "error": str(e), "messages": 0}
                results.append(entry)
                if not output_file:
                    _diag(f"[{idx}/{total}] id={cid}: ERROR {e}", essential=True)

        if output_file:
            with open(output_file, "w") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
            _diag(f"Saved {len(results)} entries to {output_file}")
        # A chat that could not be queried is a failure even when the rest
        # succeeded: the JSON carries an "error" key the caller may miss.
        return EXIT_FAILURE if any("error" in r for r in results) else EXIT_OK


@main.command("list")
@click.option("--account", default=None, help="Account alias (default: from 'auth default')")
@click.option("--output", type=click.Path(), help="Output file path")
@click.option("--format", "fmt", type=click.Choice(["yaml", "json"]), default="yaml")
@click.option("--include-left", is_flag=True, help="Include left channels")
def list_chats(account, output, fmt, include_left):
    """Export chat/folder catalog."""
    asyncio.run(_list_chats(account, output, fmt, include_left))


async def _list_chats(account, output, fmt, include_left):
    from tg_export.catalog import fetch_catalog, format_catalog_json, format_catalog_yaml

    async with _connected_api(account) as (api, account):
        chats = await fetch_catalog(api, include_left=include_left)
        result = format_catalog_json(chats) if fmt == "json" else format_catalog_yaml(chats)

        if output:
            Path(output).write_text(result, encoding="utf-8")
            _diag(f"Catalog saved to {output}")
        else:
            click.echo(result)


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
            yaml.safe_load(f)
        # Simple passthrough — generate template
        _diag(f"Generating config from catalog: {from_catalog}")
    else:
        # Fetch from API
        from tg_export.catalog import fetch_catalog

        async with _connected_api(account) as (api, account):
            chats = await fetch_catalog(api)
            template = generate_config_template(chats, account=account)
            config_path.write_text(template, encoding="utf-8")
            _diag(f"Config template saved to {config_path}")
        return

    _diag(f"Config saved to {config_path}")


def _get_dir_size(path: Path) -> int | None:
    """Get directory size using du -sb (Linux) or du -sk fallback (BSD/macOS)."""
    import subprocess

    # Why "--": prevents paths starting with "-" from being parsed as flags.
    # Why fallback: BSD du has no -b; -sk returns KiB.
    for cmd in (["du", "-sb", "--", str(path)], ["du", "-sk", "--", str(path)]):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                value = int(result.stdout.split()[0])
                return value * 1024 if cmd[1] == "-sk" else value
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
            continue
    return None


@main.command("run")
@click.option("--account", default=None, help="Account alias (default: from 'auth default')")
@click.option("--config", type=click.Path(exists=True), default=None, help="Override config path")
@click.option("--output", type=click.Path(), help="Override output directory")
@click.option("--verify", is_flag=True, help="Verify file integrity after export")
@click.option("--dry-run", is_flag=True, help="Show what would be exported")
@click.option(
    "--require-takeout",
    is_flag=True,
    help="Fail instead of falling back to the regular API when Takeout cannot be started.",
)
@click.pass_context
def run_export(ctx, account, config, output, verify, dry_run, require_takeout):
    """Run export according to config. Config resolved by account name convention."""
    quiet = bool(ctx.obj and ctx.obj.get("quiet"))
    exit_code = asyncio.run(
        _run_export(
            account,
            config,
            output,
            verify,
            dry_run,
            quiet=quiet,
            require_takeout=require_takeout,
        )
    )
    if exit_code:
        raise click.exceptions.Exit(exit_code)


async def _start_takeout(api, cfg, *, require: bool) -> bool:
    """Open a Takeout session, or report why the export falls back without one.

    Returns True when Takeout is active. A fall back is announced as an
    essential message: it changes how the whole export is fetched, so it must
    stay visible under --quiet. With ``require`` set the fall back becomes a
    hard error instead.

    Only failures that legitimately mean "no Takeout right now" are handled --
    Telegram RPC errors, the ValueError Telethon raises for an unfinished
    takeout, and connection failures. Anything else (a TypeError from a corrupt
    takeout_id, say) is a defect and must not be hidden behind a fall back.
    """
    from telethon.errors import RPCError, TakeoutInitDelayError

    try:
        await api.start_takeout(
            contacts=cfg.contacts,
            users=True,
            chats=True,
            megagroups=True,
            channels=True,
            files=True,
            # The whole session carries one limit, so it must cover the
            # largest limit any exportable chat asks for, not just defaults.
            max_file_size=cfg.max_media_file_size(),
        )
    except TakeoutInitDelayError as e:
        hours = e.seconds // 3600
        minutes = (e.seconds % 3600) // 60
        reason = (
            f"Takeout cooldown: need to wait {hours}h {minutes}m ({e.seconds}s). "
            f"Approve takeout in your Telegram client to skip the wait."
        )
    except (RPCError, ValueError, OSError) as e:
        reason = f"Takeout not available: {e}"
    else:
        _diag("Takeout session started.")
        return True

    if require:
        raise TakeoutUnavailableError(f"{reason} Takeout was required (--require-takeout).")
    _diag(
        f"{reason} Using regular API: the export continues, but it is slower "
        f"and subject to the usual rate limits.",
        essential=True,
    )
    return False


def _build_downloader(api, state, cfg, output_base: Path):
    """Assemble the media downloader together with its reuse sources.

    Files already present in a Telegram Desktop export or in a sibling account's
    export are linked instead of downloaded again, so both indexes are built
    here rather than inside the download path.
    """
    from tg_export.importer import build_tdesktop_indexes
    from tg_export.media import MediaDownloader

    tdesktop_indexes = build_tdesktop_indexes(cfg.import_existing)
    for idx in tdesktop_indexes:
        _diag(f"tdesktop import: {idx.export_path}")

    sibling_dbs = []
    for sibling in output_base.parent.iterdir():
        if sibling == output_base or not sibling.is_dir():
            continue
        sdb = sibling / STATE_DB_NAME
        if sdb.exists():
            sibling_dbs.append(sdb)
            logger.debug("sibling state DB: %s", sdb)
    if sibling_dbs:
        names = [s.parent.name for s in sibling_dbs]
        _diag(f"Sibling exports for file dedup: {', '.join(names)}")

    min_free = _mgr().load_min_free_space() or 20 * 1024**3  # default 20GB
    return MediaDownloader(
        api=api,
        state=state,
        config=cfg.defaults.media,
        min_free_bytes=min_free,
        tdesktop_indexes=tdesktop_indexes,
        sibling_db_paths=sibling_dbs,
    )


async def _print_export_summary(stats, state, output_base: Path, *, takeout_active: bool) -> None:
    """Print the final report of an export.

    Goes to stderr and is marked essential, so --quiet keeps it: the export
    artifacts themselves are the files written to disk.
    """
    from tg_export.format import format_size

    _diag("\nExport complete:", essential=True)
    # Which API served the export decides how complete and how fast it was, so
    # it belongs in the summary rather than only in a line printed at start-up
    # and long scrolled away.
    _diag(f"  API: {'takeout' if takeout_active else 'regular (no takeout)'}", essential=True)
    _diag(
        f"  Chats: {stats.chats_exported}/{stats.chats_included} (skipped {stats.chats_skipped})",
        essential=True,
    )
    _diag(f"  Messages: {stats.messages_exported}", essential=True)
    _diag(f"  Files downloaded: {stats.files_downloaded}", essential=True)
    for label, value in (
        ("Files existing", stats.files_existing),
        ("Reused from chat", stats.files_reused_chat),
        ("Reused from tdesktop", stats.files_reused_tdesktop),
        ("Reused from sibling", stats.files_reused_sibling),
        ("Skipped by size", stats.files_skipped_by_size),
        ("Skipped by type", stats.files_skipped_by_type),
    ):
        if value:
            _diag(f"  {label}: {value}", essential=True)
    if stats.data_size:
        _diag(f"  Downloaded: {format_size(stats.data_size)}", essential=True)

    file_counts = await state.count_files()
    _diag(
        f"  Files: {file_counts['files_downloaded']}/{file_counts['expected_files']} "
        f"(media messages: {file_counts['media_messages']})",
        essential=True,
    )
    db_size = state.db_path.stat().st_size if state.db_path.exists() else 0
    _diag(f"  DB size: {format_size(db_size)}", essential=True)
    # Why to_thread: du can take seconds on a large export; don't block the loop.
    total_disk = await asyncio.to_thread(_get_dir_size, output_base)
    if total_disk is not None:
        _diag(f"  Export size on disk: {format_size(total_disk)}", essential=True)
    if stats.errors:
        _diag(f"  Errors: {len(stats.errors)}", essential=True)


async def _run_export(
    account,
    config_override,
    output_override,
    verify,
    dry_run,
    quiet=False,
    require_takeout=False,
):
    from tg_export.catalog import fetch_catalog
    from tg_export.exporter import Exporter
    from tg_export.html.renderer import HtmlRenderer
    from tg_export.state import ExportState

    account, cfg, output_base = _resolve_output(
        account,
        config_override,
        output_override,
        missing_config_hint="Create it with: tg-export init --account {account}",
    )
    _diag(f"Account: {account}")
    _diag(f"Output: {output_base.resolve()}")

    # Ensure output dir exists (needed for state DB). Created private: the tree
    # holds every exported message and file. An existing directory keeps the
    # mode the user gave it.
    ensure_private_dir(output_base)

    # State DB next to output
    state_path = output_base / STATE_DB_NAME

    exporter = None
    stats = None
    # Both resources are entered inside the stack: opening the state database
    # creates a lock file, and a failure between that and the connect used to
    # leave the lock and the socket behind. The stack also unwinds them in
    # reverse order -- the takeout session is released by TgApi.disconnect
    # before the database is closed.
    resources = contextlib.AsyncExitStack()
    try:
        state = await resources.enter_async_context(ExportState(state_path))
        api, _ = await resources.enter_async_context(_connected_api(account))

        takeout_active = await _start_takeout(api, cfg, require=require_takeout)

        # Setup renderer
        renderer = HtmlRenderer(output_dir=output_base, config=cfg.output)
        renderer.setup()

        downloader = _build_downloader(api, state, cfg, output_base)

        # Fetch chat list
        chats = await fetch_catalog(api, include_left=(cfg.left_channels_action != "skip"))

        # Create exporter and run
        exporter = Exporter(
            api=api,
            state=state,
            config=cfg,
            renderer=renderer,
            downloader=downloader,
            account=account,
            quiet=quiet,
        )
        stats = await exporter.run(dry_run=dry_run, verify=verify, chat_list=chats)

        if exporter._force_shutdown:
            _diag("\nForce shutdown — state saved.", essential=True)
        else:
            # Render index
            if not dry_run:
                await _render_index(renderer, chats, cfg, state, should_stop=lambda: exporter._shutdown)

            await _print_export_summary(stats, state, output_base, takeout_active=takeout_active)

    except asyncio.CancelledError:
        _diag("\nForce shutdown — saving state...", essential=True)
    finally:
        # Releasing must not raise over the outcome of the export itself, and a
        # second Ctrl+C lands here as CancelledError.
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await resources.aclose()

    # An export that logged errors did not do what it was asked to do, so it must
    # not report success; a signal outranks that and maps to 128 + signum.
    return _export_exit_code(
        signum=exporter._shutdown_signal if exporter else None,
        error_count=len(stats.errors) if stats else 0,
    )


async def _group_chats_for_index(chats, cfg, state, should_stop):
    """Split the exported chats into folders and unfiled, with message counts.

    Returns None when ``should_stop`` fires: the caller then skips the render.
    """
    from collections import defaultdict

    from tg_export.exporter import sanitize_name

    folders = defaultdict(list)
    unfiled = []

    for chat in chats:
        if should_stop and should_stop():
            return None
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
            except aiosqlite.Error as e:
                logger.debug("_render_index: count_messages failed for %s: %s", chat.id, e)

        dir_name = f"{sanitize_name(chat.name)}_{chat.id}"
        # Why sanitize_name(folder): the on-disk folder directory is created via
        # sanitize_name (see resolve_chat_dir / render_folder_index). Using the
        # raw folder name here would produce a href to a non-existent path (404)
        # when the folder name contains spaces, cyrillic specials or /\:*?"<>|.
        folder_prefix = f"folders/{sanitize_name(chat.folder)}/" if chat.folder else "unfiled/"
        entry = {
            "name": chat.name,
            "type": chat.type.value,
            "messages": msg_count,
            "href": f"{folder_prefix}{dir_name}/messages.html",
        }
        if chat.folder:
            folders[chat.folder].append(entry)
        else:
            unfiled.append(entry)

    return folders, unfiled


def _index_sections(cfg) -> list[dict]:
    """Links to the global-data pages the config asked to export."""
    pages = (
        (cfg.personal_info, "Personal Info", "Personal Information", "personal_info.html"),
        (cfg.contacts, "Contacts", "Contacts", "contacts.html"),
        (cfg.sessions, "Sessions", "Active Sessions", "sessions.html"),
        (cfg.userpics, "Profile Photos", "Profile Photos", "userpics.html"),
        (cfg.stories, "Stories", "Stories", "stories.html"),
        (cfg.other_data or cfg.profile_music, "Other Data", "Other Data", "other_data.html"),
    )
    return [
        {"title": title, "entries": [{"name": name, "href": href, "meta": ""}]}
        for enabled, title, name, href in pages
        if enabled
    ]


async def _render_index(renderer, chats, cfg, state, should_stop=None):
    """Build and render the main index page.

    should_stop: optional callable. If returns True between chats or before
    the final jinja render, abort early. Why: render_index runs synchronously
    inside the event loop after the main export loop; without a checkpoint a
    fresh SIGINT during this phase would still have to wait for the index
    render to finish before asyncio.run() can exit.
    """
    from tg_export.exporter import sanitize_name

    if should_stop and should_stop():
        return

    grouped = await _group_chats_for_index(chats, cfg, state, should_stop)
    if grouped is None:
        return
    folders, unfiled = grouped

    sections = _index_sections(cfg)

    # Build folders_list with hrefs for folder index pages
    folders_list = []
    for folder_name, folder_chats in folders.items():
        folder_dir_name = sanitize_name(folder_name)
        folders_list.append(
            {
                "name": folder_name,
                "href": f"folders/{folder_dir_name}/index.html",
                "chats": folder_chats,
            }
        )

    if should_stop and should_stop():
        return
    # to_thread: the render is synchronous and runs while the Telegram
    # connection is still open, so in the loop thread it stalls everything else.
    await asyncio.to_thread(
        renderer.render_index, folders_list=folders_list, unfiled=unfiled, sections=sections
    )

    # Render per-folder index pages
    for folder_info in folders_list:
        if should_stop and should_stop():
            return
        adjusted = []
        for entry in folder_info["chats"]:
            dir_name = entry["href"].split("/")[-2]  # Chat_123 from folders/Folder/Chat_123/messages.html
            adjusted.append(
                {
                    "name": entry["name"],
                    "type": entry["type"],
                    "messages": entry["messages"],
                    "href": f"{dir_name}/messages.html",
                }
            )
        await asyncio.to_thread(renderer.render_folder_index, folder_info["name"], adjusted)


@main.group()
def state():
    """Manage export state (reset, show status, force re-export)."""
    pass


@state.command("show")
@click.option("--account", default=None, help="Account alias")
@click.option("--config", type=click.Path(exists=True), default=None)
@click.option("--output", type=click.Path(), default=None)
@click.option("--json", "as_json", is_flag=True, help="Output as machine-readable JSON")
@click.argument("chat_id", type=int, required=False)
def state_show(account, config, output, as_json, chat_id):
    """Show export state for all chats or a specific chat."""
    asyncio.run(_state_show(account, config, output, chat_id, as_json))


async def _state_show(account, config_override, output_override, chat_id, as_json=False):
    import json

    async with _opened_state(account, config_override, output_override) as (st, _, account):
        if chat_id:
            chat_state = await st.get_chat_state(chat_id)
            if not chat_state:
                if as_json:
                    click.echo(json.dumps(None))
                else:
                    _diag(f"No state for chat {chat_id}")
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
                _diag("No export state records.")
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
@click.option("--account", default=None, help="Account alias")
@click.option("--config", type=click.Path(exists=True), default=None)
@click.option("--output", type=click.Path(), default=None)
@click.option("--all", "reset_all", is_flag=True, help="Reset all chats")
@click.option("--delete-messages", is_flag=True, help="Also delete messages from DB")
@click.argument("chat_id", type=int, required=False)
def state_reset(account, config, output, reset_all, delete_messages, chat_id):
    """Reset export state to force re-download. Specify chat_id or --all."""
    if not chat_id and not reset_all:
        _error("Specify chat_id or --all")
        raise click.exceptions.Exit(1)
    exit_code = asyncio.run(_state_reset(account, config, output, reset_all, delete_messages, chat_id))
    if exit_code:
        raise click.exceptions.Exit(exit_code)


async def _state_reset(account, config_override, output_override, reset_all, delete_messages, chat_id):
    async with _opened_state(account, config_override, output_override) as (st, _, account):
        if reset_all:
            await st.reset_chat_progress(delete_messages=delete_messages)
            _diag("Reset all chats.")
        else:
            chat_state = await st.get_chat_state(chat_id)
            if not chat_state:
                _diag(f"No state for chat {chat_id}", essential=True)
                return EXIT_FAILURE
            await st.reset_chat_progress(chat_id, delete_messages=delete_messages)
            msg = f"Reset chat {chat_id}."
            if delete_messages:
                msg += " Messages and files records deleted."
            _diag(msg)
        return EXIT_OK


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

    async with _opened_state(account, config_override, output_override) as (
        state,
        output_base,
        account,
    ):
        # Resolve chat: by ID or by name search
        try:
            chat_id = int(chat_arg)
            entry = await state.get_catalog_entry(chat_id)
            chat_name = entry["name"] if entry else f"id={chat_id}"
        except ValueError:
            matches = await state.find_chat_by_name(chat_arg)
            if not matches:
                _error(f"No chats found matching '{chat_arg}'")
                raise click.exceptions.Exit(1) from None
            if len(matches) > 1:
                _error(f"Multiple chats match '{chat_arg}':")
                for m in matches:
                    _error(f"  {m['chat_id']}  {m['name']}  ({m['type']})")
                _error("Specify exact chat ID.")
                raise click.exceptions.Exit(1) from None
            chat_id = matches[0]["chat_id"]
            chat_name = matches[0]["name"]

        # Show what will be deleted
        counts = await state.count_chat_rows(chat_id)

        # Find chat directory on disk: scan known prefixes only, never rglob
        # the whole output tree (would also follow into sibling/back-up trees).
        from tg_export.exporter import sanitize_name

        dir_suffix = f"{sanitize_name(chat_name)}_{chat_id}"
        output_resolved = output_base.resolve()
        candidate_dirs: list[Path] = []
        # Direct prefixes: unfiled/, left/, archived/
        for prefix in ("unfiled", "left", "archived"):
            p = output_base / prefix / dir_suffix
            if p.exists():
                candidate_dirs.append(p)
        # folders/<*>/<dir_suffix>
        folders_root = output_base / "folders"
        if folders_root.is_dir():
            for folder_dir in folders_root.iterdir():
                if not folder_dir.is_dir():
                    continue
                p = folder_dir / dir_suffix
                if p.exists():
                    candidate_dirs.append(p)
        # Filter out paths that escape output_base (symlinks, .., etc.)
        chat_dirs: list[Path] = []
        for d in candidate_dirs:
            if d.is_symlink():
                _diag(f"  SKIP (symlink): {d}")
                continue
            try:
                d_resolved = d.resolve()
            except OSError:
                continue
            if not d_resolved.is_relative_to(output_resolved):
                _diag(f"  SKIP (outside output): {d}")
                continue
            chat_dirs.append(d)

        # essential: the confirmation below asks to authorise an irreversible
        # deletion, so the description of what it covers must not be suppressed.
        # A prompt with the subject hidden is a prompt the user cannot answer.
        _diag(f"Chat: {chat_name} (id={chat_id})", essential=True)
        _diag(
            f"  DB: messages={counts['messages']}, files={counts['files']}, "
            f"export_state={counts['export_state']}, catalog_cache={counts['catalog_cache']}",
            essential=True,
        )
        if chat_dirs:
            for d in chat_dirs:
                size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                from tg_export.format import format_size

                _diag(f"  Dir: {d} ({format_size(size)})", essential=True)
        else:
            _diag("  Dir: not found", essential=True)

        if not skip_confirm and not click.confirm("Delete all data for this chat?"):
            _diag("Cancelled.", essential=True)
            return

        # Purge from DB
        deleted = await state.purge_chat(chat_id)
        _diag(f"  Deleted from DB: {deleted}")

        # Remove directory
        for d in chat_dirs:
            shutil.rmtree(d)
            _diag(f"  Removed: {d}")

        _diag("Done.")


@main.command("verify")
@click.option("--account", default=None, help="Account alias (default: from 'auth default')")
@click.option("--config", type=click.Path(exists=True), default=None, help="Override config path")
@click.option("--output", type=click.Path(), help="Export output directory")
def verify_files(account, config, output):
    """Verify integrity of previously downloaded files."""
    exit_code = asyncio.run(_verify_files(account, config, output))
    if exit_code:
        raise click.exceptions.Exit(exit_code)


VERIFY_STAGING_PREFIX = ".tg-export-verify-"


def _clean_verify_staging(root: Path) -> None:
    """Remove staging directories left behind by a killed verify run.

    A SIGKILL/SIGTERM in the middle of a download skips TemporaryDirectory
    cleanup. The leftovers are harmless but accumulate next to the media, so
    sweep them at the start of the next run.
    """
    import shutil

    for path in root.rglob(f"{VERIFY_STAGING_PREFIX}*"):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)


async def _redownload_broken_file(api, state, entry: dict) -> bool:
    """Re-download one broken file, replacing it only after the download succeeded.

    Returns True when the file was replaced, False when the message no longer
    carries media or the download produced nothing. Download failures propagate.

    The old file stays untouched until the new one is fully on disk: deleting
    first meant that an interruption -- a dropped connection, Ctrl+C -- left
    nothing behind while the database still pointed at the vanished path.
    """
    import os
    import tempfile

    chat_id = entry["chat_id"]
    msg_id = entry["msg_id"]
    local_path = Path(entry["local_path"])

    tl_messages = await api.client.get_messages(chat_id, ids=msg_id)
    tl_msg = tl_messages if not isinstance(tl_messages, list) else (tl_messages[0] if tl_messages else None)
    if tl_msg is None or tl_msg.media is None:
        _error(f"  [skip] msg {msg_id}: not found or no media")
        return False

    target_dir = local_path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    # dir=target_dir keeps the staging area on the same filesystem, so the final
    # move is an atomic rename rather than a copy.
    with tempfile.TemporaryDirectory(dir=target_dir, prefix=VERIFY_STAGING_PREFIX) as staging:
        downloaded = await api.download_media(tl_msg, Path(staging))
        if not downloaded:
            _error(f"  [fail] {local_path}")
            return False

        downloaded = Path(str(downloaded))
        final_path = target_dir / downloaded.name
        os.replace(downloaded, final_path)

    # Telethon may pick a different name than the one recorded earlier; drop the
    # stale file only now, once its replacement is in place.
    if local_path != final_path and local_path.exists():
        local_path.unlink()

    await state.register_file(
        file_id=entry["file_id"],
        chat_id=chat_id,
        msg_id=msg_id,
        expected_size=entry["expected_size"],
        actual_size=final_path.stat().st_size,
        local_path=str(final_path),
        status="done",
    )
    await state.commit()
    _diag(f"  [ok] {final_path}")
    return True


async def _verify_files(account, config_override, output_override):
    async with _opened_state(account, config_override, output_override, required=False) as (
        state,
        output_base,
        account,
    ):
        if state is None:
            _diag("No state database found. Nothing to verify.")
            return EXIT_OK

        broken = await state.get_files_to_verify()
        if not broken:
            _diag("All files OK.")
            return EXIT_OK

        _diag(f"Found {len(broken)} files with issues:")
        for f in broken:
            _diag(
                f"  {f['local_path']} - status: {f['status']}, "
                f"expected: {f['expected_size']}, actual: {f['actual_size']}"
            )

        async with _connected_api(account) as (api, account):
            _clean_verify_staging(output_base)
            redownloaded = 0
            for f in broken:
                try:
                    if await _redownload_broken_file(api, state, f):
                        redownloaded += 1
                except Exception as e:
                    _diag(f"  [error] {f['local_path']}: {e}", essential=True)

            _diag(f"\nRe-downloaded: {redownloaded}/{len(broken)}", essential=True)
            if redownloaded < len(broken):
                return EXIT_FAILURE
            return EXIT_OK


# ---------------------------------------------------------------------------
# tg send / tg download — additional direct Telegram API commands
# ---------------------------------------------------------------------------


@tg.command("send")
@click.option("--account", default=None, help="Account alias")
@click.option(
    "--file",
    "-f",
    "files",
    multiple=True,
    type=click.Path(exists=True),
    help="File(s) to attach (can be specified multiple times)",
)
@click.option("--text", "-t", default=None, help="Message text")
@click.option(
    "--as-document",
    is_flag=True,
    default=False,
    help="Send files as documents without compression (keeps original quality)",
)
@click.argument("recipients", nargs=-1, required=True)
def tg_send(account, files, text, as_document, recipients):
    """Send message to one or more recipients.

    RECIPIENTS: chat IDs or usernames (multiple allowed).
    Use --text for message text and --file for attachments.
    At least --text or --file must be specified.

    Note: delivery is best-effort and not idempotent. If sending fails for some
    recipients (network/RPC error), re-running this command will resend to all
    recipients, including those who already received the message.
    """
    if not text and not files:
        _error("Error: specify --text and/or --file")
        raise click.exceptions.Exit(1)

    parsed = []
    for r in recipients:
        try:
            parsed.append(int(r))
        except ValueError:
            parsed.append(r)

    exit_code = asyncio.run(_tg_send(account, parsed, text, files, as_document))
    if exit_code:
        raise click.exceptions.Exit(exit_code)


@contextlib.contextmanager
def _upload_progress(by_bytes: bool):
    """Progress bars for an upload: current file plus overall total.

    Yields None when there is nothing to draw on -- under --quiet, and when
    the output is not a terminal. The export path makes the same call
    (``console.is_terminal and not self.quiet``); without it a redirected run
    collected bar redraws in the log.

    The percentage column is present in both modes so that the same command
    does not look different depending on how many files were passed; the byte
    mode adds size and speed on top of it.
    """
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        TaskProgressColumn,
        TextColumn,
        TransferSpeedColumn,
    )

    from tg_export.exporter import console

    if _QUIET or not console.is_terminal:
        yield None
        return

    columns = [
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
    ]
    if by_bytes:
        columns += [DownloadColumn(binary_units=True), TransferSpeedColumn()]

    with Progress(*columns, console=console) as progress:
        yield progress


async def _send_files(client, recipient, file_paths, text, as_document):
    """Send attachments to one recipient, reporting upload progress.

    Documents never join an album, so with ``as_document`` files go one by one
    and progress is counted in bytes. Compressed photos keep the album grouping
    Telethon does in chunks of 10; there the callback counts files, not bytes.
    """
    from tg_export.exporter import file_progress_description

    caption = text or ""
    by_bytes = as_document or len(file_paths) == 1

    with _upload_progress(by_bytes) as progress:
        if not by_bytes:
            task = (
                progress.add_task(f"{len(file_paths)} files", total=len(file_paths))
                if progress is not None
                else None
            )

            def album_progress(sent, total):
                # `total` from Telethon is the tail of the album still to send:
                # it slices the list into chunks of 10 and reports 25, then 15,
                # then 5, while `sent` keeps counting from the start. Adopting
                # that total pushed the bar past 100% for more than ten files.
                if progress is not None and task is not None:
                    progress.update(task, completed=min(sent, len(file_paths)))

            await client.send_file(
                recipient,
                [str(p) for p in file_paths],
                caption=caption,
                force_document=as_document,
                progress_callback=album_progress,
            )
            return

        sizes = [p.stat().st_size for p in file_paths]
        total_task = None
        if progress is not None and len(file_paths) > 1:
            total_task = progress.add_task(f"total 0/{len(file_paths)} files", total=sum(sizes))
        done_bytes = 0

        for index, path in enumerate(file_paths):
            # rich parses the description as markup, so an unescaped name is
            # printed mangled: [draft]report.txt comes out as report.txt.
            task = (
                progress.add_task(file_progress_description(path.name), total=sizes[index])
                if progress is not None
                else None
            )

            def file_progress(sent, total, task=task, done=done_bytes):
                if progress is None or task is None:
                    return
                progress.update(task, completed=sent, total=total)
                if total_task is not None:
                    progress.update(total_task, completed=done + sent)

            await client.send_file(
                recipient,
                str(path),
                caption=caption if index == 0 else "",
                force_document=as_document,
                progress_callback=file_progress,
            )
            done_bytes += sizes[index]
            if progress is not None and task is not None:
                progress.remove_task(task)
                if total_task is not None:
                    progress.update(
                        total_task,
                        completed=done_bytes,
                        description=f"total {index + 1}/{len(file_paths)} files",
                    )


async def _tg_send(account_name, recipients, text, files, as_document=False):
    async with _connected_api(account_name) as (api, _):
        file_paths = [Path(f) for f in files] if files else None
        sent_count = 0
        failed: list[tuple[object, str]] = []
        total = len(recipients)

        for recipient in recipients:
            try:
                if file_paths:
                    await _send_files(api.client, recipient, file_paths, text, as_document)
                elif text:
                    await api.client.send_message(recipient, text)

                _diag(f"  sent to {recipient}")
                sent_count += 1
            except Exception as e:
                _error(f"  error sending to {recipient}: {e}")
                failed.append((recipient, str(e)))

        if failed:
            _diag(
                f"\nDelivered to {sent_count}/{total} recipients. "
                f"{len(failed)} failed. Re-running this command will resend "
                f"to ALL {total} recipients (no idempotency).",
                essential=True,
            )
            return EXIT_FAILURE
        return EXIT_OK


@tg.command("download")
@click.option("--account", default=None, help="Account alias")
@click.option("--output", "-o", type=click.Path(), default=".", help="Output directory")
@click.argument("chat_id", type=int)
@click.argument("msg_id", type=int)
def tg_download(account, output, chat_id, msg_id):
    """Download message content: text and all media files.

    Saves message text to <msg_id>.txt and media files to the output directory.
    """
    exit_code = asyncio.run(_tg_download(account, chat_id, msg_id, output))
    if exit_code:
        raise click.exceptions.Exit(exit_code)


def _file_head_sha256(path: Path, n_bytes: int = 64 * 1024) -> str:
    """SHA-256 of the first n_bytes of a file (cheap content fingerprint)."""
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        chunk = f.read(n_bytes)
        h.update(chunk)
    return h.hexdigest()


async def _download_if_new(client, msg, out: Path, downloaded: set) -> str | None:
    """Download media, skip only if EXACTLY the same content was already saved.

    Why: previous version compared just file size, which would silently delete
    legitimately distinct files of the same size (two photos in an album, two
    PDFs of the same length). Now we compare both size and a head-SHA256
    fingerprint.
    """
    path = await client.download_media(msg, file=str(out))
    if not path:
        return None
    p = Path(path)
    p_size = p.stat().st_size
    p_hash: str | None = None
    for existing in downloaded:
        if existing == p:
            continue
        try:
            existing_size = existing.stat().st_size
        except OSError:
            continue
        if existing_size != p_size:
            continue
        if p_hash is None:
            p_hash = _file_head_sha256(p)
        try:
            existing_hash = _file_head_sha256(existing)
        except OSError:
            continue
        if existing_hash == p_hash:
            p.unlink()
            return None
    downloaded.add(p)
    return path


async def _tg_download(account_name, chat_id, msg_id, output_dir):
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    async with _connected_api(account_name) as (api, _):
        tl_msg = await api.client.get_messages(chat_id, ids=msg_id)
        if isinstance(tl_msg, list):
            tl_msg = tl_msg[0] if tl_msg else None
        if tl_msg is None:
            _diag(f"Message {msg_id} not found in chat {chat_id}", essential=True)
            return EXIT_FAILURE

        # Save text
        msg_text = getattr(tl_msg, "text", None)
        if msg_text:
            text_file = out / f"{msg_id}.txt"
            text_file.write_text(msg_text, encoding="utf-8")
            _diag(f"  text: {text_file}")

        # Download media (skip if file already exists)
        downloaded = {f for f in out.iterdir() if f.is_file()}
        if tl_msg.media:
            path = await _download_if_new(api.client, tl_msg, out, downloaded)
            if path:
                _diag(f"  media: {path}")

        # Check for grouped_id (album) — download all parts
        if tl_msg.grouped_id:
            count = 0
            async for grouped_msg in api.client.iter_messages(
                chat_id,
                min_id=msg_id - 10,
                max_id=msg_id + 10,
            ):
                if (
                    grouped_msg.grouped_id == tl_msg.grouped_id
                    and grouped_msg.id != msg_id
                    and grouped_msg.media
                ):
                    path = await _download_if_new(api.client, grouped_msg, out, downloaded)
                    if path:
                        _diag(f"  album media: {path}")
                        count += 1
            if count:
                _diag(f"  ({count} additional album files)")

        if not msg_text and not tl_msg.media:
            _diag("  (empty message, no text or media)")
        return EXIT_OK


def run_cli() -> None:
    """Console-script entry point.

    Wraps the Click app so that tg-export domain errors (TgExportError and its
    subclasses) are reported as a short message on stderr with exit code 1,
    instead of dumping a stack trace. The full traceback is shown only when
    --debug is passed.
    """
    try:
        main.main(standalone_mode=True)
    except KeyboardInterrupt:
        # Ctrl+C outside the export loop (during a prompt, a connect, a render)
        # used to surface as 0 or 1 depending on where it landed. Report it the
        # way `run` already reports an interrupted export: 128 + SIGINT.
        click.echo("Interrupted.", err=True)
        raise SystemExit(EXIT_SIGINT) from None
    except TgExportError as e:
        if _DEBUG:
            raise
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(EXIT_FAILURE) from e
