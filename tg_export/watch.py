"""Live listening: what has already been seen, and what was missed while away.

The listener is a long-running process, and everything it has to survive comes
down to one question -- where it stopped. Telegram does not answer it: an
update stream carries what happens while the socket is open and nothing about
the gap, and Telethon 1.44 does not close that gap either (`_handle_auto_re-
connect` in its updates client still carries a `TODO Catch-up` and only makes
one high-level request). So the marks are kept here, one message id per chat.

Back-fill leans on the dialog list rather than on the marks: the dialogs name
each chat together with its last message, and history is asked for only where
that id has moved past the mark. With chats named on the command line their
dialogs are asked for directly, in one request; with every chat watched, one
pass over `iter_dialogs` covers them. The cost of a start then follows the
number of active chats, not the number of chats overall.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, TextIO

from tg_export.converter import reply_mark, reply_target, text_links, with_link_targets
from tg_export.format import class_suffix, display_name, format_moment, strip_control_chars
from tg_export.privacy import ensure_private_dir, tighten_if_loose, write_private_text

logger = logging.getLogger(__name__)

#: Schema of the state file. A file written by a newer version is not guessed
#: at: the marks are rebuilt instead, which costs one quiet start.
WATCH_STATE_VERSION = 1

#: How often the marks may reach the disk. A busy group otherwise turns every
#: message into a write; the price of the interval is that a process killed
#: outright replays the last few seconds, which the reader drops by id.
SAVE_INTERVAL_SECONDS = 5.0


class WatchState:
    """The last message id seen in each chat, kept in one private file."""

    def __init__(self, path: Path, account: str, chats: dict[int, int] | None = None) -> None:
        self.path = Path(path)
        self.account = account
        self._chats: dict[int, int] = dict(chats or {})
        self._saved_at = 0.0

    @classmethod
    def load(cls, path: Path, account: str) -> WatchState:
        """The marks stored at `path`, or empty ones when there are none to read.

        An unreadable or damaged file is not a reason to refuse to start: the
        worst it costs is a start that listens from the current moment.
        """
        path = Path(path)
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls(path, account)
        except (OSError, ValueError) as e:
            logger.warning("%s does not read as watch state (%s); starting with no marks", path, e)
            return cls(path, account)

        if stored.get("version") != WATCH_STATE_VERSION:
            logger.warning(
                "%s carries watch state version %s, not %s; starting with no marks",
                path,
                stored.get("version"),
                WATCH_STATE_VERSION,
            )
            return cls(path, account)

        chats: dict[int, int] = {}
        for chat_id, message_id in (stored.get("chats") or {}).items():
            try:
                chats[int(chat_id)] = int(message_id)
            except (TypeError, ValueError):
                logger.warning("%s: chat %r carries mark %r; dropped", path, chat_id, message_id)
        return cls(path, account, chats=chats)

    def last_seen(self, chat_id: int) -> int | None:
        return self._chats.get(chat_id)

    def note(self, chat_id: int, message_id: int) -> None:
        """Remember that `message_id` was handed to the reader.

        The mark only moves forward: an edit of an old message arrives with its
        original id, and moving the mark back would replay everything after it.
        """
        if message_id > self._chats.get(chat_id, 0):
            self._chats[chat_id] = message_id

    async def save(self, *, force: bool = False) -> bool:
        """Write the marks unless the last write is younger than the interval.

        Returns whether the file was written, which is what the tests ask and
        what the caller logs.
        """
        now = time.monotonic()
        if not force and self._saved_at and now - self._saved_at < SAVE_INTERVAL_SECONDS:
            return False
        payload = json.dumps(
            {
                "version": WATCH_STATE_VERSION,
                "account": self.account,
                # UTC, like the dates Telegram hands over: the file sits next to a
                # stream whose moments are in UTC, and two zones side by side
                # read as one.
                "updated": format_moment(datetime.now(UTC)),
                "chats": {str(chat_id): message_id for chat_id, message_id in self._chats.items()},
            },
            ensure_ascii=False,
            indent=2,
        )
        await asyncio.to_thread(self._write, payload)
        self._saved_at = now
        return True

    def _write(self, payload: str) -> None:
        """Replace the file in one step, so a kill never leaves half a document."""
        ensure_private_dir(self.path.parent)
        staging = self.path.with_name(f"{self.path.name}.tmp")
        write_private_text(staging, payload)
        os.replace(staging, self.path)


def _top_message_id(dialog: Any) -> int | None:
    """The id of the last message of a dialog, or None when it has none."""
    message_id = getattr(getattr(dialog, "message", None), "id", None)
    if not message_id:
        message_id = getattr(getattr(dialog, "dialog", None), "top_message", None)
    return message_id or None


async def missed_messages(
    api: Any,
    state: WatchState,
    *,
    wanted: set[int] | None,
    excluded: set[int],
    limit: int,
) -> AsyncIterator[tuple[int, str | None, Any]]:
    """Messages that arrived while nothing was listening, oldest first.

    Each message comes with the name of its chat: the dialog already carries
    it, and a back-filled message used to reach the reader with an empty chat
    while a live one carried the name.

    A chat with no mark is not replayed: it was never listened to, and handing
    over its history would flood the reader with an archive it did not ask for.
    For the same reason named chats without a mark are not even asked for.

    At most `limit` messages per chat are handed over, and they are the latest
    ones: a reader that falls that far behind wants to know what is happening
    now. What the limit leaves out is named in the log, since the mark moves
    past it and no later catch-up returns to it.
    """
    if wanted is None:
        dialogs = api.iter_dialogs()
    else:
        asked = sorted(chat_id for chat_id in wanted if state.last_seen(chat_id) is not None)
        if not asked:
            return
        dialogs = api.peer_dialogs(asked)

    async for dialog in dialogs:
        chat_id = getattr(dialog, "id", None)
        if chat_id is None or chat_id in excluded:
            continue
        if wanted is not None and chat_id not in wanted:
            continue
        mark = state.last_seen(chat_id)
        if mark is None:
            continue
        top = _top_message_id(dialog)
        if top is None or top <= mark:
            continue
        title = getattr(dialog, "title", None) or getattr(dialog, "name", None)
        for message in await _latest_after(api, dialog.entity, chat_id, mark, limit):
            yield chat_id, title, message


async def _latest_after(api: Any, entity: Any, chat_id: int, mark: int, limit: int) -> list[Any]:
    """The last `limit` messages after `mark`, oldest first.

    Telethon's `iter_messages` with `reverse=True` starts right after `min_id`
    and climbs, so its `limit` keeps the oldest messages; without `reverse` it
    walks down from the newest and stops at `min_id`, which is what keeps the
    latest. One message more than the limit is asked for: its presence is how
    a cut is told from a gap that fits.
    """
    newest_first = [
        message async for message in api.client.iter_messages(entity, min_id=mark, limit=limit + 1)
    ]
    if len(newest_first) > limit:
        newest_first = newest_first[:limit]
        logger.warning(
            "chat %s: more than %d messages were missed; the ones after %d and before %d are skipped",
            chat_id,
            limit,
            mark,
            newest_first[-1].id,
        )
    newest_first.reverse()
    return newest_first


def message_record(chat_id: int | None, chat: str | None, message: Any, *, event: str = "message") -> dict:
    """One message as the reader of the stream sees it.

    `tg messages --json` takes its fields from here as well, so a program
    reading both parses them with one piece of code. Attachments are named, not
    fetched: the listener stays light, and `tg download` fetches a file when
    the reader decides it wants one.
    """
    file = getattr(message, "file", None)
    sender = getattr(message, "sender", None)
    text = getattr(message, "message", "") or ""
    reply_to, reply_to_peer = reply_target(message)
    return {
        "event": event,
        "chat_id": chat_id,
        "chat": chat,
        "id": getattr(message, "id", None),
        "date": format_moment(getattr(message, "date", None), missing=""),
        "sender_id": getattr(message, "sender_id", None),
        "sender": display_name(sender) if sender is not None else "",
        "text": text,
        "links": text_links(text, getattr(message, "entities", None)),
        "media": class_suffix(getattr(message, "media", None), "MessageMedia"),
        "file": (
            {"name": getattr(file, "name", None), "size": getattr(file, "size", None)}
            if file is not None
            else None
        ),
        "action": class_suffix(getattr(message, "action", None), "MessageAction"),
        "outgoing": bool(getattr(message, "out", False)),
        "reply_to": reply_to,
        "reply_to_peer": reply_to_peer,
    }


def deletion_record(chat_id: int | None, chat: str | None, ids: list[int], when: Any) -> dict:
    """A deletion, which Telegram reports without saying much.

    The chat is absent in private conversations and small groups: Telegram
    sends no peer there, since a message id alone identifies the message for a
    client that stored it. The record keeps the field and leaves it empty
    rather than dropping the event, which the reader can still match by id.
    """
    return {
        "event": "deleted",
        "chat_id": chat_id,
        "chat": chat,
        "ids": list(ids),
        "date": format_moment(when, missing=""),
    }


def text_line(record: dict) -> str:
    """One record as a line for a terminal, shaped like the `tg messages` listing."""
    chat = strip_control_chars(record.get("chat") or "")
    if record["event"] == "deleted":
        ids = ", ".join(str(i) for i in record.get("ids", []))
        return f"  {record['date']}  {chat}  deleted: {ids}"
    mark = " (edited)" if record["event"] == "edited" else ""
    reply = reply_mark(record.get("reply_to"), record.get("reply_to_peer"))
    if reply:
        mark = f"{mark} {reply}"
    sender = strip_control_chars(record.get("sender") or "")
    text = strip_control_chars(record_text(record))
    return f"  {record['date']}  {chat}  [{record['id']}]{mark}  {sender}: {text}"


def record_text(record: dict) -> str:
    """The text of a message record as a terminal line shows it.

    Link targets follow their words, media shows as its type in square
    brackets, and a service message as its action in round ones. `tg messages`
    and `tg info` print their lines through this too, so the listing of a chat
    and the live stream cannot drift apart.
    """
    text = with_link_targets(record.get("text") or "", record.get("links") or [])
    if record.get("media"):
        text = f"[{record['media']}] {text}" if text else f"[{record['media']}]"
    if record.get("action"):
        text = f"({record['action']})"
    return text


class RecordSink:
    """Where the stream goes: the standard output, and a log file when asked.

    The log is always JSON even when the terminal gets lines: a file is read by
    a program later, and a format that depends on which flag was passed is a
    format nobody can rely on.
    """

    def __init__(self, stream: Any, *, as_json: bool, log_path: Path | None) -> None:
        self._stream = stream
        self._as_json = as_json
        self._log_path = Path(log_path) if log_path is not None else None
        self._log: TextIO | None = None
        # One writer at a time, in arrival order: handlers run as separate
        # tasks, and two threads writing at once would interleave the records.
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> RecordSink:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def emit(self, record: dict) -> None:
        """Write one record, off the event loop.

        A pipe whose reader falls behind blocks the write; done on the loop,
        that would stop the updates and the pings that keep the connection.
        """
        document = json.dumps(record, ensure_ascii=False)
        line = document if self._as_json else text_line(record)
        async with self._lock:
            await asyncio.to_thread(self._write, line, document)

    async def aclose(self) -> None:
        """Close the log; the sink may be closed more than once."""
        async with self._lock:
            log, self._log = self._log, None
            if log is not None:
                await asyncio.to_thread(log.close)

    def _write(self, line: str, document: str) -> None:
        self._stream.write(line + "\n")
        self._stream.flush()
        if self._log_path is not None:
            if self._log is None:
                self._log = _open_log(self._log_path)
            self._log.write(document + "\n")


def _open_log(path: Path) -> TextIO:
    """Open the log for appending, closed to other users.

    A new file is born private; one left by an earlier run, or made by the
    user, is tightened before the first message goes into it. The file stays
    open for the whole run and is line-buffered, so every record reaches it at
    once.
    """
    ensure_private_dir(path.parent)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        tighten_if_loose(path)
        return os.fdopen(fd, "a", encoding="utf-8", buffering=1)
    except BaseException:
        os.close(fd)
        raise


def subscribe(
    client: Any,
    handler: Any,
    *,
    wanted: set[int] | None,
    excluded: set[int],
    edits: bool,
    outgoing: bool,
) -> None:
    """Put `handler` on the events this run was asked to follow.

    Telethon takes the chats either as a white list or as a black one, which is
    why the command refuses both at once. Deletions are filtered by chat only
    where Telegram names one -- channels and supergroups -- so in a private
    conversation an exclusion does not reach them.
    """
    from telethon import events

    if wanted:
        chats: list[int] | None = sorted(wanted)
        blacklist = False
    elif excluded:
        chats = sorted(excluded)
        blacklist = True
    else:
        chats = None
        blacklist = False

    # `incoming=True` is how Telethon spells "not mine"; leaving both unset
    # means no filter at all, which is the default this command wants. New
    # messages are never filtered by direction: an own one is not shown with
    # `outgoing` off, but it still moves the mark of its chat, which the
    # handler can only do if it sees the message.
    incoming = None if outgoing else True

    client.add_event_handler(
        partial(handler, kind="message"),
        events.NewMessage(chats=chats, blacklist_chats=blacklist),
    )
    if edits:
        client.add_event_handler(
            partial(handler, kind="edited"),
            events.MessageEdited(chats=chats, blacklist_chats=blacklist, incoming=incoming),
        )
        client.add_event_handler(
            partial(handler, kind="deleted"),
            events.MessageDeleted(chats=chats, blacklist_chats=blacklist),
        )


def follow_reconnects(client: Any, on_reconnect: Callable[[], Awaitable[None]]) -> Callable[[], None]:
    """Run `on_reconnect` each time Telethon brings the connection back.

    Telethon reconnects on its own and says nothing about the gap, and asking
    `is_connected` from outside does not see it either: the flag stays up for
    the whole automatic reconnect and drops only once the client gives up, at
    which point `run_until_disconnected` returns. The one signal the library
    has is the callback its sender (`MTProtoSender._reconnect` in Telethon
    1.44) runs after every successful reconnect, so the catch-up is chained in
    front of it. It goes first: the sender schedules the callback as soon as
    the socket is back, while an update needs the receive loop, the updates
    queue and a handler task -- later steps of the loop -- so the catch-up opens
    its gap before any live message can move a mark. The library's own request
    still runs when the catch-up fails.

    Returns the function that puts the library's callback back.
    """
    sender = getattr(client, "_sender", None)
    original: Callable[[], Awaitable[Any]] | None = getattr(sender, "_auto_reconnect_callback", None)
    if sender is None or not callable(original):
        logger.warning("Telethon offers no reconnect callback; what a reconnect misses is not replayed")
        return lambda: None

    async def chained() -> None:
        try:
            await on_reconnect()
        except Exception as e:  # a failed catch-up must not stop the updates
            logger.warning("catch-up after reconnect failed (%s); the next reconnect will retry it", e)
        await original()

    sender._auto_reconnect_callback = chained

    def restore() -> None:
        sender._auto_reconnect_callback = original

    return restore


class LiveStream:
    """One run of the listener: what was seen, where it goes, how events read.

    The command owns the connection and the flags; everything that happens per
    event lives here, where it can be exercised without a Click invocation.
    """

    def __init__(
        self,
        api: Any,
        state: WatchState,
        sink: RecordSink,
        *,
        wanted: set[int] | None,
        excluded: set[int],
        catchup_limit: int,
        outgoing: bool,
    ) -> None:
        self._api = api
        self._state = state
        self._sink = sink
        self._wanted = wanted
        self._excluded = excluded
        self._catchup_limit = catchup_limit
        self._outgoing = outgoing
        self._titles: dict[int, str] = {}
        # While a gap is open, a live message is shown but does not move the
        # mark of its chat: the mark is where the catch-up asks history from,
        # and a live id above it would make the chat look up to date while the
        # messages in between were never shown. Every id shown during the gap
        # is kept instead, so that neither side prints what the other already
        # has, and the marks move over the live ones once every gap is filled.
        # A gap whose catch-up failed stays open until a later one succeeds.
        self._open_gaps = 0
        self._gap_unfilled = False
        self._shown_in_gap: dict[int, set[int]] = {}
        # One catch-up at a time: a reconnect during the first one would
        # otherwise ask the same history twice and print it twice.
        self._filling = asyncio.Lock()

    async def handle(self, event: Any, *, kind: str) -> None:
        """Turn one event into a record and hand it over."""
        chat_id = getattr(event, "chat_id", None)
        if kind == "deleted":
            ids = list(getattr(event, "deleted_ids", []) or [])
            known = self._titles.get(chat_id) if chat_id is not None else None
            await self._emit(
                deletion_record(chat_id, known, ids, datetime.now(UTC)),
                chat_id=None,
                message_id=None,
            )
            return

        message = event.message
        # A message the catch-up already handed over arrives again as an event;
        # the mark is what tells the two apart. Edits carry the original id, so
        # the check is for new messages only.
        seen = self._state.last_seen(chat_id) if chat_id is not None else None
        if kind == "message" and seen is not None and message.id <= seen:
            return
        # An own message the reader asked to leave out is passed over as if it
        # had been shown: it is not printed, but the mark moves over it -- in a
        # gap only once the gap is filled, like any live message.
        hidden = not self._outgoing and bool(getattr(message, "out", False))
        if hidden and kind != "message":
            return
        if kind == "message" and chat_id is not None and (self._open_gaps or self._gap_unfilled):
            seen_in_gap = self._shown_in_gap.setdefault(chat_id, set())
            if message.id in seen_in_gap:
                return
            seen_in_gap.add(message.id)
            if not hidden:
                await self._sink.emit(message_record(chat_id, await self._title_of(event, chat_id), message))
            return
        if hidden:
            if chat_id is not None:
                await self._pass_over(chat_id, message.id)
            return
        record = message_record(chat_id, await self._title_of(event, chat_id), message, event=kind)
        await self._emit(
            record,
            chat_id=chat_id if kind == "message" else None,
            message_id=message.id if kind == "message" else None,
        )

    async def run(self, *, catchup: bool, edits: bool, announce: Callable[[str], None]) -> None:
        """Listen until the connection ends, then leave nothing behind.

        Whatever ends the run -- a disconnect, an error, an interrupt -- the
        reconnect callback of Telethon is put back, the marks reach the disk
        and the log is closed. An interrupt is the ordinary way to stop and is
        only announced; any other error goes on to the caller. `announce`
        carries the lines meant for the person at the terminal.
        """
        client = self._api.client
        async with self._sink:
            # The gap is open before the first event can arrive: a live message
            # handled ahead of the catch-up would otherwise move the mark of its
            # chat past what was missed.
            if catchup:
                self.open_gap()
            subscribe(
                client,
                self.handle,
                wanted=self._wanted,
                excluded=self._excluded,
                edits=edits,
                outgoing=self._outgoing,
            )
            wanted = self._wanted
            named = "every chat" if wanted is None else f"{len(wanted)} chat{'' if len(wanted) == 1 else 's'}"
            announce(f"Watching {named}")
            # Without the catch-up the run listens from the current moment,
            # after a reconnect as well as at the start.
            stop_following = follow_reconnects(client, self.catch_up) if catchup else None
            try:
                if catchup:
                    await self.fill_gap()
                await client.run_until_disconnected()
            except (KeyboardInterrupt, asyncio.CancelledError):
                announce("Stopped")
            finally:
                if stop_following is not None:
                    stop_following()
                await self._state.save(force=True)

    def open_gap(self) -> None:
        """Stop live messages from moving the marks until a catch-up fills the gap.

        Synchronous on purpose: called before the first await, nothing that
        arrives after the gap began can be dispatched ahead of it.
        """
        self._open_gaps += 1

    async def catch_up(self) -> None:
        """Hand over what arrived while nothing was listening.

        Opens its own gap before the first await, which is what lets the
        reconnect callback run it directly.
        """
        self.open_gap()
        await self.fill_gap()

    async def fill_gap(self) -> None:
        """Fill the gap opened by `open_gap` from history, then let live messages move marks again.

        A catch-up that fails leaves the marks where they were before the gap:
        the next one asks history from there, and the live messages shown in
        the meantime are skipped by id rather than lost.
        """
        try:
            async with self._filling:
                await self._replay_missed()
        except BaseException:
            self._gap_unfilled = True
            raise
        else:
            self._gap_unfilled = False
        finally:
            self._open_gaps -= 1
        if not self._open_gaps and not self._gap_unfilled:
            await self._close_gaps()

    async def _replay_missed(self) -> None:
        async for chat_id, title, message in missed_messages(
            self._api,
            self._state,
            wanted=self._wanted,
            excluded=self._excluded,
            limit=self._catchup_limit,
        ):
            if title:
                self._titles[chat_id] = title
            shown = self._shown_in_gap.setdefault(chat_id, set())
            if message.id in shown:
                # Already shown live; history only confirms the mark may move.
                self._state.note(chat_id, message.id)
                continue
            shown.add(message.id)
            if not self._outgoing and getattr(message, "out", False):
                # Left out, yet covered: without the mark moving over it, a
                # chat whose last message is an own one would be asked for
                # the same interval at every start.
                await self._pass_over(chat_id, message.id)
                continue
            await self._emit(
                message_record(chat_id, title or self._titles.get(chat_id), message),
                chat_id=chat_id,
                message_id=message.id,
            )

    async def _close_gaps(self) -> None:
        """Move the marks over what was shown while the gaps were open.

        Everything up to those ids is shown by now: history covered each chat
        up to the moment it was asked, and the connection carried the rest.
        """
        shown, self._shown_in_gap = self._shown_in_gap, {}
        for chat_id, ids in shown.items():
            if ids:
                self._state.note(chat_id, max(ids))
        if shown:
            await self._state.save()

    async def _emit(self, record: dict, *, chat_id: int | None, message_id: int | None) -> None:
        await self._sink.emit(record)
        if chat_id is not None and message_id is not None:
            await self._pass_over(chat_id, message_id)

    async def _pass_over(self, chat_id: int, message_id: int) -> None:
        """Move the mark of a chat over a message, whether it was printed or left out."""
        self._state.note(chat_id, message_id)
        await self._state.save()

    async def _title_of(self, event: Any, chat_id: int | None) -> str | None:
        """The name of the chat an event came from, asked for once per chat."""
        if chat_id is not None and chat_id in self._titles:
            return self._titles[chat_id]
        try:
            chat = await event.get_chat()
        except Exception as e:  # a title is not worth failing the stream over
            logger.debug("cannot name chat %s: %s", chat_id, e)
            return None
        name = getattr(chat, "title", None) or display_name(chat) or None
        if chat_id is not None and name:
            self._titles[chat_id] = name
        return name
