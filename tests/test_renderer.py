from datetime import datetime

import pytest

from tg_export.config import OutputConfig
from tg_export.html.renderer import HtmlRenderer, is_joined, render_text_parts
from tg_export.models import (
    Chat,
    ChatType,
    FileInfo,
    MediaType,
    Message,
    PhotoMedia,
    TextPart,
    TextType,
)


def _make_msg(
    id=1,
    chat_id=1,
    date=None,
    from_id=1,
    from_name="Test",
    text="Hello",
    media=None,
    action=None,
    grouped_id=None,
):
    return Message(
        id=id,
        chat_id=chat_id,
        date=date or datetime(2024, 1, 1, 10, 0),
        edited=None,
        from_id=from_id,
        from_name=from_name,
        text=[TextPart(type=TextType.text, text=text)] if text else [],
        media=media,
        action=action,
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
        grouped_id=grouped_id,
    )


@pytest.fixture
def renderer(tmp_path):
    config = OutputConfig(
        path=str(tmp_path / "output"),
        format="html",
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


def test_render_text_parts_blocks_javascript_url():
    parts = [TextPart(type=TextType.url, text="javascript:alert(1)")]
    html = render_text_parts(parts)
    assert 'href="javascript:' not in html.lower()
    assert 'href="#"' in html
    assert 'rel="noopener noreferrer"' in html


def test_render_text_parts_blocks_data_text_url():
    parts = [TextPart(type=TextType.text_url, text="link", href="data:text/html,<script>alert(1)</script>")]
    html = render_text_parts(parts)
    assert "data:text/html" not in html
    assert 'href="#"' in html


def test_render_text_parts_keeps_https_url():
    parts = [TextPart(type=TextType.url, text="https://example.com/path?a=1")]
    html = render_text_parts(parts)
    assert 'href="https://example.com/path?a=1"' in html
    assert 'rel="noopener noreferrer"' in html


def test_render_chat_monthly_split(renderer, tmp_path):
    chat = Chat(
        id=123,
        name="Test",
        type=ChatType.personal,
        username=None,
        folder=None,
        members_count=None,
        last_message_date=None,
        messages_count=5,
        is_left=False,
        is_archived=False,
        is_forum=False,
        migrated_to_id=None,
        migrated_from_id=None,
        is_monoforum=False,
    )
    messages = (
        [_make_msg(id=i, text=f"Msg {i}", date=datetime(2024, 1, 15, 10, 0)) for i in range(1, 4)]
        + [_make_msg(id=i, text=f"Msg {i}", date=datetime(2024, 2, 10, 12, 0)) for i in range(4, 7)]
        + [_make_msg(id=i, text=f"Msg {i}", date=datetime(2024, 3, 5, 8, 0)) for i in range(7, 10)]
    )
    chat_dir = tmp_path / "output" / "unfiled" / "Test_123"
    renderer.render_chat(chat, messages, chat_dir)
    # Redirect file
    assert (chat_dir / "messages.html").exists()
    # Monthly files
    assert (chat_dir / "messages_2024-01.html").exists()
    assert (chat_dir / "messages_2024-02.html").exists()
    assert (chat_dir / "messages_2024-03.html").exists()
    # Check redirect points to first month
    redirect = (chat_dir / "messages.html").read_text()
    assert "messages_2024-01.html" in redirect
    # Check TOC exists in monthly file
    jan_html = (chat_dir / "messages_2024-01.html").read_text()
    assert "January 2024" in jan_html
    assert "February 2024" in jan_html  # in TOC
    # Check hover title on timestamp
    assert 'title="2024-01-15 10:00:00"' in jan_html


def test_render_chat_escapes_xss_in_chat_name(renderer, tmp_path):
    chat = Chat(
        id=999,
        name='<script>alert("xss")</script>',
        type=ChatType.personal,
        username=None,
        folder=None,
        members_count=None,
        last_message_date=None,
        messages_count=1,
        is_left=False,
        is_archived=False,
        is_forum=False,
        migrated_to_id=None,
        migrated_from_id=None,
        is_monoforum=False,
    )
    msg = _make_msg(
        id=1, text="<img src=x onerror=alert(1)>", from_name="<b>evil</b>", date=datetime(2024, 1, 1, 10, 0)
    )
    chat_dir = tmp_path / "output" / "unfiled" / "Test_999"
    renderer.render_chat(chat, [msg], chat_dir)
    html = (chat_dir / "messages_2024-01.html").read_text()
    # Никакого исполняемого <script>/<img onerror>/<b> в выводе
    assert "<script>alert" not in html
    assert "<img src=x onerror" not in html
    assert "<b>evil</b>" not in html
    # Зато присутствует экранированный текст
    assert "&lt;script&gt;" in html or "&#34;xss&#34;" in html or "&#x27;xss&#x27;" in html


def test_render_album(renderer):
    msgs = []
    for i in range(3):
        msgs.append(
            _make_msg(
                id=i + 1,
                text="Album" if i == 2 else "",
                media=PhotoMedia(
                    type=MediaType.photo,
                    file=FileInfo(
                        id=i + 100, size=1000, name=f"photo_{i}.jpg", mime_type="image/jpeg", local_path=None
                    ),
                    width=800,
                    height=600,
                ),
                grouped_id=12345,
            )
        )
    html = renderer.render_album(msgs)
    assert 'class="message"' in html
    assert "Album" in html
