import pytest
import pytest_asyncio

from tg_export.auth import AccountManager
from tg_export.cli import common as cli_common
from tg_export.state import ExportState


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
