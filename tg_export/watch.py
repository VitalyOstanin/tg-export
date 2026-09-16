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
from datetime import datetime
from pathlib import Path
from typing import Any

from tg_export.format import format_moment
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
                "updated": format_moment(datetime.now()),
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
) -> AsyncIterator[tuple[int, Any]]:
    """Messages that arrived while nothing was listening, oldest first.

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
        async for message in api.client.iter_messages(dialog.entity, min_id=mark, limit=limit, reverse=True):
            yield chat_id, message
