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


def test_formatting_offsets_are_counted_in_utf16_units():
    """Telegram считает offset/length в кодовых единицах UTF-16.

    Код резал Python-строку по кодовым точкам, поэтому после первого же символа
    вне BMP (любой цветной эмодзи, часть иероглифов) все офсеты сдвигались:
    entity либо не проходила проверку границ и молча отбрасывалась вместе с
    форматированием, либо вырезала не тот фрагмент.
    """
    from telethon.tl.types import MessageEntityBold

    from tg_export.converter import convert_entities

    # Эмодзи занимает две единицы UTF-16, поэтому «bold» начинается с офсета 3.
    parts = convert_entities("\U0001f600 bold", [MessageEntityBold(offset=3, length=4)])

    assert [(p.type, p.text) for p in parts] == [
        (TextType.text, "\U0001f600 "),
        (TextType.bold, "bold"),
    ]


def test_a_link_after_an_emoji_keeps_its_own_text():
    """Сдвиг офсетов делал подписью ссылки соседний текст."""
    from telethon.tl.types import MessageEntityTextUrl

    from tg_export.converter import convert_entities

    text = "\U0001f680\U0001f680 click here now"
    # Два эмодзи -- четыре единицы UTF-16, затем пробел: «click» начинается с пятой.
    entity = MessageEntityTextUrl(offset=5, length=5, url="https://example.com")

    parts = convert_entities(text, [entity])

    link = [p for p in parts if p.type == TextType.text_url]
    assert len(link) == 1
    assert link[0].text == "click"
    assert link[0].href == "https://example.com"


def test_text_without_entities_is_unchanged():
    """Обычный текст не должен пострадать от перевода в единицы UTF-16."""
    from tg_export.converter import convert_entities

    parts = convert_entities("простой текст", None)

    assert [(p.type, p.text) for p in parts] == [(TextType.text, "простой текст")]


class _AnyFields:
    """Заглушка действия Telethon: любое поле есть и пустое."""

    def __getattr__(self, name):
        return None


def test_every_action_carries_the_name_of_its_own_class():
    """`type` уходит в базу и в шаблоны, где сверяется по строке.

    Имя писалось литералом рядом с классом (`ActionChatCreate(type="ActionChatCreate")`),
    и переименование dataclass оставило бы литерал прежним. Теперь имя берётся
    у класса, а этот тест сверяет его с именем действия Telethon.
    """
    from tg_export.converter import _DETAILED_ACTIONS, _PLAIN_ACTIONS, convert_action

    assert len(_DETAILED_ACTIONS) == 16, "таблица подробных действий поредела -- проверка сузилась"
    assert _PLAIN_ACTIONS, "таблица простых действий пуста"

    for tl_name, factory in _DETAILED_ACTIONS.items():
        action = factory(_AnyFields())
        expected = tl_name.replace("MessageAction", "Action", 1)
        assert action.type == type(action).__name__ == expected, tl_name

    for tl_name, cls in _PLAIN_ACTIONS.items():
        assert cls().type == cls.__name__ == tl_name.replace("MessageAction", "Action", 1)

    unknown = type("MessageActionSomethingNew", (), {})()
    assert convert_action(unknown).type == "ActionSomethingNew"
