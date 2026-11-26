"""Проверка миграции каталога медиа из legacy SQLite и связей v2."""

from __future__ import annotations

import contextlib
import sqlite3
import stat

import pytest

import tg_export.state as state_module
from tg_export.state import SCHEMA_SQL, SCHEMA_VERSION, ExportState


def _legacy_db(path) -> None:
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.execute("PRAGMA user_version = 1")
        conn.execute("INSERT INTO messages (chat_id, msg_id, text) VALUES (7, 11, 'old message')")
        conn.execute(
            "INSERT INTO files (file_id, chat_id, msg_id, expected_size, local_path, status) "
            "VALUES (99, 7, 11, 12345, 'legacy/photo.jpg', 'done')"
        )
        conn.execute(
            "INSERT INTO export_state (chat_id, last_msg_id, oldest_msg_id, full_history) "
            "VALUES (7, 11, 1, 1)"
        )
        conn.commit()


async def _scalar(state: ExportState, sql: str, args: tuple = ()):
    async with state.db.execute(sql, args) as cur:
        row = await cur.fetchone()
    assert row is not None
    return row[0]


@pytest.mark.asyncio
async def test_v1_upgrade_preserves_legacy_rows_and_does_not_guess_original(tmp_path):
    """Старые files остаются legacy; новая схема создаётся пустой и повторяемо."""
    db_path = tmp_path / "old.db"
    _legacy_db(db_path)
    for _ in range(2):
        async with ExportState(db_path) as state:
            assert await _scalar(state, "PRAGMA user_version") == SCHEMA_VERSION == 2
            assert await _scalar(state, "PRAGMA foreign_keys") == 1
            assert (
                await _scalar(state, "SELECT text FROM messages WHERE chat_id=7 AND msg_id=11")
                == "old message"
            )
            assert (
                await _scalar(state, "SELECT local_path FROM files WHERE file_id=99 AND chat_id=7")
                == "legacy/photo.jpg"
            )
            assert await _scalar(state, "SELECT full_history FROM export_state WHERE chat_id=7") == 1
            for table in (
                "media_assets",
                "media_sources",
                "media_variants",
                "media_requests",
                "media_jobs",
                "message_raw",
            ):
                assert await _scalar(state, f"SELECT count(*) FROM {table}") == 0
            async with state.db.execute("PRAGMA foreign_key_check") as cur:
                assert await cur.fetchall() == []
    backup = tmp_path / "old.db.pre-v2.bak"
    assert backup.is_file()
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    with contextlib.closing(sqlite3.connect(backup)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            conn.execute("SELECT local_path FROM files WHERE file_id=99").fetchone()[0] == "legacy/photo.jpg"
        )
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='media_assets'").fetchone() is None
    assert not (tmp_path / "old.db.pre-v2.bak.1").exists()


@pytest.mark.asyncio
async def test_migration_backup_includes_committed_wal_rows(tmp_path):
    """SQLite backup захватывает старые записи, ещё лежащие в WAL."""
    db_path = tmp_path / "wal.db"
    _legacy_db(db_path)
    with contextlib.closing(sqlite3.connect(db_path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO messages(chat_id,msg_id,text) VALUES (7,12,'in WAL')")
        writer.commit()
        assert (tmp_path / "wal.db-wal").is_file()
        async with ExportState(db_path) as state:
            assert await _scalar(state, "SELECT text FROM messages WHERE chat_id=7 AND msg_id=12") == "in WAL"
        with contextlib.closing(sqlite3.connect(tmp_path / "wal.db.pre-v2.bak")) as backup:
            assert backup.execute("SELECT text FROM messages WHERE msg_id=12").fetchone()[0] == "in WAL"


@pytest.mark.asyncio
async def test_failed_migration_rolls_back_all_new_tables_and_retries(tmp_path, monkeypatch):
    """Сбой в конце DDL не оставляет таблиц v2; следующий запуск успешен."""
    db_path = tmp_path / "old.db"
    _legacy_db(db_path)
    original = state_module.MEDIA_SCHEMA_SQL
    monkeypatch.setattr(state_module, "MEDIA_SCHEMA_SQL", original + "CREATE TABLE broken (; ")
    with pytest.raises(sqlite3.OperationalError):
        async with ExportState(db_path):
            pass

    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            conn.execute("SELECT text FROM messages WHERE chat_id=7 AND msg_id=11").fetchone()[0]
            == "old message"
        )
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='media_assets'").fetchone() is None
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='media_jobs'").fetchone() is None

    monkeypatch.setattr(state_module, "MEDIA_SCHEMA_SQL", original)
    async with ExportState(db_path) as state:
        assert await _scalar(state, "PRAGMA user_version") == 2
        assert await _scalar(state, "SELECT count(*) FROM media_assets") == 0


@pytest.mark.asyncio
async def test_unsupported_legacy_message_key_fails_before_media_ddl(tmp_path):
    """Не строим FK поверх старой таблицы без составного ключа сообщения."""
    db_path = tmp_path / "broken-old.db"
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE messages(chat_id INTEGER, msg_id INTEGER)")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    with pytest.raises(RuntimeError, match="unsupported legacy messages primary key"):
        async with ExportState(db_path):
            pass
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='media_assets'").fetchone() is None
    assert (tmp_path / "broken-old.db.pre-v2.bak").is_file()


@pytest.mark.asyncio
async def test_v2_missing_table_is_reported_without_recreating_it(tmp_path):
    """Версия 2 с потерянной таблицей не маскирует повреждение автоматическим DDL."""
    db_path = tmp_path / "damaged.db"
    async with ExportState(db_path):
        pass
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        conn.execute("DROP TABLE media_jobs")
        conn.commit()

    with pytest.raises(RuntimeError, match="incomplete media schema v2.*media_jobs"):
        async with ExportState(db_path):
            pass
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='media_jobs'").fetchone() is None


@pytest.mark.asyncio
async def test_raw_payload_and_failure_are_mutually_exclusive(tmp_path):
    """Для saved Raw допустим один payload, для сбоя — только код ошибки."""
    async with ExportState(tmp_path / "new.db") as state:
        await state.db.execute("INSERT INTO messages(chat_id,msg_id) VALUES (7,11)")
        await state.commit()
        prefix = (
            "INSERT INTO message_raw(account_id,chat_id,msg_id,captured_at,"
            "telethon_version,encoding_version,raw_status,raw_json,raw_blob,error_code) "
            "VALUES (42,7,?,'now','1.0',1,?,?,?,?)"
        )
        for values in (
            ("saved", None, None, None),
            ("saved", "{}", b"raw", None),
            ("saved", "{}", None, "unexpected"),
            ("raw_failed", "{}", None, "encode_error"),
            ("raw_failed", None, None, None),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                await state.db.execute(prefix, (11, *values))
        await state.db.rollback()
        await state.db.execute(prefix, (11, "raw_failed", None, None, "encode_error"))
        await state.db.execute("INSERT INTO messages(chat_id,msg_id) VALUES (7,12)")
        await state.db.execute(prefix, (12, "saved", None, b"raw", None))
        await state.commit()
        assert await _scalar(state, "SELECT error_code FROM message_raw WHERE msg_id=11") == "encode_error"
        assert await _scalar(state, "SELECT raw_blob FROM message_raw WHERE msg_id=12") == b"raw"


@pytest.mark.asyncio
async def test_asset_source_variant_request_job_relationships_and_atomic_write(tmp_path):
    """Один коммит связывает пять сущностей; откат не оставляет частичного графа."""
    async with ExportState(tmp_path / "new.db") as state:
        statements = (
            ("INSERT INTO messages(chat_id,msg_id) VALUES (7,11)", ()),
            (
                "INSERT INTO media_assets(asset_id,account_id,asset_kind,telegram_id,captured_at) "
                "VALUES (1,42,'document',99,'2026-01-01')",
                (),
            ),
            (
                "INSERT INTO media_sources(source_id,asset_id,account_id,chat_id,msg_id,"
                "source_kind,source_locator,observed_at) "
                "VALUES (1,1,42,7,11,'message',?, '2026-01-01')",
                ('{"peer_id":7,"msg_id":11}',),
            ),
            (
                "INSERT INTO media_variants(variant_id,asset_id,variant_key,role,thumb_type) "
                "VALUES (5,1,'static:m:160x160','static','m')",
                (),
            ),
            (
                "INSERT INTO media_requests(request_id,asset_id,variant_id,desired_role,requested_by,"
                "policy_digest,dedupe_key,result,requested_at,updated_at) "
                "VALUES (8,1,5,'thumbnail','policy','digest','one','selected','2026-01-01','2026-01-01')",
                (),
            ),
            (
                "INSERT INTO media_jobs(variant_id,updated_at) VALUES (5,'2026-01-01')",
                (),
            ),
        )
        for sql, args in statements:
            await state.db.execute(sql, args)
        await state.db.rollback()
        for table in (
            "messages",
            "media_assets",
            "media_sources",
            "media_variants",
            "media_requests",
            "media_jobs",
        ):
            assert await _scalar(state, f"SELECT count(*) FROM {table}") == 0

        for sql, args in statements:
            await state.db.execute(sql, args)
        await state.commit()
        assert await _scalar(state, "SELECT count(*) FROM media_jobs") == 1
        assert await _scalar(state, "SELECT count(*) FROM media_requests") == 1

        # A second intent can refer to the same physical variant and job.
        await state.db.execute(
            "INSERT INTO media_requests(asset_id,variant_id,desired_role,requested_by,policy_digest,"
            "dedupe_key,result,requested_at,updated_at) "
            "VALUES (1,5,'preview','explicit_fetch','digest','two','selected','2026-01-01','2026-01-01')"
        )
        await state.commit()
        assert await _scalar(state, "SELECT count(*) FROM media_requests WHERE variant_id=5") == 2
        assert await _scalar(state, "SELECT count(*) FROM media_jobs WHERE variant_id=5") == 1


@pytest.mark.asyncio
async def test_composite_foreign_keys_block_cross_account_and_cross_asset_links(tmp_path):
    """БД отвергает несовпадающие account/asset/variant и висящие задачи."""
    async with ExportState(tmp_path / "new.db") as state:
        await state.db.execute("INSERT INTO messages(chat_id,msg_id) VALUES (7,11)")
        await state.db.execute(
            "INSERT INTO media_assets(asset_id,account_id,asset_kind,telegram_id,captured_at) "
            "VALUES (1,42,'photo',99,'now'), (2,43,'photo',99,'now')"
        )
        await state.db.execute(
            "INSERT INTO media_variants(variant_id,asset_id,variant_key,role) "
            "VALUES (5,1,'static:m','static')"
        )
        await state.commit()

        invalid = (
            (
                "INSERT INTO media_sources(asset_id,account_id,chat_id,msg_id,"
                "source_kind,source_locator,observed_at) "
                "VALUES (1,43,7,11,'message','{}','now')"
            ),
            (
                "INSERT INTO media_sources(asset_id,account_id,chat_id,msg_id,"
                "source_kind,source_locator,observed_at) "
                "VALUES (1,42,7,12,'message','{}','now')"
            ),
            (
                "INSERT INTO media_requests(asset_id,variant_id,desired_role,requested_by,policy_digest,"
                "dedupe_key,result,requested_at,updated_at) "
                "VALUES (2,5,'thumbnail','policy','d','wrong-asset','selected','now','now')"
            ),
            (
                "INSERT INTO media_requests(asset_id,variant_id,desired_role,requested_by,policy_digest,"
                "dedupe_key,result,requested_at,updated_at) "
                "VALUES (1,NULL,'thumbnail','policy','d','missing-variant','selected','now','now')"
            ),
            ("INSERT INTO media_jobs(variant_id,updated_at) VALUES (999,'now')"),
        )
        for sql in invalid:
            with pytest.raises(sqlite3.IntegrityError):
                await state.db.execute(sql)
        await state.db.rollback()

        await state.db.execute(
            "INSERT INTO media_requests(asset_id,variant_id,desired_role,requested_by,policy_digest,"
            "dedupe_key,result,requested_at,updated_at) "
            "VALUES (1,NULL,'thumbnail','policy','d','none','no_thumbnail','now','now')"
        )
        await state.db.execute(
            "INSERT INTO media_sources(asset_id,account_id,chat_id,msg_id,"
            "source_kind,source_locator,observed_at) "
            "VALUES (1,42,7,11,'message','{}','now')"
        )
        await state.commit()
        await state.db.execute("DELETE FROM messages WHERE chat_id=7 AND msg_id=11")
        await state.commit()
        assert await _scalar(state, "SELECT count(*) FROM media_sources") == 0
        assert await _scalar(state, "SELECT count(*) FROM media_assets") == 2
        assert await _scalar(state, "SELECT count(*) FROM media_requests WHERE result='no_thumbnail'") == 1
