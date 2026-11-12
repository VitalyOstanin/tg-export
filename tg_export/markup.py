"""Message markup for `tg send` and `tg edit`: Markdown or HTML into Telegram entities.

Telegram keeps a message as plain text plus a list of entities -- bold from
offset 4 for 5 characters, a code block with a language, and so on. Telethon
turns marked-up text into that list and ships two parsers; each lacks part of
what Telegram can show, and the gaps are filled here:

- Markdown gets ``||spoiler||`` and the language of a code block
  (```` ```python ````), and loses the line breaks that open and close the
  block: Telethon kept them inside the block and took the language for its
  first line of code.
- HTML gets the spoiler (``<tg-spoiler>``, ``<span class="tg-spoiler">``) and
  the ``<ins>`` and ``<strike>`` spellings of underline and strikethrough.

Mentions of a user (``tg://user?id=N``) are not resolved here: Telethon turns
such a link into a mention when it sends the message.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

from telethon.extensions import html, markdown
from telethon.extensions.html import HTMLToTelegramParser
from telethon.helpers import add_surrogate, del_surrogate, strip_text
from telethon.tl.types import MessageEntityPre, MessageEntitySpoiler

PARSE_MODES = ("md", "html", "none")
DEFAULT_PARSE_MODE = "md"

PARSE_MODE_HELP = (
    "How the text is marked up: md (Markdown, the default), html, or none (sent "
    "exactly as written). See MARKUP below"
)

# Shown at the end of the help of `tg send` and `tg edit`. "\b" keeps click
# from rewrapping the lines. The same table in Russian is in docs/cli.md
# (the markup section of `tg edit`); a test keeps the two lists of html tags equal.
MARKUP_EPILOG = """\b
MARKUP (--parse-mode)
  element               md                        html
  bold                  **text**                  <b>text</b>
  italic                __text__                  <i>text</i>
  underline             -                         <u>text</u>
  strikethrough         ~~text~~                  <s>text</s>
  spoiler               ||text||                  <tg-spoiler>text</tg-spoiler>
  inline code           `12345`                   <code>12345</code>
  code block            ```python + lines + ```   <pre><code class="language-python">..</code></pre>
  link                  [text](https://host/a)    <a href="https://host/a">text</a>
  mention of a user     [name](tg://user?id=42)   <a href="tg://user?id=42">name</a>
  quote                 -                         <blockquote>text</blockquote>
  collapsed quote       -                         <blockquote expandable>text</blockquote>
  html also takes <strong> <em> <ins> <del> <strike> and <span class="tg-spoiler">.

\b
  md has no escaping: text with ** __ ~~ || or ` that must stay as it is
  goes with --parse-mode none. In html, write & < > as &amp; &lt; &gt;.
  An id in a code block or in inline code is copied with one tap.
"""


_MD_DELIMITERS = {**markdown.DEFAULT_DELIMITERS, "||": MessageEntitySpoiler}
# What may follow ``` on its line to name the language of the block.
_LANGUAGE_LINE = re.compile(r"([\w+#.-]*)\n")


def _cut(text: str, entities: list[Any], start: int, count: int) -> str:
    """Remove ``count`` units at ``start`` from surrogate-added text, keeping the entities aligned."""

    def moved(position: int) -> int:
        if position <= start:
            return position
        return start if position < start + count else position - count

    for entity in entities:
        begin, end = moved(entity.offset), moved(entity.offset + entity.length)
        entity.offset, entity.length = begin, end - begin
    return text[:start] + text[start + count :]


def _tidy_code_blocks(message: str, entities: list[Any]) -> tuple[str, list[Any]]:
    """Take the language and the framing line breaks out of every code block."""
    text = add_surrogate(message)
    cuts: list[tuple[int, int]] = []
    for entity in entities:
        if not isinstance(entity, MessageEntityPre):
            continue
        body = text[entity.offset : entity.offset + entity.length]
        head = _LANGUAGE_LINE.match(body)
        start = 0
        if head is not None and head.end() < len(body):
            if head.group(1):
                entity.language = head.group(1)
            cuts.append((entity.offset, head.end()))
            start = head.end()
        if body.endswith("\n") and len(body) - 1 > start:
            cuts.append((entity.offset + entity.length - 1, 1))
    for start, count in sorted(cuts, reverse=True):
        text = _cut(text, entities, start, count)
    return del_surrogate(text), [e for e in entities if e.length > 0]


# Telethon strips the whitespace around the parsed text before the code blocks
# are tidied, and a block at the very start would lose its opening line break:
# its first line of code would then pass for the language. A non-blank mark at
# each end keeps the text whole until the blocks are done.
_EDGE = "\x00"


def parse_markdown(message: str) -> tuple[str, list[Any]]:
    """Markdown with the spoiler and code-block languages into text and entities."""
    if not message:
        return message, []
    text, entities = markdown.parse(_EDGE + message + _EDGE, delimiters=_MD_DELIMITERS)
    text, entities = _tidy_code_blocks(text, entities)
    surrogate = add_surrogate(text)
    surrogate = _cut(surrogate, entities, len(surrogate) - 1, 1)
    surrogate = _cut(surrogate, entities, 0, 1)
    entities = [e for e in entities if e.length > 0]
    return del_surrogate(strip_text(surrogate, entities)), entities


class _HtmlParser(HTMLToTelegramParser):
    """Telethon's HTML parser with the spoiler and two more spellings.

    Checked against Telethon 1.45: it relies on the parser's private
    ``_open_tags``, ``_open_tags_meta`` and ``_building_entities``, and
    `parse_html` repeats the body of ``html.parse``. Check both again when
    Telethon is updated.
    """

    _ALIASES = {"ins": "u", "strike": "s"}
    _SPOILER = "tg-spoiler"

    def __init__(self) -> None:
        super().__init__()
        # Whether each open <span> is a spoiler, to close it the same way.
        self._spans: list[bool] = []
        # Telethon builds one entity per tag name, so a spoiler inside a
        # spoiler would close the outer one: only the outermost one counts.
        self._spoiler_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "span":
            spoiler = self._SPOILER in (dict(attrs).get("class") or "").split()
            self._spans.append(spoiler)
            tag = self._SPOILER if spoiler else tag
        tag = self._ALIASES.get(tag, tag)
        if tag != self._SPOILER:
            super().handle_starttag(tag, attrs)
            return
        self._open_tags.appendleft(tag)
        self._open_tags_meta.appendleft(None)
        self._spoiler_depth += 1
        if self._spoiler_depth == 1:
            self._building_entities[tag] = MessageEntitySpoiler(offset=len(self.text), length=0)

    def handle_endtag(self, tag: str) -> None:
        if tag == "span" and self._spans and self._spans.pop():
            tag = self._SPOILER
        tag = self._ALIASES.get(tag, tag)
        if tag == self._SPOILER and self._spoiler_depth:
            self._spoiler_depth -= 1
            if self._spoiler_depth:
                # Inner spoiler: drop its open tag, keep the outer entity.
                tag = "span"
        super().handle_endtag(tag)


def parse_html(message: str) -> tuple[str, list[Any]]:
    """HTML with the spoiler into text and entities, by the steps of Telethon's `html.parse`."""
    if not message:
        return message, []
    parser = _HtmlParser()
    parser.feed(add_surrogate(message))
    # Without close() the parser keeps back text it has not decided on yet,
    # such as a trailing "&".
    parser.close()
    text = strip_text(parser.text, parser.entities)
    parser.entities.reverse()
    parser.entities.sort(key=lambda entity: entity.offset)
    return del_surrogate(text), parser.entities


_MODES = {
    "md": SimpleNamespace(parse=parse_markdown, unparse=markdown.unparse),
    "html": SimpleNamespace(parse=parse_html, unparse=html.unparse),
    "none": None,
}


def parse_mode(name: str) -> Any:
    """The object Telethon takes as ``parse_mode`` for one of `PARSE_MODES`; None sends the text as is."""
    return _MODES[name]
