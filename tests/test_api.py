from unittest.mock import AsyncMock, MagicMock

import pytest
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
async def test_start_takeout_clears_stale_takeout_id_first():
    """Stale session.takeout_id should be ended (or cleared) before new takeout.

    Why: Telethon's TakeoutClient.__aenter__ raises ValueError ("Can't send a
    takeout request while another takeout for the current session still not
    been finished yet.") without contacting the server when takeout_id is
    non-None. We must finish/clear it first.
    """
    api = TgApi.__new__(TgApi)
    api.client = MagicMock()
    api.takeout = None
    api.client.session = MagicMock()
    api.client.session.takeout_id = 12345
    api.client.end_takeout = AsyncMock(return_value=True)
    mock_takeout_ctx = MagicMock()
    mock_takeout_client = AsyncMock()
    mock_takeout_ctx.__aenter__ = AsyncMock(return_value=mock_takeout_client)
    api.client.takeout.return_value = mock_takeout_ctx

    await api.start_takeout()

    api.client.end_takeout.assert_awaited_once_with(success=False)
    api.client.takeout.assert_called_once()
    assert api.takeout is mock_takeout_client


@pytest.mark.asyncio
async def test_start_takeout_clears_stale_id_locally_when_end_fails():
    """If end_takeout raises (e.g. server already forgot the takeout), wipe
    takeout_id locally and proceed."""
    api = TgApi.__new__(TgApi)
    api.client = MagicMock()
    api.takeout = None
    api.client.session = MagicMock()
    api.client.session.takeout_id = 999
    api.client.end_takeout = AsyncMock(side_effect=RuntimeError("server says no"))
    mock_takeout_ctx = MagicMock()
    mock_takeout_client = AsyncMock()
    mock_takeout_ctx.__aenter__ = AsyncMock(return_value=mock_takeout_client)
    api.client.takeout.return_value = mock_takeout_ctx

    await api.start_takeout()

    assert api.client.session.takeout_id is None
    api.client.takeout.assert_called_once()


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
    api.takeout.iter_messages = MagicMock(
        return_value=AsyncMock(
            __aiter__=lambda s: s,
            __anext__=AsyncMock(side_effect=StopAsyncIteration),
        )
    )

    async for _ in api.iter_messages(chat_id=123, min_id=500):
        pass

    api.takeout.iter_messages.assert_called_once_with(123, min_id=500)


@pytest.mark.asyncio
async def test_fallback_to_client_when_no_takeout():
    """Without Takeout should use client directly."""
    api = TgApi.__new__(TgApi)
    api.takeout = None
    api.client = AsyncMock()
    api.client.iter_messages = MagicMock(
        return_value=AsyncMock(
            __aiter__=lambda s: s,
            __anext__=AsyncMock(side_effect=StopAsyncIteration),
        )
    )

    async for _ in api.iter_messages(chat_id=123, min_id=0):
        pass

    api.client.iter_messages.assert_called_once_with(123, min_id=0)


@pytest.mark.asyncio
async def test_get_folders_with_dialog_filters_object():
    """get_folders should handle DialogFilters object with .filters attribute."""
    api = TgApi.__new__(TgApi)
    api.client = AsyncMock()

    # Telethon returns DialogFilters with .filters, not iterable directly
    mock_filter = MagicMock()
    mock_title = MagicMock()
    mock_title.text = "Work"
    mock_filter.title = mock_title
    mock_peer = MagicMock()
    mock_peer.user_id = 123
    del mock_peer.channel_id
    del mock_peer.chat_id
    mock_filter.include_peers = [mock_peer]

    mock_result = MagicMock()
    mock_result.filters = [mock_filter]
    api.client.return_value = mock_result

    folders = await api.get_folders()
    names = [f["name"] for f in folders]
    assert "Work" in names
    work = [f for f in folders if f["name"] == "Work"][0]
    assert 123 in work["peer_ids"]


@pytest.mark.asyncio
async def test_get_folders_with_text_with_entities_title():
    """get_folders should extract .text from TextWithEntities title."""
    api = TgApi.__new__(TgApi)
    api.client = AsyncMock()

    mock_filter = MagicMock()
    # title is TextWithEntities with .text attribute
    mock_title = MagicMock()
    mock_title.text = "Test Folder"
    mock_filter.title = mock_title
    mock_filter.include_peers = []

    mock_result = MagicMock()
    mock_result.filters = [mock_filter]
    api.client.return_value = mock_result

    folders = await api.get_folders()
    names = [f["name"] for f in folders]
    assert "Test Folder" in names


@pytest.mark.asyncio
async def test_get_folders_with_plain_string_title():
    """get_folders should handle plain string title (older Telethon)."""
    api = TgApi.__new__(TgApi)
    api.client = AsyncMock()

    mock_filter = MagicMock()
    mock_filter.title = "News"  # plain string, no .text
    mock_filter.include_peers = []

    mock_result = MagicMock()
    mock_result.filters = [mock_filter]
    api.client.return_value = mock_result

    folders = await api.get_folders()
    names = [f["name"] for f in folders]
    assert "News" in names
