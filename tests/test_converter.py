import pytest
from unittest.mock import MagicMock
from datetime import datetime
from tg_export.converter import convert_message, convert_chat
from tg_export.models import TextType, MediaType, ChatType


def _make_mock_message(text="Hello", date=None, media=None, action=None):
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
