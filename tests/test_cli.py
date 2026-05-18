from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

from tg_export.cli import main


def test_main_help():
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "auth" in result.output
    assert "account" in result.output
    assert "init" in result.output
    assert "run" in result.output
    assert "verify" in result.output


def test_auth_help():
    runner = CliRunner()
    result = runner.invoke(main, ["auth", "--help"])
    assert result.exit_code == 0
    assert "add" in result.output
    assert "credentials" in result.output
    assert "check" in result.output


def test_account_help():
    runner = CliRunner()
    result = runner.invoke(main, ["account", "--help"])
    assert result.exit_code == 0
    assert "list" in result.output
    assert "default" in result.output
    assert "remove" in result.output


@pytest.mark.asyncio
async def test_render_index_respects_should_stop():
    # Если во время финального render index пришёл shutdown — должен ранний
    # выход до тяжёлого jinja-рендера, чтобы не блокировать executor.
    from tg_export.cli import _render_index

    state_mock = AsyncMock()
    renderer_mock = MagicMock()
    cfg_mock = MagicMock()

    await _render_index(renderer_mock, [], cfg_mock, state_mock, should_stop=lambda: True)

    renderer_mock.render_index.assert_not_called()
