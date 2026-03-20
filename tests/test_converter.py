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
