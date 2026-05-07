from pathlib import Path

from tg_export.exporter import resolve_chat_dir, should_combine_migration
from tg_export.models import Chat, ChatType


def test_migrated_chat_combines_messages():
    old_group = Chat(
        id=100,
        name="Old Group",
        type=ChatType.private_group,
        username=None,
        folder=None,
        members_count=5,
        last_message_date=None,
        messages_count=1000,
        is_left=False,
        is_archived=False,
        is_forum=False,
        migrated_to_id=200,
        migrated_from_id=None,
        is_monoforum=False,
    )
    assert should_combine_migration(old_group) is True
    assert old_group.migrated_to_id == 200


def test_left_channel_dir():
    result = resolve_chat_dir(
        base=Path("/output"),
        chat_name="Left",
        chat_id=999,
        folder=None,
        is_left=True,
    )
    assert "left" in str(result)
