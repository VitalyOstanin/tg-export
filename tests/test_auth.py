from pathlib import Path

import pytest

from tg_export.auth import AccountManager


def test_config_dir_created(tmp_path):
    mgr = AccountManager(config_dir=tmp_path / "tg-export")
    mgr.ensure_dirs()
    assert (tmp_path / "tg-export" / "sessions").is_dir()


def test_list_accounts_empty(tmp_path):
    mgr = AccountManager(config_dir=tmp_path / "tg-export")
    mgr.ensure_dirs()
    assert mgr.list_accounts() == []


def test_session_path(tmp_path):
    mgr = AccountManager(config_dir=tmp_path / "tg-export")
    mgr.ensure_dirs()
    path = mgr.session_path("my_phone")
    assert path == tmp_path / "tg-export" / "sessions" / "my_phone.session"


def test_remove_account(tmp_path):
    mgr = AccountManager(config_dir=tmp_path / "tg-export")
    mgr.ensure_dirs()
    session_file = mgr.session_path("test_acc")
    session_file.touch()
    assert "test_acc" in mgr.list_accounts()
    mgr.remove_account("test_acc")
    assert "test_acc" not in mgr.list_accounts()


def test_resolve_account_error_message_points_to_account_default(tmp_path):
    import click

    mgr = AccountManager(config_dir=tmp_path / "tg-export")
    mgr.ensure_dirs()
    with pytest.raises(click.UsageError) as excinfo:
        mgr.resolve_account(None)
    msg = str(excinfo.value.message)
    assert "tg-export account default" in msg
    assert "tg-export auth default" not in msg


def test_load_credentials_raises_on_missing_file(tmp_path):
    from tg_export.auth import CredentialsError

    mgr = AccountManager(config_dir=tmp_path / "tg-export")
    mgr.ensure_dirs()
    with pytest.raises(CredentialsError):
        mgr.load_credentials()


def test_load_credentials_validates_types(tmp_path):
    from tg_export.auth import CredentialsError

    mgr = AccountManager(config_dir=tmp_path / "tg-export")
    mgr.ensure_dirs()
    cred_path = tmp_path / "tg-export" / "api_credentials.yaml"
    cred_path.write_text("api_id: not-an-int\napi_hash: abc\n")
    import os

    os.chmod(cred_path, 0o600)
    with pytest.raises(CredentialsError):
        mgr.load_credentials()


def test_credentials_file_permissions(tmp_path):
    mgr = AccountManager(config_dir=tmp_path / "tg-export")
    mgr.ensure_dirs()
    mgr.save_credentials(api_id=12345, api_hash="abc123")
    cred_path = tmp_path / "tg-export" / "api_credentials.yaml"
    assert cred_path.exists()
    mode = cred_path.stat().st_mode & 0o777
    assert mode == 0o600


@pytest.mark.asyncio
async def test_add_account_opens_the_session_through_the_fixed_subclass(tmp_path, monkeypatch):
    """Логин -- единственный путь записи в файл сессии вне FixedSQLiteSession.

    Передача строкового пути в TelegramClient заставляет Telethon построить
    обычный SQLiteSession, у которого takeout_id читается по имени колонки и
    потому берётся из чужой позиции. Обход подкласса делает файл сессии
    источником повторного повреждения, даже если конкретно при логине он
    создаётся с нуля.
    """
    from unittest.mock import AsyncMock, MagicMock

    from tg_export.session import FixedSQLiteSession

    mgr = AccountManager(config_dir=tmp_path)
    mgr.ensure_dirs()
    mgr.save_credentials(1, "hash")

    created = {}

    def fake_client(session, api_id, api_hash, *args, **kwargs):
        created["session"] = session
        client = MagicMock()
        client.connect = AsyncMock()
        client.is_user_authorized = AsyncMock(return_value=True)
        client.get_me = AsyncMock(return_value=MagicMock(first_name="Test", id=1))
        client.disconnect = MagicMock(return_value=None)
        return client

    monkeypatch.setattr("telethon.TelegramClient", fake_client)

    await mgr.add_account("acc")

    assert isinstance(created["session"], FixedSQLiteSession), (
        "клиент логина должен строиться на FixedSQLiteSession, а не на пути-строке"
    )
    created["session"].close()


def _global_config(tmp_path, content: str) -> AccountManager:
    cfg_dir = tmp_path / "tg-export"
    mgr = AccountManager(config_dir=cfg_dir)
    mgr.ensure_dirs()
    (cfg_dir / "config.yaml").write_text(content, encoding="utf-8")
    return mgr


def test_typo_in_the_proxy_section_is_reported_not_ignored(tmp_path):
    """Опечатка в имени секции давала прямое соединение без единого сообщения.

    Это ровно тот исход, ради предотвращения которого TgApi отказывается
    работать без python-socks: Telethon подключился бы напрямую, раскрыв
    настоящий адрес.
    """
    from tg_export.config import ConfigError

    mgr = _global_config(tmp_path, "proxi:\n  type: socks5\n  host: 127.0.0.1\n  port: 1080\n")
    with pytest.raises(ConfigError) as e:
        mgr.load_proxy()
    assert "proxi" in str(e.value)


def test_proxy_written_as_a_string_is_reported(tmp_path):
    """`proxy: socks5://...` строкой вместо словаря молча означало «прокси нет»."""
    from tg_export.config import ConfigError

    mgr = _global_config(tmp_path, "proxy: socks5://127.0.0.1:1080\n")
    with pytest.raises(ConfigError) as e:
        mgr.load_proxy()
    assert "proxy" in str(e.value)


def test_unknown_proxy_type_raises_a_domain_error(tmp_path):
    """Класс ValueError не входил в семейство ошибок tg-export -- вывод шёл трассировкой."""
    from tg_export.errors import TgExportError

    mgr = _global_config(tmp_path, "proxy:\n  type: socks9\n  host: 127.0.0.1\n  port: 1080\n")
    with pytest.raises(TgExportError):
        mgr.load_proxy()


def test_min_free_space_zero_disables_the_check(tmp_path):
    """`min_free_space: 0` -- осознанное отключение проверки свободного места.

    Значение подставлялось через `or`, поэтому ноль заменялся на 20 ГБ и
    отключить проверку было нельзя.
    """
    mgr = _global_config(tmp_path, "min_free_space: 0\n")
    assert mgr.load_min_free_space() == 0


def test_config_dir_follows_xdg_config_home(tmp_path, monkeypatch):
    """Каталог был зашит в ~/.config/tg-export мимо стандарта XDG."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("TG_EXPORT_CONFIG_DIR", raising=False)
    assert AccountManager().config_dir == tmp_path / "xdg" / "tg-export"


def test_config_dir_can_be_overridden_by_the_environment(tmp_path, monkeypatch):
    """Отдельные наборы аккаунтов (рабочий и личный) задать было нечем."""
    monkeypatch.setenv("TG_EXPORT_CONFIG_DIR", str(tmp_path / "work"))
    assert AccountManager().config_dir == tmp_path / "work"


def _login_client(sign_in_effects, monkeypatch):
    """Подставной TelegramClient, у которого sign_in отдаёт заданную последовательность."""
    from unittest.mock import AsyncMock, MagicMock

    client = MagicMock()
    client.connect = AsyncMock()
    client.is_user_authorized = AsyncMock(return_value=False)
    client.send_code_request = AsyncMock(
        return_value=MagicMock(type=MagicMock(), next_type=None, timeout=None)
    )
    client.sign_in = AsyncMock(side_effect=sign_in_effects)
    client.get_me = AsyncMock(return_value=MagicMock(first_name="Test", id=1))
    client.disconnect = MagicMock(return_value=None)
    monkeypatch.setattr("telethon.TelegramClient", lambda *a, **k: client)
    return client


@pytest.mark.asyncio
async def test_two_factor_login_is_recognised_by_type_not_by_name(tmp_path, monkeypatch):
    """Ветка 2FA выбиралась по подстроке в имени класса исключения.

    Проект уже отказался от такого разбора в TgApi.start_takeout: переименование
    класса в Telethon тихо ломает ветку, и вместо запроса пароля пользователь
    получает трассировку. Telethon экспортирует нужные типы явно.
    """
    from telethon.errors import SessionPasswordNeededError

    class SessionPasswordRequiredError(SessionPasswordNeededError):
        """Тот же тип под другим именем -- проверка по подстроке его не узнаёт."""

    mgr = AccountManager(config_dir=tmp_path)
    mgr.ensure_dirs()
    mgr.save_credentials(1, "hash")

    client = _login_client([SessionPasswordRequiredError(request=None), None], monkeypatch)
    monkeypatch.setattr("click.prompt", lambda *a, **k: "secret")

    await mgr.add_account("acc")

    assert client.sign_in.await_count == 2
    assert client.sign_in.await_args.kwargs == {"password": "secret"}


@pytest.mark.asyncio
async def test_a_wrong_password_is_retried_and_then_reported(tmp_path, monkeypatch):
    """Неверный пароль -- три попытки, после чего исключение уходит наружу."""
    from telethon.errors import PasswordHashInvalidError, SessionPasswordNeededError

    mgr = AccountManager(config_dir=tmp_path)
    mgr.ensure_dirs()
    mgr.save_credentials(1, "hash")

    client = _login_client(
        [
            SessionPasswordNeededError(request=None),
            PasswordHashInvalidError(request=None),
            PasswordHashInvalidError(request=None),
            PasswordHashInvalidError(request=None),
        ],
        monkeypatch,
    )
    monkeypatch.setattr("click.prompt", lambda *a, **k: "wrong")

    with pytest.raises(PasswordHashInvalidError):
        await mgr.add_account("acc")

    assert client.sign_in.await_count == 4


@pytest.mark.asyncio
async def test_an_unrelated_login_error_is_not_reclassified(tmp_path, monkeypatch):
    """`except Exception` с разбором по имени пропускал через ту же ветку всё подряд."""
    mgr = AccountManager(config_dir=tmp_path)
    mgr.ensure_dirs()
    mgr.save_credentials(1, "hash")

    _login_client([ValueError("phone number invalid")], monkeypatch)
    monkeypatch.setattr("click.prompt", lambda *a, **k: "123")

    with pytest.raises(ValueError):
        await mgr.add_account("acc")


@pytest.mark.asyncio
async def test_login_status_goes_to_stderr(tmp_path, monkeypatch, capsys):
    """stdout занят машиночитаемым выводом команд -- статус входа туда не пишут."""
    mgr = AccountManager(config_dir=tmp_path)
    mgr.ensure_dirs()
    mgr.save_credentials(1, "hash")

    _login_client([None], monkeypatch)
    monkeypatch.setattr("click.prompt", lambda *a, **k: "123")

    await mgr.add_account("acc")

    captured = capsys.readouterr()
    assert captured.out == "", f"в stdout попал статус входа: {captured.out!r}"
    assert "Logged in as" in captured.err


@pytest.mark.asyncio
async def test_a_failed_login_keeps_the_previous_session(tmp_path, monkeypatch):
    """Файл сессии удалялся до входа: сбой оставлял без старой сессии и без новой.

    Неверный код, обрыв связи или Ctrl+C на приглашении -- и восстановление
    возможно только новым полным входом с кодом из Telegram.
    """
    mgr = AccountManager(config_dir=tmp_path)
    mgr.ensure_dirs()
    mgr.save_credentials(1, "hash")
    session = mgr.session_path("acc")
    session.write_bytes(b"working session")

    client = _login_client([RuntimeError("code invalid")], monkeypatch)
    monkeypatch.setattr("click.prompt", lambda *a, **k: "123")

    with pytest.raises(RuntimeError):
        await mgr.add_account("acc")

    assert session.read_bytes() == b"working session", "прежняя сессия уничтожена неудачным входом"
    assert client.disconnect.called, "соединение осталось открытым после сбоя"
    # Лок-файл сессии живёт отдельно от неё и намеренно не удаляется:
    # flock привязан к inode, и удаление файла отдало бы блокировку двум
    # процессам сразу.
    leftovers = [
        p.name for p in mgr.sessions_dir.iterdir() if p.name not in (session.name, f"{session.name}.lock")
    ]
    assert not leftovers, f"после сбоя остались файлы неудачного входа: {leftovers}"


@pytest.mark.asyncio
async def test_a_successful_login_replaces_the_session_file(tmp_path, monkeypatch):
    """Успешный вход занимает штатный путь сессии, а не остаётся во временном."""
    mgr = AccountManager(config_dir=tmp_path)
    mgr.ensure_dirs()
    mgr.save_credentials(1, "hash")
    session = mgr.session_path("acc")
    session.write_bytes(b"old session")

    def fake_client(session_obj, api_id, api_hash, *args, **kwargs):
        from unittest.mock import AsyncMock, MagicMock

        # Telethon создаёт файл сессии при подключении -- подставной клиент тоже.
        Path(session_obj.filename).write_bytes(b"new session")
        client = MagicMock()
        client.connect = AsyncMock()
        client.is_user_authorized = AsyncMock(return_value=True)
        client.get_me = AsyncMock(return_value=MagicMock(first_name="Test", id=1))
        client.disconnect = MagicMock(return_value=None)
        return client

    monkeypatch.setattr("telethon.TelegramClient", fake_client)

    await mgr.add_account("acc")

    assert session.read_bytes() == b"new session"
    # Лок-файл сессии остаётся намеренно, см. tg_export.locking.
    assert sorted(p.name for p in mgr.sessions_dir.iterdir()) == [session.name, f"{session.name}.lock"]
    assert session.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_login_refuses_to_replace_a_session_another_process_is_using(tmp_path):
    """`auth add` завершался подменой файла сессии в обход блокировки.

    Финальная пара «удалить файл вместе с -wal/-shm, переместить новый на его
    место» выполнялась без всякой проверки: у идущего экспорта пропадали
    зафиксированные, но ещё не перенесённые в основной файл данные, а все его
    последующие записи уходили в отвязанный inode.
    """
    from tg_export.errors import ProcessLockError
    from tg_export.locking import ProcessLock

    mgr = AccountManager(config_dir=tmp_path / "tg-export")
    mgr.ensure_dirs()
    mgr.save_credentials(12345, "hash")
    target = mgr.session_path("acc")
    target.write_bytes(b"working session")

    held = ProcessLock(target, "busy")
    held.acquire()
    try:
        with pytest.raises(ProcessLockError):
            await mgr.add_account("acc")
    finally:
        held.release()

    assert target.read_bytes() == b"working session"


def test_a_leftover_staging_session_is_not_listed_as_an_account(tmp_path):
    """Промежуточный файл входа выглядел настоящим аккаунтом.

    `add_account` логинится в `.<alias>.session.new.session` и подменяет рабочий
    файл только после успеха, а перечисление отбирало по суффиксу `.session` --
    после аварийного завершения процесса (SIGKILL) в `account list` появлялся
    «аккаунт» `.<alias>.session.new`, и `auth check` пытался его открыть.
    """
    from tg_export.auth import AccountManager

    mgr = AccountManager(config_dir=tmp_path / "tg-export")
    mgr.sessions_dir.mkdir(parents=True)
    (mgr.sessions_dir / "work.session").write_bytes(b"")
    (mgr.sessions_dir / ".work.session.new.session").write_bytes(b"")

    assert mgr.list_accounts() == ["work"]


def test_a_misspelled_proxy_key_is_refused(tmp_path):
    """`user:` вместо `username:` давало подключение к прокси без учётных данных.

    Секция разбиралась выборкой по известным ключам, а состав не проверялся --
    в отличие от остальных разделов конфигурации.
    """
    import pytest as _pytest
    import yaml as _yaml

    from tg_export.auth import AccountManager
    from tg_export.config import ConfigError

    mgr = AccountManager(config_dir=tmp_path)
    mgr.ensure_dirs()
    (tmp_path / "config.yaml").write_text(
        _yaml.safe_dump({"proxy": {"type": "socks5", "host": "127.0.0.1", "port": 1080, "user": "u"}}),
        encoding="utf-8",
    )

    with _pytest.raises(ConfigError, match="user"):
        mgr.load_proxy()


def test_proxy_credentials_must_be_strings(tmp_path):
    """Пароль числом уходил в Telethon как есть."""
    import pytest as _pytest
    import yaml as _yaml

    from tg_export.auth import AccountManager
    from tg_export.config import ConfigError

    mgr = AccountManager(config_dir=tmp_path)
    mgr.ensure_dirs()
    (tmp_path / "config.yaml").write_text(
        _yaml.safe_dump({"proxy": {"host": "127.0.0.1", "port": 1080, "password": 12345}}),
        encoding="utf-8",
    )

    with _pytest.raises(ConfigError, match="proxy.password"):
        mgr.load_proxy()
