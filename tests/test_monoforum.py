from tg_export.models import Chat, ChatType
from tg_export.exporter import resolve_monoforum_dir
from pathlib import Path


def test_monoforum_detected():
    chat = Chat(
        id=100, name="Channel DMs", type=ChatType.private_supergroup,
        username=None, folder=None, members_count=None,
        last_message_date=None, messages_count=50,
        is_left=False, is_archived=False, is_forum=False,
        migrated_to_id=None, migrated_from_id=None,
        is_monoforum=True,
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
