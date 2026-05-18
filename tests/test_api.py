import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.errors import TakeoutInitDelayError

from tg_export.api import TgApi
from tg_export.session import FixedSQLiteSession


def _make_session_v8(path, takeout_id=None, tmp_auth_value=None, *, with_version=True):
    """Create a v8-shaped sessions table that matches Telethon's runtime layout.

    Telethon's `_update_session_table` writes columns in physical order
    `(dc_id, server_address, port, auth_key, takeout_id, tmp_auth_key)`, so we
    mirror that here (and add the version row, which Telethon's SQLiteSession
    expects to find on open).
    """
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE sessions (dc_id integer primary key, server_address text,"
        " port integer, auth_key blob, takeout_id integer, tmp_auth_key blob);"
        "CREATE TABLE entities (id integer primary key, hash integer not null,"
        " username text, phone integer, name text, date integer);"
        "CREATE TABLE sent_files (md5_digest blob, file_size integer, type integer,"
        " id integer, hash integer, primary key(md5_digest, file_size, type));"
        "CREATE TABLE update_state (id integer primary key, pts integer, qts integer,"
        " date integer, seq integer);"
    )
    if with_version:
        conn.execute("CREATE TABLE version (version integer primary key)")
        conn.execute("INSERT INTO version VALUES (8)")
    conn.execute(
        "INSERT INTO sessions (dc_id, server_address, port, auth_key, takeout_id, tmp_auth_key)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (2, "localhost", 443, b"x" * 256, takeout_id, tmp_auth_value),
    )
    conn.commit()
    conn.close()


def test_fixed_sqlite_session_restores_takeout_id_and_survives_open(tmp_path):
    """The whole point of FixedSQLiteSession.

    Without the workaround, Telethon's __init__ unpacks the takeout_id (int)
    into `tmp_key` and crashes via `AuthKey(data=int)` -> `sha1(int)`. We
    must (a) not crash, (b) end up with `session.takeout_id == 12345`, and
    (c) leave `auth_key` intact (no re-login required).
    """
    sp = tmp_path / "acc.session"
    _make_session_v8(sp, takeout_id=12345, tmp_auth_value=None)

    sess = FixedSQLiteSession(str(sp))
    try:
        assert sess.takeout_id == 12345
        assert sess.auth_key is not None and sess.auth_key.key == b"x" * 256
    finally:
        sess.close()

    # And after close the value is persisted in the physical takeout_id column,
    # not somewhere else.
    conn = sqlite3.connect(str(sp))
    row = conn.execute("SELECT auth_key, takeout_id, tmp_auth_key FROM sessions").fetchone()
    conn.close()
    assert row[0] == b"x" * 256
    assert row[1] == 12345


def test_fixed_sqlite_session_noop_on_clean_v8(tmp_path):
    sp = tmp_path / "clean.session"
    _make_session_v8(sp, takeout_id=None, tmp_auth_value=None)

    sess = FixedSQLiteSession(str(sp))
    try:
        assert sess.takeout_id is None
        assert sess.auth_key is not None and sess.auth_key.key == b"x" * 256
    finally:
        sess.close()


def test_fixed_sqlite_session_handles_missing_file(tmp_path):
    """Fresh session, file does not exist yet -- super().__init__ creates it."""
    sp = tmp_path / "fresh.session"
    sess = FixedSQLiteSession(str(sp))
    try:
        assert sess.takeout_id is None
    finally:
        sess.close()
    assert sp.exists()


def test_fixed_sqlite_session_clears_non_int_takeout_id(tmp_path):
    # Регрессия на 'struct.error: required argument is not an integer'.
    # Если в позицию takeout_id попало BLOB-значение (например, b'' от swap-бага
    # Telethon), FixedSQLiteSession должна очистить его, а не передать дальше
    # в InvokeWithTakeoutRequest.
    sp = tmp_path / "bad_takeout.session"
    _make_session_v8(sp, takeout_id=b"", tmp_auth_value=None)

    sess = FixedSQLiteSession(str(sp))
    try:
        assert sess.takeout_id is None
    finally:
        sess.close()


def test_fixed_sqlite_session_clears_empty_bytes_tmp_auth_key(tmp_path):
    # Регрессия №2 на struct.error: Telethon _update_session_table при
    # store_tmp_auth_key_on_disk=False пишет b'' в physical position 5
    # (tmp_auth_key column). На следующем чтении swap-баг делает
    # session._takeout_id = b'' (вместо None), и api.start_takeout уходит
    # в end_takeout(takeout_id=b'') -> InvokeWithTakeoutRequest(b'') ->
    # struct.error: required argument is not an integer.
    sp = tmp_path / "empty_tmp.session"
    _make_session_v8(sp, takeout_id=None, tmp_auth_value=b"")

    sess = FixedSQLiteSession(str(sp))
    try:
        # ключевая проверка: session._takeout_id должен быть None, не b''
        assert sess._takeout_id is None
    finally:
        sess.close()


def test_fixed_sqlite_session_clears_non_bytes_tmp_auth_key(tmp_path):
    # Симметрия: int в позиции tmp_auth_key — тоже аномалия, AuthKey(data=int)
    # упал бы дальше. Чистим. Проверяем приватное поле _tmp_auth_key, потому
    # что в Telethon MemorySession property tmp_auth_key.getter из-за бага
    # декораторов возвращает _auth_key, а не _tmp_auth_key.
    sp = tmp_path / "bad_tmp.session"
    _make_session_v8(sp, takeout_id=None, tmp_auth_value=12345)

    sess = FixedSQLiteSession(str(sp))
    try:
        # AuthKey(data=None) — falsy; bool(AuthKey) == bool(AuthKey._key)
        assert not sess._tmp_auth_key
    finally:
        sess.close()


@pytest.mark.asyncio
async def test_start_takeout_creates_session():
    """start_takeout should call client.takeout() and store result."""
    api = TgApi.__new__(TgApi)
    api.client = MagicMock()
    api.takeout = None
    mock_takeout_ctx = MagicMock()
    mock_takeout_client = AsyncMock()
    mock_takeout_ctx.__aenter__ = AsyncMock(return_value=mock_takeout_client)
    mock_takeout_ctx.__aexit__ = AsyncMock(return_value=False)
    api.client.takeout.return_value = mock_takeout_ctx

    await api.start_takeout()
    api.client.takeout.assert_called_once()
    assert api.takeout is mock_takeout_client


@pytest.mark.asyncio
async def test_start_takeout_clears_stale_takeout_id_first():
    """Stale session.takeout_id should be ended (or cleared) before new takeout.

    Why: Telethon's TakeoutClient.__aenter__ raises ValueError ("Can't send a
    takeout request while another takeout for the current session still not
    been finished yet.") without contacting the server when takeout_id is
    non-None. We must finish/clear it first.
    """
    api = TgApi.__new__(TgApi)
    api.client = MagicMock()
    api.takeout = None
    api.client.session = MagicMock()
    api.client.session.takeout_id = 12345
    api.client.end_takeout = AsyncMock(return_value=True)
    mock_takeout_ctx = MagicMock()
    mock_takeout_client = AsyncMock()
    mock_takeout_ctx.__aenter__ = AsyncMock(return_value=mock_takeout_client)
    api.client.takeout.return_value = mock_takeout_ctx

    await api.start_takeout()

    api.client.end_takeout.assert_awaited_once_with(success=False)
    api.client.takeout.assert_called_once()
    assert api.takeout is mock_takeout_client


@pytest.mark.asyncio
async def test_start_takeout_clears_stale_id_locally_when_end_fails():
    """If end_takeout raises (e.g. server already forgot the takeout), wipe
    takeout_id locally and proceed."""
    api = TgApi.__new__(TgApi)
    api.client = MagicMock()
    api.takeout = None
    api.client.session = MagicMock()
    api.client.session.takeout_id = 999
    api.client.end_takeout = AsyncMock(side_effect=RuntimeError("server says no"))
    mock_takeout_ctx = MagicMock()
    mock_takeout_client = AsyncMock()
    mock_takeout_ctx.__aenter__ = AsyncMock(return_value=mock_takeout_client)
    api.client.takeout.return_value = mock_takeout_ctx

    await api.start_takeout()

    assert api.client.session.takeout_id is None
    api.client.takeout.assert_called_once()


@pytest.mark.asyncio
async def test_start_takeout_handles_delay():
    """On TAKEOUT_INIT_DELAY should raise with wait time."""
    api = TgApi.__new__(TgApi)
    api.client = MagicMock()
    api.takeout = None
    err = TakeoutInitDelayError(request=None, capture=0)
    err.seconds = 3600
    api.client.takeout.side_effect = err

    with pytest.raises(TakeoutInitDelayError):
        await api.start_takeout()


@pytest.mark.asyncio
async def test_iter_messages_passes_min_id():
    """iter_messages should pass min_id to Telethon."""
    api = TgApi.__new__(TgApi)
    api.takeout = AsyncMock()
    api.takeout.iter_messages = MagicMock(
        return_value=AsyncMock(
            __aiter__=lambda s: s,
            __anext__=AsyncMock(side_effect=StopAsyncIteration),
        )
    )

    async for _ in api.iter_messages(chat_id=123, min_id=500):
        pass

    api.takeout.iter_messages.assert_called_once_with(123, min_id=500)


@pytest.mark.asyncio
async def test_fallback_to_client_when_no_takeout():
    """Without Takeout should use client directly."""
    api = TgApi.__new__(TgApi)
    api.takeout = None
    api.client = AsyncMock()
    api.client.iter_messages = MagicMock(
        return_value=AsyncMock(
            __aiter__=lambda s: s,
            __anext__=AsyncMock(side_effect=StopAsyncIteration),
        )
    )

    async for _ in api.iter_messages(chat_id=123, min_id=0):
        pass

    api.client.iter_messages.assert_called_once_with(123, min_id=0)


@pytest.mark.asyncio
async def test_get_folders_with_dialog_filters_object():
    """get_folders should handle DialogFilters object with .filters attribute."""
    api = TgApi.__new__(TgApi)
    api.client = AsyncMock()

    # Telethon returns DialogFilters with .filters, not iterable directly
    mock_filter = MagicMock()
    mock_title = MagicMock()
    mock_title.text = "Work"
    mock_filter.title = mock_title
    mock_peer = MagicMock()
    mock_peer.user_id = 123
    del mock_peer.channel_id
    del mock_peer.chat_id
    mock_filter.include_peers = [mock_peer]

    mock_result = MagicMock()
    mock_result.filters = [mock_filter]
    api.client.return_value = mock_result

    folders = await api.get_folders()
    names = [f["name"] for f in folders]
    assert "Work" in names
    work = [f for f in folders if f["name"] == "Work"][0]
    assert 123 in work["peer_ids"]


@pytest.mark.asyncio
async def test_get_folders_with_text_with_entities_title():
    """get_folders should extract .text from TextWithEntities title."""
    api = TgApi.__new__(TgApi)
    api.client = AsyncMock()

    mock_filter = MagicMock()
    # title is TextWithEntities with .text attribute
    mock_title = MagicMock()
    mock_title.text = "Test Folder"
    mock_filter.title = mock_title
    mock_filter.include_peers = []

    mock_result = MagicMock()
    mock_result.filters = [mock_filter]
    api.client.return_value = mock_result

    folders = await api.get_folders()
    names = [f["name"] for f in folders]
    assert "Test Folder" in names


@pytest.mark.asyncio
async def test_get_folders_with_plain_string_title():
    """get_folders should handle plain string title (older Telethon)."""
    api = TgApi.__new__(TgApi)
    api.client = AsyncMock()

    mock_filter = MagicMock()
    mock_filter.title = "News"  # plain string, no .text
    mock_filter.include_peers = []

    mock_result = MagicMock()
    mock_result.filters = [mock_filter]
    api.client.return_value = mock_result

    folders = await api.get_folders()
    names = [f["name"] for f in folders]
    assert "News" in names
