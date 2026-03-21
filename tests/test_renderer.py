import pytest
from pathlib import Path
from datetime import datetime
from tg_export.html.renderer import HtmlRenderer, is_joined, render_text_parts
from tg_export.models import (
    Message, TextPart, TextType, Chat, ChatType,
    PhotoMedia, MediaType, FileInfo,
)
from tg_export.config import OutputConfig


def _make_msg(id=1, chat_id=1, date=None, from_id=1, from_name="Test", text="Hello",
              media=None, action=None, grouped_id=None):
    return Message(
        id=id, chat_id=chat_id,
        date=date or datetime(2024, 1, 1, 10, 0),
        edited=None, from_id=from_id, from_name=from_name,
        text=[TextPart(type=TextType.text, text=text)] if text else [],
        media=media, action=action, reply_to_msg_id=None,
        reply_to_peer_id=None, forwarded_from=None,
        reactions=[], is_outgoing=False, signature=None,
        via_bot_id=None, saved_from_chat_id=None,
        inline_buttons=None, topic_id=None, grouped_id=grouped_id,
    )


@pytest.fixture
def renderer(tmp_path):
    config = OutputConfig(
        path=str(tmp_path / "output"),
        format="html",
        messages_per_file=1000,
    )
    r = HtmlRenderer(output_dir=tmp_path / "output", config=config)
    r.setup()
    return r


def test_setup_copies_static(renderer, tmp_path):
    output = tmp_path / "output"
    assert (output / "css" / "style.css").exists()
    assert (output / "js" / "script.js").exists()
    assert (output / "images").is_dir()


def test_render_message_plain_text(renderer):
    msg = _make_msg(from_name="Иван", text="Привет")
    html = renderer.render_message(msg, prev_msg=None)
    assert "Привет" in html
    assert "Иван" in html
    assert 'class="message' in html


def test_render_message_joined(renderer):
    msg1 = _make_msg(id=1, date=datetime(2024, 1, 1, 10, 0), text="First")
    msg2 = _make_msg(id=2, date=datetime(2024, 1, 1, 10, 5), text="Second")
    html = renderer.render_message(msg2, prev_msg=msg1)
    assert "joined" in html


def test_is_joined_different_author():
    msg1 = _make_msg(id=1, from_id=1)
    msg2 = _make_msg(id=2, from_id=2)
    assert is_joined(msg2, msg1) is False


def test_is_joined_too_far_apart():
    msg1 = _make_msg(id=1, date=datetime(2024, 1, 1, 10, 0))
    msg2 = _make_msg(id=2, date=datetime(2024, 1, 1, 11, 0))  # 1 hour later
    assert is_joined(msg2, msg1) is False


def test_render_text_parts_formatting():
    parts = [
        TextPart(type=TextType.text, text="Hello "),
        TextPart(type=TextType.bold, text="world"),
        TextPart(type=TextType.text, text="!"),
    ]
    html = render_text_parts(parts)
    assert '<span class="bold">world</span>' in html
    assert "Hello " in html


def test_render_chat_pagination(renderer, tmp_path):
    chat = Chat(
        id=123, name="Test", type=ChatType.personal,
        username=None, folder=None, members_count=None,
        last_message_date=None, messages_count=5,
        is_left=False, is_archived=False, is_forum=False, migrated_to_id=None,
        migrated_from_id=None, is_monoforum=False,
    )
    messages = [_make_msg(id=i, text=f"Msg {i}") for i in range(2500)]
    chat_dir = tmp_path / "output" / "unfiled" / "Test_123"
    renderer.render_chat(chat, messages, chat_dir)
    assert (chat_dir / "messages.html").exists()
    assert (chat_dir / "messages2.html").exists()
    assert (chat_dir / "messages3.html").exists()
    assert not (chat_dir / "messages4.html").exists()


def test_render_album(renderer):
    msgs = []
    for i in range(3):
        msgs.append(_make_msg(
            id=i+1, text="Album" if i == 2 else "",
            media=PhotoMedia(
                type=MediaType.photo,
                file=FileInfo(id=i+100, size=1000, name=f"photo_{i}.jpg",
                              mime_type="image/jpeg", local_path=None),
                width=800, height=600,
            ),
            grouped_id=12345,
        ))
    html = renderer.render_album(msgs)
    assert 'class="message"' in html
    assert "Album" in html
