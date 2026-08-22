"""Проверки того, что --quiet не съедает причину отказа.

Признак essential проставлялся вручную у отдельных вызовов, и из 94 вызовов
_diag его получили 20, причём 17 -- строки итоговой сводки. В результате
`--quiet tg send 123` печатал пустоту и возвращал 1, а одна и та же ошибка
«Config not found» печаталась в run и молчала в state show, state reset и
verify -- при том что справка флага и README обещают обратное.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

from tg_export.auth import AccountManager
from tg_export.cli import main


@pytest.fixture
def account_env(tmp_path, monkeypatch):
    """Настроенный аккаунт без файла конфигурации экспорта."""
    import tg_export.cli as cli

    cfg_dir = tmp_path / "config"
    mgr = AccountManager(config_dir=cfg_dir)
    mgr.ensure_dirs()
    mgr.save_credentials(1, "hash")
    mgr.set_default_account("acc")
    mgr.session_path("acc").write_bytes(b"")
    monkeypatch.setattr(cli, "_mgr", lambda: AccountManager(config_dir=cfg_dir))
    return cfg_dir


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--quiet", "tg", "send", "123"], "specify --text"),
        (["--quiet", "state", "show"], "Config not found"),
        (["--quiet", "state", "reset", "--all"], "Config not found"),
        (["--quiet", "verify"], "Config not found"),
        (["--quiet", "purge", "123"], "Config not found"),
        (["--quiet", "account", "default", "nope"], "not found"),
    ],
)
def test_quiet_keeps_the_reason_for_a_failure(account_env, argv, expected):
    result = CliRunner().invoke(main, argv)

    assert result.exit_code != 0, result.output
    assert expected in result.output, f"под --quiet потеряна причина отказа: {result.output!r}"


def test_quiet_keeps_what_purge_is_about_to_delete(tmp_path, account_env, monkeypatch):
    """Подтверждение удаления без описания удаляемого -- вопрос, на который
    пользователь не может ответить."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / ".tg-export-state.db").write_bytes(b"")
    (account_env / "acc.yaml").write_text(f"output:\n  path: {out_dir}\n")

    class _Cursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def fetchone(self):
            return (7,)

    state = MagicMock()
    state.open = AsyncMock()
    state.close = AsyncMock()
    state.get_catalog_entry = AsyncMock(return_value={"name": "Chat"})
    state.purge_chat = AsyncMock(return_value=7)
    state.db.execute = lambda *a, **k: _Cursor()
    monkeypatch.setattr("tg_export.state.ExportState", lambda *a, **k: state)

    result = CliRunner().invoke(main, ["--quiet", "purge", "42"], input="n\n")

    assert "Chat: Chat (id=42)" in result.output, result.output
    assert "messages=7" in result.output, result.output
