import logging
from datetime import datetime
from unittest.mock import MagicMock

from telethon.tl.types import ChannelForbidden, ChatForbidden

from tg_export.converter import convert_chat, convert_message
from tg_export.models import ChatType, TextType


def _make_mock_message(text: str | None = "Hello", date=None, media=None, action=None):
    msg = MagicMock()
    msg.id = 1
    msg.date = date or datetime(2024, 1, 1)
    msg.edit_date = None
    msg.from_id = MagicMock()
    msg.from_id.user_id = 123
    msg.message = text
    msg.entities = None
    msg.media = media
    msg.action = action
    msg.reply_to = None
    msg.fwd_from = None
    msg.reactions = None
    msg.out = False
    msg.post_author = None
    msg.via_bot_id = None
    msg.reply_markup = None
    msg.grouped_id = None
    return msg


def test_convert_simple_text_message():
    tl_msg = _make_mock_message(text="Привет мир")
    result = convert_message(tl_msg, chat_id=456)
    assert result.id == 1
    assert result.chat_id == 456
    assert result.text[0].type == TextType.text
    assert result.text[0].text == "Привет мир"
    assert result.media is None
    assert result.action is None


def test_convert_message_with_bold_entity():
    msg = _make_mock_message(text="Hello world")
    entity = MagicMock()
    entity.__class__.__name__ = "MessageEntityBold"
    entity.offset = 0
    entity.length = 5
    msg.entities = [entity]
    result = convert_message(msg, chat_id=1)
    types = [p.type for p in result.text]
    assert TextType.bold in types


def test_convert_empty_message():
    msg = _make_mock_message(text=None)
    result = convert_message(msg, chat_id=1)
    assert result.text == []


def test_convert_outgoing_message():
    msg = _make_mock_message(text="Out")
    msg.out = True
    result = convert_message(msg, chat_id=1)
    assert result.is_outgoing is True


def test_convert_chat_migrated_to_extracts_channel_id():
    """migrated_to is an InputChannel object, we need just the int channel_id."""
    dialog = MagicMock()
    entity = MagicMock()
    entity.__class__.__name__ = "Chat"
    entity.id = 100
    entity.title = "Old Group"
    entity.username = None
    entity.participants_count = 5
    entity.left = False
    entity.forum = False
    entity.monoforum = False
    # migrated_to is an InputChannel with channel_id and access_hash
    migrated_to = MagicMock()
    migrated_to.channel_id = 200
    migrated_to.access_hash = 9999
    entity.migrated_to = migrated_to
    dialog.entity = entity
    dialog.date = datetime(2024, 1, 1)
    dialog.unread_count = 0
    chat = convert_chat(dialog)
    assert chat.migrated_to_id == 200


def test_convert_chat_no_migration():
    dialog = MagicMock()
    entity = MagicMock()
    entity.__class__.__name__ = "User"
    entity.id = 50
    entity.first_name = "Test"
    entity.last_name = ""
    entity.username = "testuser"
    entity.is_self = False
    entity.bot = False
    entity.participants_count = None
    entity.left = False
    entity.forum = False
    entity.monoforum = False
    entity.migrated_to = None
    dialog.entity = entity
    dialog.date = datetime(2024, 1, 1)
    dialog.unread_count = 0
    chat = convert_chat(dialog)
    assert chat.migrated_to_id is None


def _make_dialog(entity) -> MagicMock:
    """Wrap a real Telethon entity into a minimal Dialog-like mock."""
    dialog = MagicMock()
    dialog.entity = entity
    dialog.date = datetime(2024, 1, 1)
    dialog.unread_count = 0
    dialog.dialog = None
    return dialog


def test_convert_chat_forbidden_is_private_group_and_left(caplog):
    """ChatForbidden is a basic group we were kicked from: no access to history."""
    entity = ChatForbidden(id=100, title="Kicked Group")
    with caplog.at_level(logging.WARNING, logger="tg_export.converter"):
        chat = convert_chat(_make_dialog(entity))
    assert chat.type is ChatType.private_group
    assert chat.is_left is True
    assert chat.name == "Kicked Group"
    assert "Unknown entity class" not in caplog.text


def test_convert_channel_forbidden_is_private_channel_and_left(caplog):
    entity = ChannelForbidden(id=200, access_hash=1, title="Banned Channel", broadcast=True)
    with caplog.at_level(logging.WARNING, logger="tg_export.converter"):
        chat = convert_chat(_make_dialog(entity))
    assert chat.type is ChatType.private_channel
    assert chat.is_left is True
    assert "Unknown entity class" not in caplog.text


def test_convert_channel_forbidden_megagroup_is_private_supergroup():
    entity = ChannelForbidden(id=300, access_hash=1, title="Banned Supergroup", megagroup=True)
    chat = convert_chat(_make_dialog(entity))
    assert chat.type is ChatType.private_supergroup
    assert chat.is_left is True


def test_convert_chat_unknown_entity_class_still_warns(caplog):
    """The fallback branch must keep reporting genuinely unknown entity classes."""
    entity = MagicMock()
    entity.__class__.__name__ = "ChatEmpty"
    entity.id = 400
    entity.title = "Empty"
    entity.left = False
    with caplog.at_level(logging.WARNING, logger="tg_export.converter"):
        chat = convert_chat(_make_dialog(entity))
    assert chat.type is ChatType.personal
    assert "Unknown entity class 'ChatEmpty'" in caplog.text
