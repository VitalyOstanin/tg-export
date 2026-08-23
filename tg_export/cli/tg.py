"""``tg`` group: direct API calls -- info, messages, send, download.

These commands talk to Telegram outside an export: they read a chat, send a
file, fetch one message. Nothing here writes to the state database.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import click

from tg_export.cli import common
from tg_export.cli.common import (
    DEFAULT_MESSAGE_TEXT_LENGTH,
    _fail,
)
from tg_export.errors import (
    EXIT_FAILURE,
    EXIT_OK,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# tg: direct Telegram API commands
# ---------------------------------------------------------------------------


@click.group()
def tg():
    """Direct Telegram API commands."""


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

    async with common._connected_api(account) as (api, account):
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
    type=click.Path(exists=True, path_type=Path),
    help="JSON catalog file (from tg-export list --json)",
)
@click.option("--type", "chat_type", default=None, help="Filter by chat type (with --from-catalog)")
@click.option(
    "--limit",
    "-n",
    "--last",
    "last_n",
    type=int,
    default=0,
    help="Show the last N messages of each chat (0 = only the counters); --last is the old spelling",
)
@click.option(
    "--output-file",
    "--output",
    "output_file",
    type=click.Path(path_type=Path),
    default=None,
    help="Save results to this JSON file (--output is the old spelling)",
)
@click.option("--json", "as_json", is_flag=True, help="Output as machine-readable JSON")
def tg_info(chat_ids, account, catalog_file, chat_type, last_n, output_file, as_json):
    """Show chat info: message count, type, title.

    Accepts one or more CHAT_IDS, or use --from-catalog with --type to batch query.
    """
    exit_code = asyncio.run(
        _tg_info(chat_ids, account, catalog_file, chat_type, last_n, output_file, as_json)
    )
    if exit_code:
        _fail(code=exit_code)


async def _tg_info(chat_ids, account, catalog_file, chat_type, last_n, output_file, as_json=False):

    from telethon.tl.functions.messages import GetHistoryRequest

    # Collect IDs
    ids = list(chat_ids)
    if catalog_file:
        with open(catalog_file, encoding="utf-8") as f:
            catalog = json.load(f)
        for entry in catalog:
            if chat_type and entry.get("type") != chat_type:
                continue
            ids.append(entry["id"])

    if not ids:
        common._diag("No chat IDs specified. Use arguments or --from-catalog --type.")
        return

    results = []
    async with common._connected_api(account) as (api, account):
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

                # The counter is progress, not data: mixed into stdout it
                # reached `tg info ... | grep` together with the results.
                if total > 1:
                    common._diag(f"  [{idx}/{total}] {title} (id={cid})")
                if not output_file and not as_json:
                    click.echo(f"{title} (id={cid}): {count} msgs, last: {last_date}")

            except Exception as e:
                entry = {"id": cid, "error": str(e), "messages": 0}
                results.append(entry)
                if not output_file:
                    common._diag(f"[{idx}/{total}] id={cid}: ERROR {e}", essential=True)

        if as_json:
            click.echo(json.dumps(results, ensure_ascii=False, indent=2))
        if output_file:
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
            common._diag(f"Saved {len(results)} entries to {output_file}")
        # A chat that could not be queried is a failure even when the rest
        # succeeded: the JSON carries an "error" key the caller may miss.
        return EXIT_FAILURE if any("error" in r for r in results) else EXIT_OK


@tg.command("send")
@click.option("--account", default=None, help="Account alias")
@click.option(
    "--file",
    "-f",
    "files",
    multiple=True,
    type=click.Path(exists=True, path_type=Path),
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
        _fail("Error: specify --text and/or --file")

    parsed = []
    for r in recipients:
        try:
            parsed.append(int(r))
        except ValueError:
            parsed.append(r)

    exit_code = asyncio.run(_tg_send(account, parsed, text, files, as_document))
    if exit_code:
        _fail(code=exit_code)


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

    from tg_export.console import console

    if common._QUIET or not console.is_terminal:
        yield None
        return

    # Only the topmost live display of a console renders; rich keeps the rest
    # on a stack and draws nothing for them. The export status and this bar
    # share one console, so reusing _send_files inside an export would create a
    # Progress that costs its updates and shows nothing. Today the paths do not
    # meet -- the export never sends files -- and this keeps it that way on
    # purpose: the upload proceeds without a second bar rather than with an
    # invisible one.
    if getattr(console, "_live_stack", None):
        logger.debug("a live display is already running on this console; upload progress is skipped")
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
    async with common._connected_api(account_name) as (api, _):
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

                common._diag(f"  sent to {recipient}")
                sent_count += 1
            except Exception as e:
                common._error(f"  error sending to {recipient}: {e}")
                failed.append((recipient, str(e)))

        if failed:
            common._diag(
                f"\nDelivered to {sent_count}/{total} recipients. "
                f"{len(failed)} failed. Re-running this command will resend "
                f"to ALL {total} recipients (no idempotency).",
                essential=True,
            )
            return EXIT_FAILURE
        return EXIT_OK


@tg.command("download")
@click.option("--account", default=None, help="Account alias")
@click.option("--output", "-o", type=click.Path(path_type=Path), default=".", help="Output directory")
@click.argument("chat_id", type=int)
@click.argument("msg_id", type=int)
def tg_download(account, output, chat_id, msg_id):
    """Download message content: text and all media files.

    Saves message text to <msg_id>.txt and media files to the output directory.
    """
    exit_code = asyncio.run(_tg_download(account, chat_id, msg_id, output))
    if exit_code:
        _fail(code=exit_code)


def _file_head_sha256(path: Path, n_bytes: int = 64 * 1024) -> str:
    """SHA-256 of the first n_bytes of a file (cheap content fingerprint)."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        chunk = f.read(n_bytes)
        h.update(chunk)
    return h.hexdigest()


async def _download_if_new(client, msg, out: Path, downloaded: set[Path]) -> str | None:
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


async def _tg_download(account_name: str | None, chat_id: int, msg_id: int, out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    async with common._connected_api(account_name) as (api, _):
        tl_msg = await api.client.get_messages(chat_id, ids=msg_id)
        if isinstance(tl_msg, list):
            tl_msg = tl_msg[0] if tl_msg else None
        if tl_msg is None:
            common._diag(f"Message {msg_id} not found in chat {chat_id}", essential=True)
            return EXIT_FAILURE

        # Save text
        msg_text = getattr(tl_msg, "text", None)
        if msg_text:
            text_file = out / f"{msg_id}.txt"
            text_file.write_text(msg_text, encoding="utf-8")
            common._diag(f"  text: {text_file}")

        # Download media (skip if file already exists)
        downloaded = {f for f in out.iterdir() if f.is_file()}
        if tl_msg.media:
            path = await _download_if_new(api.client, tl_msg, out, downloaded)
            if path:
                common._diag(f"  media: {path}")

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
                        common._diag(f"  album media: {path}")
                        count += 1
            if count:
                common._diag(f"  ({count} additional album files)")

        if not msg_text and not tl_msg.media:
            common._diag("  (empty message, no text or media)")
        return EXIT_OK
