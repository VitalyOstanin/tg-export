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
