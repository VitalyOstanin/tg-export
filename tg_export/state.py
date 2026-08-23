"""SQLite state management for incremental export."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from tg_export.errors import ProcessLockError
from tg_export.locking import ProcessLock
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
    action_from_dict,
    action_to_dict,
    decode_hook,
    encode_default,
    media_from_dict,
    media_to_dict,
)
from tg_export.privacy import restrict_file

logger = logging.getLogger(__name__)

# How long any connection waits for a lock held by another one. The default of
# sqlite3 is 5 seconds, and the reader that looks up files in a neighbour's
# database already considered that too little: a writer holds the lock for
# seconds on a large batch. One constant so every connection waits the same.
DB_TIMEOUT_SECONDS = 30.0

# Bucket key for messages that carry no date. Written by the SQL below and
# read by the renderer, which turns it into the page title -- one constant so
# that changing it cannot leave one of the two behind.
UNKNOWN_MONTH_KEY = "0000-00"

# Pragmas that make sense for any connection to this database, reading or
# writing: they size the cache and keep temporaries in memory. Declared once so
# that the read-only connection of the renderer gets the same treatment as the
# main one -- it used to get none.
_READER_PRAGMAS = (
    f"PRAGMA busy_timeout = {int(DB_TIMEOUT_SECONDS * 1000)}",
    "PRAGMA temp_store = MEMORY",
    "PRAGMA cache_size = -65536",
    "PRAGMA mmap_size = 268435456",
)


def _now() -> datetime:
    """Timestamp for the service columns, in UTC with an explicit offset.

    Message dates arrive from Telegram as aware moments, while the service
    columns used to be filled with a naive local `datetime.now()`: columns
    declared alike held two different formats, and moving the database between
    time zones made them incomparable. Rows written by earlier versions keep
    their local-time values.
    """
    return datetime.now(UTC)


# Python 3.12+ removed the default datetime adapter from sqlite3, so writing a
# datetime without this line stores a repr and fails on read.
# Why module-level: register_adapter is global to the process; once this module
# is loaded, every sqlite3 connection of tg-export writes timestamps the same.
#
# No register_converter counterpart: a converter only runs on a connection
# opened with detect_types=PARSE_DECLTYPES, and none of the connections here
# does that. Registering one anyway suggested that TIMESTAMP columns come back
# as datetime, while they are read as text and parsed explicitly where needed
# (see _row_to_message).
sqlite3.register_adapter(datetime, lambda dt: dt.isoformat())


def _plain_text(text_parts: list[TextPart]) -> str:
    """Extract plain text from TextPart list for searchable column."""
    return "".join(tp.text for tp in text_parts)


def _text_parts_to_json(parts: list[TextPart]) -> str:
    return json.dumps([asdict(tp) for tp in parts], default=encode_default, ensure_ascii=False)


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
    return json.dumps(media_to_dict(media), default=encode_default, ensure_ascii=False)


def _media_from_json(s: str | None) -> Media | None:
    if not s:
        return None
    d = json.loads(s, object_hook=decode_hook)
    return media_from_dict(d)


def _action_to_json(action: ServiceAction | None) -> str | None:
    if action is None:
        return None
    return json.dumps(action_to_dict(action), default=encode_default, ensure_ascii=False)


def _action_from_json(s: str | None) -> ServiceAction | None:
    if not s:
        return None
    d = json.loads(s, object_hook=decode_hook)
    return action_from_dict(d)


def _forward_to_json(fwd: ForwardInfo | None) -> str | None:
    if fwd is None:
        return None
    return json.dumps(asdict(fwd), default=encode_default, ensure_ascii=False)


def _forward_from_json(s: str | None) -> ForwardInfo | None:
    if not s:
        return None
    d = json.loads(s, object_hook=decode_hook)
    return ForwardInfo(**d)


def _reactions_to_json(reactions: list[Reaction]) -> str | None:
    if not reactions:
        return None
    return json.dumps([asdict(r) for r in reactions], default=encode_default, ensure_ascii=False)


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
        [[asdict(btn) for btn in row] for row in buttons], default=encode_default, ensure_ascii=False
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


def _month_range(month_key: str) -> tuple[str, str]:
    """Return [start, end) ISO date bounds for a "YYYY-MM" key.

    Why: filtering by ``date >= start AND date < end`` lets SQLite use the
    idx_messages_date index, unlike ``strftime('%Y-%m', date) = ?`` which wraps
    the indexed column in a function and forces a full scan. Stored dates are
    ISO-8601 strings, so lexicographic comparison matches chronological order.
    """
    year = int(month_key[:4])
    month = int(month_key[5:7])
    start = f"{year:04d}-{month:02d}-01"
    end = f"{year + 1:04d}-01-01" if month == 12 else f"{year:04d}-{month + 1:02d}-01"
    return start, end


def _row_to_message(row: dict[str, Any]) -> Message:
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


def _load_month(db: sqlite3.Connection, chat_id: int, month_key: str) -> list[Message]:
    """Read one month of a chat from an already open connection."""
    if month_key == UNKNOWN_MONTH_KEY:
        cur = db.execute(
            "SELECT * FROM messages WHERE chat_id=? AND date IS NULL ORDER BY msg_id",
            (chat_id,),
        )
    else:
        start, end = _month_range(month_key)
        cur = db.execute(
            "SELECT * FROM messages WHERE chat_id=? AND date >= ? AND date < ? ORDER BY msg_id",
            (chat_id, start, end),
        )
    return [_row_to_message(dict(r)) for r in cur.fetchall()]


@contextlib.contextmanager
def month_reader(db_path: Path, chat_id: int):
    """Yield a `load_month(month_key)` reading through one connection.

    Why a connection of its own: render_chat_streaming runs in a thread to keep
    the event loop free, and the aiosqlite connection of the caller may not be
    used from another thread. Why one for the whole chat rather than one per
    month: opening it costs a file open, a schema read and a cold page cache,
    and the pragmas below apply to the connection, not to the database -- a chat
    with several hundred months paid all of that per month, on a connection that
    never got large enough a cache to keep anything.
    """
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=DB_TIMEOUT_SECONDS)
    try:
        db.row_factory = sqlite3.Row
        for pragma in _READER_PRAGMAS:
            db.execute(pragma)
        yield lambda month_key: _load_month(db, chat_id, month_key)
    finally:
        db.close()


class StateLockError(ProcessLockError):
    """Raised when another process already holds the state DB lock."""


async def _shielded(coro):
    """Run `coro` to completion even if the caller is cancelled, and wait for it.

    asyncio.shield alone protects the operation but not the caller: the caller
    is cancelled at once and walks into its own finally, where it closes the
    database connection the shielded commit is still using -- aiosqlite then
    drops the connection and the operation dies with `ValueError: Connection
    closed` inside a task nobody awaits. Awaiting the inner task again before
    re-raising keeps the caller in place until the write is really done.
    """
    task = asyncio.ensure_future(coro)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


# Every table keyed by chat_id. Purging a chat means clearing all of them, and
# the preview shown before a purge must count exactly the same set -- listing
# them twice let the two drift apart.
CHAT_TABLES = ("messages", "files", "export_state", "catalog_cache")


# Bumped when the schema changes in a way `_add_missing_columns` cannot repair
# on its own (a dropped column, a changed type, a rebuilt index). Plain column
# additions are reconciled from the DDL and need no bump.
SCHEMA_VERSION = 1

# Values the `files.status` column takes. 'done' -- the file is on disk and its
# size matches; 'partial' -- it is on disk but shorter than announced; the two
# skipped statuses mean no file was fetched at all, by decision of the config.
# The set is declared here because the column carries no CHECK: it cannot be
# added to databases that already exist without rebuilding the table.
SKIPPED_FILE_STATUSES = ("skipped_by_size", "skipped_by_type")
FILE_STATUSES = ("done", "partial", *SKIPPED_FILE_STATUSES)

SCHEMA_SQL = """
            CREATE TABLE IF NOT EXISTS export_state (
                chat_id        INTEGER PRIMARY KEY,
                last_msg_id    INTEGER NOT NULL DEFAULT 0,
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

            CREATE INDEX IF NOT EXISTS idx_files_chat ON files(chat_id);
            CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);

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
"""


class ExportState:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._lock = ProcessLock(
            db_path,
            f"State DB is locked by another tg-export process: {db_path}.lock. "
            f"Make sure no other run/verify/state command is in progress.",
        )

    @property
    def db(self) -> aiosqlite.Connection:
        """Return open DB connection. Raises RuntimeError if not opened."""
        if self._db is None:
            raise RuntimeError("ExportState not opened, call open() first")
        return self._db

    def _acquire_lock(self):
        """Take the advisory lock that keeps a second export off this database."""
        try:
            self._lock.acquire()
        except ProcessLockError as e:
            raise StateLockError(str(e)) from e

    def _release_lock(self):
        self._lock.release()

    async def __aenter__(self) -> ExportState:
        """Open the database and hand it over; leaving the block always closes it."""
        await self.open()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def open(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._acquire_lock()
        try:
            self._db = await aiosqlite.connect(self.db_path, timeout=DB_TIMEOUT_SECONDS)
            self.db.row_factory = aiosqlite.Row
            await self._apply_pragmas()
            await self._create_tables()
            # The database holds the text of every exported message, phone
            # numbers and session addresses; SQLite creates it with the umask.
            for suffix in ("", "-wal", "-shm"):
                restrict_file(Path(f"{self.db_path}{suffix}"))
        except Exception:
            self._release_lock()
            raise

    async def _apply_pragmas(self):
        # WAL allows concurrent readers and one writer without escalation.
        # synchronous=NORMAL avoids fsync on every commit (durable enough with WAL).
        # cache_size negative = KiB; mmap_size in bytes.
        # No PRAGMA foreign_keys: the schema declares no FOREIGN KEY, so the
        # pragma only suggested a protection that does not exist. Integrity
        # between the tables is kept by the application -- see CHAT_TABLES and
        # purge_chat, which delete a chat from every table by hand.
        for pragma in (
            "PRAGMA journal_mode = WAL",
            "PRAGMA synchronous = NORMAL",
            *_READER_PRAGMAS,
        ):
            await self.db.execute(pragma)

    async def close(self):
        if self._db:
            await self._db.close()
            self._db = None
        self._release_lock()

    async def commit(self):
        # Why: a second SIGINT cancels the export task, including the one
        # running commit(); without shield, a partially-applied batch may be
        # lost.
        await _shielded(self.db.commit())

    async def _create_tables(self):
        # Columns are reconciled before the script runs: an index declared over
        # a column an older database lacks would fail on CREATE INDEX before any
        # ALTER had a chance to add it.
        await self._add_missing_columns()
        await self.db.executescript(SCHEMA_SQL)
        await self._drop_unused_schema()
        await self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        await self.commit()

    async def _add_missing_columns(self):
        """Bring a database created by an earlier version up to the current shape.

        ``CREATE TABLE IF NOT EXISTS`` is a no-op on an existing file, so a
        column added to the schema never appeared in databases already on disk:
        the export failed on the first write with `table catalog_cache has no
        column named is_archived`, in the middle of a run. The expected shape is
        read from the very same DDL -- a scratch in-memory database built from
        it -- so a new column migrates by being declared, with no second list to
        keep in sync.
        """
        expected = await self._declared_columns()
        for table, columns in expected.items():
            async with self.db.execute(f"PRAGMA table_info({table})") as cur:
                present = {row[1] for row in await cur.fetchall()}
            if not present:
                continue  # the table itself was just created from the DDL
            for name, decl in columns.items():
                if name in present:
                    continue
                logger.info("state DB: adding missing column %s.%s", table, name)
                await self.db.execute(f"ALTER TABLE {table} ADD COLUMN {decl}")

    @staticmethod
    async def _declared_columns() -> dict[str, dict[str, str]]:
        """Column declarations of the current schema, per table.

        A column that cannot be added to an existing table (NOT NULL without a
        default) is left out: such a change needs a real migration, and silently
        failing on ALTER would be worse than the mismatch.
        """
        declared: dict[str, dict[str, str]] = {}
        async with aiosqlite.connect(":memory:") as scratch:
            await scratch.executescript(SCHEMA_SQL)
            async with scratch.execute("SELECT name FROM sqlite_master WHERE type='table'") as cur:
                tables = [row[0] for row in await cur.fetchall()]
            for table in tables:
                columns: dict[str, str] = {}
                async with scratch.execute(f"PRAGMA table_info({table})") as cur:
                    for _, name, col_type, notnull, default, pk in await cur.fetchall():
                        if pk or (notnull and default is None):
                            continue
                        decl = f"{name} {col_type}" if col_type else name
                        if default is not None:
                            decl += f" DEFAULT {default}"
                        columns[name] = decl
                declared[table] = columns
        return declared

    async def _drop_unused_schema(self):
        """Remove schema objects nothing reads.

        `takeout` was a second, parallel store for takeout_id next to the one
        in the Telethon session file, and was never written to; `users_cache`
        and `meta` were never touched either. The two indexes had no reader but
        were paid for on every insert. Dropping them here also cleans databases
        created by earlier versions -- IF EXISTS makes it a no-op afterwards.
        """
        for statement in (
            "DROP TABLE IF EXISTS takeout",
            "DROP TABLE IF EXISTS users_cache",
            "DROP TABLE IF EXISTS meta",
            "DROP INDEX IF EXISTS idx_messages_grouped",
            "DROP INDEX IF EXISTS idx_files_local_path",
        ):
            await self.db.execute(statement)

    # -- export_state --

    async def get_chat_state(self, chat_id: int) -> dict[str, Any] | None:
        """Get full export state for a chat."""
        async with self.db.execute("SELECT * FROM export_state WHERE chat_id=?", (chat_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    # Column whitelist for _upsert_chat_state. Guards against an unknown key
    # passed via **fields and against SQL injection through a column name.
    _UPSERT_COLS = frozenset({"last_msg_id", "oldest_msg_id", "full_history", "messages_count"})

    async def _upsert_chat_state(self, chat_id: int, **fields):
        """UPSERT into export_state.

        Why: there used to be 4 separate setters. set_oldest_msg_id's INSERT
        branch did not set last_msg_id (NOT NULL) and failed with IntegrityError
        when creating a new row. set_full_history and update_messages_count were
        UPDATE-only and silently lost for a chat without a row. This helper
        always creates the row correctly, filling missing NOT NULL columns with
        zeros.
        """
        unknown = set(fields) - self._UPSERT_COLS
        if unknown:
            raise ValueError(f"Unknown export_state columns: {sorted(unknown)}")
        if not fields:
            raise ValueError("commit_phase_progress requires at least one field")

        now = _now()
        # The INSERT branch must provide values for all NOT NULL columns.
        insert_values = {
            "chat_id": chat_id,
            "last_msg_id": 0,
            **fields,
            "updated_at": now,
        }
        insert_cols = list(insert_values.keys())
        update_cols = [*fields.keys(), "updated_at"]
        update_values = {**fields, "updated_at": now}

        # last_msg_id only ever grows: "everything above this id is exported".
        # Callers write it from a copy read at the start of a chat export, so a
        # plain assignment would roll the pointer back whenever something else
        # advanced it meanwhile -- phase 1 of the same run, or a parallel
        # process -- and the messages in between would be re-fetched forever.
        def _assignment(column: str) -> str:
            if column == "last_msg_id":
                return "last_msg_id=MAX(export_state.last_msg_id, ?)"
            return f"{column}=?"

        sql = (
            f"INSERT INTO export_state ({', '.join(insert_cols)}) "
            f"VALUES ({', '.join('?' * len(insert_cols))}) "
            f"ON CONFLICT(chat_id) DO UPDATE SET "
            f"{', '.join(_assignment(c) for c in update_cols)}"
        )
        params = [insert_values[c] for c in insert_cols] + [update_values[c] for c in update_cols]
        await self.db.execute(sql, params)
        await self.commit()

    async def set_last_msg_id(self, chat_id: int, msg_id: int):
        await self._upsert_chat_state(chat_id, last_msg_id=msg_id)

    async def set_oldest_msg_id(self, chat_id: int, msg_id: int):
        await self._upsert_chat_state(chat_id, oldest_msg_id=msg_id)

    async def set_full_history(self, chat_id: int, full: bool = True):
        await self._upsert_chat_state(chat_id, full_history=int(full))

    async def update_messages_count(self, chat_id: int, count: int):
        await self._upsert_chat_state(chat_id, messages_count=count)

    async def commit_phase_progress(
        self,
        chat_id: int,
        last_msg_id: int,
        oldest_msg_id: int,
        full_history: bool,
        messages_count: int,
    ):
        """Atomically write phase-2 progress in a single UPSERT.

        Why: phase 2 used to do 4 separate commits (last/oldest/full_history/
        messages_count). A network/process interruption between them left an
        inconsistent state: e.g. oldest_msg_id written but last_msg_id not, so
        the next run restarted the chat from scratch.
        """
        await self._upsert_chat_state(
            chat_id,
            last_msg_id=last_msg_id,
            oldest_msg_id=oldest_msg_id,
            full_history=int(full_history),
            messages_count=messages_count,
        )

    async def reset_chat_progress(self, chat_id: int | None = None, *, delete_messages: bool = False):
        """Rewind export progress for one chat, or for every chat when chat_id is None.

        The regular writers may only move last_msg_id forward (see
        _upsert_chat_state), so a deliberate rewind cannot go through them --
        it belongs here, where the intent is explicit.
        """
        where = "" if chat_id is None else " WHERE chat_id=?"
        params: tuple = () if chat_id is None else (chat_id,)
        # messages_count goes with the messages: message_counts() prefers the
        # recorded number over COUNT(*), so a row left saying "5000" while the
        # table is empty makes the index page advertise messages nobody has.
        counted = ", messages_count=0" if delete_messages else ""
        await self.db.execute(
            f"UPDATE export_state SET last_msg_id=0, oldest_msg_id=0, full_history=0{counted}{where}",
            params,
        )
        if delete_messages:
            await self.db.execute(f"DELETE FROM messages{where}", params)
            await self.db.execute(f"DELETE FROM files{where}", params)
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
        now = _now()
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

    async def get_file(self, file_id: int, chat_id: int) -> dict[str, Any] | None:
        # Named columns, not *: this runs once per media file, and the callers
        # read only these two -- the rest (paths, hashes) would be materialised
        # into Python strings for nothing.
        async with self.db.execute(
            "SELECT status, local_path, expected_size FROM files WHERE file_id=? AND chat_id=?",
            (file_id, chat_id),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_file_any_chat(self, file_id: int) -> dict[str, Any] | None:
        """Find file_id in any chat (for intra-account deduplication)."""
        async with self.db.execute(
            "SELECT chat_id, local_path FROM files WHERE file_id=? AND status='done' LIMIT 1", (file_id,)
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
        # Why the skipped statuses are excluded: such a file was never
        # downloaded on purpose (too large, or a type the config leaves out), so
        # `status != 'done'` used to hand verify the very files the
        # configuration told it to leave alone.
        # Why the NULL branch: in SQLite a comparison with NULL yields NULL, not
        # true, so a row of unknown size never reached verification at all.
        placeholders = ", ".join("?" for _ in SKIPPED_FILE_STATUSES)
        async with self.db.execute(
            f"SELECT * FROM files WHERE status NOT IN ({placeholders}) AND ("
            "  status != 'done'"
            "  OR (expected_size > 0 AND actual_size IS NULL)"
            "  OR (expected_size > 0 AND actual_size != expected_size)"
            ")",
            tuple(SKIPPED_FILE_STATUSES),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    # -- messages --

    def _msg_to_params(self, msg: Message) -> tuple[Any, ...]:
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

        Messages with NULL date are bucketed into ``UNKNOWN_MONTH_KEY``.
        """
        sql = (
            f"SELECT DISTINCT COALESCE(strftime('%Y-%m', date), '{UNKNOWN_MONTH_KEY}') AS m "
            "FROM messages WHERE chat_id=? ORDER BY m"
        )
        async with self.db.execute(sql, (chat_id,)) as cur:
            rows = await cur.fetchall()
            return [r["m"] for r in rows]

    async def count_chat_rows(self, chat_id: int) -> dict[str, int]:
        """Count the rows a purge of this chat would delete, per table."""
        counts = {}
        for table in CHAT_TABLES:
            async with self.db.execute(f"SELECT COUNT(*) FROM {table} WHERE chat_id=?", (chat_id,)) as cur:
                row = await cur.fetchone()
                counts[table] = row[0] if row else 0
        return counts

    async def count_all_rows(self) -> dict[str, int]:
        """Count every row of the chat-scoped tables, for a whole-database reset.

        `state reset --all --delete-messages` empties `messages` and `files`
        across the account; the confirmation needs the same kind of summary
        `purge` prints for a single chat.
        """
        counts = {}
        for table in CHAT_TABLES:
            async with self.db.execute(f"SELECT COUNT(*) FROM {table}") as cur:
                row = await cur.fetchone()
                counts[table] = row[0] if row else 0
        return counts

    async def message_counts(self) -> dict[int, int]:
        """Number of messages per chat, for every chat at once.

        The index page asked for this chat by chat -- one or two round-trips
        each -- while `state show` had long been doing it in a single query.
        The count recorded in ``export_state`` wins when it is set, exactly as
        the per-chat path decided. A chat known to the state but holding no
        messages is reported as zero rather than left out: the caller has no
        number of its own to fall back on but the top_message id Telegram
        offers, which is an approximation in the thousands, not a count.
        """
        counts: dict[int, int] = {}
        async with self.db.execute("SELECT chat_id, COUNT(*) FROM messages GROUP BY chat_id") as cur:
            for chat_id, count in await cur.fetchall():
                counts[chat_id] = count
        async with self.db.execute("SELECT chat_id, messages_count FROM export_state") as cur:
            for chat_id, count in await cur.fetchall():
                if count > 0 or chat_id not in counts:
                    counts[chat_id] = count
        return counts

    async def list_chat_states(self) -> list[dict[str, Any]]:
        """Return the export progress of every known chat, newest update first."""
        # LEFT JOIN with one grouping instead of a correlated COUNT(*) per row:
        # the subquery walked the chat_id index once for every chat in the
        # table, so the cost was the number of chats times the size of each.
        async with self.db.execute(
            "SELECT es.*, COALESCE(m.msg_count, 0) AS msg_count "
            "FROM export_state es "
            "LEFT JOIN (SELECT chat_id, COUNT(*) AS msg_count FROM messages GROUP BY chat_id) m "
            "ON m.chat_id = es.chat_id "
            "ORDER BY es.updated_at DESC"
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]

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
        """Delete all data for a chat. Returns counts of deleted rows.

        Why shield: the deletes across four tables plus the final commit form
        one logical transaction. Without shield, a cancellation (e.g. SIGINT)
        between two DELETEs could leave some tables purged and others not,
        producing partially-consistent chat data. shield runs the whole block
        to completion even if the surrounding task is cancelled.
        """

        async def _purge() -> dict[str, int]:
            counts = await self.count_chat_rows(chat_id)
            for table in CHAT_TABLES:
                await self.db.execute(f"DELETE FROM {table} WHERE chat_id=?", (chat_id,))
            await self.db.commit()
            return counts

        return await _shielded(_purge())

    async def find_chat_by_name(self, name: str) -> list[dict]:
        """Search chats in catalog_cache by name (case-insensitive substring)."""
        async with self.db.execute(
            "SELECT chat_id, name, type FROM catalog_cache WHERE name LIKE ?", (f"%{name}%",)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_catalog_entry(self, chat_id: int) -> dict[str, Any] | None:
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
        limit: int | None = None,
    ) -> list[Message]:
        """Search messages using SQL columns (no JSON parsing needed).

        ``limit`` caps the result set: the text predicate is a ``LIKE '%...%'``
        no index can serve, and every matching row is then rebuilt into a
        Message with up to six JSON columns parsed, so the cost otherwise grows
        with the number of matches.
        """
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
        tail = ""
        if limit is not None:
            tail = " LIMIT ?"
            params.append(limit)
        async with self.db.execute(
            f"SELECT * FROM messages WHERE {where} ORDER BY msg_id{tail}", params
        ) as cur:
            rows = await cur.fetchall()
            return [_row_to_message(dict(r)) for r in rows]

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
        now = _now()
        await self.db.execute(
            """INSERT INTO catalog_cache
               (chat_id, name, type, folder, members_count, messages_count,
                last_message_date, is_left, is_archived, is_forum, is_monoforum, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                   name=excluded.name, type=excluded.type, folder=excluded.folder,
                   members_count=excluded.members_count, messages_count=excluded.messages_count,
                   last_message_date=excluded.last_message_date, is_left=excluded.is_left,
                   is_archived=excluded.is_archived, is_forum=excluded.is_forum,
                   is_monoforum=excluded.is_monoforum, updated_at=excluded.updated_at""",
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
            ),
        )
        # Why: previously commit was deferred to the next batch-commit; if
        # iter_messages threw before the first batch the catalog entry would
        # be lost and statistics would show an empty chat.
        await self.commit()
