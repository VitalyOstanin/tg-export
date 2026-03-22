import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime
from tg_export.exporter import Exporter
from tg_export.html.renderer import HtmlRenderer
from tg_export.models import Message, TextPart, TextType, Chat, ChatType
from tg_export.config import OutputConfig, Config


@pytest.mark.asyncio
async def test_full_export_cycle(tmp_path, state):
    """Full cycle: config -> export -> HTML -> check output exists."""
    output = tmp_path / "output" / "test_account"

    config = Config(
        output=OutputConfig(
            path=str(tmp_path / "output"),
        ),
        unmatched_action="export_with_defaults",
    )

    renderer = HtmlRenderer(output_dir=output, config=config.output)
    renderer.setup()

    downloader = AsyncMock()

    exporter = Exporter(
        api=AsyncMock(), state=state, config=config,
        renderer=renderer, downloader=downloader, account="test_account",
    )

    # Export with empty chat list — should succeed with 0 chats
    stats = await exporter.run(dry_run=False, chat_list=[])
    assert stats.chats_exported == 0
    assert output.exists()
    assert (output / "css" / "style.css").exists()
