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
    fail,
)
from tg_export.errors import (
    EXIT_FAILURE,
    EXIT_OK,
)
from tg_export.format import format_moment
from tg_export.privacy import ensure_private_dir, write_private_text

logger = logging.getLogger(__name__)


@click.group()
def tg():
    """Direct Telegram API commands."""


@tg.command("messages")
@click.argument("chat_id", type=int)
@click.option("--account", default=None, help=common.ACCOUNT_HELP)
@click.option("--limit", "-n", default=10, help="Number of messages to show")
@click.option(
    "--truncate",
    type=int,
    default=None,
    help=f"Cut message text to N characters (0 = no cut, default: {DEFAULT_MESSAGE_TEXT_LENGTH})",
)
@click.option("--no-truncate", is_flag=True, default=False, help="Print message text in full")
@click.option("--json", "as_json", is_flag=True, help="Output as machine-readable JSON")
def tg_messages(chat_id, account, limit, truncate, no_truncate, as_json):
    """Show recent messages from a chat."""
    if no_truncate:
        if truncate:
            raise click.BadParameter("--no-truncate cannot be combined with --truncate")
        truncate = 0
    elif truncate is None:
        # Cutting is for the terminal: a program reading the JSON wants the
        # text as it was sent, and asks for a cut explicitly when it wants one.
        truncate = 0 if as_json else DEFAULT_MESSAGE_TEXT_LENGTH
    elif truncate < 0:
        raise click.BadParameter("--truncate must be 0 or greater")
    asyncio.run(_tg_messages(chat_id, account, limit, truncate, as_json))


def _sender_name(sender) -> str:
    """A name on one line: `first last`, empty when there is no name."""
    first = getattr(sender, "first_name", "") or ""
    last = getattr(sender, "last_name", "") or ""
    return f"{first} {last}".strip()


def _message_preview(msg, *, truncate: int = DEFAULT_MESSAGE_TEXT_LENGTH) -> tuple[str, str]:
    """The (sender, text) pair of one line of a message listing.

    Media shows as its type in square brackets, a service message as its action
    in round ones. This was copied into `tg messages` and `tg info`, and the
    copies drifted: one cut the text by the setting, the other by a literal.
    """
    sender = _sender_name(msg.sender) if msg.sender else ""
    text = msg.message or ""
    if msg.media:
        media_type = msg.media.__class__.__name__.replace("MessageMedia", "")
        text = f"[{media_type}] {text}" if text else f"[{media_type}]"
    if msg.action:
        text = f"({msg.action.__class__.__name__.replace('MessageAction', '')})"
    return sender, text[:truncate] if truncate else text


async def _tg_messages(chat_id, account, limit, truncate=DEFAULT_MESSAGE_TEXT_LENGTH, as_json=False):
    """Print the last messages of a chat: date, sender and the cut text."""
    async with common.connected_api(account) as (api, account):
        entity = await api.client.get_entity(chat_id)
        title = getattr(entity, "title", None) or _entity_name(entity)
        if as_json:
            common.diag(f"# {title} (id={chat_id})")
        else:
            click.echo(f"# {title} (id={chat_id})\n")

        collected = []
        async for msg in api.client.iter_messages(entity, limit=limit):  # pyright: ignore[reportArgumentType]
            date_str = format_moment(msg.date, missing="?")
            sender, text = _message_preview(msg, truncate=truncate)
            if as_json:
                # The line for the terminal carries the media type and the
                # service action inside the text; a program reading this wants
                # the text as it was sent and the rest in fields of its own.
                body = msg.message or ""
                collected.append(
                    {
                        "id": msg.id,
                        "date": date_str,
                        "sender": sender,
                        "text": body[:truncate] if truncate else body,
                        "media": _class_suffix(msg.media, "MessageMedia"),
                        "action": _class_suffix(msg.action, "MessageAction"),
                    }
                )
            else:
                click.echo(f"  {date_str}  [{msg.id}]  {sender}: {text}")
        if as_json:
            click.echo(json.dumps(collected, ensure_ascii=False, indent=2))


def _class_suffix(value, prefix: str) -> str | None:
    """`MessageMediaPhoto` -> `Photo`; None when there is no such value."""
    return value.__class__.__name__.replace(prefix, "") if value else None


def _entity_name(entity) -> str:
    return _sender_name(entity) or "Unknown"


@tg.command("info")
@click.argument("chat_ids", type=int, nargs=-1)
@click.option("--account", default=None, help=common.ACCOUNT_HELP)
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
    if not chat_ids and not catalog_file:
        # A call there is nothing to build from is a refusal to parse it, not
        # a successful run: the branch used to print a hint and return None,
        # which the caller read as exit code 0 -- under --quiet the command
        # said nothing and still reported success.
        raise click.UsageError("No chat IDs specified. Use arguments or --from-catalog --type.")
    exit_code = asyncio.run(
        _tg_info(chat_ids, account, catalog_file, chat_type, last_n, output_file, as_json)
    )
    if exit_code:
        fail(code=exit_code)


def _write_info_results(results: list[dict], output_file, as_json: bool) -> None:
    """Hand the results over the way the flags asked for."""
    if as_json:
        click.echo(json.dumps(results, ensure_ascii=False, indent=2))
    if output_file:
        # Private from the start: the records carry chat names and, with
        # --last-n, the sender and text of the last messages -- the very data
        # the export directory is created 0700 for.
        write_private_text(output_file, json.dumps(results, ensure_ascii=False, indent=2))
        common.diag(f"Saved {len(results)} entries to {output_file}")


async def _resolve_entities(api, ids: list) -> dict:
    """Entities of the given chats, resolved in one request when it works.

    An id missing from the session cache costs `get_entity` a request of its
    own, so a catalog of hundreds of chats used to mean hundreds of round-trips
    in a row. Telethon takes a list and resolves them together; the request
    fails as a whole if any one id cannot be resolved, and then the caller
    falls back to asking per chat, which is what reports the failing one
    without losing the rest.
    """
    if len(ids) < 2:
        return {}
    try:
        answered = await api.client.get_entity(list(ids))
    except Exception as e:  # the per-chat path reports what failed
        logger.debug("batch entity resolution failed (%s); falling back to one by one", e)
        return {}
    if not isinstance(answered, list):
        answered = [answered]
    return {cid: entity for cid, entity in zip(ids, answered, strict=False) if entity is not None}


# How many chats are asked for their history at once. GetHistoryRequest takes
# one peer, so the only way to stop waiting out a full round-trip per chat is to
# overlap the requests -- kept low, since the server counts them per account.
INFO_CONCURRENCY = 4

# How far around a message its album neighbours are looked for. Telegram sends
# an album as up to ten separate messages posted in one go, so their ids sit
# close together -- but they are not required to be consecutive, and a message
# posted in between shifts them apart. A wider window costs one request either
# way, a narrower one loses parts of the album.
_ALBUM_ID_WINDOW = 10

# How much of a file is hashed to tell two downloads apart. The head alone is
# enough to separate different files of the same size, and reading a whole
# video for a duplicate check is not.
_FINGERPRINT_HEAD_BYTES = 64 * 1024


async def _one_chat_info(api, cid, entity, *, last_n: int) -> dict:
    """Counters and the last messages of one chat, or the error it answered with."""
    from telethon.tl.functions.messages import GetHistoryRequest

    try:
        if entity is None:
            entity = await api.client.get_entity(cid)
        title = getattr(entity, "title", None) or _entity_name(entity)
        result: Any = await api.client(
            GetHistoryRequest(
                peer=entity,  # pyright: ignore[reportArgumentType]
                offset_id=0,
                offset_date=None,
                add_offset=0,
                limit=max(last_n, 1),
                max_id=0,
                min_id=0,
                hash=0,
            )
        )
    except Exception as e:  # one unreachable chat must not end the rest
        return {"id": cid, "error": str(e), "messages": 0}

    last_date = None
    messages = []
    for msg in result.messages:
        date_str = format_moment(msg.date, missing="?")
        if last_date is None:
            last_date = date_str
        if last_n > 0:
            sender, text = _message_preview(msg)
            messages.append({"date": date_str, "sender": sender, "text": text})

    entry = {"id": cid, "name": title, "messages": _result_count(result), "last_date": last_date}
    if messages:
        entry["last_messages"] = messages
    return entry


def _result_count(result: Any) -> int:
    """Total messages of a chat, as the history answer reports it."""
    return getattr(result, "count", len(result.messages))


def _report_info_lines(results: list[dict], *, output_file, as_json: bool) -> None:
    """Progress and the human-readable lines of `tg info`.

    The counter is progress, not data: mixed into stdout it reached
    `tg info ... | grep` together with the results.
    """
    total = len(results)
    for idx, entry in enumerate(results, 1):
        cid = entry["id"]
        if "error" in entry:
            if not output_file:
                common.diag(f"[{idx}/{total}] id={cid}: ERROR {entry['error']}", essential=True)
            continue
        if total > 1:
            common.diag(f"  [{idx}/{total}] {entry['name']} (id={cid})")
        if not output_file and not as_json:
            click.echo(f"{entry['name']} (id={cid}): {entry['messages']} msgs, last: {entry['last_date']}")


async def _tg_info(chat_ids, account, catalog_file, chat_type, last_n, output_file, as_json=False):
    """Report the last activity of each chat named directly or taken from a catalog.

    The identifiers are resolved in one request and the histories are read with
    bounded parallelism; the order of the output is the order of the input.
    """
    ids = list(chat_ids)
    if catalog_file:
        with open(catalog_file, encoding="utf-8") as f:
            catalog = json.load(f)
        for entry in catalog:
            if chat_type and entry.get("type") != chat_type:
                continue
            ids.append(entry["id"])

    if not ids:
        # A filter that matched no chat is a legitimate empty result, and the
        # contract of --json is "stdout holds a JSON document" -- an empty
        # stdout is not one, and jq fails to parse it.
        _write_info_results([], output_file, as_json)
        return EXIT_OK

    async with common.connected_api(account) as (api, account):
        entities = await _resolve_entities(api, ids)
        semaphore = asyncio.Semaphore(INFO_CONCURRENCY)

        async def one(cid):
            async with semaphore:
                return await _one_chat_info(api, cid, entities.get(cid), last_n=last_n)

        # gather keeps the order of the input, so the report follows the order
        # the chats were asked for even though the requests overlap.
        results = list(await asyncio.gather(*(one(cid) for cid in ids)))
        _report_info_lines(results, output_file=output_file, as_json=as_json)
        _write_info_results(results, output_file, as_json)
        # A chat that could not be queried is a failure even when the rest
        # succeeded: the JSON carries an "error" key the caller may miss.
        return EXIT_FAILURE if any("error" in r for r in results) else EXIT_OK


@tg.command("send")
@click.option("--account", default=None, help=common.ACCOUNT_HELP)
@click.option(
    "--file",
    "-f",
    "files",
    multiple=True,
    type=click.Path(exists=True, path_type=Path),
    help="File(s) to attach (can be specified multiple times)",
)
@click.option(
    "--text",
    "-t",
    default=None,
    help=(
        "Message text (passing it here puts it on the command line, where other "
        "local users can read it; use --text-file for anything private)"
    ),
)
@click.option(
    "--text-file",
    "text_file",
    default=None,
    type=click.Path(path_type=Path, allow_dash=True),
    help="Read the message text from this file, or from standard input when given as -",
)
@click.option(
    "--as-document",
    is_flag=True,
    default=False,
    help="Send files as documents without compression (keeps original quality)",
)
@click.argument("recipients", nargs=-1, required=True)
def tg_send(account, files, text, text_file, as_document, recipients):
    """Send message to one or more recipients.

    RECIPIENTS: chat IDs or usernames (multiple allowed).
    Use --text for message text and --file for attachments.
    At least --text or --file must be specified.

    Note: delivery is best-effort and not idempotent. If sending fails for some
    recipients (network/RPC error), re-running this command will resend to all
    recipients, including those who already received the message.
    """
    if text and text_file:
        raise click.UsageError("give the text either with --text or with --text-file, not both")
    if text_file:
        text = (
            click.get_text_stream("stdin").read()
            if str(text_file) == "-"
            else Path(text_file).read_text(encoding="utf-8")
        )
    if not text and not files:
        raise click.UsageError("specify --text, --text-file and/or --file")

    parsed = []
    for r in recipients:
        try:
            parsed.append(int(r))
        except ValueError:
            parsed.append(r)

    exit_code = asyncio.run(_tg_send(account, parsed, text, files, as_document))
    if exit_code:
        fail(code=exit_code)


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
    async with common.connected_api(account_name) as (api, _):
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

                common.diag(f"  sent to {recipient}")
                sent_count += 1
            except Exception as e:
                common.error(f"  error sending to {recipient}: {e}")
                failed.append((recipient, str(e)))

        if failed:
            common.diag(
                f"\nDelivered to {sent_count}/{total} recipients. "
                f"{len(failed)} failed. Re-running this command will resend "
                f"to ALL {total} recipients (no idempotency).",
                essential=True,
            )
            return EXIT_FAILURE
        return EXIT_OK


@tg.command("download")
@click.option("--account", default=None, help=common.ACCOUNT_HELP)
@click.option("--output", "-o", type=click.Path(path_type=Path), default=".", help="Output directory")
@click.argument("chat_id", type=int)
@click.argument("msg_id", type=int)
def tg_download(account, output, chat_id, msg_id):
    """Download message content: text and all media files.

    Saves message text to <msg_id>.txt and media files to the output directory.
    """
    exit_code = asyncio.run(_tg_download(account, chat_id, msg_id, output))
    if exit_code:
        fail(code=exit_code)


def _file_head_sha256(path: Path, n_bytes: int = _FINGERPRINT_HEAD_BYTES) -> str:
    """SHA-256 of the first n_bytes of a file (cheap content fingerprint)."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        chunk = f.read(n_bytes)
        h.update(chunk)
    return h.hexdigest()


async def _download_if_new(client, msg, out: Path, downloaded: set[Path]) -> str | None:
    """Download media, skip only if exactly the same content was already saved.

    Sameness is decided by size together with a head-SHA256 fingerprint: size
    alone marks legitimately distinct files as duplicates -- two photos of an
    album or two PDFs of the same length -- and the newcomer is deleted.
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


def _save_message_text(out: Path, msg_id: int, text: str) -> Path:
    """Save the text of a message, readable by its owner alone."""
    path = out / f"{msg_id}.txt"
    write_private_text(path, text)
    return path


async def _tg_download(account_name: str | None, chat_id: int, msg_id: int, out: Path) -> int:
    ensure_private_dir(out)
    out.mkdir(parents=True, exist_ok=True)
    async with common.connected_api(account_name) as (api, _):
        from tg_export.api import one_message

        tl_msg = one_message(await api.client.get_messages(chat_id, ids=msg_id))
        if tl_msg is None:
            common.diag(f"Message {msg_id} not found in chat {chat_id}", essential=True)
            return EXIT_FAILURE

        msg_text = getattr(tl_msg, "text", None)
        if msg_text:
            text_file = _save_message_text(out, msg_id, msg_text)
            common.diag(f"  text: {text_file}")

        # Only what this call downloaded: the set decides whether a new file is a
        # duplicate to be deleted, and the output directory defaults to the current
        # one -- an unrelated file of the same size and head bytes made the media
        # disappear without a line about it, and so did the message text written
        # a few lines above.
        downloaded: set[Path] = set()
        if tl_msg.media:
            path = await _download_if_new(api.client, tl_msg, out, downloaded)
            if path:
                common.diag(f"  media: {path}")

        if tl_msg.grouped_id:
            count = 0
            async for grouped_msg in api.client.iter_messages(
                chat_id,
                min_id=msg_id - _ALBUM_ID_WINDOW,
                max_id=msg_id + _ALBUM_ID_WINDOW,
            ):
                if (
                    grouped_msg.grouped_id == tl_msg.grouped_id
                    and grouped_msg.id != msg_id
                    and grouped_msg.media
                ):
                    path = await _download_if_new(api.client, grouped_msg, out, downloaded)
                    if path:
                        common.diag(f"  album media: {path}")
                        count += 1
            if count:
                common.diag(f"  ({count} additional album files)")

        if not msg_text and not tl_msg.media:
            common.diag("  (empty message, no text or media)")
        return EXIT_OK
