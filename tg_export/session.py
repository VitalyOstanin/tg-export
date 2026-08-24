"""SQLiteSession subclass that works around a Telethon column-order bug.

Why this exists: ``telethon/sessions/sqlite.py`` reads the row via
``select * from sessions`` and unpacks 6 values as
``(dc_id, server_address, port, key, tmp_key, takeout_id)``, while
``_update_session_table`` writes them positionally --
``insert or replace into sessions values (?,?,?,?,?,?)`` -- from the tuple
``(dc_id, server_address, port, auth_key, takeout_id, tmp_auth_key)``.
Positions 5 and 6 are swapped on read but not on write.

While both columns are NULL, ``AuthKey(data=None)`` short-circuits and the
asymmetry is invisible. Once a successful Takeout stores a non-NULL
``takeout_id``, the next start unpacks that integer into ``tmp_key``,
``AuthKey(data=int)`` calls ``sha1(int)``, and Telethon crashes with
``TypeError: object supporting the buffer API required``.

Because the write path is positional, position -- not column name -- carries
the meaning: position 5 always holds ``takeout_id`` and position 6 always
holds ``tmp_auth_key``, whatever those columns happen to be called. Session
files in the wild come with both physical layouts:

* canonical -- ``..., auth_key, takeout_id, tmp_auth_key``;
* swapped   -- ``..., auth_key, tmp_auth_key, takeout_id``.

Reading by name therefore picks up the wrong value on swapped files and
destroys the real ``takeout_id``; this class reads by position instead.

Older files need the same care. A v7 file has no ``tmp_auth_key`` column at
all: ``_upgrade_database`` appends it on open and the very next ``select *``
reads the surviving ``takeout_id`` as ``tmp_key``, so an untouched v7 file with
an active Takeout crashes on start rather than merely losing the id.

Strategy: before calling ``super().__init__()``, read positions 5 and 6, copy
them into a backup table, NULL them out on disk so the buggy positional unpack
sees a clean slate, then call ``super().__init__()``. After it returns, restore
the values via the public setters (the write path is correct) and drop the
backup table. Reading, backing up and clearing happen in one ``BEGIN
IMMEDIATE`` transaction, so a crash in the middle -- or a second process
opening the same file -- cannot lose the value: the next start finds it in the
backup table.

Upstream fixed the unpack in commit b6a451e07 (2026-08-19), which is not on
PyPI yet. Even after upgrading past it this class cannot simply be deleted:
files written with the swapped layout still need migrating.

The description above holds for Telethon 1.44.0, session schema version 8.
Nothing here fails loudly when that changes -- the values are read and written
by position, so a reordered or extended ``sessions`` table would quietly put
them in the wrong slots. ``tests/test_telethon_contract.py`` pins the layout
and the version so an upgrade that moves them is reported.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession

from tg_export.errors import ProcessLockError
from tg_export.privacy import create_private_file, restrict_file
from tg_export.state import DB_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

BACKUP_TABLE = "tg_export_session_backup"

# Both mean "someone else holds the file": SQLITE_BUSY for a lock held by
# another connection, SQLITE_LOCKED for one held within the same one. Matched
# by prefix because SQLite appends a reason to the extended names
# (SQLITE_BUSY_SNAPSHOT, SQLITE_LOCKED_SHAREDCACHE).
_BUSY_ERROR_PREFIXES = ("SQLITE_BUSY", "SQLITE_LOCKED")


def _is_busy(error: sqlite3.OperationalError) -> bool:
    """True when the operation failed because another writer holds the file."""
    name = getattr(error, "sqlite_errorname", "")
    return name.startswith(_BUSY_ERROR_PREFIXES)


# Telethon writes exactly these six values, in this order, ignoring column
# names. Positions 4 and 5 (0-based) are the two we have to rescue.
_TAKEOUT_ID_POS = 4
_TMP_AUTH_KEY_POS = 5
_SESSION_COLUMN_COUNT = 6
_SWAPPABLE_COLUMNS = {"takeout_id", "tmp_auth_key"}
# A v7 file has no tmp_auth_key column yet -- Telethon adds it while upgrading
# to v8 and then immediately misreads the takeout_id sitting in position 5.
_V7_COLUMN_COUNT = 5


class FixedSQLiteSession(SQLiteSession):
    def __init__(self, session_id=None, store_tmp_auth_key_on_disk: bool = False) -> None:
        # The file carries the authorisation key, and Telethon creates it with
        # the process umask: an empty private file made first takes that
        # decision away from the umask, leaving no window in which another
        # local user can open a descriptor to it.
        session_file = self._session_path(session_id)
        if session_file is not None and str(session_id) != ":memory:":
            create_private_file(session_file)
        saved_takeout_id, saved_tmp_auth_key = self._extract_and_clear(session_id)
        super().__init__(session_id, store_tmp_auth_key_on_disk)
        # Defense-in-depth: even if the pre-init read missed something, after
        # super().__init__() Telethon's swap bug could set _takeout_id to a
        # non-int (e.g. b'' from the physical tmp_auth_key column). Normalize it.
        if self._takeout_id is not None and not isinstance(self._takeout_id, int):
            if self._is_placeholder(self._takeout_id):
                logger.debug("Post-init takeout_id holds the empty placeholder; clearing.")
            else:
                logger.warning(
                    "Post-init takeout_id has unusable type %s; clearing.",
                    type(self._takeout_id).__name__,
                )
            self._takeout_id = None
        if saved_takeout_id is not None:
            self.takeout_id = saved_takeout_id
        if saved_tmp_auth_key:
            self.tmp_auth_key = AuthKey(data=saved_tmp_auth_key)
        self._drop_backup()
        # The file carries the authorisation key; Telethon creates it with the
        # process umask, which normally leaves it readable by everyone.
        if self.filename != ":memory:":
            restrict_file(Path(self.filename))

    def _drop_backup(self) -> None:
        """Discard the staged copy once the values are back in the session table.

        Only safe after the restore above: until then the backup is the only
        place the real takeout_id exists.
        """
        if self.filename == ":memory:":
            return
        try:
            c = self._cursor()
            try:
                c.execute(f"DROP TABLE IF EXISTS {BACKUP_TABLE}")
            finally:
                c.close()
            self.save()
        except sqlite3.Error as e:
            logger.debug("session backup cleanup skipped (%s): %s", self.filename, e)

    @staticmethod
    def _session_path(session_id) -> Path | None:
        if not session_id:
            return None
        sp = str(session_id)
        return Path(sp if sp.endswith(".session") else f"{sp}.session")

    @staticmethod
    def _is_placeholder(value) -> bool:
        """True for the empty BLOB Telethon writes when it keeps no tmp_auth_key.

        With ``store_tmp_auth_key_on_disk=False`` every save puts ``b''`` into
        the tmp_auth_key slot. Reaching the takeout_id slot through the swapped
        unpack, it is the documented consequence of the very bug this module
        works around -- not something to report as unexpected.
        """
        return isinstance(value, bytes | bytearray) and len(value) == 0

    @staticmethod
    def _normalize(takeout_id_raw, tmp_auth_key_raw, where) -> tuple[int | None, bytes | None]:
        """Coerce the two rescued values to the types Telethon can consume.

        The same read/write asymmetry can place a BLOB into the takeout_id slot
        (e.g. b'') or an int into the tmp_auth_key slot. The Telethon serializer
        would then break on struct.pack (struct.error: required argument is not
        an integer) or on AuthKey(data=int) via sha1. Drop anomalies instead.
        """
        takeout_id: int | None
        if isinstance(takeout_id_raw, int):
            takeout_id = takeout_id_raw
        else:
            if FixedSQLiteSession._is_placeholder(takeout_id_raw):
                logger.debug("Empty tmp_auth_key placeholder in the takeout_id slot of %s.", where)
            elif takeout_id_raw is not None:
                logger.warning(
                    "Dropping unusable takeout_id (%s) found in %s. A single occurrence is the "
                    "one-off sanitation of a damaged file; seeing it on every start means another "
                    "writer keeps putting it back.",
                    type(takeout_id_raw).__name__,
                    where,
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
                    "Dropping unusable tmp_auth_key (%s) found in %s. A single occurrence is the "
                    "one-off sanitation of a damaged file; seeing it on every start means another "
                    "writer keeps putting it back.",
                    type(tmp_auth_key_raw).__name__,
                    where,
                )
            tmp_auth_key = None

        return takeout_id, tmp_auth_key

    @staticmethod
    def _read_backup(conn: sqlite3.Connection, path) -> tuple[int | None, bytes | None]:
        """Read what a previous, interrupted start left staged."""
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (BACKUP_TABLE,),
        ).fetchone()
        if present is None:
            return None, None
        row = conn.execute(f"SELECT takeout_id, tmp_auth_key FROM {BACKUP_TABLE}").fetchone()
        if row is None:
            return None, None
        return FixedSQLiteSession._normalize(row[0], row[1], f"table {BACKUP_TABLE} of {path}")

    @staticmethod
    def _rescue_within_transaction(conn: sqlite3.Connection, path: Path) -> tuple[int | None, bytes | None]:
        """Read positions 5 and 6, stage a copy of them and clear them on disk."""
        # IMMEDIATE takes the write lock up front: the read below decides
        # what the clearing UPDATE destroys, so a second process must not
        # slip between them.
        conn.execute("BEGIN IMMEDIATE")
        info = conn.execute("PRAGMA table_info(sessions)").fetchall()
        names = [row[1] for row in info]
        has_tmp_column = (
            len(names) == _SESSION_COLUMN_COUNT and set(names[_TAKEOUT_ID_POS:]) == _SWAPPABLE_COLUMNS
        )
        is_v7 = len(names) == _V7_COLUMN_COUNT and names[_TAKEOUT_ID_POS] == "takeout_id"
        if not has_tmp_column and not is_v7:
            # Either a pre-v5 file with no takeout_id at all, or a shape
            # Telethon itself could not have written positionally.
            conn.rollback()
            return None, None

        row = conn.execute("SELECT * FROM sessions").fetchone()
        takeout_id_raw = row[_TAKEOUT_ID_POS] if row else None
        tmp_auth_key_raw = row[_TMP_AUTH_KEY_POS] if row and has_tmp_column else None
        # Why `is not None` rather than bool(): Telethon writes b'' into
        # position 6 when store_tmp_auth_key_on_disk is False. That is
        # falsy, but on the next read the swap bug turns it into
        # session._takeout_id = b'', which breaks struct.pack in
        # InvokeWithTakeoutRequest. Treat b'' as "has data" and clear it
        # so Telethon reads NULL/NULL.
        has_row_data = takeout_id_raw is not None or tmp_auth_key_raw is not None

        takeout_id, tmp_auth_key = FixedSQLiteSession._normalize(
            takeout_id_raw, tmp_auth_key_raw, f"table sessions of {path}"
        )
        backup_takeout_id, backup_tmp_auth_key = FixedSQLiteSession._read_backup(conn, path)
        # Live columns win over the backup: a start that restored the
        # value but died before dropping the table leaves a stale copy
        # behind, and it must not overwrite what is on disk now.
        if takeout_id is None:
            takeout_id = backup_takeout_id
        if not tmp_auth_key:
            tmp_auth_key = backup_tmp_auth_key

        has_backup = backup_takeout_id is not None or backup_tmp_auth_key is not None
        if not has_row_data and not has_backup:
            conn.rollback()
            return None, None

        # Three states reach this point and they are not the same event:
        # a value worth carrying over, the routine empty placeholder, and
        # a damaged value already reported by _normalize. Only the first
        # one is news, and staging a backup of nothing is pure noise.
        if takeout_id is not None or tmp_auth_key:
            logger.info(
                "Carrying takeout state in %s across Telethon's column-order bug: "
                "takeout_id=%s, tmp_auth_key=%d bytes.",
                path,
                takeout_id if takeout_id is not None else "none",
                len(tmp_auth_key) if tmp_auth_key else 0,
            )
            conn.execute(f"CREATE TABLE IF NOT EXISTS {BACKUP_TABLE} (takeout_id integer, tmp_auth_key blob)")
            conn.execute(f"DELETE FROM {BACKUP_TABLE}")
            conn.execute(
                f"INSERT INTO {BACKUP_TABLE} (takeout_id, tmp_auth_key) VALUES (?, ?)",
                (takeout_id, tmp_auth_key),
            )
        else:
            logger.debug(
                "Nothing to carry over in %s; clearing the placeholder so the swapped unpack reads NULL.",
                path,
            )
        # Clear even if every value turned out anomalous: on the next
        # super().__init__() Telethon, with the same swap bug, would read
        # them back into the wrong slots again. Naming the columns here is
        # unambiguous -- both v8 layouts hold exactly these two at positions
        # 5 and 6, and a v7 file has only the first of them.
        if has_tmp_column:
            conn.execute("UPDATE sessions SET takeout_id = NULL, tmp_auth_key = NULL")
        else:
            conn.execute("UPDATE sessions SET takeout_id = NULL")
        conn.commit()
        return takeout_id, tmp_auth_key

    @staticmethod
    def _extract_and_clear(session_id) -> tuple[int | None, bytes | None]:
        path = FixedSQLiteSession._session_path(session_id)
        if path is None or not path.exists():
            return None, None
        try:
            conn = sqlite3.connect(str(path), timeout=DB_TIMEOUT_SECONDS)
            try:
                return FixedSQLiteSession._rescue_within_transaction(conn, path)
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
        except sqlite3.OperationalError as e:
            if _is_busy(e):
                # Contention is not "nothing to rescue": the values are still in
                # the columns, and returning here would hand the file straight to
                # Telethon's swapped unpack -- the very failure this module
                # exists to prevent. The lock in TgApi keeps other tg-export
                # processes away, so reaching this point means a foreign writer.
                raise ProcessLockError(
                    f"Telegram session {path} is in use by another program: "
                    f"the takeout state could not be read. Close it and try again."
                ) from e
            logger.warning("session pre-init read skipped (%s): %s", path, e)
            return None, None
        except sqlite3.Error as e:
            logger.warning("session pre-init read skipped (%s): %s", path, e)
            return None, None
