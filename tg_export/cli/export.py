"""Top-level commands built around an export: config, list, init, run, purge, verify.

``run`` is the centre of the module: it resolves the configuration, opens the
state database, starts Takeout when it is available and hands the work to the
exporter. The rest either prepare its input (``config``, ``list``, ``init``) or
work on its output (``purge``, ``verify``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

import aiosqlite
import click
import yaml

from tg_export.cli import common
from tg_export.cli.common import (
    STATE_DB_NAME,
    _export_exit_code,
    _fail,
)
from tg_export.errors import (
    EXIT_FAILURE,
    EXIT_OK,
    TakeoutUnavailableError,
)
from tg_export.privacy import ensure_private_dir, write_private_text
from tg_export.verify import RedownloadResult, clean_staging, redownload_broken_file

logger = logging.getLogger(__name__)


@click.command("config")
@click.option("--verbose", "-v", is_flag=True, help="Verbose: show per-account filters")
def show_config(verbose):
    """Show current configuration (global + per-account)."""
    mgr = common._mgr()

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

        from tg_export.auth import DEFAULT_MIN_FREE_SPACE

        mfs = data.get("min_free_space", DEFAULT_MIN_FREE_SPACE)
        # Check free space on the output partition, not cwd
        disk_check_path = Path.cwd()
        default_name = mgr.get_default_account()
        if default_name:
            try:
                cfg_path = mgr.config_path(default_name)
                if cfg_path.exists():
                    acc_cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
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
        creds = yaml.safe_load(cred_path.read_text(encoding="utf-8"))
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


def _show_account_config(config_path):
    """Show per-account config details (verbose mode)."""
    from tg_export.config import load_config

    cfg = load_config(config_path)

    click.echo(f"    output.path: {cfg.output.path}")
    click.echo(f"    output.format: {cfg.output.format}")

    defaults = cfg.defaults
    click.echo(f"    defaults.media.types: {defaults.media.types}")
    click.echo(f"    defaults.media.max_file_size: {defaults.media.max_file_size_bytes // 1024**2}MB")
    if defaults.date_from or defaults.date_to:
        click.echo(f"    defaults.date_range: {_date_range(defaults.date_from, defaults.date_to)}")

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
def list_chats(account, output, as_json, fmt, include_left):
    """Export chat/folder catalog."""
    asyncio.run(_list_chats(account, output, "json" if as_json else fmt, include_left))


async def _list_chats(account, output, fmt, include_left):
    from tg_export.catalog import fetch_catalog, format_catalog_json, format_catalog_yaml

    async with common._connected_api(account) as (api, account):
        chats = await fetch_catalog(api, include_left=include_left)
        result = format_catalog_json(chats) if fmt == "json" else format_catalog_yaml(chats)

        if output:
            output.write_text(result, encoding="utf-8")
            common._diag(f"Catalog saved to {output}")
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
@click.option("--output", type=click.Path(path_type=Path), default=None, help="Override output config path")
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite an existing config, keeping the previous one as <name>.bak.",
)
def init_config(account, from_catalog, output, force):
    """Generate config template from catalog. Saves to ~/.config/tg-export/<account>.yaml."""
    asyncio.run(_init_config(account, from_catalog, output, force))


async def _init_config(account, from_catalog, output, force=False):
    """Write a config template, either from a saved catalog or from the account.

    An existing config is not overwritten without --force: the chat list
    changes over time, so repeating init to refresh it is expected, and the
    config is the one artefact of the setup that cannot be recovered -- the
    session and the catalog can, the rules, limits and dates written by hand
    cannot.
    """
    from tg_export.catalog import generate_config_template

    mgr = common._mgr()
    account = mgr.resolve_account(account)
    config_path = output or mgr.config_path(account)
    if config_path.exists() and not force:
        _fail(
            f"Config {config_path} already exists. "
            f"Pass --force to overwrite it (the previous one is kept as {config_path.name}.bak), "
            f"or --output to write elsewhere."
        )

    if from_catalog:
        chats = _chats_from_catalog_file(from_catalog)
    else:
        from tg_export.catalog import fetch_catalog

        async with common._connected_api(account) as (api, account):
            chats = await fetch_catalog(api)

    if config_path.exists():
        # Keep what is being replaced: --force is asked for to refresh the chat
        # list, and the rules around it are what the user cannot rewrite from
        # memory.
        backup = config_path.with_suffix(config_path.suffix + ".bak")
        shutil.copy2(config_path, backup)
        common._diag(f"Previous config kept as {backup}")
    # The template carries a line per chat of the account -- ids, names and
    # message counts -- so it is written private from the start, like the
    # credentials file and the state database.
    write_private_text(config_path, generate_config_template(chats, account=account))
    common._diag(f"Config template saved to {config_path}")


def _chats_from_catalog_file(path: Path):
    """Read the chat list out of a catalog written by ``tg chats --format yaml``."""
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


@click.command("run")
@click.option("--account", default=None, help=common.ACCOUNT_HELP)
@click.option(
    "--config", type=click.Path(exists=True, path_type=Path), default=None, help="Override config path"
)
@click.option("--output", type=click.Path(path_type=Path), help="Override output directory")
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
def run_export(account, config, output, verify, dry_run, require_takeout, no_takeout, rerender):
    """Run export according to config. Config resolved by account name convention."""
    if require_takeout and no_takeout:
        _fail("--require-takeout and --no-takeout ask for opposite things; pass one of them.")
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
        _fail(code=exit_code)


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
        common._diag("Takeout session started.")
        return True

    if require:
        raise TakeoutUnavailableError(f"{reason} Takeout was required (--require-takeout).")
    common._diag(
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
        common._diag(f"tdesktop import: {idx.export_path}")

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
        common._diag(f"Sibling exports for file dedup: {', '.join(names)}")

    min_free = common._mgr().load_min_free_space()
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

    common._diag("\nExport complete:", essential=True)
    # Which API served the export decides how complete and how fast it was, so
    # it belongs in the summary rather than only in a line printed at start-up
    # and long scrolled away.
    common._diag(f"  API: {'takeout' if takeout_active else 'regular (no takeout)'}", essential=True)
    common._diag(
        f"  Chats: {stats.chats_exported}/{stats.chats_included} (skipped {stats.chats_skipped})",
        essential=True,
    )
    common._diag(f"  Messages: {stats.messages_exported}", essential=True)
    common._diag(f"  Files downloaded: {stats.files_downloaded}", essential=True)
    for label, value in (
        ("Files existing", stats.files_existing),
        ("Reused from chat", stats.files_reused_chat),
        ("Reused from tdesktop", stats.files_reused_tdesktop),
        ("Reused from sibling", stats.files_reused_sibling),
        ("Skipped by size", stats.files_skipped_by_size),
        ("Skipped by type", stats.files_skipped_by_type),
    ):
        if value:
            common._diag(f"  {label}: {value}", essential=True)
    if stats.data_size:
        common._diag(f"  Downloaded: {format_size(stats.data_size)}", essential=True)

    file_counts = await state.count_files()
    common._diag(
        f"  Files: {file_counts['files_downloaded']}/{file_counts['expected_files']} "
        f"(media messages: {file_counts['media_messages']})",
        essential=True,
    )
    db_size = state.db_path.stat().st_size if state.db_path.exists() else 0
    common._diag(f"  DB size: {format_size(db_size)}", essential=True)
    # Why to_thread: du can take seconds on a large export; don't block the loop.
    total_disk = await asyncio.to_thread(_get_dir_size, output_base)
    if total_disk is not None:
        common._diag(f"  Export size on disk: {format_size(total_disk)}", essential=True)
    _print_export_errors(stats.errors)


# How many failures the summary spells out before switching to a count. A run
# that failed on hundreds of files would otherwise scroll the summary itself
# out of the terminal; the full set is in the log, one warning per failure.
_SUMMARY_ERROR_LIMIT = 10


def _print_export_errors(errors) -> None:
    """Print the failures behind the counter.

    The list was collected and thrown away: `Errors: 137` told the user that
    the export is incomplete and nothing about what was missing.
    """
    if not errors:
        return
    common._diag(f"  Errors: {len(errors)}", essential=True)
    for error in errors[:_SUMMARY_ERROR_LIMIT]:
        common._diag(f"    {error}", essential=True)
    hidden = len(errors) - _SUMMARY_ERROR_LIMIT
    if hidden > 0:
        common._diag(f"    ... and {hidden} more (see the log)", essential=True)


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
):
    from tg_export.catalog import fetch_catalog
    from tg_export.exporter import Exporter, _forget_cancellation
    from tg_export.html.renderer import HtmlRenderer
    from tg_export.state import ExportState

    account, cfg, output_base = common._resolve_output(
        account,
        config_override,
        output_override,
        missing_config_hint="Create it with: tg-export init --account {account}",
    )
    common._diag(f"Account: {account}")
    common._diag(f"Output: {output_base.resolve()}")

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
        api, _ = await resources.enter_async_context(common._connected_api(account))

        takeout_active = await _takeout_for_run(
            api, cfg, require_takeout=require_takeout, no_takeout=no_takeout
        )

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
        common._diag("\nForce shutdown — saving state...", essential=True)
        # Cancellation ends here: the command answers it with an exit code, so
        # the task must not keep a pending request that would turn the ordinary
        # return below into a cancellation for anything awaiting this one.
        _forget_cancellation()
    finally:
        # Releasing must not raise over the outcome of the export itself, and a
        # second Ctrl+C lands here as CancelledError.
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await resources.aclose()

    # An export that logged errors did not do what it was asked to do, so it must
    # not report success; a signal outranks that and maps to 128 + signum.
    return _export_exit_code(
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
        common._diag("Takeout not requested (--no-takeout); using the regular API.")
        return False
    return await _start_takeout(api, cfg, require=require_takeout)


async def _report_export_result(
    exporter, stats, chats, cfg, state, renderer, output_base, *, dry_run, takeout_active
):
    """Render the index and print the summary of an export that has finished.

    A forced shutdown skips both: the index would describe a tree the run did
    not finish writing.
    """
    if exporter.force_shutdown:
        common._diag("\nForce shutdown — state saved.", essential=True)
        return
    if not dry_run:
        await _render_index(renderer, chats, cfg, state, should_stop=lambda: exporter.shutdown_requested)
    await _print_export_summary(stats, state, output_base, takeout_active=takeout_active)


async def _group_chats_for_index(chats, cfg, state, should_stop):
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


@click.command("purge")
@click.argument("chat", required=True)
@click.option("--account", default=None, help=common.ACCOUNT_HELP)
@click.option(
    "--config", type=click.Path(exists=True, path_type=Path), default=None, help="Override config path"
)
@click.option("--output", type=click.Path(path_type=Path), help="Export output directory")
@click.option("--yes", is_flag=True, help="Skip confirmation")
def purge_chat(chat, account, config, output, yes):
    """Purge chat data: messages, files, state, and rendered HTML.

    CHAT can be a chat ID (number) or a name (substring search).
    """
    asyncio.run(_purge_chat(chat, account, config, output, yes))


async def _purge_chat(chat_arg, account, config_override, output_override, skip_confirm):

    async with common._opened_state(account, config_override, output_override) as (
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
                _fail(f"No chats found matching '{chat_arg}'")
            if len(matches) > 1:
                common._error(f"Multiple chats match '{chat_arg}':")
                for m in matches:
                    common._error(f"  {m['chat_id']}  {m['name']}  ({m['type']})")
                _fail("Specify exact chat ID.")
            chat_id = matches[0]["chat_id"]
            chat_name = matches[0]["name"]
        else:
            entry = await state.get_catalog_entry(chat_id)
            chat_name = entry["name"] if entry else f"id={chat_id}"

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
                common._diag(f"  SKIP (symlink): {d}")
                continue
            try:
                d_resolved = d.resolve()
            except OSError:
                continue
            if not d_resolved.is_relative_to(output_resolved):
                common._diag(f"  SKIP (outside output): {d}")
                continue
            chat_dirs.append(d)

        # essential: the confirmation below asks to authorise an irreversible
        # deletion, so the description of what it covers must not be suppressed.
        # A prompt with the subject hidden is a prompt the user cannot answer.
        common._diag(f"Chat: {chat_name} (id={chat_id})", essential=True)
        common._diag(
            f"  DB: messages={counts['messages']}, files={counts['files']}, "
            f"export_state={counts['export_state']}, catalog_cache={counts['catalog_cache']}",
            essential=True,
        )
        if chat_dirs:
            for d in chat_dirs:
                size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                from tg_export.format import format_size

                common._diag(f"  Dir: {d} ({format_size(size)})", essential=True)
        else:
            common._diag("  Dir: not found", essential=True)

        if not skip_confirm and not click.confirm("Delete all data for this chat?"):
            common._diag("Cancelled.", essential=True)
            return

        # Purge from DB
        deleted = await state.purge_chat(chat_id)
        common._diag(f"  Deleted from DB: {deleted}")

        # Remove directory
        for d in chat_dirs:
            shutil.rmtree(d)
            common._diag(f"  Removed: {d}")

        common._diag("Done.")


@click.command("verify")
@click.option("--account", default=None, help=common.ACCOUNT_HELP)
@click.option(
    "--config", type=click.Path(exists=True, path_type=Path), default=None, help="Override config path"
)
@click.option("--output", type=click.Path(path_type=Path), help="Export output directory")
def verify_files(account, config, output):
    """Verify integrity of previously downloaded files."""
    exit_code = asyncio.run(_verify_files(account, config, output))
    if exit_code:
        _fail(code=exit_code)


async def _verify_files(account, config_override, output_override):
    async with common._opened_state(account, config_override, output_override, required=False) as (
        state,
        output_base,
        account,
    ):
        if state is None:
            common._diag("No state database found. Nothing to verify.")
            return EXIT_OK

        broken = await state.get_files_to_verify()
        if not broken:
            common._diag("All files OK.")
            return EXIT_OK

        common._diag(f"Found {len(broken)} files with issues:")
        for f in broken:
            common._diag(
                f"  {f['local_path']} - status: {f['status']}, "
                f"expected: {f['expected_size']}, actual: {f['actual_size']}"
            )

        async with common._connected_api(account) as (api, account):
            clean_staging(output_base)
            redownloaded = 0
            for f in broken:
                try:
                    result, final_path = await redownload_broken_file(api, state, f)
                except Exception as e:
                    common._diag(f"  [error] {f['local_path']}: {e}", essential=True)
                    continue
                if result is RedownloadResult.no_media:
                    common._error(f"  [skip] msg {f['msg_id']}: not found or no media")
                elif result is RedownloadResult.nothing_downloaded:
                    common._error(f"  [fail] {f['local_path']}")
                else:
                    redownloaded += 1
                    common._diag(f"  [ok] {final_path}")

            common._diag(f"\nRe-downloaded: {redownloaded}/{len(broken)}", essential=True)
            if redownloaded < len(broken):
                return EXIT_FAILURE
            return EXIT_OK


# ---------------------------------------------------------------------------
# tg send / tg download — additional direct Telegram API commands
# ---------------------------------------------------------------------------
