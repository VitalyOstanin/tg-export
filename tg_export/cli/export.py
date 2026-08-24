"""Top-level commands built around an export: config, list, init, run, purge, verify.

``run`` is the centre of the module: it resolves the configuration, opens the
state database, starts Takeout when it is available and hands the work to the
exporter. The rest either prepare its input (``config``, ``list``, ``init``) or
work on its output (``purge``, ``verify``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tg_export.media import MediaDownloader
    from tg_export.models import Chat

import asyncio
import contextlib
import json
import logging
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

import aiosqlite
import click
import yaml
from click import ParameterSource

from tg_export.cli import common
from tg_export.cli.common import (
    STATE_DB_NAME,
    export_exit_code,
    fail,
)
from tg_export.console import confirm
from tg_export.errors import (
    EXIT_FAILURE,
    EXIT_OK,
    TakeoutUnavailableError,
)
from tg_export.format import format_size
from tg_export.media import clean_staging
from tg_export.privacy import ensure_private_dir, write_private_text
from tg_export.verify import RedownloadResult, redownload_broken_files

logger = logging.getLogger(__name__)


@click.command("config")
@click.option("--verbose", "-v", is_flag=True, help="Verbose: show per-account filters")
@click.option("--json", "as_json", is_flag=True, help="Output as machine-readable JSON")
def show_config(verbose, as_json) -> None:
    """Show current configuration (global + per-account)."""
    mgr = common.account_manager()
    if as_json:
        click.echo(json.dumps(_config_payload(mgr, verbose=verbose), ensure_ascii=False, indent=2))
        return

    global_path = mgr.config_dir / "config.yaml"
    cred_path = mgr.config_dir / "api_credentials.yaml"

    click.echo(f"# Global: {global_path}")
    if global_path.exists():
        data = mgr.load_global_config()
        click.echo(f"  proxy: {_proxy_caption(_proxy_view(mgr))}")

        mfs = _min_free_space_caption(mgr, data)
        # Check free space on the output partition, not cwd
        disk_check_path = _free_space_check_path(mgr)
        usage = shutil.disk_usage(disk_check_path)
        free_gb = usage.free / 1024**3
        click.echo(f"  min_free_space: {mfs}  # available: {free_gb:.1f} GB (on {disk_check_path})")
    else:
        click.echo("  (not found)")

    click.echo(f"\n# Credentials: {cred_path}")
    if cred_path.exists():
        creds = yaml.safe_load(cred_path.read_text(encoding="utf-8"))
        click.echo(f"  api_id: {creds.get('api_id')}")
        click.echo(f"  api_hash: {creds.get('api_hash', '')[:4]}...")
    else:
        click.echo("  (not found)")

    default = mgr.get_default_account()
    click.echo(f"\n# Default account: {default or '(not set)'}")

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


def _config_payload(mgr, *, verbose: bool) -> dict:
    """The same summary as the human-readable output, as one document.

    The lines of `config` are data -- paths, the reserve in force, which
    accounts have a session -- and reading them back meant a regular expression
    over `  key: value`. The api_hash is not here at all: the terminal shows
    four characters of it to tell one credential from another, which a program
    reading this has no use for.
    """
    global_path = mgr.config_dir / "config.yaml"
    cred_path = mgr.config_dir / "api_credentials.yaml"
    data = mgr.load_global_config() if global_path.exists() else {}
    disk_check_path = _free_space_check_path(mgr)
    usage = shutil.disk_usage(disk_check_path)
    default = mgr.get_default_account()

    payload: dict = {
        "config_dir": str(mgr.config_dir),
        "global_config": str(global_path) if global_path.exists() else None,
        "proxy": _proxy_view(mgr) if global_path.exists() else None,
        "min_free_space": _min_free_space_caption(mgr, data) if global_path.exists() else None,
        "free_space_checked_at": str(disk_check_path),
        "free_bytes": usage.free,
        "credentials": {"path": str(cred_path), "api_id": _credential_api_id(cred_path)}
        if cred_path.exists()
        else None,
        "default_account": default,
        "accounts": [],
    }
    for acc in mgr.list_accounts():
        config_path = mgr.config_path(acc)
        entry = {
            "name": acc,
            "default": acc == default,
            "session": mgr.session_path(acc).exists(),
            "config": str(config_path) if config_path.exists() else None,
        }
        if verbose and config_path.exists():
            entry["settings"] = _account_settings(config_path)
        payload["accounts"].append(entry)
    return payload


def _credential_api_id(cred_path: Path) -> Any:
    creds = yaml.safe_load(cred_path.read_text(encoding="utf-8")) or {}
    return creds.get("api_id")


def _rule_payload(rule) -> dict:
    """One rule as data: the same facts `_rule_summary` puts in a caption."""
    if rule.skip:
        return {"skip": True}
    return {
        "skip": False,
        "media": {"types": list(rule.media.types), "max_file_size": rule.media.max_file_size_bytes}
        if rule.media
        else None,
        "date_from": rule.date_from,
        "date_to": rule.date_to,
    }


def _account_settings(config_path: Path) -> dict:
    """Every section of one account, as the loader reads them.

    The same sections as the verbose text output, for the same reason it names
    all of them: a section absent from the output cannot be told apart from one
    the loader does not know. Rules come as structures rather than captions --
    a program reading this compares values, not phrasing.
    """
    from tg_export.config import GLOBAL_DATA_SECTIONS, load_config

    cfg = load_config(config_path)
    defaults = cfg.defaults
    return {
        "output": {"path": str(cfg.output.path), "format": cfg.output.format},
        "defaults": {
            "media": {
                "types": list(defaults.media.types),
                "max_file_size": format_size(defaults.media.max_file_size_bytes),
                "concurrent_downloads": defaults.media.concurrent_downloads,
            },
            "export_service_messages": defaults.export_service_messages,
            "date_from": defaults.date_from,
            "date_to": defaults.date_to,
        },
        **{name: getattr(cfg, name) for name in GLOBAL_DATA_SECTIONS},
        "type_rules": {key: _rule_payload(rule) for key, rule in cfg.type_rules.items()},
        "folders": {
            name: {
                "skip": folder.skip,
                "media": {"types": list(folder.media.types)} if folder.media else None,
                "chats": len(folder.chats),
            }
            for name, folder in cfg.folders.items()
        },
        "chats": [{"id": rule.id, "name": rule.name, **_rule_payload(rule)} for rule in cfg.chats],
        "unmatched": cfg.unmatched_action,
        "left_channels": cfg.left_channels_action,
        "archived": cfg.archived_action,
        "import_existing": [{"type": entry.type, "path": str(entry.path)} for entry in cfg.import_existing],
    }


def _proxy_view(mgr) -> dict | None:
    """The proxy as the export reads it, without the password.

    Reading the raw YAML here showed a setting the export refuses to work
    with as the one in force: a typo in a key name gave port `None` in
    `config` and the default port in `run`. The password never leaves this
    function -- the text output has always masked it, and the JSON one is
    read by programs that have no use for it; whether one is set at all is
    still worth knowing, so that stays as a flag.
    """
    from tg_export.config import ConfigError

    try:
        proxy = mgr.load_proxy()
    except ConfigError as e:
        return {"invalid": str(e)}
    if proxy is None:
        return None
    kind, host, port, rdns, username, password = proxy
    return {
        "type": kind,
        "host": host,
        "port": port,
        "rdns": rdns,
        "username": username,
        "password_set": bool(password),
    }


def _proxy_caption(view: dict | None) -> str:
    """One line about the proxy, in the shape the other captions use."""
    if view is None:
        return "none"
    if "invalid" in view:
        return f"invalid: {view['invalid']}"
    auth = f" auth={view['username']}:***" if view["username"] else ""
    return f"{view['type']}://{view['host']}:{view['port']} rdns={view['rdns']}{auth}"


def _min_free_space_caption(mgr, data: dict) -> str:
    """The reserve as the export reads it, not as it is written in the file.

    A record `parse_size` rejects used to be shown as the setting in force,
    while `run` refused with a ConfigError on the very same file.
    """
    from tg_export.config import ConfigError

    try:
        return format_size(mgr.load_min_free_space())
    except ConfigError as e:
        return f"{data.get('min_free_space')!r} (invalid: {e})"


def _free_space_check_path(mgr) -> Path:
    """Directory whose free space this line is about: the one the export writes to.

    The path is read the way the export reads it -- `load_config` expands `~`
    and `account_output_dir` appends the alias. Parsing the YAML here directly
    did neither, so `~/exports` looked like a missing directory and the line
    reported the current working directory instead. Before the first run the
    export directory does not exist yet: its nearest existing parent stands for
    the same partition.
    """
    from tg_export.config import ConfigError, load_config

    default_name = mgr.get_default_account()
    if not default_name:
        return Path.cwd()
    cfg_path = mgr.config_path(default_name)
    if not cfg_path.exists():
        return Path.cwd()
    try:
        base = load_config(cfg_path).output.path
    except (OSError, ConfigError) as e:
        logger.debug("show config: cannot read account config %s: %s", default_name, e)
        return Path.cwd()
    output_base = common.account_output_dir(Path(base), default_name)
    return next((p for p in (output_base, *output_base.parents) if p.exists()), Path.cwd())


def _date_range(date_from, date_to) -> str:
    """A date range caption: one shape for defaults and for rules alike."""
    return f"{date_from or '...'} — {date_to or '...'}"


def _rule_summary(rule) -> str:
    """A rule caption: how the rule differs from the defaults.

    The block was copied for `type_rules` and for `chats`, and the copies
    drifted apart in how they spelled the date range.
    """
    if rule.skip:
        return "skip"
    parts = []
    if rule.media:
        parts.append(f"media={rule.media.types}")
    if rule.date_from or rule.date_to:
        parts.append(f"dates={_date_range(rule.date_from, rule.date_to)}")
    return ", ".join(parts) or "defaults"


def _global_data_summary(cfg) -> str:
    """One line naming every global-data section together with its value.

    Every section is named even when it is on: a section missing from the
    output cannot be told apart from one the loader does not know.
    """
    from tg_export.config import GLOBAL_DATA_SECTIONS

    return ", ".join(f"{name}={'on' if getattr(cfg, name) else 'off'}" for name in GLOBAL_DATA_SECTIONS)


def _show_account_config(config_path) -> None:
    """Show per-account config details (verbose mode)."""
    from tg_export.config import load_config

    cfg = load_config(config_path)

    click.echo(f"    output.path: {cfg.output.path}")
    click.echo(f"    output.format: {cfg.output.format}")

    defaults = cfg.defaults
    click.echo(f"    defaults.media.types: {defaults.media.types}")
    click.echo(f"    defaults.media.max_file_size: {format_size(defaults.media.max_file_size_bytes)}")
    click.echo(f"    defaults.media.concurrent_downloads: {defaults.media.concurrent_downloads}")
    click.echo(f"    defaults.export_service_messages: {defaults.export_service_messages}")
    if defaults.date_from or defaults.date_to:
        click.echo(f"    defaults.date_range: {_date_range(defaults.date_from, defaults.date_to)}")

    click.echo(f"    global data: {_global_data_summary(cfg)}")

    if cfg.type_rules:
        click.echo("    type_rules:")
        for key, rule in cfg.type_rules.items():
            click.echo(f"      {key}: {_rule_summary(rule)}")

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
            click.echo(f"      {ident}: {_rule_summary(rule)}")

    click.echo(f"    unmatched: {cfg.unmatched_action}")
    click.echo(f"    left_channels: {cfg.left_channels_action}")
    click.echo(f"    archived: {cfg.archived_action}")
    click.echo(f"    import_existing: {len(cfg.import_existing)} sources")
    for entry in cfg.import_existing:
        click.echo(f"      {entry.type}: {entry.path}")


def _save_catalog(path: Path, text: str) -> None:
    """Write the catalog private from the start.

    It carries a line per chat of the account -- ids, names and message counts
    -- which is exactly why the config template `init` writes is private, and
    this file is what `init --from-catalog` reads back.
    """
    write_private_text(path, text)


@click.command("list")
@click.option("--account", default=None, help=common.ACCOUNT_HELP)
@click.option(
    "--output-file",
    "--output",
    "output",
    type=click.Path(path_type=Path),
    help="Write the catalog to this file instead of stdout (--output is the old spelling)",
)
@click.option("--json", "as_json", is_flag=True, help="Output as machine-readable JSON")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["yaml", "json"]),
    default="yaml",
    help="Output format (default: yaml); --json is the short form of --format json",
)
@click.option("--include-left", is_flag=True, help="Include left channels")
@click.pass_context
def list_chats(ctx, account, output, as_json, fmt, include_left) -> None:
    """Export chat/folder catalog."""
    if as_json and ctx.get_parameter_source("fmt") is not ParameterSource.DEFAULT:
        # `--json` is the short form of `--format json`, so the two together
        # name two formats. The flag used to win silently, and a run asking for
        # YAML got JSON with nothing to tell it apart.
        raise click.UsageError("--json and --format name the format twice; pass one of them.")
    asyncio.run(_list_chats(account, output, "json" if as_json else fmt, include_left))


async def _list_chats(account, output, fmt, include_left) -> None:
    from tg_export.catalog import fetch_catalog, format_catalog_json, format_catalog_yaml

    async with common.connected_api(account) as (api, account):
        chats = await fetch_catalog(api, include_left=include_left)
        result = format_catalog_json(chats) if fmt == "json" else format_catalog_yaml(chats)

        if output:
            _save_catalog(output, result)
            common.diag(f"Catalog saved to {output}")
        else:
            click.echo(result)


@click.command("init")
@click.option("--account", default=None, help=common.ACCOUNT_HELP)
@click.option(
    "--from-catalog",
    "--from",
    "from_catalog",
    type=click.Path(exists=True, path_type=Path),
    help="Build the config from this catalog file (--from is the old spelling)",
)
@click.option(
    "--output-file",
    "--output",
    "output",
    type=click.Path(path_type=Path),
    default=None,
    help="Write the config to this file instead of the path by convention (--output is the old spelling)",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite an existing config, keeping the previous one as <name>.bak.",
)
def init_config(account, from_catalog, output, force) -> None:
    """Generate config template from catalog. Saves to ~/.config/tg-export/<account>.yaml."""
    asyncio.run(_init_config(account, from_catalog, output, force))


async def _init_config(account, from_catalog, output, force=False) -> None:
    """Write a config template, either from a saved catalog or from the account.

    An existing config is not overwritten without --force: the chat list
    changes over time, so repeating init to refresh it is expected, and the
    config is the one artefact of the setup that cannot be recovered -- the
    session and the catalog can, the rules, limits and dates written by hand
    cannot.
    """
    from tg_export.catalog import generate_config_template

    mgr = common.account_manager()
    account = mgr.resolve_account(account)
    config_path = output or mgr.config_path(account)
    if config_path.exists() and not force:
        fail(
            f"Config {config_path} already exists. "
            f"Pass --force to overwrite it (the previous one is kept as {config_path.name}.bak), "
            f"or --output-file to write elsewhere."
        )

    if from_catalog:
        chats = _chats_from_catalog_file(from_catalog)
    else:
        from tg_export.catalog import fetch_catalog

        async with common.connected_api(account) as (api, account):
            chats = await fetch_catalog(api)

    if config_path.exists():
        # Keep what is being replaced: --force is asked for to refresh the chat
        # list, and the rules around it are what the user cannot rewrite from
        # memory.
        backup = config_path.with_suffix(config_path.suffix + ".bak")
        shutil.copy2(config_path, backup)
        common.diag(f"Previous config kept as {backup}")
    # The template carries a line per chat of the account -- ids, names and
    # message counts -- so it is written private from the start, like the
    # credentials file and the state database.
    write_private_text(config_path, generate_config_template(chats, account=account))
    common.diag(f"Config template saved to {config_path}")


def _chats_from_catalog_file(path: Path) -> list[Chat]:
    """Read the chat list out of a catalog written by ``list --format yaml``."""
    from tg_export.catalog import chats_from_catalog
    from tg_export.config import ConfigError

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        raise ConfigError(f"Cannot read catalog {path}: {e}") from e
    return chats_from_catalog(data)


def _get_dir_size(path: Path) -> int | None:
    """Get directory size using du -sb (Linux) or du -sk fallback (BSD/macOS).

    Why an external command rather than walking the tree in Python: an export
    holds hundreds of thousands of files, and os.walk with a stat() per file
    takes tens of seconds on a cold cache, whereas du does the same traversal
    in C. The size is one line of the summary, so it is not worth that wait --
    and when du is missing the line is simply dropped (None), which the caller
    handles.

    Why hard links matter here: files reused from a neighbouring export are
    linked, not copied, and du counts each inode once -- the number reported is
    the space actually taken, not the sum of file sizes.
    """
    # Why "--": prevents paths starting with "-" from being parsed as flags.
    # Why fallback: BSD du has no -b; -sk returns KiB.
    reasons = []
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
            reasons.append(f"{cmd[1]}: exit {result.returncode}")
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError) as e:
            reasons.append(f"{cmd[1]}: {type(e).__name__}")
    # The caller drops the line about the size, and until now it did so without
    # a word anywhere about why -- the two attempts failed silently.
    logger.debug("size of %s unknown, du failed (%s)", path, "; ".join(reasons))
    return None


@click.command("run")
@common.over_an_export
@click.option("--verify", is_flag=True, help="Verify file integrity after export")
@click.option("--dry-run", is_flag=True, help="Show what would be exported")
@click.option(
    "--require-takeout",
    is_flag=True,
    help="Fail instead of falling back to the regular API when Takeout cannot be started.",
)
@click.option(
    "--no-takeout",
    is_flag=True,
    help="Do not ask for Takeout at all; export through the regular API.",
)
@click.option(
    "--rerender",
    is_flag=True,
    help="Rebuild the HTML pages of every chat, including those with nothing new.",
)
def run_export(account, config, output, verify, dry_run, require_takeout, no_takeout, rerender) -> None:
    """Run export according to config. Config resolved by account name convention."""
    if require_takeout and no_takeout:
        raise click.UsageError(
            "--require-takeout and --no-takeout ask for opposite things; pass one of them."
        )
    exit_code = asyncio.run(
        _run_export(
            account,
            config,
            output,
            verify,
            dry_run,
            quiet=common._QUIET,
            require_takeout=require_takeout,
            no_takeout=no_takeout,
            rerender=rerender,
        )
    )
    if exit_code:
        fail(code=exit_code)


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
        common.diag("Takeout session started.")
        return True

    if require:
        raise TakeoutUnavailableError(f"{reason} Takeout was required (--require-takeout).")
    common.diag(
        f"{reason} Using regular API: the export continues, but it is slower "
        f"and subject to the usual rate limits.",
        essential=True,
    )
    return False


def _build_downloader(api, state, cfg, output_base: Path, min_free_bytes: int) -> MediaDownloader:
    """Assemble the media downloader together with its reuse sources.

    Files already present in a Telegram Desktop export or in a sibling account's
    export are linked instead of downloaded again, so both indexes are built
    here rather than inside the download path.
    """
    from tg_export.importer import build_tdesktop_indexes
    from tg_export.media import MediaDownloader

    tdesktop_indexes = build_tdesktop_indexes(cfg.import_existing)
    for idx in tdesktop_indexes:
        common.diag(f"tdesktop import: {idx.export_path}")

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
        common.diag(f"Sibling exports for file dedup: {', '.join(names)}")

    return MediaDownloader(
        api=api,
        state=state,
        config=cfg.defaults.media,
        min_free_bytes=min_free_bytes,
        tdesktop_indexes=tdesktop_indexes,
        sibling_db_paths=sibling_dbs,
    )


async def _print_export_summary(
    stats, state, output_base: Path, *, takeout_active: bool, interrupted: bool = False
) -> None:
    """Print the final report of an export.

    Goes to stderr and is marked essential, so --quiet keeps it: the export
    artifacts themselves are the files written to disk.

    An interrupted run prints the same numbers -- what it did manage to save --
    but must not call itself complete: the counters of a run stopped on the
    214th chat of 243 look exactly like those of a finished one, and the
    heading was the only place the difference could be read.
    """
    heading = "Export interrupted — state saved:" if interrupted else "Export complete:"
    common.diag(f"\n{heading}", essential=True)
    # Which API served the export decides how complete and how fast it was, so
    # it belongs in the summary rather than only in a line printed at start-up
    # and long scrolled away.
    common.diag(f"  API: {'takeout' if takeout_active else 'regular (no takeout)'}", essential=True)
    common.diag(
        f"  Chats: {stats.chats_exported}/{stats.chats_included} (skipped {stats.chats_skipped})",
        essential=True,
    )
    common.diag(f"  Messages: {stats.messages_exported}", essential=True)
    common.diag(f"  Files downloaded: {stats.files_downloaded}", essential=True)
    for label, value in (
        ("Files existing", stats.files_existing),
        ("Reused from chat", stats.files_reused_chat),
        ("Reused from tdesktop", stats.files_reused_tdesktop),
        ("Reused from sibling", stats.files_reused_sibling),
        ("Skipped by size", stats.files_skipped_by_size),
        ("Skipped by type", stats.files_skipped_by_type),
    ):
        if value:
            common.diag(f"  {label}: {value}", essential=True)
    if stats.data_size:
        common.diag(f"  Downloaded: {format_size(stats.data_size)}", essential=True)

    file_counts = await state.count_files()
    common.diag(
        f"  Files: {file_counts['files_downloaded']}/{file_counts['expected_files']} "
        f"(media messages: {file_counts['media_messages']})",
        essential=True,
    )
    db_size = state.db_path.stat().st_size if state.db_path.exists() else 0
    common.diag(f"  DB size: {format_size(db_size)}", essential=True)
    # Why to_thread: du can take seconds on a large export; don't block the loop.
    total_disk = await asyncio.to_thread(_get_dir_size, output_base)
    if total_disk is not None:
        common.diag(f"  Export size on disk: {format_size(total_disk)}", essential=True)
    _print_export_errors(stats.errors)


# How many failures the summary spells out before switching to a count. A run
# that failed on hundreds of files would otherwise scroll the summary itself
# out of the terminal; the full set is in the log, one warning per failure.
_SUMMARY_ERROR_LIMIT = 10


async def _close_downloader(downloader) -> None:
    """Close the sibling readers off the loop.

    Closing takes the lock a lookup holds for its whole query, and a lookup
    waits up to 30 seconds for a busy writer in the sibling export. A download
    orphaned by a forced shutdown can still be inside such a lookup, and then
    this wait on the loop stops the remaining coroutines and the signal
    handling with them.
    """
    await asyncio.to_thread(downloader.close)


def _print_export_errors(errors) -> None:
    """Print the failures behind the counter.

    The list was collected and thrown away: `Errors: 137` told the user that
    the export is incomplete and nothing about what was missing.
    """
    if not errors:
        return
    common.diag(f"  Errors: {len(errors)}", essential=True)
    for error in errors[:_SUMMARY_ERROR_LIMIT]:
        common.diag(f"    {error}", essential=True)
    hidden = len(errors) - _SUMMARY_ERROR_LIMIT
    if hidden > 0:
        common.diag(f"    ... and {hidden} more (see the log)", essential=True)


def _export_destination(account, config_override, output_override) -> tuple[str, Any, Path, Path]:
    """Where this export writes: the account, its config, the tree and the state DB.

    The directory is created private -- the tree holds every exported message
    and file -- while an existing one keeps the mode the user gave it.
    """
    account, cfg, output_base = common.resolve_output(
        account,
        config_override,
        output_override,
    )
    common.diag(f"Account: {account}")
    common.diag(f"Output: {output_base.resolve()}")
    ensure_private_dir(output_base)
    return account, cfg, output_base, output_base / STATE_DB_NAME


async def _run_export(
    account,
    config_override,
    output_override,
    verify,
    dry_run,
    quiet=False,
    require_takeout=False,
    no_takeout=False,
    rerender=False,
) -> int:
    from tg_export.catalog import fetch_catalog
    from tg_export.exporter import Exporter, forget_cancellation
    from tg_export.html.renderer import HtmlRenderer
    from tg_export.state import ExportState

    account, cfg, output_base, state_path = _export_destination(account, config_override, output_override)
    # Read before anything reaches the network: a bad `min_free_space` used to
    # be parsed inside `_build_downloader`, that is, after the connect and
    # after the Takeout request -- and a refused Takeout request costs a
    # cooldown of up to twenty-four hours.
    min_free_bytes = common.account_manager().load_min_free_space()

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
        api, _ = await resources.enter_async_context(common.connected_api(account))

        takeout_active = await _takeout_for_run(
            api, cfg, require_takeout=require_takeout, no_takeout=no_takeout
        )

        renderer = HtmlRenderer(output_dir=output_base, config=cfg.output)
        renderer.setup()

        downloader = _build_downloader(api, state, cfg, output_base, min_free_bytes)
        # The downloader keeps a read-only connection per sibling database for
        # the whole export; the stack closes them together with the rest.
        resources.push_async_callback(_close_downloader, downloader)

        chats = await fetch_catalog(api, include_left=(cfg.left_channels_action != "skip"))

        exporter = Exporter(
            api=api,
            state=state,
            config=cfg,
            renderer=renderer,
            downloader=downloader,
            account=account,
            quiet=quiet,
        )
        stats = await exporter.run(dry_run=dry_run, verify=verify, chat_list=chats, rerender=rerender)

        await _report_export_result(
            exporter,
            stats,
            chats,
            cfg,
            state,
            renderer,
            output_base,
            dry_run=dry_run,
            takeout_active=takeout_active,
        )

    except asyncio.CancelledError:
        common.diag("\nForce shutdown — saving state...", essential=True)
        # Cancellation ends here: the command answers it with an exit code, so
        # the task must not keep a pending request that would turn the ordinary
        # return below into a cancellation for anything awaiting this one.
        forget_cancellation()
    finally:
        # Releasing must not raise over the outcome of the export itself, and a
        # second Ctrl+C lands here as CancelledError. Silence, though, used to
        # hide a database that did not close: the user read "Export complete"
        # and exit code 0 over a failed WAL flush.
        try:
            await resources.aclose()
        except (Exception, asyncio.CancelledError) as e:
            logger.warning("resources of the export were not released cleanly: %s", e)
            # Into the outcome, not only into the log: an unreleased resource
            # means the database did not flush, and the run must not report
            # success over records it failed to write.
            if stats is not None:
                stats.errors.append(f"resources of the export were not released cleanly: {e}")

    # An export that logged errors did not do what it was asked to do, so it must
    # not report success; a signal outranks that and maps to 128 + signum.
    return export_exit_code(
        signum=exporter.shutdown_signal if exporter else None,
        error_count=len(stats.errors) if stats else 0,
    )


async def _takeout_for_run(api, cfg, *, require_takeout: bool, no_takeout: bool) -> bool:
    """Whether the export runs through a Takeout session.

    Asking for Takeout costs a request that may answer with a cooldown of up to
    24 hours, and the export then falls back anyway. --no-takeout skips the
    question when the caller already knows they do not want to wait for it.
    """
    if no_takeout:
        common.diag("Takeout not requested (--no-takeout); using the regular API.")
        return False
    return await _start_takeout(api, cfg, require=require_takeout)


async def _report_export_result(
    exporter, stats, chats, cfg, state, renderer, output_base, *, dry_run, takeout_active
) -> None:
    """Render the index and print the summary of an export that has finished.

    A forced shutdown skips both: the index would describe a tree the run did
    not finish writing.
    """
    if exporter.force_shutdown:
        common.diag("\nForce shutdown — state saved.", essential=True)
        return
    if not dry_run:
        await _render_index(renderer, chats, cfg, state, should_stop=lambda: exporter.shutdown_requested)
    await _print_export_summary(
        stats,
        state,
        output_base,
        takeout_active=takeout_active,
        interrupted=exporter.shutdown_requested,
    )


async def _group_chats_for_index(chats, cfg, state, should_stop) -> tuple[Any, Any] | None:
    """Split the exported chats into folders and unfiled, with message counts.

    Returns None when ``should_stop`` fires: the caller then skips the render.
    """
    from tg_export.exporter import sanitize_name

    folders = defaultdict(list)
    unfiled = []

    try:
        counts = await state.message_counts()
    except aiosqlite.Error as e:
        logger.debug("_render_index: message_counts failed: %s", e)
        counts = {}

    for chat in chats:
        if should_stop and should_stop():
            return None
        chat_cfg = cfg.resolve_chat_config(chat.id, chat.name, chat.folder, chat.type.value)
        if chat_cfg is None:
            continue
        # Counts for every chat were read once before the loop: asking per chat
        # meant one or two round-trips through aiosqlite for each of them.
        # A chat present with zero keeps its zero: chat.messages_count is the
        # top_message id, an approximation Telegram hands over, so `or` used to
        # replace an honest "nothing exported" with a number in the thousands.
        msg_count = counts.get(chat.id, chat.messages_count)

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
        # The page carries the saved ringtones only, so profile_music decides it.
        (cfg.profile_music, "Other Data", "Other Data", "other_data.html"),
    )
    return [
        {"title": title, "entries": [{"name": name, "href": href, "meta": ""}]}
        for enabled, title, name, href in pages
        if enabled
    ]


async def _render_index(renderer, chats, cfg, state, should_stop=None) -> None:
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


@click.command("purge")
@click.argument("chat", required=True)
@common.over_an_export
@click.option("--yes", is_flag=True, help="Skip confirmation")
def purge_chat(chat, account, config, output, yes) -> None:
    """Purge chat data: messages, files, state, and rendered HTML.

    CHAT can be a chat ID (number) or a name (substring search).
    """
    asyncio.run(_purge_chat(chat, account, config, output, yes))


async def _purge_chat(chat_arg, account, config_override, output_override, skip_confirm) -> None:
    """Delete one chat from the state database and from disk.

    The chat is named either by id or by name, the rows and the directories
    are counted before the question, and both are removed only after it.
    """
    async with common.opened_state(account, config_override, output_override) as (
        state,
        output_base,
        account,
    ):
        # Resolve chat: by ID or by name search. Only the parse belongs in the
        # try: aiosqlite raises ValueError("Connection closed") on a closed
        # connection, and catching it here used to send a numeric argument into
        # the name search, which reports "No chats found" instead of the failure.
        try:
            chat_id = int(chat_arg)
        except ValueError:
            matches = await state.find_chat_by_name(chat_arg)
            if not matches:
                fail(f"No chats found matching '{chat_arg}'")
            if len(matches) > 1:
                common.error(f"Multiple chats match '{chat_arg}':")
                for m in matches:
                    common.error(f"  {m['chat_id']}  {m['name']}  ({m['type']})")
                fail("Specify exact chat ID.")
            chat_id = matches[0]["chat_id"]
            chat_name = matches[0]["name"]
        else:
            entry = await state.get_catalog_entry(chat_id)
            chat_name = entry["name"] if entry else f"id={chat_id}"

        counts = await state.count_chat_rows(chat_id)

        # Find chat directory on disk: scan known prefixes only, never rglob
        # the whole output tree (would also follow into sibling/back-up trees).
        from tg_export.exporter import sanitize_name

        dir_suffix = f"{sanitize_name(chat_name)}_{chat_id}"
        output_resolved = output_base.resolve()
        candidate_dirs: list[Path] = []
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
                common.diag(f"  SKIP (symlink): {d}")
                continue
            try:
                d_resolved = d.resolve()
            except OSError:
                continue
            if not d_resolved.is_relative_to(output_resolved):
                common.diag(f"  SKIP (outside output): {d}")
                continue
            chat_dirs.append(d)

        # essential: the confirmation below asks to authorise an irreversible
        # deletion, so the description of what it covers must not be suppressed.
        # A prompt with the subject hidden is a prompt the user cannot answer.
        common.diag(f"Chat: {chat_name} (id={chat_id})", essential=True)
        common.diag(common.db_rows_line(counts), essential=True)
        if chat_dirs:
            for d in chat_dirs:
                # du rather than a stat() per file: a chat directory holds tens
                # of thousands of them, and the wait falls right before an
                # interactive question. When du is missing the size is dropped.
                size = _get_dir_size(d)
                measured = f" ({format_size(size)})" if size is not None else ""
                common.diag(f"  Dir: {d}{measured}", essential=True)
        else:
            common.diag("  Dir: not found", essential=True)

        if not skip_confirm and not confirm("Delete all data for this chat?", without_an_answer="--yes"):
            common.diag("Cancelled.", essential=True)
            return

        deleted = await state.purge_chat(chat_id)
        common.diag(common.db_rows_line(deleted, "Deleted from DB"))

        for d in chat_dirs:
            shutil.rmtree(d)
            common.diag(f"  Removed: {d}")

        common.diag("Done.")


@click.command("verify")
@common.over_an_export
def verify_files(account, config, output) -> None:
    """Verify integrity of previously downloaded files."""
    exit_code = asyncio.run(_verify_files(account, config, output))
    if exit_code:
        fail(code=exit_code)


async def _verify_files(account, config_override, output_override) -> int:
    async with common.opened_state_if_any(account, config_override, output_override) as (
        state,
        output_base,
        account,
    ):
        if state is None:
            common.diag("No state database found. Nothing to verify.")
            return EXIT_OK

        broken = await state.get_files_to_verify()
        if not broken:
            common.diag("All files OK.")
            return EXIT_OK

        common.diag(f"Found {len(broken)} files with issues:")
        for f in broken:
            common.diag(
                f"  {f['local_path']} - status: {f['status']}, "
                f"expected: {f['expected_size']}, actual: {f['actual_size']}"
            )

        # The config is read again here for one number: how many downloads may
        # run at once. `opened_state` drops it, and the alternative -- one file
        # at a time -- leaves the network idle for a whole round-trip per file.
        _, cfg, _ = common.resolve_output(account, config_override, output_override)

        async with common.connected_api(account) as (api, account):
            await asyncio.to_thread(clean_staging, output_base)
            redownloaded = 0
            outcomes = await redownload_broken_files(
                api, state, broken, concurrency=cfg.defaults.media.concurrent_downloads
            )
            for outcome in outcomes:
                entry = outcome.entry
                if outcome.error is not None:
                    common.diag(f"  [error] {entry['local_path']}: {outcome.error}", essential=True)
                elif outcome.result is RedownloadResult.no_media:
                    common.error(f"  [skip] msg {entry['msg_id']}: not found or no media")
                elif outcome.result is RedownloadResult.nothing_downloaded:
                    common.error(f"  [fail] {entry['local_path']}")
                elif outcome.result is RedownloadResult.replaced:
                    redownloaded += 1
                    common.diag(f"  [ok] {outcome.path}")
                else:
                    # An outcome added to the enum and left unhandled here: the
                    # file stays broken, so it must not pass as re-downloaded.
                    common.error(f"  [fail] {entry['local_path']}: unhandled outcome {outcome.result!r}")

            common.diag(f"\nRe-downloaded: {redownloaded}/{len(broken)}", essential=True)
            if redownloaded < len(broken):
                return EXIT_FAILURE
            return EXIT_OK
