"""``tg`` group: direct API calls -- info, messages, send, scheduled, delete, download.

These commands talk to Telegram outside an export: they read a chat, send a
file, fetch one message. Nothing here writes to the state database.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import click

from tg_export.cli import common
from tg_export.cli.chatref import CHAT_REF, resolve_chat
from tg_export.cli.common import (
    DEFAULT_MESSAGE_TEXT_LENGTH,
    fail,
)
from tg_export.console import confirm
from tg_export.converter import reply_mark, reply_target, text_links
from tg_export.errors import (
    EXIT_FAILURE,
    EXIT_OK,
    ChatResolveError,
)
from tg_export.format import display_name, format_moment, strip_control_chars
from tg_export.privacy import ensure_private_dir, write_private_text
from tg_export.schedule import (
    FORMATS_HELP,
    SCHEDULE_TIME,
    check_schedule,
    describe,
    explain_refusal,
    local_now,
)

logger = logging.getLogger(__name__)


@click.group()
def tg() -> None:
    """Direct Telegram API commands."""


@tg.command("messages")
@click.argument("chat_id", type=CHAT_REF)
@click.option("--account", default=None, help=common.ACCOUNT_HELP)
# IntRange, not int: -5 messages is not a request the command can serve, and
# it used to reach Telethon as a limit of its own.
@click.option("--limit", "-n", type=click.IntRange(min=1), default=10, help="Number of messages to show")
@click.option(
    "--truncate",
    type=int,
    default=None,
    help=f"Cut message text to N characters (0 = no cut, default: {DEFAULT_MESSAGE_TEXT_LENGTH})",
)
@click.option("--no-truncate", is_flag=True, default=False, help="Print message text in full")
@click.option("--json", "as_json", is_flag=True, help="Output as machine-readable JSON")
def tg_messages(chat_id, account, limit, truncate, no_truncate, as_json) -> None:
    """Show recent messages from a chat.

    CHAT_ID: chat id, @username, bare username or a t.me link.
    """
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
    return display_name(sender)


def _message_preview(msg, *, truncate: int = DEFAULT_MESSAGE_TEXT_LENGTH) -> tuple[str, str]:
    """The (sender, text) pair of one line of a message listing.

    Built from the same record as a line of `tg watch`: this was once copied
    into `tg messages` and `tg info`, and the copies drifted -- one cut the text
    by the setting, the other by a literal.
    """
    from tg_export.watch import message_record, record_text

    record = message_record(None, None, msg)
    text = record_text(record)
    return record["sender"], text[:truncate] if truncate else text


# The fields of `tg messages --json`, in the order they are printed. The values
# come from the record of `tg watch`; only the cut of the text is done here.
_LISTING_FIELDS = ("id", "date", "sender", "text", "links", "media", "action", "reply_to", "reply_to_peer")


async def _tg_messages(chat_id, account, limit, truncate=DEFAULT_MESSAGE_TEXT_LENGTH, as_json=False) -> None:
    """Print the last messages of a chat: date, sender and the cut text."""
    async with common.connected_api(account) as (api, account):
        entity = await resolve_chat(api.client, chat_id)
        title = getattr(entity, "title", None) or _entity_name(entity)
        if as_json:
            common.diag(f"# {title} (id={chat_id})")
        else:
            click.echo(f"# {title} (id={chat_id})\n")

        from tg_export.watch import message_record

        collected = []
        async for msg in api.client.iter_messages(entity, limit=limit):  # pyright: ignore[reportArgumentType]
            if as_json:
                record = message_record(None, None, msg)
                if truncate:
                    # Links are counted over the cut text: a link reaching past
                    # the cut would point outside the `text` it travels with.
                    record["text"] = record["text"][:truncate]
                    record["links"] = text_links(record["text"], msg.entities)
                collected.append({field: record[field] for field in _LISTING_FIELDS})
            else:
                # Telegram controls both, and a terminal executes escape
                # sequences it is handed: the same rule already applied to
                # names on the way to the filesystem.
                sender, text = _message_preview(msg, truncate=truncate)
                mark = reply_mark(*reply_target(msg))
                date_str = format_moment(msg.date, missing="?")
                head = f"  {date_str}  [{msg.id}] {mark}" if mark else f"  {date_str}  [{msg.id}]"
                click.echo(f"{head}  {strip_control_chars(sender)}: {strip_control_chars(text)}")
        if as_json:
            click.echo(json.dumps(collected, ensure_ascii=False, indent=2))


def _entity_name(entity) -> str:
    return _sender_name(entity) or "Unknown"


@tg.command("info")
@click.argument("chat_ids", type=CHAT_REF, nargs=-1)
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
    type=click.IntRange(min=0),
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
def tg_info(chat_ids, account, catalog_file, chat_type, last_n, output_file, as_json) -> None:
    """Show chat info: message count, type, title.

    Accepts one or more CHAT_IDS -- ids, @usernames or t.me links -- or use
    --from-catalog with --type to batch query.
    """
    if chat_type and not catalog_file:
        # The filter picks chats out of a catalog; without one there is nothing
        # to pick from, and the call used to run over every id named on the
        # command line as if no filter had been asked for.
        raise click.UsageError("--type applies to the chats of --from-catalog; pass --from-catalog.")
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
        # Failures are counted apart: a record of a chat that could not be
        # queried carries an "error" key instead of its numbers, and "Saved 2
        # entries" over one result and one failure read as two results.
        failed = sum(1 for entry in results if "error" in entry)
        saved = f"Saved {len(results)} entries to {output_file}"
        common.diag(f"{saved} ({failed} of them failed)" if failed else saved)


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
            # Reported whatever the results are then done with: the line goes
            # to stderr, so it competes neither with the JSON on stdout nor
            # with the file. Skipping it when --output-file was given left the
            # command exiting 1 without a word about what failed, and under
            # --quiet with no output at all.
            common.diag(
                f"[{idx}/{total}] id={cid}: ERROR {strip_control_chars(str(entry['error']))}",
                essential=True,
            )
            continue
        if total > 1:
            common.diag(f"  [{idx}/{total}] {strip_control_chars(entry['name'])} (id={cid})")
        if not output_file and not as_json:
            click.echo(
                f"{strip_control_chars(entry['name'])} (id={cid}): "
                f"{entry['messages']} msgs, last: {entry['last_date']}"
            )


async def _tg_info(chat_ids, account, catalog_file, chat_type, last_n, output_file, as_json=False) -> int:
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

        async def one(cid) -> dict:
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
@click.option(
    "--mime-type",
    "mime_type",
    default=None,
    help=(
        "MIME type to declare for every attached file instead of the one guessed "
        "from its name; the receiving app opens a file by it when its own system "
        "knows no type for the extension"
    ),
)
@click.option(
    "--schedule",
    "schedule",
    default=None,
    type=SCHEDULE_TIME,
    help=(
        "Do not send now: put the message into the chat's scheduled messages, and "
        f"Telegram sends it at this time. Accepted: {FORMATS_HELP}"
    ),
)
@click.argument("recipients", type=CHAT_REF, nargs=-1, required=True)
def tg_send(account, files, text, text_file, as_document, mime_type, schedule, recipients) -> None:
    """Send message to one or more recipients.

    RECIPIENTS: chat ids, @usernames or t.me links (multiple allowed).
    Use --text for message text and --file for attachments.
    At least --text or --file must be specified. With --schedule the message
    waits on Telegram's side and leaves at the given time; see and cancel it
    with `tg scheduled list` and `tg scheduled delete`.

    Note: delivery is best-effort and not idempotent. If sending fails for some
    recipients (network/RPC error), re-running this command will resend to all
    recipients, including those who already received the message.
    """
    if text and text_file:
        raise click.UsageError("give the text either with --text or with --text-file, not both")
    if text_file:
        if str(text_file) == "-":
            with click.open_file("-", encoding="utf-8") as stdin:
                text = stdin.read()
        else:
            text = Path(text_file).read_text(encoding="utf-8")
    if not text and not files:
        raise click.UsageError("specify --text, --text-file and/or --file")

    if mime_type and not files:
        raise click.UsageError("--mime-type applies to files; give them with --file")

    exit_code = asyncio.run(
        _tg_send(account, list(recipients), text, files, as_document, mime_type=mime_type, schedule=schedule)
    )
    if exit_code:
        fail(code=exit_code)


@contextlib.contextmanager
def _upload_progress(by_bytes: bool) -> Iterator[Any]:
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


async def _send_files(
    client, recipient, file_paths, text, as_document, mime_type=None, schedule=None
) -> list[Any]:
    """Send attachments to one recipient, reporting upload progress.

    Documents never join an album, so with ``as_document`` files go one by one
    and progress is counted in bytes. Compressed photos keep the album grouping
    Telethon does in chunks of 10; there the callback counts files, not bytes.
    A declared ``mime_type`` sends the files one by one as well, each with it.
    ``schedule`` puts every message into the scheduled queue for that moment.

    Returns the messages Telegram answered with -- with ``schedule``, their ids
    are those of the scheduled queue.
    """
    from tg_export.exporter import file_progress_description

    caption = text or ""
    by_bytes = as_document or len(file_paths) == 1 or mime_type is not None

    with _upload_progress(by_bytes) as progress:
        if not by_bytes:
            task = (
                progress.add_task(f"{len(file_paths)} files", total=len(file_paths))
                if progress is not None
                else None
            )

            def album_progress(sent, total) -> None:
                # `total` from Telethon is the tail of the album still to send:
                # it slices the list into chunks of 10 and reports 25, then 15,
                # then 5, while `sent` keeps counting from the start. Adopting
                # that total pushed the bar past 100% for more than ten files.
                if progress is not None and task is not None:
                    progress.update(task, completed=min(sent, len(file_paths)))

            sent = await client.send_file(
                recipient,
                [str(p) for p in file_paths],
                caption=caption,
                force_document=as_document,
                progress_callback=album_progress,
                schedule=schedule,
            )
            return _as_list(sent)

        sizes = [p.stat().st_size for p in file_paths]
        total_task = None
        if progress is not None and len(file_paths) > 1:
            total_task = progress.add_task(f"total 0/{len(file_paths)} files", total=sum(sizes))
        done_bytes = 0
        messages: list[Any] = []

        for index, path in enumerate(file_paths):
            # rich parses the description as markup, so an unescaped name is
            # printed mangled: [draft]report.txt comes out as report.txt.
            task = (
                progress.add_task(file_progress_description(path.name), total=sizes[index])
                if progress is not None
                else None
            )

            def file_progress(sent, total, task=task, done=done_bytes) -> None:
                if progress is None or task is None:
                    return
                progress.update(task, completed=sent, total=total)
                if total_task is not None:
                    progress.update(total_task, completed=done + sent)

            sent = await _send_one_file(
                client,
                recipient,
                path,
                caption=caption if index == 0 else "",
                as_document=as_document,
                mime_type=mime_type,
                progress_callback=file_progress,
                schedule=schedule,
            )
            messages.extend(_as_list(sent))
            done_bytes += sizes[index]
            if progress is not None and task is not None:
                progress.remove_task(task)
                if total_task is not None:
                    progress.update(
                        total_task,
                        completed=done_bytes,
                        description=f"total {index + 1}/{len(file_paths)} files",
                    )
        return messages


def _ids_suffix(messages, label: str = "id") -> str:
    """` (id 5)` or ` (ids 5, 6)` for the messages a send returned; empty when none has an id."""
    ids = [str(m.id) for m in messages if isinstance(getattr(m, "id", None), int)]
    if not ids:
        return ""
    return f" ({label}{'s' if len(ids) > 1 else ''} {', '.join(ids)})"


def _as_list(sent) -> list[Any]:
    """What a send returned, as a list: one message, an album, or nothing."""
    if sent is None:
        return []
    return list(sent) if isinstance(sent, list | tuple) else [sent]


async def _send_one_file(
    client, recipient, path, *, caption, as_document, progress_callback, mime_type=None, schedule=None
) -> Any:
    """Send one file, uploading its parts over several connections at once.

    The upload is done here rather than inside `send_file` so that a large
    file leaves at the speed of the line instead of one part per round trip;
    small files still go the library's way. Because the message is then built
    from a handle rather than from a path, the attributes and the mime type
    are read from the file on disk and passed along -- taken from the handle
    they would come from its name alone, and a video would lose its duration
    and dimensions.
    """
    from telethon import utils

    from tg_export.upload import upload_file

    handle = await upload_file(client, path, progress_callback=progress_callback)
    # A declared type is kept as given; otherwise it is guessed from the name.
    attributes, mime_type = utils.get_attributes(str(path), force_document=as_document, mime_type=mime_type)
    return await client.send_file(
        recipient,
        handle,
        caption=caption,
        force_document=as_document,
        attributes=attributes,
        mime_type=mime_type,
        schedule=schedule,
    )


async def _tg_send(
    account_name, recipients, text, files, as_document=False, mime_type=None, schedule=None
) -> int:
    async with common.connected_api(account_name) as (api, _):
        file_paths = [Path(f) for f in files] if files else None
        sent_count = 0
        failed: list[tuple[object, str]] = []
        total = len(recipients)

        for recipient in recipients:
            try:
                if schedule is not None:
                    # Uploads to the recipients before this one take time, and a
                    # moment that came close meanwhile would be sent at once
                    # rather than scheduled.
                    check_schedule(schedule, local_now())
                if file_paths:
                    messages = await _send_files(
                        api.client, recipient, file_paths, text, as_document, mime_type, schedule
                    )
                else:
                    messages = _as_list(await api.client.send_message(recipient, text, schedule=schedule))

                if schedule is None:
                    common.diag(f"  sent to {recipient}{_ids_suffix(messages)}")
                else:
                    suffix = _ids_suffix(messages, "scheduled id")
                    common.diag(f"  scheduled to {recipient} for {describe(schedule)}{suffix}")
                sent_count += 1
            except Exception as e:
                reason = explain_refusal(e) or str(e)
                verb = "scheduling for" if schedule is not None else "sending to"
                common.error(f"  error {verb} {recipient}: {reason}")
                failed.append((recipient, reason))

        if failed:
            done = "Delivered to" if schedule is None else "Scheduled for"
            common.diag(
                f"\n{done} {sent_count}/{total} recipients. "
                f"{len(failed)} failed. Re-running this command will resend "
                f"to ALL {total} recipients (no idempotency).",
                essential=True,
            )
            return EXIT_FAILURE
        return EXIT_OK


@tg.group("scheduled")
def tg_scheduled() -> None:
    """Messages waiting in a chat's scheduled queue, put there by the --schedule option of tg send."""


@tg_scheduled.command("list")
@click.argument("chat_ids", type=CHAT_REF, nargs=-1, required=True)
@click.option("--account", default=None, help=common.ACCOUNT_HELP)
@click.option(
    "--truncate",
    type=click.IntRange(min=0),
    default=None,
    help=f"Cut message text to N characters (0 = no cut, default: {DEFAULT_MESSAGE_TEXT_LENGTH})",
)
@click.option("--json", "as_json", is_flag=True, help="Output as machine-readable JSON")
def tg_scheduled_list(chat_ids, account, truncate, as_json) -> None:
    """Show the scheduled messages of one or more chats, soonest first.

    CHAT_IDS: chat ids, @usernames or t.me links. The id in square brackets is
    the one `tg scheduled delete` takes.
    """
    if truncate is None:
        truncate = 0 if as_json else DEFAULT_MESSAGE_TEXT_LENGTH
    exit_code = asyncio.run(_tg_scheduled_list(list(chat_ids), account, truncate, as_json))
    if exit_code:
        fail(code=exit_code)


async def _scheduled_messages(client, entity) -> list[Any]:
    """The scheduled queue of one chat, soonest first."""
    messages = [m async for m in client.iter_messages(entity, scheduled=True)]
    return sorted(messages, key=lambda m: (m.date is None, m.date, m.id))


def _scheduled_record(msg, truncate: int) -> dict:
    """One scheduled message for `--json`: the listing fields plus the moment it leaves."""
    from tg_export.watch import message_record

    record = message_record(None, None, msg)
    if truncate:
        record["text"] = record["text"][:truncate]
        record["links"] = text_links(record["text"], msg.entities)
    out = {field: record[field] for field in _LISTING_FIELDS}
    out["scheduled_for"] = describe(msg.date) if msg.date is not None else None
    return out


async def _tg_scheduled_list(chat_ids, account, truncate, as_json) -> int:
    collected: list[dict] = []
    failed = False
    async with common.connected_api(account) as (api, _):
        for chat_id in chat_ids:
            try:
                entity = await resolve_chat(api.client, chat_id)
                messages = await _scheduled_messages(api.client, entity)
            except Exception as e:
                common.error(f"  error reading the scheduled messages of {chat_id}: {e}")
                failed = True
                continue
            if as_json:
                collected.append(
                    {"chat": chat_id, "messages": [_scheduled_record(m, truncate) for m in messages]}
                )
                continue
            title = getattr(entity, "title", None) or _entity_name(entity)
            click.echo(f"# {strip_control_chars(title)} (id={chat_id}): {len(messages)} scheduled")
            for msg in messages:
                _, text = _message_preview(msg, truncate=truncate)
                when = describe(msg.date) if msg.date is not None else "?"
                click.echo(f"  {when}  [{msg.id}]  {strip_control_chars(text)}")
    if as_json:
        click.echo(json.dumps(collected, ensure_ascii=False, indent=2))
    return EXIT_FAILURE if failed else EXIT_OK


@tg_scheduled.command("delete")
@click.argument("chat_id", type=CHAT_REF)
@click.argument("message_ids", type=click.IntRange(min=1), nargs=-1)
@click.option("--account", default=None, help=common.ACCOUNT_HELP)
@click.option(
    "--all",
    "delete_all",
    is_flag=True,
    default=False,
    help="Delete every scheduled message of the chat",
)
def tg_scheduled_delete(chat_id, message_ids, account, delete_all) -> None:
    """Delete scheduled messages of a chat without sending them.

    CHAT_ID: chat id, @username or t.me link. MESSAGE_IDS: the ids
    `tg scheduled list` shows in square brackets; or --all instead of them.
    """
    if delete_all and message_ids:
        raise click.UsageError("give either message ids or --all, not both")
    if not delete_all and not message_ids:
        raise click.UsageError("give the ids of the scheduled messages to delete, or --all")
    exit_code = asyncio.run(_tg_scheduled_delete(chat_id, list(message_ids), account, delete_all))
    if exit_code:
        fail(code=exit_code)


async def _tg_scheduled_delete(chat_id, message_ids, account, delete_all) -> int:
    """Delete the scheduled messages asked for; an id the queue does not hold is a failure.

    Telegram deletes what it finds and stays silent about the rest, so the
    queue is read first: a mistyped id would otherwise pass for a deletion.
    """
    from telethon.tl.functions.messages import DeleteScheduledMessagesRequest

    async with common.connected_api(account) as (api, _):
        entity = await resolve_chat(api.client, chat_id)
        queued = {m.id for m in await _scheduled_messages(api.client, entity)}
        wanted = sorted(queued) if delete_all else list(dict.fromkeys(message_ids))
        missing = [i for i in wanted if i not in queued]
        present = [i for i in wanted if i in queued]
        if present:
            await api.client(DeleteScheduledMessagesRequest(peer=entity, id=present))
            listed = ", ".join(map(str, present))
            common.diag(f"  deleted {len(present)} scheduled message(s) of {chat_id}: {listed}")
        elif delete_all:
            common.diag(f"  {chat_id} has no scheduled messages")
        for i in missing:
            common.error(f"  {chat_id} has no scheduled message {i}")
        return EXIT_FAILURE if missing else EXIT_OK


@tg.command("delete")
@click.argument("chat_id", type=CHAT_REF)
@click.argument("message_ids", type=click.IntRange(min=1), nargs=-1, required=True)
@click.option("--account", default=None, help=common.ACCOUNT_HELP)
@click.option(
    "--for-everyone",
    "revoke",
    is_flag=True,
    default=False,
    help=(
        "In a private chat, delete the messages for the other side as well; by default "
        "they disappear only for you. In groups and channels Telegram deletes them for "
        "everyone either way"
    ),
)
@click.option(
    "--whole-album",
    is_flag=True,
    default=False,
    help="When a named message is part of an album, delete every part of that album",
)
@click.option("--yes", "skip_confirm", is_flag=True, default=False, help="Delete without asking")
def tg_delete(chat_id, message_ids, account, revoke, whole_album, skip_confirm) -> None:
    """Delete messages of a chat by their ids. This cannot be undone.

    CHAT_ID: chat id, @username, bare username, a t.me link or `me` for Saved
    Messages. MESSAGE_IDS: the ids `tg messages` shows in square brackets and
    `tg send` prints after sending.

    The messages are read first and listed -- date, file name, start of the
    text -- and the deletion waits for a yes; --yes answers it beforehand. An
    id the chat does not hold stops the command before anything is deleted.
    """
    exit_code = asyncio.run(
        _tg_delete(
            chat_id,
            list(message_ids),
            account,
            revoke=revoke,
            whole_album=whole_album,
            skip_confirm=skip_confirm,
        )
    )
    if exit_code:
        fail(code=exit_code)


# Parts of one album are sent together and get consecutive ids, at most ten of them.
_ALBUM_REACH = 9


async def _album_parts(client, entity, messages) -> dict[int, list[int]]:
    """The ids of every album the messages belong to, keyed by the album's grouped id."""
    groups = {m.grouped_id for m in messages if getattr(m, "grouped_id", None)}
    if not groups:
        return {}
    ids = [m.id for m in messages]
    around = list(range(max(1, min(ids) - _ALBUM_REACH), max(ids) + _ALBUM_REACH + 1))
    parts: dict[int, list[int]] = {}
    for m in await client.get_messages(entity, ids=around):
        if m is not None and getattr(m, "grouped_id", None) in groups:
            parts.setdefault(m.grouped_id, []).append(m.id)
    return {group: sorted(found) for group, found in parts.items()}


def _deletion_scope(entity, revoke: bool) -> str:
    """For whom the messages disappear, said the way the preview prints it."""
    if getattr(entity, "is_self", False):
        return "Saved Messages: there is no other side"
    if getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False) or hasattr(entity, "title"):
        return "a group or channel: Telegram deletes them for everyone"
    if revoke:
        return "deleted for everyone in the chat (--for-everyone)"
    return "deleted only for you; the other side keeps them (--for-everyone deletes them there too)"


def _deletion_line(msg) -> str:
    """One message of the preview: id, date, file name, start of the text."""
    _, text = _message_preview(msg)
    file = getattr(msg, "file", None)
    name = getattr(file, "name", None) if file is not None else None
    parts = [f"  [{msg.id}]", format_moment(msg.date, missing="?")]
    if name:
        parts.append(f"file {strip_control_chars(name)}")
    if text:
        parts.append(strip_control_chars(text))
    if not getattr(msg, "out", True):
        parts.append("(not yours)")
    return "  ".join(parts)


async def _tg_delete(chat_id, message_ids, account, *, revoke, whole_album, skip_confirm) -> int:
    """Delete messages after listing them; nothing is deleted when an id is wrong.

    In private chats and small groups Telegram takes the ids alone, without
    the chat: a message id there is counted across all of them, and a mistyped
    id would delete a message of another conversation. Reading the messages
    through the chat first filters those out -- Telethon drops a message that
    belongs elsewhere -- so only what the listing showed is deleted.
    """
    from telethon.errors.rpcerrorlist import MessageDeleteForbiddenError

    async with common.connected_api(account) as (api, _):
        client = api.client
        entity = await resolve_chat(client, chat_id)
        wanted = list(dict.fromkeys(message_ids))
        found = await client.get_messages(entity, ids=wanted)
        missing = [i for i, m in zip(wanted, found, strict=True) if m is None]
        if missing:
            for i in missing:
                common.error(f"  {chat_id} has no message {i}")
            common.error("Nothing was deleted.")
            return EXIT_FAILURE
        messages = list(found)

        albums = await _album_parts(client, entity, messages)
        if whole_album:
            extra = sorted({i for ids in albums.values() for i in ids} - set(wanted))
            if extra:
                messages += [m for m in await client.get_messages(entity, ids=extra) if m is not None]
        chosen = {m.id for m in messages}
        messages.sort(key=lambda m: m.id)

        # essential: this is what the question below asks to authorise.
        title = getattr(entity, "title", None) or _entity_name(entity)
        common.diag(f"Chat: {strip_control_chars(title)} (id={chat_id})", essential=True)
        for msg in messages:
            common.diag(_deletion_line(msg), essential=True)
        for ids in albums.values():
            if not set(ids) <= chosen:
                named = ", ".join(str(i) for i in ids if i in chosen)
                common.diag(
                    f"  {named}: part of an album of {', '.join(map(str, ids))}; only the named "
                    f"part is deleted (--whole-album deletes all of it)",
                    essential=True,
                )
        common.diag(f"  {_deletion_scope(entity, revoke)}", essential=True)

        question = f"Delete {len(messages)} message(s)? This cannot be undone."
        if not skip_confirm and not confirm(question, without_an_answer="--yes"):
            common.diag("Cancelled.", essential=True)
            return EXIT_OK

        ids = [m.id for m in messages]
        try:
            await client.delete_messages(entity, ids, revoke=revoke)
        except MessageDeleteForbiddenError:
            common.error(
                f"  Telegram refused to delete messages of {chat_id}: deleting other members' "
                f"messages in a group or channel needs admin rights"
            )
            return EXIT_FAILURE
        except Exception as e:
            common.error(f"  error deleting messages of {chat_id}: {e}")
            return EXIT_FAILURE

        # Telegram skips what it may not delete without a word, so the result
        # is read back rather than assumed.
        left = [m.id for m in await client.get_messages(entity, ids=ids) if m is not None]
        gone = [i for i in ids if i not in left]
        if gone:
            common.diag(f"  deleted {len(gone)} message(s) of {chat_id}: {', '.join(map(str, gone))}")
        if left:
            common.error(f"  Telegram kept messages of {chat_id} still in place: {', '.join(map(str, left))}")
            return EXIT_FAILURE
        return EXIT_OK


@tg.command("download")
@click.option("--account", default=None, help=common.ACCOUNT_HELP)
@click.option("--output", "-o", type=click.Path(path_type=Path), default=".", help="Output directory")
@click.option("--json", "as_json", is_flag=True, help="Output as machine-readable JSON")
@click.argument("chat_id", type=CHAT_REF)
@click.argument("msg_id", type=int)
def tg_download(account, output, chat_id, msg_id, as_json) -> None:
    """Download message content: text and all media files.

    CHAT_ID: chat id, @username, bare username or a t.me link.

    Saves message text to <msg_id>.txt and media files to the output directory.
    """
    exit_code = asyncio.run(_tg_download(account, chat_id, msg_id, output, as_json=as_json))
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


async def _tg_download(
    account_name: str | None, chat_id: int | str, msg_id: int, out: Path, *, as_json: bool = False
) -> int:
    """Save the text and the media of one message; report what was saved.

    The names of the files are chosen by Telethon, so the paths are the result
    of the command: with `--json` they go to stdout as a document, and the
    lines for the terminal stay in the diagnostic stream.
    """
    ensure_private_dir(out)
    out.mkdir(parents=True, exist_ok=True)
    async with common.connected_api(account_name) as (api, _):
        from tg_export.api import one_message

        entity = await resolve_chat(api.client, chat_id)
        tl_msg = one_message(await api.client.get_messages(entity, ids=msg_id))
        if tl_msg is None:
            common.diag(f"Message {msg_id} not found in chat {chat_id}", essential=True)
            return EXIT_FAILURE

        saved: dict[str, object] = {"chat_id": chat_id, "msg_id": msg_id, "text_file": None, "media": []}
        msg_text = getattr(tl_msg, "text", None)
        if msg_text:
            text_file = _save_message_text(out, msg_id, msg_text)
            saved["text_file"] = str(text_file)
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
                saved["media"].append(str(path))  # pyright: ignore[reportAttributeAccessIssue]
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
                        saved["media"].append(str(path))  # pyright: ignore[reportAttributeAccessIssue]
                        common.diag(f"  album media: {path}")
                        count += 1
            if count:
                common.diag(f"  ({count} additional album files)")

        if not msg_text and not tl_msg.media:
            common.diag("  (empty message, no text or media)")
        if as_json:
            click.echo(json.dumps(saved, ensure_ascii=False, indent=2))
        return EXIT_OK


@tg.command("watch")
@click.argument("chats", type=CHAT_REF, nargs=-1)
@click.option("--account", default=None, help=common.ACCOUNT_HELP)
@click.option(
    "--exclude",
    "excluded",
    type=CHAT_REF,
    multiple=True,
    help="Chat to leave out; repeatable, and used instead of naming chats to watch",
)
@click.option(
    "--json/--text",
    "as_json",
    default=None,
    help="One JSON document per line, or lines for a terminal (default: JSON unless stdout is a terminal)",
)
@click.option(
    "--log",
    "log_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Append every record to this file as JSON, whatever the terminal gets",
)
@click.option(
    "--no-catchup",
    is_flag=True,
    default=False,
    help="Start from the current moment and do not replay what was missed, at start or after a reconnect",
)
@click.option(
    "--catchup-limit",
    type=click.IntRange(min=1),
    default=100,
    help=(
        "Replay at most this many missed messages per chat -- the latest ones; "
        "anything older is skipped, and the skip is reported on stderr"
    ),
)
@click.option(
    "--no-outgoing",
    is_flag=True,
    default=False,
    help="Leave out messages sent from this account, live and in the replay of what was missed",
)
@click.option("--no-edits", is_flag=True, default=False, help="Leave out edits and deletions")
@click.option(
    "--state",
    "state_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Where the marks of what was already shown are kept",
)
def tg_watch(
    chats,
    account,
    excluded,
    as_json,
    log_path,
    no_catchup,
    catchup_limit,
    no_outgoing,
    no_edits,
    state_path,
) -> None:
    """Follow chats live, printing every message as it arrives.

    CHATS: chats to watch -- ids, @usernames or t.me links. Without them every
    chat is watched, and --exclude leaves individual chats out.

    The listener holds the session of the account for as long as it runs, so an
    export of the same account cannot run beside it.
    """
    if chats and excluded:
        raise click.UsageError("name the chats to watch, or exclude chats with --exclude -- not both")
    if as_json is None:
        as_json = not sys.stdout.isatty()

    exit_code = asyncio.run(
        _tg_watch(
            account,
            list(chats),
            list(excluded),
            as_json=as_json,
            log_path=log_path,
            catchup=not no_catchup,
            catchup_limit=catchup_limit,
            outgoing=not no_outgoing,
            edits=not no_edits,
            state_path=state_path,
        )
    )
    if exit_code:
        fail(code=exit_code)


async def _watch_ids(api, refs: list) -> set[int]:
    """The ids of the chats named on the command line, resolved before listening.

    A reference that names nothing stops the command here, with every failing
    one reported: finding that out after an hour of listening is worse than
    finding it out at once.
    """
    from telethon import utils as tl_utils

    ids: set[int] = set()
    refused: list[str] = []
    for ref in refs:
        try:
            entity = await resolve_chat(api.client, ref)
        except ChatResolveError as e:
            refused.append(str(e))
            continue
        ids.add(tl_utils.get_peer_id(entity))
    if refused:
        raise ChatResolveError("; ".join(refused))
    return ids


async def _tg_watch(
    account_name: str | None,
    chats: list,
    excluded: list,
    *,
    as_json: bool,
    log_path: Path | None,
    catchup: bool,
    catchup_limit: int,
    outgoing: bool,
    edits: bool,
    state_path: Path | None,
) -> int:
    """Listen until the connection ends, printing what arrives and what was missed."""
    from tg_export.watch import LiveStream, RecordSink, WatchState

    async with common.connected_api(account_name) as (api, account):
        try:
            wanted = await _watch_ids(api, chats) or None
            excluded_ids = await _watch_ids(api, excluded)
        except ChatResolveError as e:
            # Reported here rather than left to the entry point: a refusal the
            # user can act on belongs with the command that asked for the chat.
            common.error(str(e))
            return EXIT_FAILURE

        path = state_path or common.account_manager().watch_state_path(account)
        stream = LiveStream(
            api,
            WatchState.load(path, account),
            RecordSink(sys.stdout, as_json=as_json, log_path=log_path),
            wanted=wanted,
            excluded=excluded_ids,
            catchup_limit=catchup_limit,
            outgoing=outgoing,
        )
        await stream.run(catchup=catchup, edits=edits, announce=common.diag)
        return EXIT_OK
