import contextlib
import sqlite3
from datetime import datetime

import pytest

from tg_export.models import (
    FileInfo,
    MediaType,
    Message,
    PhotoMedia,
    TextPart,
    TextType,
)


def _make_msg(msg_id=1, chat_id=123, text="Hello", from_id=100, from_name="Test", media=None, date=None):
    return Message(
        id=msg_id,
        chat_id=chat_id,
        date=date or datetime(2024, 1, 1),
        edited=None,
        from_id=from_id,
        from_name=from_name,
        text=[TextPart(type=TextType.text, text=text)] if text else [],
        media=media,
        action=None,
        reply_to_msg_id=None,
        reply_to_peer_id=None,
        forwarded_from=None,
        reactions=[],
        is_outgoing=False,
        signature=None,
        via_bot_id=None,
        saved_from_chat_id=None,
        inline_buttons=None,
        topic_id=None,
        grouped_id=None,
    )


@pytest.mark.asyncio
async def test_export_state_roundtrip(state):
    await state.set_last_msg_id(chat_id=123, msg_id=456)
    result = await state.get_last_msg_id(chat_id=123)
    assert result == 456


@pytest.mark.asyncio
async def test_export_state_returns_none_for_unknown_chat(state):
    result = await state.get_last_msg_id(chat_id=999)
    assert result is None


@pytest.mark.asyncio
async def test_set_oldest_msg_id_creates_record_when_missing(state):
    # Регрессия: ранее INSERT-ветка set_oldest_msg_id не указывала last_msg_id
    # и падала с NOT NULL constraint failed: export_state.last_msg_id.
    await state.set_oldest_msg_id(chat_id=999, msg_id=100)

    chat_state = await state.get_chat_state(chat_id=999)
    assert chat_state is not None
    assert chat_state["oldest_msg_id"] == 100
    assert chat_state["last_msg_id"] == 0


@pytest.mark.asyncio
async def test_set_full_history_creates_record_when_missing(state):
    await state.set_full_history(chat_id=999)

    chat_state = await state.get_chat_state(chat_id=999)
    assert chat_state is not None
    assert chat_state["full_history"] == 1
    assert chat_state["last_msg_id"] == 0


@pytest.mark.asyncio
async def test_update_messages_count_creates_record_when_missing(state):
    await state.update_messages_count(chat_id=999, count=42)

    chat_state = await state.get_chat_state(chat_id=999)
    assert chat_state is not None
    assert chat_state["messages_count"] == 42
    assert chat_state["last_msg_id"] == 0


@pytest.mark.asyncio
async def test_commit_phase_progress_creates_record(state):
    await state.commit_phase_progress(
        chat_id=999,
        last_msg_id=500,
        oldest_msg_id=100,
        full_history=True,
        messages_count=42,
    )

    chat_state = await state.get_chat_state(chat_id=999)
    assert chat_state is not None
    assert chat_state["last_msg_id"] == 500
    assert chat_state["oldest_msg_id"] == 100
    assert chat_state["full_history"] == 1
    assert chat_state["messages_count"] == 42


@pytest.mark.asyncio
async def test_commit_phase_progress_updates_existing_record(state):
    await state.set_last_msg_id(chat_id=999, msg_id=100)
    await state.commit_phase_progress(
        chat_id=999,
        last_msg_id=500,
        oldest_msg_id=50,
        full_history=False,
        messages_count=10,
    )

    chat_state = await state.get_chat_state(chat_id=999)
    assert chat_state["last_msg_id"] == 500
    assert chat_state["oldest_msg_id"] == 50
    assert chat_state["full_history"] == 0
    assert chat_state["messages_count"] == 10


@pytest.mark.asyncio
async def test_file_registration(state):
    await state.register_file(
        file_id=100,
        chat_id=123,
        msg_id=1,
        expected_size=5000,
        actual_size=5000,
        local_path="photos/photo.jpg",
        status="done",
    )
    info = await state.get_file(file_id=100, chat_id=123)
    assert info["expected_size"] == 5000
    assert info["status"] == "done"


@pytest.mark.asyncio
async def test_message_store_and_load(state):
    msg = _make_msg(msg_id=1, chat_id=123, text="Привет мир", from_name="Иван")
    await state.store_message(msg)
    messages = await state.load_messages(chat_id=123)
    assert len(messages) == 1
    assert messages[0].from_name == "Иван"
    assert messages[0].text[0].text == "Привет мир"


@pytest.mark.asyncio
async def test_message_store_multiple_and_order(state):
    await state.store_message(_make_msg(msg_id=2, text="Second"))
    await state.store_message(_make_msg(msg_id=1, text="First"))
    messages = await state.load_messages(chat_id=123)
    assert len(messages) == 2
    assert messages[0].text[0].text == "First"
    assert messages[1].text[0].text == "Second"


@pytest.mark.asyncio
async def test_message_with_media_roundtrip(state):
    media = PhotoMedia(
        type=MediaType.photo,
        file=FileInfo(id=1, size=1000, name="p.jpg", mime_type="image/jpeg", local_path=None),
        width=800,
        height=600,
    )
    msg = _make_msg(msg_id=1, media=media, text="")
    await state.store_message(msg)
    loaded = (await state.load_messages(chat_id=123))[0]
    assert isinstance(loaded.media, PhotoMedia)
    assert loaded.media.width == 800
    assert loaded.media.file is not None
    assert loaded.media.file.name == "p.jpg"


@pytest.mark.asyncio
async def test_search_by_text(state):
    await state.store_message(_make_msg(msg_id=1, text="Привет"))
    await state.store_message(_make_msg(msg_id=2, text="Мир"))
    await state.store_message(_make_msg(msg_id=3, text="Привет мир"))
    results = await state.search_messages(chat_id=123, text_query="Привет")
    assert len(results) == 2


@pytest.mark.asyncio
async def test_search_by_media_type(state):
    media = PhotoMedia(
        type=MediaType.photo,
        file=FileInfo(id=1, size=1000, name="p.jpg", mime_type="image/jpeg", local_path=None),
        width=800,
        height=600,
    )
    await state.store_message(_make_msg(msg_id=1, media=media, text=""))
    await state.store_message(_make_msg(msg_id=2, text="no media"))
    results = await state.search_messages(chat_id=123, media_type="photo")
    assert len(results) == 1
    assert results[0].id == 1


@pytest.mark.asyncio
async def test_search_by_from_id(state):
    await state.store_message(_make_msg(msg_id=1, from_id=100, text="A"))
    await state.store_message(_make_msg(msg_id=2, from_id=200, text="B"))
    results = await state.search_messages(chat_id=123, from_id=200)
    assert len(results) == 1
    assert results[0].text[0].text == "B"


@pytest.mark.asyncio
async def test_state_lock_prevents_second_open(tmp_path):
    from tg_export.state import ExportState, StateLockError

    db = tmp_path / "lock_test.db"
    s1 = ExportState(db_path=db)
    await s1.open()
    s2 = ExportState(db_path=db)
    try:
        with pytest.raises(StateLockError):
            await s2.open()
    finally:
        await s1.close()
    # После close() второй экземпляр должен открыться без ошибки.
    s3 = ExportState(db_path=db)
    await s3.open()
    await s3.close()


@pytest.mark.asyncio
async def test_verify_files_finds_partial(state):
    await state.register_file(
        file_id=100,
        chat_id=123,
        msg_id=1,
        expected_size=5000,
        actual_size=3000,
        local_path="photos/photo.jpg",
        status="partial",
    )
    broken = await state.get_files_to_verify()
    assert len(broken) == 1
    assert broken[0]["file_id"] == 100


@pytest.mark.asyncio
async def test_lock_is_not_released_by_removing_its_file(tmp_path):
    """flock привязан к inode, а не к имени: удаляя файл блокировки при
    освобождении, процесс открывает дорогу второму экземпляру.

    Последовательность: A держит блокировку, B успевает открыть тот же файл,
    A завершается и удаляет файл, B берёт блокировку на осиротевшем inode, а C
    создаёт новый файл и берёт блокировку на нём. B и C работают с одной базой
    состояния: взаимно удаляют файлы при уборке и затирают указатели.
    """
    import os

    import pytest as _pytest

    from tg_export.state import ExportState, StateLockError

    fcntl = _pytest.importorskip("fcntl")

    db = tmp_path / "orphan_lock.db"
    a = ExportState(db_path=db)
    await a.open()
    lock_path = a._lock.path
    assert lock_path is not None

    fd_b = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        await a.close()
        fcntl.flock(fd_b, fcntl.LOCK_EX | fcntl.LOCK_NB)

        c = ExportState(db_path=db)
        with _pytest.raises(StateLockError):
            await c.open()
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd_b, fcntl.LOCK_UN)
        os.close(fd_b)


@pytest.mark.asyncio
async def test_message_with_reactions_roundtrip(state):
    """Реакции переживают запись и чтение по-колоночным путём.

    Раньше сохранность реакций проверялась через Message.to_json/from_json --
    вторую, не используемую в продукте сериализацию.
    """
    from tg_export.models import Reaction, ReactionType

    msg = _make_msg(msg_id=1, text="hi")
    msg.reactions = [
        Reaction(type=ReactionType.emoji, emoji="\U0001f44d", document_id=None, count=5, recent=None)
    ]
    await state.store_message(msg)

    loaded = (await state.load_messages(chat_id=123))[0]

    assert len(loaded.reactions) == 1
    assert loaded.reactions[0].emoji == "\U0001f44d"
    assert loaded.reactions[0].count == 5


@pytest.mark.asyncio
async def test_unused_tables_and_indexes_are_dropped(tmp_path):
    """Таблицы takeout, users_cache и meta и два индекса создавались при каждом
    открытии, но ничем не читались; индексы при этом оплачивались на каждой
    записи. Базы прежних версий тоже приводятся в порядок."""
    from tg_export.state import ExportState

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE takeout (account TEXT PRIMARY KEY, takeout_id INTEGER, created_at TIMESTAMP);"
        "CREATE TABLE users_cache (user_id INTEGER PRIMARY KEY, display_name TEXT NOT NULL);"
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
    )
    conn.commit()
    conn.close()

    state = ExportState(db)
    await state.open()
    try:
        async with state.db.execute("SELECT name FROM sqlite_master") as cur:
            names = {row[0] for row in await cur.fetchall()}
    finally:
        await state.close()

    assert not names & {"takeout", "users_cache", "meta"}, names
    assert not names & {"idx_messages_grouped", "idx_files_local_path"}, names


@pytest.mark.asyncio
async def test_state_is_an_async_context_manager(tmp_path):
    """Выход из `async with` закрывает БД и отпускает блокировку и по исключению."""
    from tg_export.state import ExportState

    state = ExportState(db_path=tmp_path / "ctx.db")

    with pytest.raises(RuntimeError):
        async with state as entered:
            assert entered is state
            await entered.get_chat_state(1)
            raise RuntimeError("boom")

    assert state._db is None
    assert not state._lock.held


@pytest.mark.asyncio
async def test_chat_row_counts_cover_every_table_purge_deletes(tmp_path):
    """Предпросмотр purge и само удаление обязаны смотреть на один список таблиц.

    Кортеж таблиц был записан дважды -- в `purge_chat` и в команде CLI, которая
    показывает, что будет удалено. Правка схемы в одном месте давала purge,
    удаляющий не то, о чём предупредил.
    """
    from tg_export.state import CHAT_TABLES, ExportState

    async with ExportState(db_path=tmp_path / "counts.db") as state:
        counts = await state.count_chat_rows(42)
        assert set(counts) == set(CHAT_TABLES)
        assert all(v == 0 for v in counts.values()), counts


@pytest.mark.asyncio
async def test_list_chat_states_returns_progress_with_message_counts(tmp_path):
    """`state show` без chat_id читает сводку по всем чатам через слой состояния."""
    from tg_export.state import ExportState

    async with ExportState(db_path=tmp_path / "list.db") as state:
        await state.set_last_msg_id(7, 10)
        await state.set_full_history(7, True)
        rows = await state.list_chat_states()

    assert len(rows) == 1
    row = dict(rows[0])
    assert row["chat_id"] == 7
    assert row["last_msg_id"] == 10
    assert row["msg_count"] == 0


@pytest.mark.asyncio
async def test_a_database_from_an_older_version_gets_the_new_columns(tmp_path):
    """Схема создавалась через CREATE TABLE IF NOT EXISTS -- на старом файле это no-op.

    Колонка `is_archived`, добавленная в catalog_cache, на файле состояния от
    прежней версии не появлялась, и первая же запись каталога падала с
    `OperationalError: table catalog_cache has no column named is_archived` --
    уже внутри запущенного экспорта.
    """
    from tg_export.state import ExportState

    db_path = tmp_path / "old.db"
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        conn.executescript("""
            CREATE TABLE catalog_cache (
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
        """)
        conn.commit()

    async with ExportState(db_path) as state:
        await state.cache_catalog(
            chat_id=1,
            name="Chat",
            chat_type="personal",
            folder=None,
            members_count=None,
            messages_count=3,
            last_message_date=None,
            is_left=False,
            is_archived=True,
            is_forum=False,
            is_monoforum=False,
        )
        await state.commit()
        async with state.db.execute("SELECT is_archived FROM catalog_cache WHERE chat_id=1") as cur:
            row = await cur.fetchone()
    assert row is not None and row[0] == 1


@pytest.mark.asyncio
async def test_a_missing_message_column_is_added_on_open(tmp_path):
    """Догоняются любые недостающие колонки, а не только та, что уже сломала выгрузку."""
    from tg_export.state import ExportState

    db_path = tmp_path / "old.db"
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        conn.executescript("""
            CREATE TABLE messages (
                chat_id INTEGER NOT NULL,
                msg_id  INTEGER NOT NULL,
                text    TEXT,
                PRIMARY KEY (chat_id, msg_id)
            );
        """)
        conn.commit()

    async with ExportState(db_path) as state, state.db.execute("PRAGMA table_info(messages)") as cur:
        columns = {row[1] for row in await cur.fetchall()}
    assert {"grouped_id", "topic_id", "reactions"} <= columns, columns


@pytest.mark.asyncio
async def test_a_fresh_database_records_its_schema_version(tmp_path):
    """Версия схемы -- то, по чему следующая правка поймёт, что мигрировать."""
    from tg_export.state import SCHEMA_VERSION, ExportState

    async with ExportState(tmp_path / "new.db") as state, state.db.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    assert row is not None and row[0] == SCHEMA_VERSION
    assert SCHEMA_VERSION >= 1


@pytest.mark.asyncio
async def test_a_deliberately_skipped_file_is_not_offered_for_verification(tmp_path):
    """`status != 'done'` смешивал «пропущено намеренно» и «не докачано».

    Файл, пропущенный по размеру или по типу, физически не скачивался -- verify
    пытался бы его «перекачать», хотя конфигурация прямо это запретила.
    """
    from tg_export.state import ExportState

    async with ExportState(tmp_path / "state.db") as state:
        await state.register_file(
            file_id=1,
            chat_id=10,
            msg_id=1,
            expected_size=100,
            actual_size=0,
            local_path="<skipped_by_size>",
            status="skipped_by_size",
        )
        await state.register_file(
            file_id=2,
            chat_id=10,
            msg_id=2,
            expected_size=100,
            actual_size=40,
            local_path="/tmp/partial.jpg",
            status="partial",
        )
        await state.commit()

        to_verify = await state.get_files_to_verify()

    assert [row["file_id"] for row in to_verify] == [2]


@pytest.mark.asyncio
async def test_a_file_of_unknown_size_is_offered_for_verification(tmp_path):
    """Сравнение с NULL даёт NULL, и такая строка не попадала в проверку никогда."""
    from tg_export.state import ExportState

    db_path = tmp_path / "state.db"
    async with ExportState(db_path) as state:
        await state.db.execute(
            "INSERT INTO files (file_id, chat_id, msg_id, expected_size, actual_size, local_path, status) "
            "VALUES (3, 10, 3, 100, NULL, '/tmp/unknown.jpg', 'done')"
        )
        await state.commit()

        to_verify = await state.get_files_to_verify()

    assert [row["file_id"] for row in to_verify] == [3]


@pytest.mark.asyncio
async def test_the_lock_wait_is_set_explicitly(tmp_path):
    """Ожидание блокировки зависело от умолчания stdlib (5 с).

    Соседний код считает это недостаточным: чтение из соседней базы открывается
    с таймаутом 30 с, потому что пишущий процесс удерживает блокировку секундами
    на большом батче.
    """
    from tg_export.state import DB_TIMEOUT_SECONDS, ExportState

    async with ExportState(tmp_path / "state.db") as state, state.db.execute("PRAGMA busy_timeout") as cur:
        row = await cur.fetchone()

    assert row is not None and row[0] == int(DB_TIMEOUT_SECONDS * 1000)


@pytest.mark.asyncio
async def test_service_timestamps_carry_their_offset(tmp_path):
    """Служебные метки писались наивным локальным временем, даты сообщений -- со смещением.

    Одинаково объявленные колонки хранили значения двух форматов, и перенос базы
    между часовыми поясами делал их несравнимыми.
    """
    from tg_export.state import ExportState

    async with ExportState(tmp_path / "state.db") as state:
        await state.set_last_msg_id(10, 5)
        await state.commit()
        async with state.db.execute("SELECT updated_at FROM export_state WHERE chat_id=10") as cur:
            row = await cur.fetchone()

    assert row is not None
    stamp = datetime.fromisoformat(row[0])
    assert stamp.tzinfo is not None, f"метка без смещения: {row[0]}"


@pytest.mark.asyncio
async def test_message_counts_are_read_for_all_chats_at_once(tmp_path):
    """Индексная страница ходила в базу по одному-двум запросам на каждый чат.

    В соседней команде `state show` та же задача давно решена одним запросом.
    """
    from tg_export.state import ExportState

    async with ExportState(tmp_path / "state.db") as state:
        await state.store_messages_batch([_make_msg(msg_id=1, chat_id=10), _make_msg(msg_id=2, chat_id=10)])
        await state.store_messages_batch([_make_msg(msg_id=1, chat_id=20)])
        await state.update_messages_count(20, 500)
        await state.commit()

        counts = await state.message_counts()

    # Для чата 10 сохранённого счётчика нет -- считаются строки таблицы; для
    # чата 20 берётся записанное состоянием значение.
    assert counts == {10: 2, 20: 500}


@pytest.mark.asyncio
async def test_search_can_be_limited(state):
    """Стоимость поиска не должна зависеть от числа совпадений.

    Отбор идёт по `LIKE '%...%'` -- индекс тут неприменим, и каждая найденная
    строка проходит полную сборку Message с разбором JSON-полей.
    """
    for msg_id in range(1, 6):
        await state.store_message(_make_msg(msg_id=msg_id, chat_id=123, text="Привет мир"))
    await state.commit()

    results = await state.search_messages(chat_id=123, text_query="Привет", limit=2)

    assert [m.id for m in results] == [1, 2]
