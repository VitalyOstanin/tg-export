from datetime import datetime
from pathlib import Path

import pytest
from conftest import make_chat, make_message

from tg_export.config import OutputConfig
from tg_export.html.renderer import HtmlRenderer, is_joined, render_text_parts
from tg_export.models import (
    FileInfo,
    MediaType,
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
    return make_message(
        id=id,
        chat_id=chat_id,
        date=date or datetime(2024, 1, 1, 10, 0),
        from_id=from_id,
        from_name=from_name,
        text=[TextPart(type=TextType.text, text=text)] if text else [],
        media=media,
        action=action,
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


def _chat(chat_id=1, name="Chat"):
    return make_chat(id=chat_id, name=name)


def _render(renderer, tmp_path, messages):
    """Отрендерить сообщения рабочим путём и вернуть готовый HTML.

    Разметку собирают шаблоны; отдельной сборки строк на Python больше нет,
    поэтому проверки идут по тому же выводу, который получает пользователь.
    """
    chat_dir = tmp_path / "rendered"
    renderer.render_chat_streaming(
        chat=_chat(),
        month_keys=["2024-01"],
        load_month=lambda key: messages,
        chat_dir=chat_dir,
    )
    pages = [p for p in sorted(chat_dir.glob("messages*.html")) if p.name != "messages.html"]
    return "\n".join(p.read_text(encoding="utf-8") for p in pages)


def test_setup_copies_static(renderer, tmp_path):
    output = tmp_path / "output"
    assert (output / "css" / "style.css").exists()
    assert (output / "js" / "script.js").exists()
    assert (output / "images").is_dir()


def test_render_message_plain_text(renderer, tmp_path):
    msg = _make_msg(from_name="Иван", text="Привет")
    html = _render(renderer, tmp_path, [msg])
    assert "Привет" in html
    assert "Иван" in html
    assert 'class="message' in html


def test_render_message_joined(renderer, tmp_path):
    msg1 = _make_msg(id=1, date=datetime(2024, 1, 1, 10, 0), text="First")
    msg2 = _make_msg(id=2, date=datetime(2024, 1, 1, 10, 5), text="Second")
    html = _render(renderer, tmp_path, [msg1, msg2])
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
    chat = make_chat(id=123, name="Test", messages_count=5)
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


def test_render_chat_streaming_respects_should_stop(renderer, tmp_path):
    # На force-shutdown рендерер должен прерваться между месяцами и не
    # тянуть jinja2 для оставшихся buckets — иначе ThreadPoolExecutor не
    # завершится и asyncio.run зависнет на shutdown_default_executor.
    chat = make_chat(id=777, name="Stoppable", messages_count=3)
    month_keys = ["2024-01", "2024-02", "2024-03"]
    msgs_by_month = {
        "2024-01": [_make_msg(id=1, text="Jan", date=datetime(2024, 1, 15))],
        "2024-02": [_make_msg(id=2, text="Feb", date=datetime(2024, 2, 15))],
        "2024-03": [_make_msg(id=3, text="Mar", date=datetime(2024, 3, 15))],
    }
    calls = []

    def load_month(key):
        calls.append(key)
        return msgs_by_month[key]

    stop_after = 1  # рендерим только первый месяц

    def should_stop():
        return len(calls) >= stop_after

    chat_dir = tmp_path / "output" / "unfiled" / "Stoppable_777"
    renderer.render_chat_streaming(chat, month_keys, load_month, chat_dir, should_stop=should_stop)

    # Первый месяц отрендерен.
    assert (chat_dir / "messages_2024-01.html").exists()
    # Второй и третий — нет: цикл прерван до load_month("2024-02").
    assert not (chat_dir / "messages_2024-02.html").exists()
    assert not (chat_dir / "messages_2024-03.html").exists()
    assert calls == ["2024-01"]


def test_render_chat_escapes_xss_in_chat_name(renderer, tmp_path):
    chat = make_chat(id=999, name='<script>alert("xss")</script>', messages_count=1)
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


def test_render_album(renderer, tmp_path):
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
    html = _render(renderer, tmp_path, msgs)
    assert 'class="message"' in html
    assert "Album" in html


def test_inline_button_url_goes_through_the_scheme_filter(renderer, tmp_path):
    """URL кнопки должен проходить проверку схемы: javascript: в кнопке иначе
    выполняется при нажатии на неё в открытой странице выгрузки."""
    from tg_export.models import InlineButton, InlineButtonType

    msg = _make_msg(text="see below")
    msg.inline_buttons = [[InlineButton(type=InlineButtonType.url, text="Click", data="javascript:alert(1)")]]

    html = _render(renderer, tmp_path, [msg])

    assert "javascript:" not in html, html
    assert 'href="#"' in html, html


def test_inline_button_keeps_an_ordinary_url(renderer, tmp_path):
    from tg_export.models import InlineButton, InlineButtonType

    msg = _make_msg(text="see below")
    msg.inline_buttons = [
        [InlineButton(type=InlineButtonType.url, text="Click", data="https://example.com/a?b=1")]
    ]

    html = _render(renderer, tmp_path, [msg])

    assert "https://example.com/a?b=1" in html, html


def test_reaction_label_is_escaped(renderer, tmp_path):
    """Эмодзи реакции приходит из Telegram и может быть произвольной строкой."""
    from tg_export.models import Reaction, ReactionType

    msg = _make_msg()
    msg.reactions = [
        Reaction(type=ReactionType.emoji, emoji="<img src=x onerror=alert(1)>", document_id=None, count=2)
    ]

    html = _render(renderer, tmp_path, [msg])

    assert "<img src=x" not in html, html
    assert "&lt;img" in html, html


def test_partially_rendered_chat_is_not_taken_for_a_current_one(tmp_path):
    """Прерванный рендер обязан достраиваться на следующем прогоне.

    Рендерер намеренно не пишет messages.html, когда его остановили между
    месяцами -- это и есть признак незавершённости. Проверка «страницы
    актуальны» смотрела на маску messages*.html, под которую подпадают уже
    записанные помесячные страницы, поэтому чат без новых сообщений больше
    никогда не дорисовывался: часть месяцев отсутствует, а ссылка с индекса
    ведёт на несуществующий messages.html.
    """
    from tg_export.exporter import Exporter, ExportStats

    chat_dir = tmp_path / "chat"
    chat_dir.mkdir()
    (chat_dir / "messages_2024-01.html").write_text("<html>", encoding="utf-8")

    stats = ExportStats()
    stats.begin_chat(messages_in_db=10, messages_total=10)

    assert Exporter._pages_are_current(chat_dir, stats) is False

    (chat_dir / "messages.html").write_text("<html>", encoding="utf-8")
    assert Exporter._pages_are_current(chat_dir, stats) is True


def test_custom_emoji_is_rendered_like_any_unhandled_type():
    """Ветка `custom_emoji` дословно повторяла финальный `else` и была удалена.

    `SIM114` такое не ловит: правило сравнивает соседние `elif`, но не `elif` с
    финальным `else`. Тест закрепляет, что специального вывода у этого типа нет,
    -- если он появится, ветку вернут вместе с проверкой.
    """
    emoji = render_text_parts([TextPart(type=TextType.custom_emoji, text="<b>x</b>")])
    plain = render_text_parts([TextPart(type=TextType.text, text="<b>x</b>")])

    assert emoji == plain == "&lt;b&gt;x&lt;/b&gt;"


def test_the_bucket_for_dateless_messages_is_one_constant():
    """Ключ-заглушка выписывался литералом в шести местах двух модулей.

    Записывает его SQL в `state.py`, а подпись страницы строит рендерер: смена
    значения в одном месте оставила бы «Unknown date» попыткой разобрать ключ
    как `%Y-%m`. Проверяются обе стороны связи -- значение и отсутствие копий.
    """
    from tg_export.html.renderer import HtmlRenderer
    from tg_export.state import UNKNOWN_MONTH_KEY

    pages = HtmlRenderer._build_pages_info([UNKNOWN_MONTH_KEY, "2024-01"])

    assert [p["label"] for p in pages] == ["Unknown date", "January 2024"]

    package = Path(__file__).resolve().parent.parent / "tg_export"
    copies = [
        f"{path.relative_to(package)}:{i}"
        for path in package.glob("**/*.py")
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "0000-00" in line and "UNKNOWN_MONTH_KEY =" not in line
    ]

    assert not copies, f"ключ выписан литералом мимо UNKNOWN_MONTH_KEY: {copies}"


def test_a_link_that_leaves_the_export_tree_is_dropped():
    """Ветка «относительная ссылка» пропускала адреса, ведущие на чужой хост.

    Экспорт открывают файлом, и в документе `file:` адрес `//host/path`
    разрешается в `file://host/path`, а `\\\\host\\share` браузеры на движке
    Chromium приводят к тому же виду. Отправитель сообщения выбирает `url`
    целиком, поэтому такая ссылка -- его решение, а не решение владельца
    выгрузки.
    """
    from tg_export.html.renderer import _safe_href

    assert _safe_href("//evil.example/x") == "#"
    assert _safe_href("\\\\evil.example\\share") == "#"
    assert _safe_href("photos/file.jpg") == "photos/file.jpg"
    assert _safe_href("/photos/file.jpg") == "/photos/file.jpg"
    assert _safe_href("#anchor") == "#anchor"
    assert _safe_href("https://example.com/x") == "https://example.com/x"
