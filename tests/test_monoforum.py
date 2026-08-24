from pathlib import Path

from conftest import make_chat

from tg_export.exporter import resolve_monoforum_dir
from tg_export.models import ChatType


def test_monoforum_detected():
    chat = make_chat(
        id=100, name="Channel DMs", type=ChatType.private_supergroup, messages_count=50, is_monoforum=True
    )
    assert chat.is_monoforum is True


def test_monoforum_dir_in_channel_folder():
    result = resolve_monoforum_dir(
        base=Path("/output"),
        channel_name="My Channel",
        channel_id=200,
        monoforum_name="DMs",
        monoforum_id=100,
        folder="News",
    )
    assert "My_Channel_200" in str(result)
    assert "DMs_100" in str(result)
