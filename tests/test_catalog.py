import pytest
from tg_export.catalog import format_catalog_yaml, generate_config_template
from tg_export.models import Chat, ChatType
from datetime import datetime


def test_format_catalog_yaml():
    chats = [
        Chat(id=123, name="Рабочий чат", type=ChatType.private_supergroup,
             username=None, folder="Работа", members_count=12,
             last_message_date=datetime(2026, 3, 20), messages_count=45230,
             is_left=False, is_archived=False, is_forum=True, migrated_to_id=None,
             migrated_from_id=None, is_monoforum=False),
        Chat(id=456, name="Иван", type=ChatType.personal,
             username="ivan", folder=None, members_count=None,
             last_message_date=datetime(2026, 3, 19), messages_count=3200,
             is_left=False, is_archived=False, is_forum=False, migrated_to_id=None,
             migrated_from_id=None, is_monoforum=False),
    ]
    yaml_str = format_catalog_yaml(chats)
    assert "Рабочий чат" in yaml_str
    assert "private_supergroup" in yaml_str
    assert "is_forum: true" in yaml_str
    assert "Работа" in yaml_str  # folder
    assert "unfiled" in yaml_str  # Иван не в папке


def test_generate_config_template():
    chats = [
        Chat(id=123, name="Test", type=ChatType.personal,
             username=None, folder=None, members_count=None,
             last_message_date=None, messages_count=100,
             is_left=False, is_archived=False, is_forum=False, migrated_to_id=None,
             migrated_from_id=None, is_monoforum=False),
    ]
    yaml_str = generate_config_template(chats)
    assert "defaults:" in yaml_str


def test_format_catalog_includes_archived():
    chats = [
        Chat(id=888, name="Archived Chat", type=ChatType.personal,
             username=None, folder=None, members_count=None,
             last_message_date=None, messages_count=100,
             is_left=False, is_archived=True, is_forum=False, migrated_to_id=None,
             migrated_from_id=None, is_monoforum=False),
    ]
    yaml_str = format_catalog_yaml(chats)
    assert "archived:" in yaml_str
    assert "Archived Chat" in yaml_str
    assert "is_archived: true" in yaml_str


def test_format_catalog_includes_left():
    chats = [
        Chat(id=999, name="Old Channel", type=ChatType.public_channel,
             username="old", folder=None, members_count=None,
             last_message_date=None, messages_count=500,
             is_left=True, is_archived=False, is_forum=False, migrated_to_id=None,
             migrated_from_id=None, is_monoforum=False),
    ]
    yaml_str = format_catalog_yaml(chats)
    assert "left:" in yaml_str
    assert "Old Channel" in yaml_str
