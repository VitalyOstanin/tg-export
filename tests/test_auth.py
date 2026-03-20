import pytest
from pathlib import Path
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


def test_credentials_file_permissions(tmp_path):
    mgr = AccountManager(config_dir=tmp_path / "tg-export")
    mgr.ensure_dirs()
    mgr.save_credentials(api_id=12345, api_hash="abc123")
    cred_path = tmp_path / "tg-export" / "api_credentials.yaml"
    assert cred_path.exists()
    import stat
    mode = cred_path.stat().st_mode & 0o777
    assert mode == 0o600
