import pytest
from unittest.mock import AsyncMock, MagicMock
from telethon.errors import TakeoutInitDelayError
from tg_export.api import TgApi


@pytest.mark.asyncio
async def test_start_takeout_creates_session():
    """start_takeout should call client.takeout() and store result."""
    api = TgApi.__new__(TgApi)
    api.client = MagicMock()
    api.takeout = None
    mock_takeout_ctx = MagicMock()
    mock_takeout_client = AsyncMock()
    mock_takeout_ctx.__aenter__ = AsyncMock(return_value=mock_takeout_client)
    mock_takeout_ctx.__aexit__ = AsyncMock(return_value=False)
    api.client.takeout.return_value = mock_takeout_ctx

    await api.start_takeout()
    api.client.takeout.assert_called_once()
    assert api.takeout is mock_takeout_client


@pytest.mark.asyncio
async def test_start_takeout_handles_delay():
    """On TAKEOUT_INIT_DELAY should raise with wait time."""
    api = TgApi.__new__(TgApi)
    api.client = MagicMock()
    api.takeout = None
    err = TakeoutInitDelayError(request=None, capture=0)
    err.seconds = 3600
    api.client.takeout.side_effect = err

    with pytest.raises(TakeoutInitDelayError):
        await api.start_takeout()


@pytest.mark.asyncio
async def test_iter_messages_passes_min_id():
    """iter_messages should pass min_id to Telethon."""
    api = TgApi.__new__(TgApi)
    api.takeout = AsyncMock()
    api.takeout.iter_messages = MagicMock(return_value=AsyncMock(
        __aiter__=lambda s: s,
        __anext__=AsyncMock(side_effect=StopAsyncIteration),
    ))

    async for _ in api.iter_messages(chat_id=123, min_id=500):
        pass

    api.takeout.iter_messages.assert_called_once_with(123, min_id=500)


@pytest.mark.asyncio
async def test_fallback_to_client_when_no_takeout():
    """Without Takeout should use client directly."""
    api = TgApi.__new__(TgApi)
    api.takeout = None
    api.client = AsyncMock()
    api.client.iter_messages = MagicMock(return_value=AsyncMock(
        __aiter__=lambda s: s,
        __anext__=AsyncMock(side_effect=StopAsyncIteration),
    ))

    async for _ in api.iter_messages(chat_id=123, min_id=0):
        pass

    api.client.iter_messages.assert_called_once_with(123, min_id=0)
