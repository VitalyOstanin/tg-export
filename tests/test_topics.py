from tg_export.models import Message, TextPart, TextType, ForumTopic
from tg_export.exporter import group_by_topic
from datetime import datetime


def _msg(id, topic_id, text="Hi"):
    return Message(
        id=id, chat_id=1, date=datetime(2024, 1, 1), edited=None,
        from_id=1, from_name="A", text=[TextPart(type=TextType.text, text=text)],
        media=None, action=None, reply_to_msg_id=None, reply_to_peer_id=None,
        forwarded_from=None, reactions=[], is_outgoing=False, signature=None,
        via_bot_id=None, saved_from_chat_id=None, inline_buttons=None,
        topic_id=topic_id, grouped_id=None,
    )


def test_topic_messages_grouped_by_topic():
    topics = [
        ForumTopic(id=1, title="General", icon_emoji=None, is_closed=False, is_pinned=True, messages_count=100),
        ForumTopic(id=2, title="Off-topic", icon_emoji=None, is_closed=False, is_pinned=False, messages_count=50),
    ]
    messages = [_msg(1, 1, "Hi"), _msg(2, 2, "OT")]
    grouped = group_by_topic(messages, topics)
    assert len(grouped) == 2
    assert grouped[1][0].text[0].text == "Hi"
    assert grouped[2][0].text[0].text == "OT"
