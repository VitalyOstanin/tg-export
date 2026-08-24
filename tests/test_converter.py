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
    """migrated_to -- объект InputChannel, а нужен только числовой channel_id."""
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
    """Обернуть настоящую сущность Telethon в минимальную заглушку, похожую на Dialog."""
    dialog = MagicMock()
    dialog.entity = entity
    dialog.date = datetime(2024, 1, 1)
    dialog.unread_count = 0
    dialog.dialog = None
    return dialog


def test_convert_chat_forbidden_is_private_group_and_left(caplog):
    """ChatForbidden -- обычная группа, из которой нас исключили: доступа к истории нет."""
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
    """Запасная ветка обязана и дальше сообщать о действительно неизвестных классах сущностей."""
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


def test_an_animation_is_classified_as_a_gif_not_a_video():
    """Ветка видео стояла выше анимации, а gif в Telegram -- mp4 с обоими атрибутами.

    Из-за порядка `MediaType.gif` и каталог `gifs/` были недостижимы:
    `media.types: [gif]` не скачивал ни одной анимации, `[video]` скачивал их
    вместе с видео.
    """
    from tg_export.converter import _classify_document
    from tg_export.models import MediaType

    video_attr = MagicMock(duration=3, w=320, h=240, round_message=False)
    attrs = {"DocumentAttributeVideo": video_attr, "DocumentAttributeAnimated": MagicMock()}

    media_type, *_ = _classify_document(attrs, "video/mp4")

    assert media_type is MediaType.gif


def test_a_plain_video_is_still_a_video():
    from tg_export.converter import _classify_document
    from tg_export.models import MediaType

    attrs = {"DocumentAttributeVideo": MagicMock(duration=3, w=320, h=240, round_message=False)}

    media_type, _name, duration, w, h = _classify_document(attrs, "video/mp4")

    assert media_type is MediaType.video
    assert (duration, w, h) == (3, 320, 240)


# ---------------------------------------------------------------------------
# Классификация документов: каждая ветка отдельно
# ---------------------------------------------------------------------------


def _classify(attrs: dict, mime_type: str | None = None):
    from tg_export.converter import _classify_document

    return _classify_document(attrs, mime_type)


def test_a_round_message_is_a_video_note():
    from tg_export.models import MediaType

    attrs = {"DocumentAttributeVideo": MagicMock(duration=5, w=240, h=240, round_message=True)}

    assert _classify(attrs, "video/mp4")[0] is MediaType.video_note


def test_a_voice_message_is_told_apart_from_an_audio_file():
    from tg_export.models import MediaType

    voice = {"DocumentAttributeAudio": MagicMock(duration=7, voice=True)}
    music = {"DocumentAttributeAudio": MagicMock(duration=180, voice=False)}

    assert _classify(voice, "audio/ogg")[0] is MediaType.voice
    assert _classify(music, "audio/mpeg")[0] is MediaType.document


def test_a_sticker_is_classified_by_its_attribute():
    from tg_export.models import MediaType

    assert _classify({"DocumentAttributeSticker": MagicMock(alt="🙂")}, None)[0] is MediaType.sticker


def test_a_gif_mime_type_alone_is_enough():
    from tg_export.models import MediaType

    assert _classify({}, "image/gif")[0] is MediaType.gif


def test_a_document_without_telling_attributes_stays_a_document():
    from tg_export.models import MediaType

    attrs = {"DocumentAttributeFilename": MagicMock(file_name="отчёт.pdf")}
    media_type, name, *_ = _classify(attrs, "application/pdf")

    assert media_type is MediaType.document
    assert name == "отчёт.pdf"


# ---------------------------------------------------------------------------
# Медиа сообщения
# ---------------------------------------------------------------------------


def _photo_with(size_obj) -> MagicMock:
    photo = MagicMock(id=11)
    photo.sizes = [size_obj] if size_obj is not None else []
    return MagicMock(photo=photo, spoiler=False)


def test_the_size_of_a_photo_is_taken_from_the_largest_variant():
    from tg_export.converter import _photo_media

    progressive = type("PhotoSizeProgressive", (), {"w": 1280, "h": 720, "sizes": [100, 5000, 900]})()

    from tg_export.models import PhotoMedia

    media = _photo_media(_photo_with(progressive))

    assert isinstance(media, PhotoMedia) and media.file is not None
    assert (media.width, media.height) == (1280, 720)
    assert media.file.size == 5000


def test_a_cached_photo_reports_the_length_of_its_bytes():
    from tg_export.converter import _photo_media

    cached = type("PhotoCachedSize", (), {"w": 90, "h": 90, "bytes": b"1234567890"})()

    media = _photo_media(_photo_with(cached))

    assert media is not None and media.file is not None
    assert media.file.size == 10


def test_a_photo_without_variants_is_still_converted():
    from tg_export.converter import _photo_media
    from tg_export.models import PhotoMedia

    media = _photo_media(_photo_with(None))

    assert isinstance(media, PhotoMedia) and media.file is not None
    assert (media.width, media.height, media.file.size) == (0, 0, 0)


def test_a_media_without_its_object_converts_to_nothing():
    from tg_export.converter import _document_media, _photo_media

    assert _photo_media(MagicMock(photo=None)) is None
    assert _document_media(MagicMock(document=None)) is None


def test_a_poll_carries_the_votes_of_every_answer():
    from tg_export.converter import _poll_media

    option_a, option_b = b"a", b"b"
    poll = MagicMock(
        question=MagicMock(text="Идём?"),
        answers=[
            MagicMock(text=MagicMock(text="Да"), option=option_a),
            MagicMock(text=MagicMock(text="Нет"), option=option_b),
        ],
    )
    results = MagicMock(results=[MagicMock(option=option_a, voters=3), MagicMock(option=option_b, voters=1)])

    from tg_export.models import PollMedia

    media = _poll_media(MagicMock(poll=poll, results=results))

    assert isinstance(media, PollMedia)
    assert [answer.voters for answer in media.answers] == [3, 1]


# ---------------------------------------------------------------------------
# Ответы, пересылки и кнопки
# ---------------------------------------------------------------------------


def test_a_reply_inside_a_forum_topic_reports_the_topic():
    from tg_export.converter import _convert_reply

    tl_msg = MagicMock()
    tl_msg.reply_to = MagicMock(reply_to_msg_id=15, reply_to_peer_id=None, forum_topic=True)

    reply_to_msg_id, reply_to_peer_id, topic_id = _convert_reply(tl_msg)

    assert (reply_to_msg_id, reply_to_peer_id, topic_id) == (15, None, 15)


def test_a_message_without_a_reply_reports_nothing():
    from tg_export.converter import _convert_reply

    tl_msg = MagicMock()
    tl_msg.reply_to = None

    assert _convert_reply(tl_msg) == (None, None, None)


def test_a_forward_keeps_the_name_when_the_sender_is_hidden():
    from tg_export.converter import _convert_forward

    tl_msg = MagicMock()
    tl_msg.fwd_from = MagicMock(from_id=None, from_name="Аноним", date=datetime(2026, 1, 2))

    forward = _convert_forward(tl_msg)

    assert forward is not None
    assert (forward.from_id, forward.from_name) == (None, "Аноним")


def test_a_message_that_was_not_forwarded_has_no_forward_info():
    from tg_export.converter import _convert_forward

    tl_msg = MagicMock()
    tl_msg.fwd_from = None

    assert _convert_forward(tl_msg) is None


def test_inline_buttons_keep_their_rows():
    from tg_export.converter import _convert_inline_buttons

    url_button = type("KeyboardButtonUrl", (), {"text": "Открыть", "url": "https://example.org"})()
    data_button = type("KeyboardButtonCallback", (), {"text": "Да", "data": b"yes"})()
    tl_msg = MagicMock()
    tl_msg.reply_markup = MagicMock(rows=[MagicMock(buttons=[url_button, data_button])])

    rows = _convert_inline_buttons(tl_msg)

    assert rows is not None
    assert [button.text for button in rows[0]] == ["Открыть", "Да"]
    assert rows[0][0].data == "https://example.org"


def test_a_message_without_a_keyboard_has_no_buttons():
    from tg_export.converter import _convert_inline_buttons

    tl_msg = MagicMock()
    tl_msg.reply_markup = None

    assert _convert_inline_buttons(tl_msg) is None
