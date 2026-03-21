"""SQLite state management for incremental export."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import sqlite3

import aiosqlite

# Python 3.12+ removed default datetime adapters from sqlite3
sqlite3.register_adapter(datetime, lambda dt: dt.isoformat())
sqlite3.register_converter("timestamp", lambda b: datetime.fromisoformat(b.decode()))

from tg_export.models import (
    Message, TextPart, TextType, Media, MediaType, Reaction, ReactionType,
    ForwardInfo, InlineButton, InlineButtonType, ServiceAction,
    _media_to_dict, _media_from_dict, _action_to_dict, _action_from_dict,
    _encode_default, _decode_hook,
)


def _plain_text(text_parts: list[TextPart]) -> str:
    """Extract plain text from TextPart list for searchable column."""
    return "".join(tp.text for tp in text_parts)


def _json_dumps(obj: Any) -> str | None:
    if obj is None:
        return None
    return json.dumps(obj, default=_encode_default, ensure_ascii=False)


def _text_parts_to_json(parts: list[TextPart]) -> str:
    return json.dumps([asdict(tp) for tp in parts], default=_encode_default, ensure_ascii=False)


def _text_parts_from_json(s: str | None) -> list[TextPart]:
    if not s:
        return []
    raw = json.loads(s)
    result = []
    for tp in raw:
        tp_type = TextType(tp.pop("type"))
        result.append(TextPart(type=tp_type, **tp))
    return result


def _media_to_json(media: Media | None) -> str | None:
    if media is None:
        return None
    return json.dumps(_media_to_dict(media), default=_encode_default, ensure_ascii=False)


def _media_from_json(s: str | None) -> Media | None:
    if not s:
        return None
    d = json.loads(s, object_hook=_decode_hook)
    return _media_from_dict(d)


def _action_to_json(action: ServiceAction | None) -> str | None:
    if action is None:
        return None
    return json.dumps(_action_to_dict(action), default=_encode_default, ensure_ascii=False)


def _action_from_json(s: str | None) -> ServiceAction | None:
    if not s:
        return None
    d = json.loads(s, object_hook=_decode_hook)
    return _action_from_dict(d)


def _forward_to_json(fwd: ForwardInfo | None) -> str | None:
    if fwd is None:
        return None
    return json.dumps(asdict(fwd), default=_encode_default, ensure_ascii=False)


def _forward_from_json(s: str | None) -> ForwardInfo | None:
    if not s:
        return None
    d = json.loads(s, object_hook=_decode_hook)
    return ForwardInfo(**d)


def _reactions_to_json(reactions: list[Reaction]) -> str | None:
    if not reactions:
        return None
    return json.dumps([asdict(r) for r in reactions], default=_encode_default, ensure_ascii=False)


def _reactions_from_json(s: str | None) -> list[Reaction]:
    if not s:
        return []
    raw = json.loads(s)
    result = []
    for r in raw:
        r["type"] = ReactionType(r["type"])
        result.append(Reaction(**r))
    return result


def _buttons_to_json(buttons: list[list[InlineButton]] | None) -> str | None:
    if buttons is None:
        return None
    return json.dumps([[asdict(btn) for btn in row] for row in buttons],
                      default=_encode_default, ensure_ascii=False)


def _buttons_from_json(s: str | None) -> list[list[InlineButton]] | None:
    if not s:
        return None
    raw = json.loads(s)
    return [
        [InlineButton(type=InlineButtonType(btn["type"]), **{k: v for k, v in btn.items() if k != "type"})
         for btn in row]
        for row in raw
    ]


def _row_to_message(row: dict) -> Message:
    """Reconstruct Message from database row."""
    return Message(
        id=row["msg_id"],
        chat_id=row["chat_id"],
        date=datetime.fromisoformat(row["date"]) if row["date"] else datetime(1970, 1, 1),
        edited=datetime.fromisoformat(row["edited"]) if row["edited"] else None,
        from_id=row["from_id"],
        from_name=row["from_name"] or "",
        text=_text_parts_from_json(row["text_parts"]),
        media=_media_from_json(row["media"]),
        action=_action_from_json(row["action"]),
        reply_to_msg_id=row["reply_to_msg_id"],
        reply_to_peer_id=row["reply_to_peer_id"],
        forwarded_from=_forward_from_json(row["forwarded_from"]),
        reactions=_reactions_from_json(row["reactions"]),
        is_outgoing=bool(row["is_outgoing"]),
        signature=row["signature"],
        via_bot_id=row["via_bot_id"],
        saved_from_chat_id=row["saved_from_chat_id"],
        inline_buttons=_buttons_from_json(row["inline_buttons"]),
        topic_id=row["topic_id"],
        grouped_id=row["grouped_id"],
    )


class ExportState:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def open(self):
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._create_tables()

    async def close(self):
        if self._db:
            await self._db.close()
            self._db = None

    async def _create_tables(self):
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS export_state (
                chat_id        INTEGER PRIMARY KEY,
                last_msg_id    INTEGER NOT NULL,
                oldest_msg_id  INTEGER DEFAULT 0,
                full_history   INTEGER DEFAULT 0,
                messages_count INTEGER DEFAULT 0,
                updated_at     TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS messages (
                chat_id          INTEGER NOT NULL,
                msg_id           INTEGER NOT NULL,
                date             TIMESTAMP,
                edited           TIMESTAMP,
                from_id          INTEGER,
                from_name        TEXT,
                text             TEXT,
                text_parts       TEXT,
                media_type       TEXT,
                media            TEXT,
                action_type      TEXT,
                action           TEXT,
                reply_to_msg_id  INTEGER,
                reply_to_peer_id INTEGER,
                forwarded_from   TEXT,
                reactions        TEXT,
                is_outgoing      INTEGER,
                signature        TEXT,
                via_bot_id       INTEGER,
                saved_from_chat_id INTEGER,
                inline_buttons   TEXT,
                topic_id         INTEGER,
                grouped_id       INTEGER,
                PRIMARY KEY (chat_id, msg_id)
            );

            CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(chat_id, date);
            CREATE INDEX IF NOT EXISTS idx_messages_from ON messages(chat_id, from_id);
            CREATE INDEX IF NOT EXISTS idx_messages_media ON messages(chat_id, media_type);

            CREATE TABLE IF NOT EXISTS files (
                file_id        INTEGER NOT NULL,
                chat_id        INTEGER NOT NULL,
                msg_id         INTEGER,
                expected_size  INTEGER NOT NULL,
                actual_size    INTEGER,
                local_path     TEXT NOT NULL,
                sha256_head    TEXT,
                status         TEXT DEFAULT 'done',
                downloaded_at  TIMESTAMP,
                PRIMARY KEY (file_id, chat_id)
            );

            CREATE TABLE IF NOT EXISTS takeout (
                account    TEXT PRIMARY KEY,
                takeout_id INTEGER,
                created_at TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS users_cache (
                user_id      INTEGER PRIMARY KEY,
                display_name TEXT NOT NULL,
                username     TEXT,
                updated_at   TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS catalog_cache (
                chat_id           INTEGER PRIMARY KEY,
                name              TEXT,
                type              TEXT,
                folder            TEXT,
                members_count     INTEGER,
                messages_count    INTEGER,
                last_message_date TIMESTAMP,
                is_left           INTEGER DEFAULT 0,
                is_archived       INTEGER DEFAULT 0,
                is_forum          INTEGER DEFAULT 0,
                is_monoforum      INTEGER DEFAULT 0,
                updated_at        TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        await self._db.commit()

    # -- export_state --

    async def get_chat_state(self, chat_id: int) -> dict | None:
        """Get full export state for a chat."""
        async with self._db.execute(
            "SELECT * FROM export_state WHERE chat_id=?", (chat_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def set_last_msg_id(self, chat_id: int, msg_id: int):
        await self._db.execute(
            """INSERT INTO export_state (chat_id, last_msg_id, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET last_msg_id=?, updated_at=?""",
            (chat_id, msg_id, datetime.now(), msg_id, datetime.now()),
        )
        await self._db.commit()

    async def set_oldest_msg_id(self, chat_id: int, msg_id: int):
        await self._db.execute(
            """UPDATE export_state SET oldest_msg_id=?, updated_at=?
               WHERE chat_id=?""",
            (msg_id, datetime.now(), chat_id),
        )
        await self._db.commit()

    async def set_full_history(self, chat_id: int, full: bool = True):
        await self._db.execute(
            """UPDATE export_state SET full_history=?, updated_at=?
               WHERE chat_id=?""",
            (int(full), datetime.now(), chat_id),
        )
        await self._db.commit()

    async def update_messages_count(self, chat_id: int, count: int):
        await self._db.execute(
            "UPDATE export_state SET messages_count=?, updated_at=? WHERE chat_id=?",
            (count, datetime.now(), chat_id),
        )
        await self._db.commit()

    async def get_last_msg_id(self, chat_id: int) -> int | None:
        async with self._db.execute(
            "SELECT last_msg_id FROM export_state WHERE chat_id=?", (chat_id,)
        ) as cur:
            row = await cur.fetchone()
            return row["last_msg_id"] if row else None

    # -- files --

    async def register_file(
        self, file_id: int, chat_id: int, msg_id: int,
        expected_size: int, actual_size: int | None,
        local_path: str, status: str = "done",
    ):
        await self._db.execute(
            """INSERT INTO files (file_id, chat_id, msg_id, expected_size, actual_size, local_path, status, downloaded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(file_id, chat_id) DO UPDATE SET
                   actual_size=?, local_path=?, status=?, downloaded_at=?""",
            (file_id, chat_id, msg_id, expected_size, actual_size, local_path, status, datetime.now(),
             actual_size, local_path, status, datetime.now()),
        )
        await self._db.commit()

    async def get_file(self, file_id: int, chat_id: int) -> dict | None:
        async with self._db.execute(
            "SELECT * FROM files WHERE file_id=? AND chat_id=?", (file_id, chat_id)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_files_to_verify(self) -> list[dict]:
        async with self._db.execute(
            "SELECT * FROM files WHERE status != 'done' OR actual_size != expected_size"
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    # -- messages --

    def _msg_to_params(self, msg: Message) -> tuple:
        """Convert Message to SQL parameter tuple (insert + update values)."""
        values = (
            msg.chat_id, msg.id,
            msg.date.isoformat() if msg.date else None,
            msg.edited.isoformat() if msg.edited else None,
            msg.from_id, msg.from_name,
            _plain_text(msg.text),
            _text_parts_to_json(msg.text),
            msg.media.type.value if msg.media else None,
            _media_to_json(msg.media),
            msg.action.type if msg.action else None,
            _action_to_json(msg.action),
            msg.reply_to_msg_id, msg.reply_to_peer_id,
            _forward_to_json(msg.forwarded_from),
            _reactions_to_json(msg.reactions),
            int(msg.is_outgoing), msg.signature, msg.via_bot_id,
            msg.saved_from_chat_id,
            _buttons_to_json(msg.inline_buttons),
            msg.topic_id, msg.grouped_id,
        )
        # UPDATE values are same as INSERT values minus chat_id and msg_id
        return values + values[2:]

    _UPSERT_SQL = """INSERT INTO messages (
                chat_id, msg_id, date, edited, from_id, from_name,
                text, text_parts, media_type, media, action_type, action,
                reply_to_msg_id, reply_to_peer_id, forwarded_from,
                reactions, is_outgoing, signature, via_bot_id,
                saved_from_chat_id, inline_buttons, topic_id, grouped_id
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(chat_id, msg_id) DO UPDATE SET
                date=?, edited=?, from_id=?, from_name=?,
                text=?, text_parts=?, media_type=?, media=?,
                action_type=?, action=?,
                reply_to_msg_id=?, reply_to_peer_id=?, forwarded_from=?,
                reactions=?, is_outgoing=?, signature=?, via_bot_id=?,
                saved_from_chat_id=?, inline_buttons=?, topic_id=?, grouped_id=?"""

    async def store_message(self, msg: Message):
        """Store single message (no commit — caller should batch-commit)."""
        await self._db.execute(self._UPSERT_SQL, self._msg_to_params(msg))

    async def store_messages_batch(self, messages: list[Message]):
        """Store a batch of messages in a single transaction."""
        params = [self._msg_to_params(msg) for msg in messages]
        await self._db.executemany(self._UPSERT_SQL, params)
        await self._db.commit()

    async def commit(self):
        """Explicit commit for batched operations."""
        await self._db.commit()

    async def load_messages(self, chat_id: int) -> list[Message]:
        """Load all messages for a chat, sorted by msg_id."""
        async with self._db.execute(
            "SELECT * FROM messages WHERE chat_id=? ORDER BY msg_id", (chat_id,)
        ) as cur:
            rows = await cur.fetchall()
            return [_row_to_message(dict(r)) for r in rows]

    async def count_messages(self, chat_id: int) -> int:
        """Count messages for a chat."""
        async with self._db.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id=?", (chat_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def search_messages(
        self, chat_id: int,
        text_query: str | None = None,
        media_type: str | None = None,
        from_id: int | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> list[Message]:
        """Search messages using SQL columns (no JSON parsing needed)."""
        clauses = ["chat_id = ?"]
        params: list[Any] = [chat_id]

        if text_query:
            clauses.append("text LIKE ?")
            params.append(f"%{text_query}%")
        if media_type:
            clauses.append("media_type = ?")
            params.append(media_type)
        if from_id is not None:
            clauses.append("from_id = ?")
            params.append(from_id)
        if date_from:
            clauses.append("date >= ?")
            params.append(date_from.isoformat())
        if date_to:
            clauses.append("date <= ?")
            params.append(date_to.isoformat())

        where = " AND ".join(clauses)
        async with self._db.execute(
            f"SELECT * FROM messages WHERE {where} ORDER BY msg_id", params
        ) as cur:
            rows = await cur.fetchall()
            return [_row_to_message(dict(r)) for r in rows]

    # -- takeout --

    async def save_takeout(self, account: str, takeout_id: int):
        await self._db.execute(
            """INSERT INTO takeout (account, takeout_id, created_at)
               VALUES (?, ?, ?)
               ON CONFLICT(account) DO UPDATE SET takeout_id=?, created_at=?""",
            (account, takeout_id, datetime.now(), takeout_id, datetime.now()),
        )
        await self._db.commit()

    async def get_takeout(self, account: str) -> int | None:
        async with self._db.execute(
            "SELECT takeout_id FROM takeout WHERE account=?", (account,)
        ) as cur:
            row = await cur.fetchone()
            return row["takeout_id"] if row else None

    # -- users_cache --

    async def cache_user(self, user_id: int, display_name: str, username: str | None):
        await self._db.execute(
            """INSERT INTO users_cache (user_id, display_name, username, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET display_name=?, username=?, updated_at=?""",
            (user_id, display_name, username, datetime.now(),
             display_name, username, datetime.now()),
        )
        await self._db.commit()

    async def get_user(self, user_id: int) -> dict | None:
        async with self._db.execute(
            "SELECT * FROM users_cache WHERE user_id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    # -- catalog_cache --

    async def cache_catalog(self, chat_id: int, name: str, chat_type: str,
                            folder: str | None, members_count: int | None,
                            messages_count: int, last_message_date: datetime | None,
                            is_left: bool, is_archived: bool, is_forum: bool, is_monoforum: bool):
        await self._db.execute(
            """INSERT INTO catalog_cache
               (chat_id, name, type, folder, members_count, messages_count,
                last_message_date, is_left, is_archived, is_forum, is_monoforum, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                   name=?, type=?, folder=?, members_count=?, messages_count=?,
                   last_message_date=?, is_left=?, is_archived=?, is_forum=?, is_monoforum=?, updated_at=?""",
            (chat_id, name, chat_type, folder, members_count, messages_count,
             last_message_date, int(is_left), int(is_archived), int(is_forum), int(is_monoforum), datetime.now(),
             name, chat_type, folder, members_count, messages_count,
             last_message_date, int(is_left), int(is_archived), int(is_forum), int(is_monoforum), datetime.now()),
        )
        await self._db.commit()

    async def get_catalog(self) -> list[dict]:
        async with self._db.execute("SELECT * FROM catalog_cache") as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    # -- meta --

    async def set_meta(self, key: str, value: str):
        await self._db.execute(
            """INSERT INTO meta (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value=?""",
            (key, value, value),
        )
        await self._db.commit()

    async def get_meta(self, key: str) -> str | None:
        async with self._db.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ) as cur:
            row = await cur.fetchone()
            return row["value"] if row else None
