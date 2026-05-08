"""SQLiteSession subclass that works around a Telethon 1.43+ column-order bug.

Why this exists: telethon/sessions/sqlite.py:62-68 reads the row via
``select * from sessions`` and unpacks 6 values as
``(dc_id, server_address, port, key, tmp_key, takeout_id)``.
``_update_session_table`` (sqlite.py:211-218) writes them in physical
schema order ``(dc_id, server_address, port, auth_key, takeout_id,
tmp_auth_key)``. Columns 5 and 6 are swapped on read but not on write.

While both columns are NULL, ``AuthKey(data=None)`` short-circuits and
the asymmetry is invisible. Once a successful Takeout stores a non-NULL
``takeout_id``, the next start unpacks that integer into ``tmp_key``,
``AuthKey(data=int)`` calls ``sha1(int)``, and Telethon crashes with
``TypeError: object supporting the buffer API required``.

Strategy: before calling ``super().__init__()``, read ``takeout_id`` and
``tmp_auth_key`` *by name* (so we get the real values), NULL them out on
disk so the buggy positional unpack sees a clean slate, then call
``super().__init__()``. After it returns, restore the values via the
public setters (which go through ``_update_session_table`` -- the write
path is correct).

Upstream is unaware of the bug as of 2026-05; once it's fixed and we
upgrade past it, this class can be deleted.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession

logger = logging.getLogger(__name__)


class FixedSQLiteSession(SQLiteSession):
    def __init__(self, session_id=None, store_tmp_auth_key_on_disk: bool = False):
        saved_takeout_id, saved_tmp_auth_key = self._extract_and_clear(session_id)
        super().__init__(session_id, store_tmp_auth_key_on_disk)
        if saved_takeout_id is not None:
            self.takeout_id = saved_takeout_id
        if saved_tmp_auth_key:
            self.tmp_auth_key = AuthKey(data=saved_tmp_auth_key)

    @staticmethod
    def _extract_and_clear(session_id) -> tuple[int | None, bytes | None]:
        if not session_id:
            return None, None
        sp = str(session_id)
        path = Path(sp if sp.endswith(".session") else f"{sp}.session")
        if not path.exists():
            return None, None
        try:
            conn = sqlite3.connect(str(path), timeout=5)
            try:
                info = conn.execute("PRAGMA table_info(sessions)").fetchall()
                cols = {row[1] for row in info}
                if not cols.issuperset({"tmp_auth_key", "takeout_id"}):
                    return None, None
                row = conn.execute("SELECT takeout_id, tmp_auth_key FROM sessions").fetchone()
                if row is None:
                    return None, None
                takeout_id, tmp_auth_key = row
                if takeout_id is None and not tmp_auth_key:
                    return None, None
                logger.info(
                    "Detected stale takeout_id/tmp_auth_key in %s; "
                    "staging restore via FixedSQLiteSession (Telethon column-order bug)",
                    path,
                )
                conn.execute("UPDATE sessions SET takeout_id = NULL, tmp_auth_key = NULL")
                conn.commit()
                return takeout_id, tmp_auth_key
            finally:
                conn.close()
        except sqlite3.Error as e:
            logger.debug("session pre-init read skipped (%s): %s", path, e)
            return None, None
