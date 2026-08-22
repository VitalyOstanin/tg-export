"""Права доступа к секретам и подстановка непроверенных значений в пути.

Файл сессии несёт ключ авторизации, выгрузка и база состояния -- тексты всех
сообщений, телефоны и адреса сессий, глобальная конфигурация -- логин и пароль
прокси. Всё это по умолчанию оказывалось доступным на чтение другим локальным
пользователям.
"""

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from tg_export.auth import AccountManager


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_session_file_is_not_readable_by_others(tmp_path):
    """Файл сессии содержит ключ авторизации: с ним чужой процесс входит в
    аккаунт без пароля и второго фактора."""
    from tg_export.session import FixedSQLiteSession

    sp = tmp_path / "acc.session"
    sess = FixedSQLiteSession(str(sp))
    try:
        assert sp.exists()
        assert _mode(sp) & 0o077 == 0, oct(_mode(sp))
    finally:
        sess.close()


@pytest.mark.asyncio
async def test_state_database_is_not_readable_by_others(tmp_path):
    """В базе состояния лежат тексты всех выгруженных сообщений."""
    from tg_export.state import ExportState

    db = tmp_path / "state.db"
    state = ExportState(db)
    await state.open()
    try:
        assert _mode(db) & 0o077 == 0, oct(_mode(db))
    finally:
        await state.close()


def test_global_config_permissions_are_tightened(tmp_path):
    """В config.yaml лежат логин и пароль прокси, а права не проверялись вовсе --
    в отличие от api_credentials.yaml."""
    mgr = AccountManager(config_dir=tmp_path)
    mgr.ensure_dirs()
    cfg = tmp_path / "config.yaml"
    cfg.write_text("proxy:\n  username: user\n  password: secret\n")
    os.chmod(cfg, 0o664)

    mgr.load_global_config()

    assert _mode(cfg) & 0o077 == 0, oct(_mode(cfg))


def test_export_directory_is_created_private(tmp_path):
    """Каталог выгрузки: HTML со всеми сообщениями и скачанные файлы."""
    from tg_export.privacy import ensure_private_dir

    target = tmp_path / "export_output"
    ensure_private_dir(target)

    assert _mode(target) & 0o077 == 0, oct(_mode(target))


def test_existing_directory_permissions_are_left_alone(tmp_path):
    """Уже созданный пользователем каталог не переопределяется: он мог быть
    открыт намеренно, например для веб-сервера."""
    from tg_export.privacy import ensure_private_dir

    target = tmp_path / "existing"
    target.mkdir(mode=0o755)
    os.chmod(target, 0o755)

    ensure_private_dir(target)

    assert _mode(target) == 0o755, oct(_mode(target))


def test_api_hash_prompt_hides_input():
    """api_hash -- секрет того же уровня, что и пароль: набранный в открытую,
    он остаётся в истории оболочки и виден в списке процессов."""
    from tg_export.cli import auth_credentials

    option = next(p for p in auth_credentials.params if p.name == "api_hash")
    assert option.hide_input is True


@pytest.mark.parametrize("name", ["../evil", "a/b", "", ".", "..", "a\\b"])
def test_account_name_is_validated(tmp_path, name):
    """Имя аккаунта подставляется в путь файла сессии без проверки."""
    from tg_export.errors import TgExportError

    mgr = AccountManager(config_dir=tmp_path)
    with pytest.raises(TgExportError):
        mgr.session_path(name)


def test_sibling_lookup_handles_a_path_with_a_question_mark(tmp_path):
    """Путь подставляется в SQLite-URI конкатенацией: `?` в имени каталога
    превращает остаток пути в параметры и снимает режим «только чтение»."""
    from tg_export.media import _lookup_file_in_db

    db_path = tmp_path / "why?not.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE files (file_id integer, local_path text, status text)")
    conn.execute("INSERT INTO files VALUES (7, '/tmp/x.jpg', 'done')")
    conn.commit()
    conn.close()

    assert _lookup_file_in_db(db_path, 7) == "/tmp/x.jpg"
