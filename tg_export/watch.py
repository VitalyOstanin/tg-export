"""Live listening: what has already been seen, and what was missed while away.

The listener is a long-running process, and everything it has to survive comes
down to one question -- where it stopped. Telegram does not answer it: an
update stream carries what happens while the socket is open and nothing about
the gap, and Telethon 1.44 does not close that gap either (`_handle_auto_re-
connect` in its updates client still carries a `TODO Catch-up` and only makes
one high-level request). So the marks are kept here, one message id per chat.

Back-fill leans on the dialog list rather than on the marks: one pass over
`iter_dialogs` names every chat together with its last message, and history is
asked for only where that id has moved past the mark. The cost of a start then
follows the number of active chats, not the number of chats overall.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from tg_export.format import class_suffix, display_name, format_moment, strip_control_chars
from tg_export.privacy import ensure_private_dir, write_private_text

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

    Each message comes with the name of its chat: the dialog list already
    carries it, and a back-filled message used to reach the reader with an
    empty chat while a live one carried the name.

    A chat with no mark is not replayed: it was never listened to, and handing
    over its history would flood the reader with an archive it did not ask for.
    """
    async for dialog in api.iter_dialogs():
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
        async for message in api.client.iter_messages(dialog.entity, min_id=mark, limit=limit, reverse=True):
            yield chat_id, title, message


def message_record(chat_id: int | None, chat: str | None, message: Any, *, event: str = "message") -> dict:
    """One message as the reader of the stream sees it.

    The names repeat what `tg messages --json` already prints, so a program
    reading both parses them with one piece of code. Attachments are named, not
    fetched: the listener stays light, and `tg download` fetches a file when
    the reader decides it wants one.
    """
    file = getattr(message, "file", None)
    sender = getattr(message, "sender", None)
    return {
        "event": event,
        "chat_id": chat_id,
        "chat": chat,
        "id": getattr(message, "id", None),
        "date": format_moment(getattr(message, "date", None), missing=""),
        "sender_id": getattr(message, "sender_id", None),
        "sender": display_name(sender) if sender is not None else "",
        "text": getattr(message, "message", "") or "",
        "media": class_suffix(getattr(message, "media", None), "MessageMedia"),
        "file": (
            {"name": getattr(file, "name", None), "size": getattr(file, "size", None)}
            if file is not None
            else None
        ),
        "action": class_suffix(getattr(message, "action", None), "MessageAction"),
        "outgoing": bool(getattr(message, "out", False)),
        "reply_to": getattr(message, "reply_to_msg_id", None),
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
    text = record.get("text") or ""
    if record.get("media"):
        text = f"[{record['media']}] {text}".strip()
    if record.get("action"):
        text = f"({record['action']})"
    sender = strip_control_chars(record.get("sender") or "")
    return f"  {record['date']}  {chat}  [{record['id']}]{mark}  {sender}: {strip_control_chars(text)}"


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

    async def emit(self, record: dict) -> None:
        document = json.dumps(record, ensure_ascii=False)
        self._stream.write((document if self._as_json else text_line(record)) + "\n")
        self._stream.flush()
        if self._log_path is not None:
            await asyncio.to_thread(self._append, document)

    def _append(self, document: str) -> None:
        """Add one line to the log, creating it closed to other users."""
        ensure_private_dir(self._log_path.parent)  # pyright: ignore[reportOptionalMemberAccess]
        fd = os.open(str(self._log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(document + "\n")


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
    # means no filter at all, which is the default this command wants.
    incoming = None if outgoing else True

    client.add_event_handler(
        partial(handler, kind="message"),
        events.NewMessage(chats=chats, blacklist_chats=blacklist, incoming=incoming),
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


async def connection_watchdog(client: Any, on_reconnect: Any, *, interval: float = 30.0) -> None:
    """Run `on_reconnect` after the connection comes back.

    Telethon reconnects on its own and says nothing about the gap, so the
    catch-up has to be driven from outside. Asking the client whether it is
    connected keeps this independent of the library's internals, which is where
    the missing catch-up already lives.
    """
    was_connected = client.is_connected()
    while True:
        await asyncio.sleep(interval)
        connected = client.is_connected()
        if connected and not was_connected:
            logger.info("connection is back; asking for what was missed")
            await on_reconnect()
        was_connected = connected


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
    ) -> None:
        self._api = api
        self._state = state
        self._sink = sink
        self._wanted = wanted
        self._excluded = excluded
        self._catchup_limit = catchup_limit
        self._titles: dict[int, str] = {}

    async def handle(self, event: Any, *, kind: str) -> None:
        """Turn one event into a record and hand it over."""
        chat_id = getattr(event, "chat_id", None)
        if kind == "deleted":
            ids = list(getattr(event, "deleted_ids", []) or [])
            known = self._titles.get(chat_id) if chat_id is not None else None
            await self._emit(
                deletion_record(chat_id, known, ids, datetime.now()),
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
        record = message_record(chat_id, await self._title_of(event, chat_id), message, event=kind)
        await self._emit(
            record,
            chat_id=chat_id if kind == "message" else None,
            message_id=message.id if kind == "message" else None,
        )

    async def catch_up(self) -> None:
        """Hand over what arrived while nothing was listening."""
        async for chat_id, title, message in missed_messages(
            self._api,
            self._state,
            wanted=self._wanted,
            excluded=self._excluded,
            limit=self._catchup_limit,
        ):
            if title:
                self._titles[chat_id] = title
            await self._emit(
                message_record(chat_id, title or self._titles.get(chat_id), message),
                chat_id=chat_id,
                message_id=message.id,
            )

    async def _emit(self, record: dict, *, chat_id: int | None, message_id: int | None) -> None:
        await self._sink.emit(record)
        if chat_id is not None and message_id is not None:
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
