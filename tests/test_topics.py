from datetime import datetime

from conftest import make_message

from tg_export.exporter import group_by_topic
from tg_export.models import ForumTopic, TextPart, TextType


def _msg(id, topic_id, text="Hi"):
    return make_message(
        id=id,
        date=datetime(2024, 1, 1),
        from_id=1,
        from_name="A",
        text=[TextPart(type=TextType.text, text=text)],
        topic_id=topic_id,
    )


def test_topic_messages_grouped_by_topic():
    topics = [
        ForumTopic(
            id=1, title="General", icon_emoji=None, is_closed=False, is_pinned=True, messages_count=100
        ),
        ForumTopic(
            id=2, title="Off-topic", icon_emoji=None, is_closed=False, is_pinned=False, messages_count=50
        ),
    ]
    messages = [_msg(1, 1, "Hi"), _msg(2, 2, "OT")]
    grouped = group_by_topic(messages, topics)
    assert len(grouped) == 2
    assert grouped[1][0].text[0].text == "Hi"
    assert grouped[2][0].text[0].text == "OT"
