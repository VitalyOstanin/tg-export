from pathlib import Path

from conftest import make_chat

from tg_export.exporter import resolve_chat_dir, should_combine_migration
from tg_export.models import ChatType


def test_migrated_chat_combines_messages():
    old_group = make_chat(
        id=100,
        name="Old Group",
        type=ChatType.private_group,
        members_count=5,
        messages_count=1000,
        migrated_to_id=200,
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
