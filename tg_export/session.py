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
        # Defense-in-depth: даже если pre-init read что-то пропустил, после
        # super().__init__() swap-баг Telethon мог поставить _takeout_id в
        # non-int (например, b'' из physical tmp_auth_key column). Нормализуем.
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
                # Why `is not None` для обоих, а не bool(): Telethon
                # _update_session_table при store_tmp_auth_key_on_disk=False
                # пишет b'' в physical position 5 (tmp_auth_key). Это
                # falsy для bool(), но при следующем чтении swap-баг
                # ставит session._takeout_id = b'', что валит struct.pack
                # в InvokeWithTakeoutRequest. Считаем b'' тоже «есть данные»
                # и зачищаем БД, чтобы Telethon прочитал NULL/NULL.
                has_data = takeout_id_raw is not None or tmp_auth_key_raw is not None
                if not has_data:
                    return None, None

                # Type validation: одна и та же асимметрия read/write могла
                # подсунуть BLOB в позицию takeout_id (например, b'') или int
                # в позицию tmp_auth_key. Дальше Telethon-сериализатор сломается
                # на struct.pack (struct.error: required argument is not an
                # integer) или AuthKey(data=int) на sha1. Чистим аномалии.
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
                # Зачищаем БД даже если все значения оказались аномалиями: при
                # следующем super().__init__() Telethon с тем же swap-багом
                # снова прочитал бы их в неправильные слоты.
                conn.execute("UPDATE sessions SET takeout_id = NULL, tmp_auth_key = NULL")
                conn.commit()
                return takeout_id, tmp_auth_key
            finally:
                conn.close()
        except sqlite3.Error as e:
            logger.debug("session pre-init read skipped (%s): %s", path, e)
            return None, None
