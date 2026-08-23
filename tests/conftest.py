import pytest
import pytest_asyncio

from tg_export.auth import AccountManager
from tg_export.cli import common as cli_common
from tg_export.state import ExportState


@pytest.fixture(autouse=True)
def config_dir_of_its_own(tmp_path, monkeypatch):
    """Каждый тест получает свой каталог конфигурации, а не каталог разработчика.

    `AccountManager()` без явного пути читает окружение
    (`TG_EXPORT_CONFIG_DIR` > `XDG_CONFIG_HOME` > `~/.config/tg-export`), а
    `_mgr()` ещё и создаёт каталоги. Изоляция держалась на том, что каждый тест
    сам подменяет менеджер: забытая подмена в тесте `init` означала бы запись
    шаблона поверх настоящей конфигурации того, кто запустил тесты.
    """
    monkeypatch.setenv("TG_EXPORT_CONFIG_DIR", str(tmp_path / "tg-export-config"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


@pytest_asyncio.fixture
async def state(tmp_path):
    s = ExportState(tmp_path / "state.db")
    await s.open()
    yield s
    await s.close()


@pytest.fixture
def account_env(tmp_path, monkeypatch):
    """Настроенный аккаунт без файла конфигурации экспорта."""

    cfg_dir = tmp_path / "config"
    mgr = AccountManager(config_dir=cfg_dir)
    mgr.ensure_dirs()
    mgr.save_credentials(1, "hash")
    mgr.set_default_account("acc")
    mgr.session_path("acc").write_bytes(b"")
    monkeypatch.setattr(cli_common, "_mgr", lambda: AccountManager(config_dir=cfg_dir))
    return cfg_dir
