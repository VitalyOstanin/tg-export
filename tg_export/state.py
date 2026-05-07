"""SQLite state management for incremental export."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None  # type: ignore[assignment]

from tg_export.models import (
    ForwardInfo,
    InlineButton,
    InlineButtonType,
    Media,
    Message,
    Reaction,
    ReactionType,
    ServiceAction,
    TextPart,
    TextType,
    _action_from_dict,
    _action_to_dict,
    _decode_hook,
    _encode_default,
    _media_from_dict,
    _media_to_dict,
)

# Python 3.12+ removed default datetime adapters from sqlite3.
# Why module-level: register_* are global to the process; once loaded, all
# sqlite3 connections in tg-export get correct datetime handling.
sqlite3.register_adapter(datetime, lambda dt: dt.isoformat())
sqlite3.register_converter("timestamp", lambda b: datetime.fromisoformat(b.decode()))


def _plain_text(text_parts: list[TextPart]) -> str:
    """Extract plain text from TextPart list for searchable column."""
    return "".join(tp.text for tp in text_parts)


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
    return json.dumps(
        [[asdict(btn) for btn in row] for row in buttons], default=_encode_default, ensure_ascii=False
    )


def _buttons_from_json(s: str | None) -> list[list[InlineButton]] | None:
    if not s:
        return None
    raw = json.loads(s)
    return [
        [
            InlineButton(type=InlineButtonType(btn["type"]), **{k: v for k, v in btn.items() if k != "type"})
            for btn in row
        ]
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


def _load_messages_for_month_sync(db_path: Path, chat_id: int, month_key: str) -> list:
    """Synchronous month loader for use inside asyncio.to_thread workers.

    Why: render_chat_streaming runs in a thread to avoid blocking the event
    loop. Reusing the aiosqlite connection from another thread is unsafe;
    open a short-lived read-only sqlite3 connection per month instead.
    """
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        db.row_factory = sqlite3.Row
        if month_key == "0000-00":
            cur = db.execute(
                "SELECT * FROM messages WHERE chat_id=? AND date IS NULL ORDER BY msg_id",
                (chat_id,),
            )
        else:
            cur = db.execute(
                "SELECT * FROM messages WHERE chat_id=? AND strftime('%Y-%m', date) = ? ORDER BY msg_id",
                (chat_id, month_key),
            )
        rows = cur.fetchall()
        return [_row_to_message(dict(r)) for r in rows]
    finally:
        db.close()


class StateLockError(RuntimeError):
    """Raised when another process already holds the state DB lock."""


class ExportState:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._lock_fd: int | None = None
        self._lock_path: Path | None = None

    @property
    def db(self) -> aiosqlite.Connection:
        """Return open DB connection. Raises RuntimeError if not opened."""
        if self._db is None:
            raise RuntimeError("ExportState not opened, call open() first")
        return self._db

    def _acquire_lock(self):
        """Acquire advisory lock on <db>.lock to prevent concurrent processes.

        Why: SQLite alone returns 'database is locked' on contention; an
        explicit lock with a clear error message tells the user that another
        tg-export is running, instead of cryptic OperationalError later.
        """
        if _fcntl is None:
            return  # Windows: no-op (Telegram session is already single-active)
        self._lock_path = self.db_path.with_suffix(self.db_path.suffix + ".lock")
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as e:
            os.close(fd)
            raise StateLockError(
                f"State DB is locked by another tg-export process: {self._lock_path}. "
                f"Make sure no other run/verify/state command is in progress."
            ) from e
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._lock_fd = fd

    def _release_lock(self):
        if self._lock_fd is not None and _fcntl is not None:
            with contextlib.suppress(OSError):
                _fcntl.flock(self._lock_fd, _fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(self._lock_fd)
            self._lock_fd = None
        if self._lock_path is not None:
            try:
                self._lock_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
            self._lock_path = None

    async def open(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._acquire_lock()
        try:
            self._db = await aiosqlite.connect(self.db_path)
            self.db.row_factory = aiosqlite.Row
            await self._apply_pragmas()
            await self._create_tables()
        except Exception:
            self._release_lock()
            raise

    async def _apply_pragmas(self):
        # WAL allows concurrent readers and one writer without escalation.
        # synchronous=NORMAL avoids fsync on every commit (durable enough with WAL).
        # cache_size negative = KiB; mmap_size in bytes.
        for pragma in (
            "PRAGMA journal_mode = WAL",
            "PRAGMA synchronous = NORMAL",
            "PRAGMA temp_store = MEMORY",
            "PRAGMA cache_size = -65536",
            "PRAGMA mmap_size = 268435456",
            "PRAGMA foreign_keys = ON",
        ):
            await self.db.execute(pragma)

    async def close(self):
        if self._db:
            await self._db.close()
            self._db = None
        self._release_lock()

    async def commit(self):
        # Why: a second SIGINT cancels asyncio tasks, including the one running
        # commit(); without shield, a partially-applied batch may be lost. Shield
        # ensures the commit completes atomically even if the surrounding task
        # is cancelled.
        await asyncio.shield(self.db.commit())

    async def _create_tables(self):
        await self.db.executescript("""
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
            CREATE INDEX IF NOT EXISTS idx_messages_grouped ON messages(chat_id, grouped_id);

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

            CREATE INDEX IF NOT EXISTS idx_files_chat ON files(chat_id);
            CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
            CREATE INDEX IF NOT EXISTS idx_files_local_path ON files(local_path);

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
        await self.commit()

    # -- export_state --

    async def get_chat_state(self, chat_id: int) -> dict | None:
        """Get full export state for a chat."""
        async with self.db.execute("SELECT * FROM export_state WHERE chat_id=?", (chat_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def set_last_msg_id(self, chat_id: int, msg_id: int):
        await self.db.execute(
            """INSERT INTO export_state (chat_id, last_msg_id, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET last_msg_id=?, updated_at=?""",
            (chat_id, msg_id, datetime.now(), msg_id, datetime.now()),
        )
        await self.commit()

    async def set_oldest_msg_id(self, chat_id: int, msg_id: int):
        # Why UPSERT: set_oldest_msg_id may be called before set_last_msg_id
        # for a freshly seen chat; without an INSERT branch the UPDATE is a no-op.
        await self.db.execute(
            """INSERT INTO export_state (chat_id, oldest_msg_id, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET oldest_msg_id=?, updated_at=?""",
            (chat_id, msg_id, datetime.now(), msg_id, datetime.now()),
        )
        await self.commit()

    async def set_full_history(self, chat_id: int, full: bool = True):
        await self.db.execute(
            """UPDATE export_state SET full_history=?, updated_at=?
               WHERE chat_id=?""",
            (int(full), datetime.now(), chat_id),
        )
        await self.commit()

    async def update_messages_count(self, chat_id: int, count: int):
        await self.db.execute(
            "UPDATE export_state SET messages_count=?, updated_at=? WHERE chat_id=?",
            (count, datetime.now(), chat_id),
        )
        await self.commit()

    async def get_last_msg_id(self, chat_id: int) -> int | None:
        async with self.db.execute("SELECT last_msg_id FROM export_state WHERE chat_id=?", (chat_id,)) as cur:
            row = await cur.fetchone()
            return row["last_msg_id"] if row else None

    # -- files --

    async def register_file(
        self,
        file_id: int,
        chat_id: int,
        msg_id: int,
        expected_size: int,
        actual_size: int | None,
        local_path: str,
        status: str = "done",
    ):
        now = datetime.now()
        await self.db.execute(
            """INSERT INTO files (file_id, chat_id, msg_id, expected_size, actual_size, local_path, status, downloaded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(file_id, chat_id) DO UPDATE SET
                   actual_size=?, local_path=?, status=?, downloaded_at=?""",
            (
                file_id,
                chat_id,
                msg_id,
                expected_size,
                actual_size,
                local_path,
                status,
                now,
                actual_size,
                local_path,
                status,
                now,
            ),
        )
        # Why: kill -9 between download and the next batch-commit otherwise leaves
        # the file on disk but unregistered in DB; _cleanup_orphaned_files removes
        # it on next run, forcing a re-download.
        await self.commit()

    async def get_file(self, file_id: int, chat_id: int) -> dict | None:
        async with self.db.execute(
            "SELECT * FROM files WHERE file_id=? AND chat_id=?", (file_id, chat_id)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_file_any_chat(self, file_id: int) -> dict | None:
        """Find file_id in any chat (for intra-account deduplication)."""
        async with self.db.execute(
            "SELECT * FROM files WHERE file_id=? AND status='done' LIMIT 1", (file_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_known_paths(self, chat_id: int) -> set[str]:
        """Return set of local_path strings registered for a chat."""
        async with self.db.execute("SELECT local_path FROM files WHERE chat_id=?", (chat_id,)) as cur:
            rows = await cur.fetchall()
            return {r[0] for r in rows}

    async def get_files_to_verify(self) -> list[dict]:
        # Why expected_size > 0: when expected_size is 0 (Telegram didn't report
        # a size up-front), actual_size != expected_size will always be true and
        # we'd needlessly re-download every file.
        async with self.db.execute(
            "SELECT * FROM files WHERE status != 'done' "
            "OR (expected_size > 0 AND actual_size != expected_size)"
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    # -- messages --

    def _msg_to_params(self, msg: Message) -> tuple:
        """Convert Message to SQL parameter tuple (insert + update values)."""
        values = (
            msg.chat_id,
            msg.id,
            msg.date.isoformat() if msg.date else None,
            msg.edited.isoformat() if msg.edited else None,
            msg.from_id,
            msg.from_name,
            _plain_text(msg.text),
            _text_parts_to_json(msg.text),
            msg.media.type.value if msg.media else None,
            _media_to_json(msg.media),
            msg.action.type if msg.action else None,
            _action_to_json(msg.action),
            msg.reply_to_msg_id,
            msg.reply_to_peer_id,
            _forward_to_json(msg.forwarded_from),
            _reactions_to_json(msg.reactions),
            int(msg.is_outgoing),
            msg.signature,
            msg.via_bot_id,
            msg.saved_from_chat_id,
            _buttons_to_json(msg.inline_buttons),
            msg.topic_id,
            msg.grouped_id,
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
        await self.db.execute(self._UPSERT_SQL, self._msg_to_params(msg))

    async def store_messages_batch(self, messages: list[Message]):
        """Store a batch of messages in a single transaction."""
        params = [self._msg_to_params(msg) for msg in messages]
        await self.db.executemany(self._UPSERT_SQL, params)
        await self.commit()

    async def load_messages(self, chat_id: int) -> list[Message]:
        """Load all messages for a chat, sorted by msg_id."""
        async with self.db.execute(
            "SELECT * FROM messages WHERE chat_id=? ORDER BY msg_id", (chat_id,)
        ) as cur:
            rows = await cur.fetchall()
            return [_row_to_message(dict(r)) for r in rows]

    async def list_message_months(self, chat_id: int) -> list[str]:
        """Return sorted list of "YYYY-MM" keys present for the chat.

        Messages with NULL date are bucketed into "0000-00".
        """
        sql = (
            "SELECT DISTINCT COALESCE(strftime('%Y-%m', date), '0000-00') AS m "
            "FROM messages WHERE chat_id=? ORDER BY m"
        )
        async with self.db.execute(sql, (chat_id,)) as cur:
            rows = await cur.fetchall()
            return [r["m"] for r in rows]

    async def load_messages_for_month(self, chat_id: int, month_key: str) -> list[Message]:
        """Load messages for a single (chat, "YYYY-MM") bucket, ordered by msg_id.

        Why: streaming render reads one month at a time to keep peak memory
        proportional to one month rather than the whole chat.
        """
        if month_key == "0000-00":
            sql = "SELECT * FROM messages WHERE chat_id=? AND date IS NULL ORDER BY msg_id"
            params: tuple = (chat_id,)
        else:
            sql = "SELECT * FROM messages WHERE chat_id=? AND strftime('%Y-%m', date) = ? ORDER BY msg_id"
            params = (chat_id, month_key)
        async with self.db.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [_row_to_message(dict(r)) for r in rows]

    async def count_messages(self, chat_id: int) -> int:
        """Count messages for a chat."""
        async with self.db.execute("SELECT COUNT(*) FROM messages WHERE chat_id=?", (chat_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def count_files(self, chat_id: int | None = None) -> dict[str, int]:
        """Count files with media in messages and downloaded files.

        Returns dict with keys: media_messages, expected_files, files_downloaded.
        - media_messages: messages with any media (including unsupported)
        - expected_files: messages with downloadable file (has file.id, not unsupported)
        - files_downloaded: files with status='done' in files table
        If chat_id is None, counts across all chats.
        """
        if chat_id is not None:
            msg_where = "WHERE chat_id=? AND"
            file_where = "WHERE chat_id=? AND"
            msg_args: tuple = (chat_id,)
            file_args: tuple = (chat_id,)
        else:
            msg_where = "WHERE"
            file_where = "WHERE"
            msg_args = ()
            file_args = ()

        q_media = f"SELECT COUNT(*) FROM messages {msg_where} media_type IS NOT NULL AND media_type != ''"
        q_expected = (
            f"SELECT COUNT(*) FROM messages {msg_where}"
            " media_type IS NOT NULL AND media_type != ''"
            " AND json_extract(media, '$.file.id') IS NOT NULL"
        )
        q_files = f"SELECT COUNT(*) FROM files {file_where} status='done'"

        async with self.db.execute(q_media, msg_args) as cur:
            row = await cur.fetchone()
            media_messages = row[0] if row else 0
        async with self.db.execute(q_expected, msg_args) as cur:
            row = await cur.fetchone()
            expected_files = row[0] if row else 0
        async with self.db.execute(q_files, file_args) as cur:
            row = await cur.fetchone()
            files_downloaded = row[0] if row else 0
        return {
            "media_messages": media_messages,
            "expected_files": expected_files,
            "files_downloaded": files_downloaded,
        }

    async def purge_chat(self, chat_id: int) -> dict[str, int]:
        """Delete all data for a chat. Returns counts of deleted rows."""
        counts = {}
        for table in ("messages", "files", "export_state", "catalog_cache"):
            async with self.db.execute(f"SELECT COUNT(*) FROM {table} WHERE chat_id=?", (chat_id,)) as cur:
                row = await cur.fetchone()
                counts[table] = row[0] if row else 0
            await self.db.execute(f"DELETE FROM {table} WHERE chat_id=?", (chat_id,))
        await self.commit()
        return counts

    async def find_chat_by_name(self, name: str) -> list[dict]:
        """Search chats in catalog_cache by name (case-insensitive substring)."""
        async with self.db.execute(
            "SELECT chat_id, name, type FROM catalog_cache WHERE name LIKE ?", (f"%{name}%",)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_catalog_entry(self, chat_id: int) -> dict | None:
        """Direct lookup of a chat by id; avoids the LIKE '%%' full-table scan."""
        async with self.db.execute(
            "SELECT chat_id, name, type FROM catalog_cache WHERE chat_id=?",
            (chat_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def search_messages(
        self,
        chat_id: int,
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
        async with self.db.execute(f"SELECT * FROM messages WHERE {where} ORDER BY msg_id", params) as cur:
            rows = await cur.fetchall()
            return [_row_to_message(dict(r)) for r in rows]

    # -- takeout --

    async def save_takeout(self, account: str, takeout_id: int):
        await self.db.execute(
            """INSERT INTO takeout (account, takeout_id, created_at)
               VALUES (?, ?, ?)
               ON CONFLICT(account) DO UPDATE SET takeout_id=?, created_at=?""",
            (account, takeout_id, datetime.now(), takeout_id, datetime.now()),
        )
        await self.commit()

    async def get_takeout(self, account: str) -> int | None:
        async with self.db.execute("SELECT takeout_id FROM takeout WHERE account=?", (account,)) as cur:
            row = await cur.fetchone()
            return row["takeout_id"] if row else None

    # -- users_cache --

    async def cache_user(self, user_id: int, display_name: str, username: str | None):
        await self.db.execute(
            """INSERT INTO users_cache (user_id, display_name, username, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET display_name=?, username=?, updated_at=?""",
            (user_id, display_name, username, datetime.now(), display_name, username, datetime.now()),
        )

    async def get_user(self, user_id: int) -> dict | None:
        async with self.db.execute("SELECT * FROM users_cache WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    # -- catalog_cache --

    async def cache_catalog(
        self,
        chat_id: int,
        name: str,
        chat_type: str,
        folder: str | None,
        members_count: int | None,
        messages_count: int,
        last_message_date: datetime | None,
        is_left: bool,
        is_archived: bool,
        is_forum: bool,
        is_monoforum: bool,
    ):
        now = datetime.now()
        await self.db.execute(
            """INSERT INTO catalog_cache
               (chat_id, name, type, folder, members_count, messages_count,
                last_message_date, is_left, is_archived, is_forum, is_monoforum, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                   name=?, type=?, folder=?, members_count=?, messages_count=?,
                   last_message_date=?, is_left=?, is_archived=?, is_forum=?, is_monoforum=?, updated_at=?""",
            (
                chat_id,
                name,
                chat_type,
                folder,
                members_count,
                messages_count,
                last_message_date,
                int(is_left),
                int(is_archived),
                int(is_forum),
                int(is_monoforum),
                now,
                name,
                chat_type,
                folder,
                members_count,
                messages_count,
                last_message_date,
                int(is_left),
                int(is_archived),
                int(is_forum),
                int(is_monoforum),
                now,
            ),
        )
        # Why: previously commit was deferred to the next batch-commit; if
        # iter_messages threw before the first batch the catalog entry would
        # be lost and statistics would show an empty chat.
        await self.commit()

    async def get_catalog(self) -> list[dict]:
        async with self.db.execute("SELECT * FROM catalog_cache") as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    # -- meta --

    async def set_meta(self, key: str, value: str):
        await self.db.execute(
            """INSERT INTO meta (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value=?""",
            (key, value, value),
        )
        await self.commit()

    async def get_meta(self, key: str) -> str | None:
        async with self.db.execute("SELECT value FROM meta WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
            return row["value"] if row else None
