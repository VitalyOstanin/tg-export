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
