"""SQLite state management for incremental export."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import aiosqlite


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
                messages_count INTEGER DEFAULT 0,
                updated_at     TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS messages (
                chat_id        INTEGER NOT NULL,
                msg_id         INTEGER NOT NULL,
                data           TEXT NOT NULL,
                PRIMARY KEY (chat_id, msg_id)
            );

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

    async def set_last_msg_id(self, chat_id: int, msg_id: int):
        await self._db.execute(
            """INSERT INTO export_state (chat_id, last_msg_id, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET last_msg_id=?, updated_at=?""",
            (chat_id, msg_id, datetime.now(), msg_id, datetime.now()),
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

    async def store_message(self, chat_id: int, msg_id: int, data: str):
        await self._db.execute(
            """INSERT INTO messages (chat_id, msg_id, data)
               VALUES (?, ?, ?)
               ON CONFLICT(chat_id, msg_id) DO UPDATE SET data=?""",
            (chat_id, msg_id, data, data),
        )
        await self._db.commit()

    async def load_messages(self, chat_id: int) -> list[str]:
        async with self._db.execute(
            "SELECT data FROM messages WHERE chat_id=? ORDER BY msg_id", (chat_id,)
        ) as cur:
            rows = await cur.fetchall()
            return [row["data"] for row in rows]

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
                            is_left: bool, is_forum: bool, is_monoforum: bool):
        await self._db.execute(
            """INSERT INTO catalog_cache
               (chat_id, name, type, folder, members_count, messages_count,
                last_message_date, is_left, is_forum, is_monoforum, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                   name=?, type=?, folder=?, members_count=?, messages_count=?,
                   last_message_date=?, is_left=?, is_forum=?, is_monoforum=?, updated_at=?""",
            (chat_id, name, chat_type, folder, members_count, messages_count,
             last_message_date, int(is_left), int(is_forum), int(is_monoforum), datetime.now(),
             name, chat_type, folder, members_count, messages_count,
             last_message_date, int(is_left), int(is_forum), int(is_monoforum), datetime.now()),
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
