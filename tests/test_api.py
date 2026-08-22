import logging
import sqlite3
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from telethon.errors import TakeoutInitDelayError
from telethon.errors.rpcerrorlist import TakeoutInvalidError

from tg_export.api import TgApi
from tg_export.session import FixedSQLiteSession


def _make_session_v8(path, takeout_id=None, tmp_auth_value=None, *, with_version=True, swapped=False):
    """Create a v8-shaped sessions table and fill it the way Telethon does.

    Telethon's `_update_session_table` writes the row positionally --
    `insert or replace into sessions values (?,?,?,?,?,?)` -- with the tuple
    `(dc_id, server_address, port, auth_key, takeout_id, tmp_auth_key)`. The
    physical column order therefore decides which column each value lands in,
    and that order differs between session files in the wild:

    * canonical (`swapped=False`) -- `..., auth_key, takeout_id, tmp_auth_key`,
      what `_create_table` produces on a freshly created file;
    * swapped (`swapped=True`) -- `..., auth_key, tmp_auth_key, takeout_id`,
      found on files whose schema grew through a different upgrade path.

    The insert here is positional too, so the fixture reproduces exactly what
    Telethon would leave on disk for the given physical layout.
    """
    tail = "tmp_auth_key blob, takeout_id integer" if swapped else "takeout_id integer, tmp_auth_key blob"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE sessions (dc_id integer primary key, server_address text,"
        f" port integer, auth_key blob, {tail});"
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
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
        (2, "localhost", 443, b"x" * 256, takeout_id, tmp_auth_value),
    )
    conn.commit()
    conn.close()


def _make_session_v7(path, takeout_id=None):
    """Create a v7-shaped sessions table: takeout_id exists, tmp_auth_key does not.

    Telethon adds tmp_auth_key only when upgrading a v7 file (`old == 7` in
    `_upgrade_database`), so before that the table has five columns and the
    takeout_id sits in the last one.
    """
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE sessions (dc_id integer primary key, server_address text,"
        " port integer, auth_key blob, takeout_id integer);"
        "CREATE TABLE entities (id integer primary key, hash integer not null,"
        " username text, phone integer, name text, date integer);"
        "CREATE TABLE sent_files (md5_digest blob, file_size integer, type integer,"
        " id integer, hash integer, primary key(md5_digest, file_size, type));"
        "CREATE TABLE update_state (id integer primary key, pts integer, qts integer,"
        " date integer, seq integer);"
        "CREATE TABLE version (version integer primary key);"
        "INSERT INTO version VALUES (7);"
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
        (2, "localhost", 443, b"x" * 256, takeout_id),
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


def test_fixed_sqlite_session_restores_takeout_id_with_swapped_columns(tmp_path):
    # Регрессия на потерю takeout_id: часть файлов сессии имеет физический
    # порядок колонок (..., auth_key, tmp_auth_key, takeout_id). Telethon пишет
    # строку позиционно, поэтому реальный takeout_id оказывается в колонке с
    # именем tmp_auth_key, а b'' -- в колонке takeout_id. Чтение по ИМЕНИ видит
    # мусор и стирает настоящее значение; читать нужно по ПОЗИЦИИ.
    sp = tmp_path / "swapped.session"
    _make_session_v8(sp, takeout_id=12345, tmp_auth_value=b"", swapped=True)

    sess = FixedSQLiteSession(str(sp))
    try:
        assert sess.takeout_id == 12345
        assert sess.auth_key is not None and sess.auth_key.key == b"x" * 256
    finally:
        sess.close()


def test_fixed_sqlite_session_handles_v7_schema(tmp_path):
    # Регрессия: на схеме version = 7 колонок пять, tmp_auth_key ещё нет.
    # Обход пропускал такой файл, Telethon доводил схему до 8 и тут же читал
    # takeout_id как tmp_key -- AuthKey(data=int) -> sha1(int) ->
    # TypeError: object supporting the buffer API required.
    sp = tmp_path / "v7.session"
    _make_session_v7(sp, takeout_id=4242)

    sess = FixedSQLiteSession(str(sp))
    try:
        assert sess.takeout_id == 4242
        assert sess.auth_key is not None and sess.auth_key.key == b"x" * 256
    finally:
        sess.close()


def test_fixed_sqlite_session_handles_v7_schema_without_takeout(tmp_path):
    # Тот же файл без takeout_id: обход не должен ничего менять и мешать
    # штатному апгрейду схемы силами Telethon.
    sp = tmp_path / "v7_clean.session"
    _make_session_v7(sp, takeout_id=None)

    sess = FixedSQLiteSession(str(sp))
    try:
        assert sess.takeout_id is None
        assert sess.auth_key is not None and sess.auth_key.key == b"x" * 256
    finally:
        sess.close()


def test_fixed_sqlite_session_round_trip_survives_reopen(tmp_path):
    # Полный цикл: записать takeout_id через сеттер, закрыть, открыть заново.
    # Без этого теста потеря значения на повторном открытии не ловится ни одной
    # из проверок выше -- все они смотрят только на первое открытие.
    sp = tmp_path / "roundtrip.session"
    _make_session_v8(sp, takeout_id=None, tmp_auth_value=None)

    sess = FixedSQLiteSession(str(sp))
    try:
        sess.takeout_id = 777
    finally:
        sess.close()

    for _ in range(3):
        sess = FixedSQLiteSession(str(sp))
        try:
            assert sess.takeout_id == 777
        finally:
            sess.close()


def test_fixed_sqlite_session_round_trip_survives_reopen_swapped(tmp_path):
    # Тот же цикл на файле с обратным порядком колонок.
    sp = tmp_path / "roundtrip_swapped.session"
    _make_session_v8(sp, takeout_id=None, tmp_auth_value=None, swapped=True)

    sess = FixedSQLiteSession(str(sp))
    try:
        sess.takeout_id = 888
    finally:
        sess.close()

    for _ in range(3):
        sess = FixedSQLiteSession(str(sp))
        try:
            assert sess.takeout_id == 888
        finally:
            sess.close()


def test_fixed_sqlite_session_recovers_after_crash_between_clear_and_restore(tmp_path):
    # Обнуление колонок и восстановление значения идут в двух разных операциях.
    # Если процесс прервётся между ними, takeout_id пропадёт безвозвратно.
    # Вызов _extract_and_clear без последующего восстановления воспроизводит
    # именно такое прерывание.
    sp = tmp_path / "crash.session"
    _make_session_v8(sp, takeout_id=555, tmp_auth_value=b"")

    FixedSQLiteSession._extract_and_clear(str(sp))

    sess = FixedSQLiteSession(str(sp))
    try:
        assert sess.takeout_id == 555
    finally:
        sess.close()


def _open_session_capturing_logs(path, caplog, level=logging.INFO):
    """Open the session and return the log records it emitted at `level`+."""
    caplog.clear()
    with caplog.at_level(level, logger="tg_export.session"):
        sess = FixedSQLiteSession(str(path))
        sess.close()
    return [r for r in caplog.records if r.name == "tg_export.session"]


def test_healthy_session_start_says_nothing(tmp_path, caplog):
    """Исправный запуск должен молчать.

    Telethon при `store_tmp_auth_key_on_disk=False` пишет в tmp_auth_key `b''`,
    поэтому признак «в строке что-то есть» истинен всегда, и сообщение о
    восстановлении печаталось на каждом запуске -- при том что восстанавливать
    было нечего.
    """
    sp = tmp_path / "healthy.session"
    _make_session_v8(sp, takeout_id=None, tmp_auth_value=b"")

    records = _open_session_capturing_logs(sp, caplog)

    assert [r.getMessage() for r in records] == []


def test_real_takeout_id_is_reported_with_its_value(tmp_path, caplog):
    """Перенос настоящего takeout_id -- единственное состояние, о котором стоит
    сообщать, и сообщение должно называть сам идентификатор."""
    sp = tmp_path / "live.session"
    _make_session_v8(sp, takeout_id=777, tmp_auth_value=b"")

    records = _open_session_capturing_logs(sp, caplog)

    messages = [r.getMessage() for r in records]
    assert len(messages) == 1, messages
    assert "777" in messages[0]
    assert not [r for r in records if r.levelno >= logging.WARNING]


def test_empty_placeholder_is_not_called_unexpected(tmp_path, caplog):
    """`b''` в позиции takeout_id -- задокументированный след ошибки Telethon,
    а не аномалия: предупреждать о нём значит называть неожиданным то, что
    модуль сам описывает как ожидаемое."""
    sp = tmp_path / "placeholder.session"
    _make_session_v8(sp, takeout_id=b"", tmp_auth_value=b"")

    records = _open_session_capturing_logs(sp, caplog, level=logging.DEBUG)

    warnings = [r.getMessage() for r in records if r.levelno >= logging.WARNING]
    assert warnings == [], warnings


def test_unusable_takeout_id_names_the_place_it_came_from(tmp_path, caplog):
    """Настоящая аномалия должна остаться предупреждением и указать, откуда
    взято значение: строка сессии или отложенная копия. Без этого разовая
    санация не отличается от повреждения, возникающего снова и снова."""
    sp = tmp_path / "broken.session"
    _make_session_v8(sp, takeout_id=b"\x01\x02", tmp_auth_value=b"")

    records = _open_session_capturing_logs(sp, caplog)

    warnings = [r.getMessage() for r in records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, warnings
    assert "sessions" in warnings[0], warnings[0]

    sess = FixedSQLiteSession(str(sp))
    try:
        assert sess.takeout_id is None
    finally:
        sess.close()


def _make_takeout_api(*, takeout_id=None):
    """Build a TgApi whose client is a mock, shaped the way start_takeout reads it."""
    api = TgApi.__new__(TgApi)
    api.client = MagicMock()
    api.takeout = None
    api._takeout_stack = None
    api.client.session = MagicMock()
    api.client.session.takeout_id = takeout_id
    api.client.end_takeout = AsyncMock(return_value=True)
    return api


def _make_takeout_ctx():
    """Return (context manager, proxy client) mimicking client.takeout()."""
    ctx = MagicMock()
    takeout_client = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=takeout_client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, takeout_client


@pytest.mark.asyncio
async def test_start_takeout_creates_session():
    """A first run initialises the takeout with the requested export parameters."""
    api = _make_takeout_api()
    ctx, takeout_client = _make_takeout_ctx()
    api.client.takeout.return_value = ctx

    await api.start_takeout(files=True)

    api.client.takeout.assert_called_once_with(finalize=False, files=True)
    assert api.takeout is takeout_client


@pytest.mark.asyncio
async def test_start_takeout_reuses_the_id_left_by_a_previous_run():
    """A stored takeout_id must be picked up, not thrown away.

    Telegram answers InitTakeoutSessionRequest with a cooldown of up to 24h,
    so every discarded id costs a day of waiting. Reuse requires calling
    takeout() without any argument: Telethon builds the init request when the
    id is empty *or* any argument is set, and refuses to send it over a live
    id.
    """
    api = _make_takeout_api(takeout_id=12345)
    ctx, takeout_client = _make_takeout_ctx()
    api.client.takeout.return_value = ctx

    await api.start_takeout(files=True, max_file_size=100)

    api.client.takeout.assert_called_once_with(finalize=False)
    api.client.end_takeout.assert_not_awaited()
    assert api.takeout is takeout_client


@pytest.mark.asyncio
async def test_start_takeout_starts_a_new_session_when_the_stored_id_is_dead():
    """Reuse is verified by a probe request: entering the context never
    contacts the server when the id is set, so a takeout the server has
    already forgotten would only surface on the first export request."""
    api = _make_takeout_api(takeout_id=999)
    dead_ctx, dead_client = _make_takeout_ctx()
    dead_client.side_effect = TakeoutInvalidError(request=None)
    fresh_ctx, fresh_client = _make_takeout_ctx()
    api.client.takeout.side_effect = [dead_ctx, fresh_ctx]

    await api.start_takeout(files=True)

    assert api.client.takeout.call_args_list == [
        call(finalize=False),
        call(finalize=False, files=True),
    ]
    api.client.end_takeout.assert_awaited_once_with(success=False)
    assert api.takeout is fresh_client


@pytest.mark.asyncio
async def test_start_takeout_clears_a_dead_id_locally_when_end_takeout_fails():
    """If the server refuses to finish the forgotten takeout, drop the id
    locally so the fresh init request is not rejected by Telethon."""
    api = _make_takeout_api(takeout_id=999)
    dead_ctx, dead_client = _make_takeout_ctx()
    dead_client.side_effect = TakeoutInvalidError(request=None)
    fresh_ctx, fresh_client = _make_takeout_ctx()
    api.client.takeout.side_effect = [dead_ctx, fresh_ctx]
    api.client.end_takeout = AsyncMock(side_effect=RuntimeError("server says no"))

    await api.start_takeout(files=True)

    assert api.client.session.takeout_id is None
    assert api.takeout is fresh_client


@pytest.mark.asyncio
async def test_stop_takeout_keeps_the_session_for_the_next_run():
    """Releasing the context must not finish the takeout: the id is the whole
    point of keeping it."""
    api = _make_takeout_api()
    ctx, _ = _make_takeout_ctx()
    api.client.takeout.return_value = ctx
    await api.start_takeout(files=True)

    await api.stop_takeout()

    ctx.__aexit__.assert_awaited_once()
    api.client.end_takeout.assert_not_awaited()
    assert api.takeout is None


@pytest.mark.asyncio
async def test_stop_takeout_finishes_the_session_when_asked():
    api = _make_takeout_api()
    ctx, _ = _make_takeout_ctx()
    api.client.takeout.return_value = ctx
    await api.start_takeout(files=True)

    await api.stop_takeout(success=True)

    api.client.end_takeout.assert_awaited_once_with(success=True)
    assert api.takeout is None


@pytest.mark.asyncio
async def test_disconnect_releases_takeout_without_finishing_it():
    api = _make_takeout_api()
    ctx, _ = _make_takeout_ctx()
    api.client.takeout.return_value = ctx
    api.client.disconnect = MagicMock(return_value=None)
    await api.start_takeout(files=True)

    await api.disconnect()

    ctx.__aexit__.assert_awaited_once()
    api.client.end_takeout.assert_not_awaited()
    assert api.takeout is None


@pytest.mark.asyncio
async def test_start_takeout_handles_delay():
    """On TAKEOUT_INIT_DELAY should raise with wait time."""
    api = _make_takeout_api()
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
