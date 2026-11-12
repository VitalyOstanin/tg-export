"""Правка сообщений (`tg edit`) и разметка текста (`--parse-mode`) в `tg send` и `tg edit`."""

from __future__ import annotations

import asyncio
import contextlib
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner
from telethon.errors.rpcerrorlist import (
    MessageAuthorRequiredError,
    MessageEditTimeExpiredError,
    MessageNotModifiedError,
)
from telethon.tl.types import (
    MessageEntityBlockquote,
    MessageEntityBold,
    MessageEntityCode,
    MessageEntityItalic,
    MessageEntityPre,
    MessageEntitySpoiler,
    MessageEntityStrike,
    MessageEntityTextUrl,
    MessageEntityUnderline,
    MessageMediaDocument,
    MessageMediaWebPage,
    WebPageEmpty,
)

from tg_export import markup
from tg_export.cli import tg as cli_tg


def _spans(text, entities):
    """(тип, кусок текста) для каждой сущности; смещения в UTF-16, как у Telegram."""
    units = text.encode("utf-16-le")
    return [(type(e), units[e.offset * 2 : (e.offset + e.length) * 2].decode("utf-16-le")) for e in entities]


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def test_md_code_block_loses_its_framing_line_breaks():
    """Переносы сразу после ``` и перед закрывающими ``` в блок не входят."""
    text, entities = markup.parse_markdown("Номер:\n```\n12345\n```\nготово")

    assert text == "Номер:\n12345\nготово"
    assert _spans(text, entities) == [(MessageEntityPre, "12345")]
    assert entities[0].language == ""


@pytest.mark.parametrize(
    ("source", "language"),
    [("```\n4242\n4243\n```", ""), ("```go\n4242\n4243\n```", "go")],
)
def test_md_code_block_at_the_very_start_keeps_its_first_line(source, language):
    """Блок в самом начале текста: первая строка кода не принимается за язык."""
    text, entities = markup.parse_markdown(source)

    assert text == "4242\n4243"
    assert entities[0].language == language
    assert _spans(text, entities) == [(MessageEntityPre, "4242\n4243")]


@pytest.mark.parametrize(
    ("source", "text", "body"),
    [
        ("a\n```\nx\n```\nb", "a\nx\nb", "x"),
        ("t\n```a b\nc\n```\nz", "t\na b\nc\nz", "a b\nc"),
    ],
)
def test_md_code_block_loses_the_closing_line_break_with_or_without_language(source, text, body):
    """Перенос перед закрывающими ``` срезается, даже если первая строка -- код, а не язык."""
    got, entities = markup.parse_markdown(source)

    assert got == text
    assert _spans(got, entities) == [(MessageEntityPre, body)]


def test_md_code_block_takes_the_language():
    """Слово после ``` -- язык блока, а не первая строка кода."""
    text, entities = markup.parse_markdown("```python\nx = 1\ny = 2\n```")

    assert text == "x = 1\ny = 2"
    assert entities[0].language == "python"
    assert _spans(text, entities) == [(MessageEntityPre, "x = 1\ny = 2")]


def test_md_one_line_code_block_stays_as_is():
    """Блок в одну строку без переноса: языка нет, текст не трогается."""
    text, entities = markup.parse_markdown("```a b```")

    assert text == "a b"
    assert _spans(text, entities) == [(MessageEntityPre, "a b")]


def test_md_entities_after_a_code_block_keep_their_place():
    """Сущности после блока сдвигаются вместе с вырезанными переносами, в том числе за эмодзи."""
    text, entities = markup.parse_markdown("😀 ```go\nx\n``` `id 9` **жир** __кур__ ~~зач~~ ||тайна||")

    assert _spans(text, entities) == [
        (MessageEntityPre, "x"),
        (MessageEntityCode, "id 9"),
        (MessageEntityBold, "жир"),
        (MessageEntityItalic, "кур"),
        (MessageEntityStrike, "зач"),
        (MessageEntitySpoiler, "тайна"),
    ]


def test_md_link_and_mention_stay_text_urls():
    """Ссылка на пользователя остаётся ссылкой tg://user: упоминанием её делает Telethon при отправке."""
    text, entities = markup.parse_markdown("[сайт](https://example.org/a) [Имя](tg://user?id=42)")

    assert text == "сайт Имя"
    assert [e.url for e in entities] == ["https://example.org/a", "tg://user?id=42"]
    assert all(isinstance(e, MessageEntityTextUrl) for e in entities)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


def test_html_covers_telegram_formatting():
    """Все элементы Telegram, включая спойлер, ins/strike и сворачиваемую цитату."""
    text, entities = markup.parse_html(
        '<b>ж</b> <i>к</i> <ins>п</ins> <strike>з</strike> <span class="tg-spoiler">с</span> '
        '<tg-spoiler>т</tg-spoiler> <code>9</code> <pre><code class="language-go">x</code></pre> '
        '<blockquote expandable>ц</blockquote> <a href="tg://user?id=42">и</a>'
    )

    assert _spans(text, entities) == [
        (MessageEntityBold, "ж"),
        (MessageEntityItalic, "к"),
        (MessageEntityUnderline, "п"),
        (MessageEntityStrike, "з"),
        (MessageEntitySpoiler, "с"),
        (MessageEntitySpoiler, "т"),
        (MessageEntityCode, "9"),
        (MessageEntityPre, "x"),
        (MessageEntityBlockquote, "ц"),
        (MessageEntityTextUrl, "и"),
    ]
    pre = next(e for e in entities if isinstance(e, MessageEntityPre))
    quote = next(e for e in entities if isinstance(e, MessageEntityBlockquote))
    assert pre.language == "go"
    assert quote.collapsed


def test_html_plain_span_is_not_a_spoiler():
    """Обычный <span> разметки не даёт, и закрытие вложенного спойлера не путается."""
    text, entities = markup.parse_html('<span>а <span class="tg-spoiler">б</span></span> в')

    assert text == "а б в"
    assert _spans(text, entities) == [(MessageEntitySpoiler, "б")]


@pytest.mark.parametrize(
    ("source", "expected"),
    [("a & b", "a & b"), ("<b>x</b> y &amp; z", "x y & z"), ("tail &", "tail &")],
)
def test_html_keeps_the_text_after_an_ampersand(source, expected):
    """Текст после & и в конце сообщения не теряется в буфере разборщика."""
    text, _ = markup.parse_html(source)

    assert text == expected


@pytest.mark.parametrize(
    "source",
    [
        "<tg-spoiler>а <tg-spoiler>б</tg-spoiler> в</tg-spoiler>",
        '<span class="tg-spoiler">а <span class="x tg-spoiler">б</span> в</span>',
    ],
)
def test_html_nested_spoiler_keeps_the_outer_one_whole(source):
    """Вложенный спойлер не закрывает внешний раньше времени; class из нескольких слов распознаётся."""
    text, entities = markup.parse_html(source)

    assert _spans(text, entities) == [(MessageEntitySpoiler, "а б в")]


def test_html_spoiler_class_among_others():
    text, entities = markup.parse_html('<span class="note tg-spoiler">б</span>')

    assert _spans(text, entities) == [(MessageEntitySpoiler, "б")]


def test_none_mode_sends_text_as_is():
    assert markup.parse_mode("none") is None
    assert markup.parse_mode("md").parse is markup.parse_markdown


# ---------------------------------------------------------------------------
# tg edit
# ---------------------------------------------------------------------------


def _msg(*, out=True, media=None):
    return SimpleNamespace(id=7, out=out, media=media)


def _run_edit(client, text="новый `текст`", *, mode="md", entity: object = "entity"):
    api = MagicMock()
    api.client = client

    @contextlib.asynccontextmanager
    async def connected(_account):
        yield api, "acc"

    with (
        patch("tg_export.cli.common.connected_api", connected),
        patch.object(cli_tg, "resolve_chat", AsyncMock(return_value=entity)),
    ):
        return asyncio.run(cli_tg._tg_edit("me", 7, "acc", text, parse_mode=markup.parse_mode(mode)))


def _client(message, *, refuse=None):
    client = MagicMock()
    client.get_messages = AsyncMock(return_value=message)
    client.edit_message = AsyncMock(side_effect=refuse)
    return client


def test_edit_replaces_the_text_with_the_chosen_markup(capsys):
    """Текст уходит с выбранным режимом разметки, вывод называет сообщение."""
    client = _client(_msg())

    code = _run_edit(client, mode="html")

    assert code == 0
    args, kwargs = client.edit_message.await_args
    assert args == ("entity", 7, "новый `текст`")
    assert kwargs["parse_mode"] is markup.parse_mode("html")
    assert "edited the text of message 7" in capsys.readouterr().err


def test_edit_of_a_document_is_its_caption(capsys):
    """У документа правится подпись, и пустой текст её снимает."""
    client = _client(_msg(media=MessageMediaDocument()))

    code = _run_edit(client, "")

    assert code == 0
    assert client.edit_message.await_args.args[2] == ""
    assert "edited the caption of message 7" in capsys.readouterr().err


def test_edit_refuses_to_empty_a_text_message(capsys):
    """Текстовое сообщение нельзя оставить пустым: Telegram отказал бы, команда говорит сама."""
    client = _client(_msg())

    code = _run_edit(client, "  \n")

    assert code == 1
    client.edit_message.assert_not_awaited()
    assert "tg delete" in capsys.readouterr().err


def test_edit_link_preview_is_not_a_caption(capsys):
    """Превью ссылки с картинкой -- текстовое сообщение, а не подпись: пустым его не оставить."""
    client = _client(_msg(media=MessageMediaWebPage(webpage=WebPageEmpty(id=1))))

    assert _run_edit(client, "") == 1
    client.edit_message.assert_not_awaited()
    assert "tg delete" in capsys.readouterr().err


@pytest.mark.parametrize("source", ["** **", "<b> </b>"])
def test_edit_refuses_markup_that_leaves_no_text(capsys, source):
    """Разметка без видимого текста -- та же понятная ошибка, а не отказ Telethon."""
    client = _client(_msg())

    assert _run_edit(client, source, mode="html" if source.startswith("<") else "md") == 1
    client.edit_message.assert_not_awaited()
    assert "tg delete" in capsys.readouterr().err


def test_edit_missing_message(capsys):
    """Номер, которого в чате нет, -- ошибка без попытки правки."""
    client = _client(None)

    assert _run_edit(client) == 1
    client.edit_message.assert_not_awaited()
    assert "has no message 7" in capsys.readouterr().err


def test_edit_foreign_message(capsys):
    """Чужое сообщение не правится: так сказано до обращения к Telegram."""
    client = _client(_msg(out=False))

    assert _run_edit(client) == 1
    client.edit_message.assert_not_awaited()
    assert "not yours" in capsys.readouterr().err


def test_edit_post_of_another_channel_admin_goes_to_telegram():
    """В канале админ правит и чужие посты: решает Telegram, а не предварительная проверка."""
    client = _client(_msg(out=False))

    assert _run_edit(client, entity=SimpleNamespace(broadcast=True)) == 0
    client.edit_message.assert_awaited_once()


def test_not_yours_reads_the_same_before_and_after_telegram(capsys):
    """Предварительная проверка и отказ Telegram говорят одними словами."""
    _run_edit(_client(_msg(out=False)))
    before = capsys.readouterr().err
    _run_edit(_client(_msg(), refuse=MessageAuthorRequiredError(None)))
    assert capsys.readouterr().err == before


@pytest.mark.parametrize(
    ("refusal", "code", "said"),
    [
        (MessageNotModifiedError(None), 0, "nothing changed"),
        (MessageAuthorRequiredError(None), 1, "not yours"),
        (MessageEditTimeExpiredError(None), 1, "time for editing"),
        (RuntimeError("MESSAGE_TOO_LONG"), 1, "MESSAGE_TOO_LONG"),
    ],
)
def test_edit_refusals_are_explained(capsys, refusal, code, said):
    """Отказы Telegram объясняются; тот же текст -- не ошибка."""
    assert _run_edit(_client(_msg(), refuse=refusal)) == code
    assert said in capsys.readouterr().err


def test_edit_needs_a_text():
    """Без текста -- ошибка формы вызова, а пустой --text допустим (снять подпись)."""
    result = CliRunner().invoke(cli_tg.tg, ["edit", "me", "7"])
    assert result.exit_code == 2
    assert "--text" in result.output


def test_edit_refuses_both_text_sources(tmp_path):
    body = tmp_path / "t.md"
    body.write_text("x", encoding="utf-8")
    result = CliRunner().invoke(cli_tg.tg, ["edit", "me", "7", "--text", "a", "--text-file", str(body)])
    assert result.exit_code == 2


def test_edit_reads_the_text_file(tmp_path):
    """Текст из файла и режим разметки доходят до правки."""
    body = tmp_path / "t.html"
    body.write_text("<b>жир</b>", encoding="utf-8")
    with patch.object(cli_tg, "_tg_edit", AsyncMock(return_value=0)) as edit:
        result = CliRunner().invoke(
            cli_tg.tg, ["edit", "me", "7", "--text-file", str(body), "--parse-mode", "html"]
        )

    assert result.exit_code == 0, result.output
    assert edit.await_args is not None
    args, kwargs = edit.await_args
    assert args == ("me", 7, None, "<b>жир</b>")
    assert kwargs["parse_mode"] is markup.parse_mode("html")


@pytest.mark.parametrize("command", ["edit", "send"])
def test_help_lists_the_markup_examples_and_exit_codes(command):
    """Справка сама по себе достаточна: разметка, примеры, коды возврата."""
    out = CliRunner().invoke(cli_tg.tg, [command, "--help"], terminal_width=120).output
    expected = (
        "--parse-mode",
        "||text||",
        "<tg-spoiler>",
        "blockquote expandable",
        "Examples:",
        "Exit codes:",
    )
    for part in expected:
        assert part in out


def test_help_and_docs_list_the_same_html_tags():
    """Таблица разметки в справке и в docs/cli.md не расходится по набору тегов."""
    docs = (Path(__file__).parent.parent / "docs" / "cli.md").read_text(encoding="utf-8")
    section = docs.split("#### Разметка текста", 1)[1].split("\n### ", 1)[0]

    def tags(text):
        return set(re.findall(r"<([a-z][\w-]*)", text))

    assert tags(markup.MARKUP_EPILOG) == tags(section)


# ---------------------------------------------------------------------------
# tg send --parse-mode
# ---------------------------------------------------------------------------


def test_send_passes_the_markup():
    """Режим разметки доходит до send_message; none отправляет текст как есть."""
    api = MagicMock()
    api.client = AsyncMock()
    api.client.send_message.return_value = SimpleNamespace(id=55)

    @contextlib.asynccontextmanager
    async def connected(_account):
        yield api, "acc"

    with patch("tg_export.cli.common.connected_api", connected):
        asyncio.run(cli_tg._tg_send("acc", ["me"], "**x**", None, parse_mode=markup.parse_mode("none")))

    assert api.client.send_message.await_args.kwargs["parse_mode"] is None


def test_send_defaults_to_markdown():
    """Без флага текст разбирается как Markdown -- так было и раньше, через Telethon."""
    with patch.object(cli_tg, "_tg_send", AsyncMock(return_value=0)) as send:
        result = CliRunner().invoke(cli_tg.tg, ["send", "me", "--text", "`1`"])

    assert result.exit_code == 0, result.output
    assert send.await_args is not None
    assert send.await_args.kwargs["parse_mode"] is markup.parse_mode("md")
