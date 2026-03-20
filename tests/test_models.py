import json
from datetime import datetime
from tg_export.models import (
    Message, TextPart, TextType, Media, MediaType,
    PhotoMedia, FileInfo, Reaction, ReactionType,
    ForwardInfo, ChatType, Chat,
)


def test_message_json_roundtrip():
    msg = Message(
        id=123,
        chat_id=456,
        date=datetime(2024, 1, 15, 10, 30, 0),
        edited=None,
        from_id=789,
        from_name="Иван",
        text=[TextPart(type=TextType.text, text="Привет")],
        media=None,
        action=None,
        reply_to_msg_id=None,
        reply_to_peer_id=None,
        forwarded_from=None,
        reactions=[],
        is_outgoing=False,
        signature=None,
        via_bot_id=None,
        saved_from_chat_id=None,
        inline_buttons=None,
        topic_id=None,
        grouped_id=None,
    )
    json_str = msg.to_json()
    restored = Message.from_json(json_str)
    assert restored.id == 123
    assert restored.from_name == "Иван"
    assert restored.text[0].text == "Привет"
    assert restored.date == datetime(2024, 1, 15, 10, 30, 0)


def test_message_with_photo_json_roundtrip():
    msg = Message(
        id=1,
        chat_id=2,
        date=datetime(2024, 6, 1),
        edited=None,
        from_id=3,
        from_name="Test",
        text=[],
        media=PhotoMedia(
            type=MediaType.photo,
            file=FileInfo(id=100, size=5000, name="photo.jpg", mime_type="image/jpeg", local_path=None),
            width=800,
            height=600,
        ),
        action=None,
        reply_to_msg_id=None,
        reply_to_peer_id=None,
        forwarded_from=None,
        reactions=[Reaction(type=ReactionType.emoji, emoji="\U0001f44d", document_id=None, count=5, recent=None)],
        is_outgoing=True,
        signature=None,
        via_bot_id=None,
        saved_from_chat_id=None,
        inline_buttons=None,
        topic_id=None,
        grouped_id=None,
    )
    json_str = msg.to_json()
    restored = Message.from_json(json_str)
    assert isinstance(restored.media, PhotoMedia)
    assert restored.media.width == 800
    assert restored.media.file.name == "photo.jpg"
    assert restored.reactions[0].emoji == "\U0001f44d"


def test_chat_type_enum():
    assert ChatType.self == "self"
    assert ChatType.private_supergroup == "private_supergroup"
