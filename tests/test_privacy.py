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
from unittest.mock import MagicMock

import click
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
    from tg_export.cli.auth import auth_credentials

    option = next(p for p in auth_credentials.params if p.name == "api_hash")
    assert isinstance(option, click.Option)
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
    from tg_export.media import _SiblingReaders

    db_path = tmp_path / "why?not.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE files (file_id integer, local_path text, status text)")
    conn.execute("INSERT INTO files VALUES (7, '/tmp/x.jpg', 'done')")
    conn.commit()
    conn.close()

    readers = _SiblingReaders()
    try:
        assert readers.lookup(db_path, 7) == "/tmp/x.jpg"
    finally:
        readers.close()


@pytest.fixture
def relaxed_umask():
    """Umask, при котором файл создаётся доступным на чтение всем.

    Иначе проверка «создан приватным» проходит на строгом umask разработчика и
    ничего не доказывает.
    """
    previous = os.umask(0o022)
    yield
    os.umask(previous)


def _no_second_step(*args, **kwargs):
    """Заглушка ужатия прав постфактум: проверяется режим при создании."""


def test_the_credentials_file_is_private_from_the_moment_it_appears(tmp_path, monkeypatch, relaxed_umask):
    """api_hash даёт доступ к API от имени пользователя.

    Между созданием файла и `chmod` он доступен другим локальным пользователям
    на чтение, а дескриптор, открытый в этом окне, сохраняет доступ и после
    ужатия прав.
    """
    mgr = AccountManager(config_dir=tmp_path / "cfg")
    mgr.ensure_dirs()
    monkeypatch.setattr(os, "chmod", _no_second_step)

    mgr.save_credentials(1, "hash")

    assert _mode(tmp_path / "cfg" / "api_credentials.yaml") == 0o600


def test_the_session_file_is_private_from_the_moment_it_appears(tmp_path, monkeypatch, relaxed_umask):
    """Ключ авторизации из файла сессии даёт вход в аккаунт без второго фактора."""
    from tg_export.session import FixedSQLiteSession

    monkeypatch.setattr("tg_export.session.restrict_file", _no_second_step)

    sp = tmp_path / "acc.session"
    sess = FixedSQLiteSession(str(sp))
    try:
        assert _mode(sp) == 0o600
    finally:
        sess.close()


@pytest.mark.asyncio
async def test_the_state_database_is_private_from_the_moment_it_appears(tmp_path, monkeypatch, relaxed_umask):
    """В базе состояния лежат тексты всех сообщений, телефоны и адреса сессий."""
    from tg_export.state import ExportState

    monkeypatch.setattr("tg_export.state.restrict_file", _no_second_step)

    db_path = tmp_path / "state.db"
    async with ExportState(db_path):
        assert _mode(db_path) == 0o600


def test_a_refused_chmod_is_reported(tmp_path, caplog):
    """Молча проглоченный отказ оставляет секрет открытым и без следа об этом."""
    from tg_export.privacy import restrict_file

    target = tmp_path / "secret"
    target.write_text("x", encoding="utf-8")

    with caplog.at_level("WARNING"), pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "chmod", MagicMock(side_effect=OSError("read-only filesystem")))
        restrict_file(target)

    assert any("read-only filesystem" in r.getMessage() for r in caplog.records), caplog.text


def test_the_account_config_is_created_private(tmp_path, relaxed_umask):
    """`init` пишет в конфиг по строке на каждый чат аккаунта.

    Это идентификаторы, имена и число сообщений всех контактов, групп и
    каналов -- те же данные, ради которых закрыты база состояния и выгрузка.
    """
    import yaml
    from click.testing import CliRunner

    from tg_export.cli import main

    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        yaml.dump({"unfiled": [{"id": 20, "name": "Notes", "type": "self", "messages": 1}]}),
        encoding="utf-8",
    )
    out = tmp_path / "acc.yaml"

    result = CliRunner().invoke(
        main, ["init", "--account", "acc", "--from-catalog", str(catalog), "--output", str(out)]
    )

    assert result.exit_code == 0, result.output
    assert _mode(out) == 0o600


def test_a_loose_account_config_is_tightened_when_it_is_read(tmp_path, relaxed_umask, caplog):
    """Конфиг, написанный руками или доставшийся от прежней версии, ужимается при чтении.

    Глобальный `config.yaml` уже получает такое обращение; конфиг аккаунта
    несёт список всех чатов и до сих пор оставался с правами по umask.
    """
    from tg_export.config import load_config

    path = tmp_path / "acc.yaml"
    path.write_text("output:\n  path: ./out\n", encoding="utf-8")
    os.chmod(path, 0o644)

    with caplog.at_level("WARNING"):
        load_config(path)

    assert _mode(path) == 0o600
