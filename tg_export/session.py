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
        # Defense-in-depth: even if the pre-init read missed something, after
        # super().__init__() Telethon's swap bug could set _takeout_id to a
        # non-int (e.g. b'' from the physical tmp_auth_key column). Normalize it.
        if self._takeout_id is not None and not isinstance(self._takeout_id, int):
            logger.warning(
                "Post-init takeout_id has unexpected type %s; clearing.",
                type(self._takeout_id).__name__,
            )
            self._takeout_id = None
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
                takeout_id_raw, tmp_auth_key_raw = row
                # Why `is not None` for both, not bool(): Telethon's
                # _update_session_table with store_tmp_auth_key_on_disk=False
                # writes b'' into physical position 5 (tmp_auth_key). That is
                # falsy for bool(), but on the next read the swap bug sets
                # session._takeout_id = b'', which breaks struct.pack in
                # InvokeWithTakeoutRequest. Treat b'' as "has data" too and
                # clear the DB so Telethon reads NULL/NULL.
                has_data = takeout_id_raw is not None or tmp_auth_key_raw is not None
                if not has_data:
                    return None, None

                # Type validation: the same read/write asymmetry could place a
                # BLOB into the takeout_id slot (e.g. b'') or an int into the
                # tmp_auth_key slot. The Telethon serializer would then break on
                # struct.pack (struct.error: required argument is not an integer)
                # or AuthKey(data=int) on sha1. Clean up anomalies.
                takeout_id: int | None
                if isinstance(takeout_id_raw, int):
                    takeout_id = takeout_id_raw
                else:
                    if takeout_id_raw is not None:
                        logger.warning(
                            "Unexpected takeout_id type %s in %s; clearing.",
                            type(takeout_id_raw).__name__,
                            path,
                        )
                    takeout_id = None
                tmp_auth_key: bytes | None
                if isinstance(tmp_auth_key_raw, bytes):
                    tmp_auth_key = tmp_auth_key_raw
                elif isinstance(tmp_auth_key_raw, bytearray):
                    tmp_auth_key = bytes(tmp_auth_key_raw)
                else:
                    if tmp_auth_key_raw is not None:
                        logger.warning(
                            "Unexpected tmp_auth_key type %s in %s; clearing.",
                            type(tmp_auth_key_raw).__name__,
                            path,
                        )
                    tmp_auth_key = None

                logger.info(
                    "Detected stale takeout_id/tmp_auth_key in %s; "
                    "staging restore via FixedSQLiteSession (Telethon column-order bug)",
                    path,
                )
                # Clear the DB even if every value turned out anomalous: on the
                # next super().__init__() Telethon, with the same swap bug, would
                # read them back into the wrong slots again.
                conn.execute("UPDATE sessions SET takeout_id = NULL, tmp_auth_key = NULL")
                conn.commit()
                return takeout_id, tmp_auth_key
            finally:
                conn.close()
        except sqlite3.Error as e:
            logger.debug("session pre-init read skipped (%s): %s", path, e)
            return None, None
