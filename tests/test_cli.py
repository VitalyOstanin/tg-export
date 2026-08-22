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


class _RecordingDiag:
    """Collect _diag calls so tests can assert on visibility, not just text."""

    def __init__(self):
        self.calls = []

    def __call__(self, message, *, essential=False, **kwargs):
        self.calls.append((message, essential))

    def texts(self, *, essential_only=False):
        return [m for m, e in self.calls if e or not essential_only]


def _takeout_cfg():
    cfg = MagicMock()
    cfg.contacts = True
    cfg.defaults.media.max_file_size_bytes = 1024
    return cfg


@pytest.mark.asyncio
async def test_start_takeout_returns_true_on_success(monkeypatch):
    from tg_export import cli

    diag = _RecordingDiag()
    monkeypatch.setattr(cli, "_diag", diag)
    api = MagicMock()
    api.start_takeout = AsyncMock()

    assert await cli._start_takeout(api, _takeout_cfg(), require=False) is True
    assert any("Takeout session started" in m for m in diag.texts())


@pytest.mark.asyncio
async def test_start_takeout_fallback_is_essential(monkeypatch):
    # Откат на обычный API меняет способ выгрузки и должен быть виден даже
    # под --quiet: раньше сообщение печаталось без essential и пропадало.
    from telethon.errors import RPCError

    from tg_export import cli

    diag = _RecordingDiag()
    monkeypatch.setattr(cli, "_diag", diag)
    api = MagicMock()
    api.start_takeout = AsyncMock(side_effect=RPCError(request=None, message="no takeout"))

    assert await cli._start_takeout(api, _takeout_cfg(), require=False) is False
    essential = diag.texts(essential_only=True)
    assert any("regular API" in m for m in essential)


@pytest.mark.asyncio
async def test_start_takeout_cooldown_is_essential(monkeypatch):
    from telethon.errors import TakeoutInitDelayError

    from tg_export import cli

    diag = _RecordingDiag()
    monkeypatch.setattr(cli, "_diag", diag)
    err = TakeoutInitDelayError(request=None, capture=0)
    err.seconds = 7200
    api = MagicMock()
    api.start_takeout = AsyncMock(side_effect=err)

    assert await cli._start_takeout(api, _takeout_cfg(), require=False) is False
    essential = diag.texts(essential_only=True)
    assert any("cooldown" in m and "2h" in m for m in essential)


@pytest.mark.asyncio
async def test_start_takeout_require_turns_fallback_into_error(monkeypatch):
    from telethon.errors import RPCError

    from tg_export import cli
    from tg_export.errors import TakeoutUnavailableError

    monkeypatch.setattr(cli, "_diag", _RecordingDiag())
    api = MagicMock()
    api.start_takeout = AsyncMock(side_effect=RPCError(request=None, message="no takeout"))

    with pytest.raises(TakeoutUnavailableError):
        await cli._start_takeout(api, _takeout_cfg(), require=True)


@pytest.mark.asyncio
async def test_start_takeout_does_not_swallow_programming_errors(monkeypatch):
    # Широкий except Exception прятал и дефекты самого кода — например TypeError
    # из повреждённого takeout_id. Такие ошибки должны доходить до вызывающего.
    from tg_export import cli

    monkeypatch.setattr(cli, "_diag", _RecordingDiag())
    api = MagicMock()
    api.start_takeout = AsyncMock(side_effect=TypeError("object supporting the buffer API required"))

    with pytest.raises(TypeError):
        await cli._start_takeout(api, _takeout_cfg(), require=False)
