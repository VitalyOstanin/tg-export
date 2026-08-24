"""HTML renderer with Jinja2 templates."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
from collections.abc import Callable
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, NamedTuple, Self

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from tg_export.config import OutputConfig
from tg_export.format import (
    DAY_FORMAT,
    MOMENT_WITH_SECONDS_FORMAT,
    MONTH_KEY_FORMAT,
    MONTH_LABEL_FORMAT,
    TIME_FORMAT,
    format_moment,
    format_size,
)
from tg_export.media import MEDIA_SUBDIR_NAMES
from tg_export.models import (
    Chat,
    Message,
    TextPart,
    TextType,
)
from tg_export.state import UNKNOWN_MONTH_KEY

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

JOIN_WITHIN_SECONDS = 900  # 15 minutes


# Where the shared stylesheet and the script sit, relative to the root of the
# export. Written out nine times before -- and a tenth time in
# scripts/smoke_installed_package.py, which checks that both files are there.
CSS_PATH = "css/style.css"
JS_PATH = "js/script.js"


class HtmlRenderer:
    def __init__(self, output_dir: Path, config: OutputConfig) -> None:
        self.output_dir = output_dir
        self.config = config
        self.env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=select_autoescape(
                enabled_extensions=("html", "html.j2"),
                default_for_string=True,
                default=True,
            ),
        )
        # Register helpers for use in Jinja2 templates
        self.env.globals["render_text"] = render_text_parts
        self.env.globals["format_size"] = format_size
        self.env.filters["safe_href"] = _safe_href

    def setup(self) -> None:
        """Copy static resources to output directory."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for subdir in ("css", "js", "images"):
            src = STATIC_DIR / subdir
            dst = self.output_dir / subdir
            if src.exists():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)

    @staticmethod
    def _build_pages_info(month_keys: list[str]) -> list[dict]:
        pages_info = []
        for key in month_keys:
            filename = f"messages_{key}.html"
            if key == UNKNOWN_MONTH_KEY:
                label = "Unknown date"
            else:
                try:
                    dt = datetime.strptime(key, MONTH_KEY_FORMAT)
                    label = dt.strftime(MONTH_LABEL_FORMAT)
                except ValueError:
                    label = key
            pages_info.append({"key": key, "filename": filename, "label": label})
        return pages_info

    @staticmethod
    def _authored(item_type: str, moment, author: str | None, **extra) -> dict[str, Any]:
        """The fields every authored item carries: who wrote it and when.

        An album and a single message differ in what they hold, not in how they
        are stamped -- the four keys used to be written out twice.
        """
        return {
            "type": item_type,
            "author": author or "Unknown",
            "author_initial": (author or "?")[0].upper(),
            "time": format_moment(moment, fmt=TIME_FORMAT),
            "full_date": format_moment(moment, fmt=MOMENT_WITH_SECONDS_FORMAT),
            **extra,
        }

    @staticmethod
    def _build_items(processed: list[Any]) -> list[dict[str, Any]]:
        items: list[dict] = []
        prev_msg = None
        prev_date = None

        def open_day(moment) -> None:
            """Start a new day in the page when the previous item was another one."""
            nonlocal prev_date
            day = moment.date() if moment else None
            if day != prev_date:
                items.append({"type": "date_separator", "date": format_moment(moment, fmt=DAY_FORMAT)})
                prev_date = day

        for entry in processed:
            if isinstance(entry, list):
                first = entry[0]
                open_day(first.date)
                items.append(HtmlRenderer._authored("album", first.date, first.from_name, msgs=entry))
                prev_msg = entry[-1]
            else:
                msg = entry
                open_day(msg.date)

                if msg.action:
                    items.append({"type": "service", "msg": msg})
                else:
                    items.append(
                        HtmlRenderer._authored(
                            "message",
                            msg.date,
                            msg.from_name,
                            msg=msg,
                            joined=is_joined(msg, prev_msg),
                        )
                    )
                prev_msg = msg
        return items

    def _render_page(
        self,
        *,
        chat: Chat,
        chat_dir: Path,
        items: list[dict],
        pages_info: list[dict],
        page_idx: int,
        rel: str,
        template,
    ) -> None:
        pinfo = pages_info[page_idx]
        prev_href = pages_info[page_idx - 1]["filename"] if page_idx > 0 else None
        next_href = pages_info[page_idx + 1]["filename"] if page_idx < len(pages_info) - 1 else None
        html = template.render(
            title=f"{chat.name} - {pinfo['label']} - tg-export",
            css_path=f"{rel}/{CSS_PATH}",
            js_path=f"{rel}/{JS_PATH}",
            chat_name=chat.name,
            chat_type=chat.type.value,
            chat_members=chat.members_count,
            index_href=f"{rel}/index.html",
            prev_href=prev_href,
            next_href=next_href,
            page_label=pinfo["label"],
            pages_info=pages_info,
            current_page=pinfo["filename"],
            items=items,
        )
        (chat_dir / pinfo["filename"]).write_text(html, encoding="utf-8")

    def render_chat(self, chat: Chat, messages: list[Message], chat_dir: Path) -> None:
        """Render chat split by month with TOC and prev/next navigation.

        In-memory variant: requires the full message list.
        Prefer render_chat_streaming for large chats.
        """
        chat_dir.mkdir(parents=True, exist_ok=True)

        for old in chat_dir.glob("messages*.html"):
            old.unlink()

        anchors = _PathAnchors.for_chat(chat_dir)
        for msg in messages:
            _fix_media_path(msg, chat_dir, anchors)

        processed = _group_albums(messages)

        monthly: dict[str, list] = {}
        for entry in processed:
            first_msg = entry[0] if isinstance(entry, list) else entry
            key = format_moment(first_msg.date, fmt=MONTH_KEY_FORMAT) or UNKNOWN_MONTH_KEY
            monthly.setdefault(key, []).append(entry)

        if not monthly:
            monthly = {UNKNOWN_MONTH_KEY: []}

        month_keys = sorted(monthly.keys())
        pages_info = self._build_pages_info(month_keys)
        rel = _relative_path(chat_dir, self.output_dir)
        template = self.env.get_template("chat.html.j2")

        for page_idx, pinfo in enumerate(pages_info):
            items = self._build_items(monthly[pinfo["key"]])
            self._render_page(
                chat=chat,
                chat_dir=chat_dir,
                items=items,
                pages_info=pages_info,
                page_idx=page_idx,
                rel=rel,
                template=template,
            )

        if pages_info:
            redirect_html = f'<!DOCTYPE html><html><head><meta http-equiv="refresh" content="0;url={pages_info[0]["filename"]}"></head></html>'
            (chat_dir / "messages.html").write_text(redirect_html, encoding="utf-8")

    def render_chat_streaming(
        self,
        chat: Chat,
        month_keys: list[str],
        load_month: Callable[[str], list[Message]],
        chat_dir: Path,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        """Render chat month-by-month, loading each month on demand.

        Why: large chats (100k+ messages) consume hundreds of MB if all messages
        are materialised at once; streaming keeps peak memory proportional to
        one month.

        load_month(month_key) must return the message list for that month.
        Albums spanning month boundaries are not supported (Telegram albums are
        created within seconds, so this is a non-issue in practice).

        should_stop: optional callable. Called before loading each month and
        before rendering its page; if it returns True, rendering aborts (and
        the redirect file is NOT written, to leave a clear partial-state signal
        for next run). Why: render runs inside asyncio.to_thread, and the
        worker thread cannot be cancelled by task.cancel — without a checkpoint
        the executor blocks asyncio shutdown until the whole chat is rendered.
        """
        chat_dir.mkdir(parents=True, exist_ok=True)

        for old in chat_dir.glob("messages*.html"):
            old.unlink()

        if not month_keys:
            month_keys = [UNKNOWN_MONTH_KEY]
        pages_info = self._build_pages_info(sorted(month_keys))
        rel = _relative_path(chat_dir, self.output_dir)
        template = self.env.get_template("chat.html.j2")
        anchors = _PathAnchors.for_chat(chat_dir)

        aborted = False
        for page_idx, pinfo in enumerate(pages_info):
            if should_stop and should_stop():
                aborted = True
                break
            messages = load_month(pinfo["key"])
            for msg in messages:
                _fix_media_path(msg, chat_dir, anchors)
            processed = _group_albums(messages)
            items = self._build_items(processed)
            self._render_page(
                chat=chat,
                chat_dir=chat_dir,
                items=items,
                pages_info=pages_info,
                page_idx=page_idx,
                rel=rel,
                template=template,
            )

        if pages_info and not aborted:
            redirect_html = f'<!DOCTYPE html><html><head><meta http-equiv="refresh" content="0;url={pages_info[0]["filename"]}"></head></html>'
            (chat_dir / "messages.html").write_text(redirect_html, encoding="utf-8")

    def render_index(
        self,
        folders_list: list[dict[str, Any]],
        unfiled: list[dict[str, Any]],
        sections: list[dict[str, Any]],
    ) -> None:
        """Render main index page.

        folders_list: list of {name, href, chats} dicts.
        """
        template = self.env.get_template("index.html.j2")
        html = template.render(
            title="Telegram Export - tg-export",
            css_path=CSS_PATH,
            js_path=JS_PATH,
            generated=format_moment(datetime.now()),
            folders_list=folders_list,
            unfiled=unfiled,
            sections=sections,
        )
        (self.output_dir / "index.html").write_text(html, encoding="utf-8")

    def render_folder_index(self, folder_name: str, chats: list[dict]) -> None:
        """Render per-folder index page with chat list."""
        from tg_export.exporter import sanitize_name

        folder_dir = self.output_dir / "folders" / sanitize_name(folder_name)
        folder_dir.mkdir(parents=True, exist_ok=True)
        rel = _relative_path(folder_dir, self.output_dir)

        template = self.env.get_template("folder_index.html.j2")
        html = template.render(
            title=f"{folder_name} - tg-export",
            css_path=f"{rel}/{CSS_PATH}",
            js_path=f"{rel}/{JS_PATH}",
            folder_name=folder_name,
            index_href=f"{rel}/index.html",
            chats=chats,
        )
        (folder_dir / "index.html").write_text(html, encoding="utf-8")

    def render_personal_info(self, user_data: dict[str, Any]) -> None:
        """Render personal information page."""
        self._render_global_page(
            "personal_info.html.j2", "personal_info.html", "Personal Information", **user_data
        )

    def render_contacts(self, contacts: list[dict], frequent: list[dict]) -> None:
        """Render contacts page."""
        self._render_global_page(
            "contacts.html.j2", "contacts.html", "Contacts", contacts=contacts, frequent=frequent
        )

    def render_sessions(self, app_sessions: list[dict], web_sessions: list[dict]) -> None:
        """Render sessions page."""
        self._render_global_page(
            "sessions.html.j2",
            "sessions.html",
            "Active Sessions",
            app_sessions=app_sessions,
            web_sessions=web_sessions,
        )

    def render_userpics(self, photos: list[dict]) -> None:
        """Render profile photos gallery page."""
        self._render_global_page("userpics.html.j2", "userpics.html", "Profile Photos", photos=photos)

    def render_stories(self, stories: list[dict]) -> None:
        """Render stories page."""
        self._render_global_page("stories.html.j2", "stories.html", "Stories", stories=stories)

    def render_other_data(self, data: dict[str, Any]) -> None:
        """Render other data page."""
        self._render_global_page("other_data.html.j2", "other_data.html", "Other Data", **data)

    def _render_global_page(self, template_name: str, filename: str, title: str, **payload) -> None:
        """Render one page of the global data and write it into the export root.

        The six pages differ in template, file name, title and payload; the
        rest -- the asset paths, the link back to the index and the write --
        was written out once per page, seven copies of the same five lines.
        """
        template = self.env.get_template(template_name)
        html = template.render(
            title=f"{title} - tg-export",
            css_path=CSS_PATH,
            js_path=JS_PATH,
            index_href="index.html",
            **payload,
        )
        (self.output_dir / filename).write_text(html, encoding="utf-8")

    # -- Private rendering helpers --


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def is_joined(msg: Message, prev_msg: Message | None) -> bool:
    """Check if message should be visually joined with previous."""
    if prev_msg is None:
        return False
    if msg.action or prev_msg.action:
        return False
    if msg.forwarded_from or prev_msg.forwarded_from:
        return False
    if msg.from_id != prev_msg.from_id:
        return False
    if msg.date and prev_msg.date:
        delta = (msg.date - prev_msg.date).total_seconds()
        if abs(delta) > JOIN_WITHIN_SECONDS:
            return False
    return True


_SAFE_URL_SCHEMES = ("http://", "https://", "mailto:", "tel:", "tg://")

# Why: minimal RFC-5321 sanity check before building "mailto:". Reject anything
# that looks like an injection vector (CR/LF, quotes, angle brackets, control
# chars), not full RFC validation.
_EMAIL_RE = re.compile(r"^[^\s@<>\"']+@[^\s@<>\"']+\.[^\s@<>\"']+$")
_PHONE_RE = re.compile(r"^\+?[\d\s().-]+$")


def _safe_href(url: str) -> str:
    """Return URL if it uses a safe scheme, otherwise '#'.

    Why: protect HTML output from javascript:/data:/vbscript: injections via
    Telegram URL/text_url entities or inline-button URLs.
    """
    if not url:
        return "#"
    stripped = url.lstrip().lower()
    if stripped.startswith(_SAFE_URL_SCHEMES):
        return url
    # A pages of the export is opened as a file, so what the document resolves
    # a scheme-less address against is `file:`. `//host/path` becomes
    # `file://host/path`, and `\\host\share` is normalised into the same
    # thing by Chromium-based browsers -- on Windows that is a request to an
    # SMB share. The sender of the message chooses the address in full, so
    # neither shape is a relative link inside the export.
    # Both slashes, in any mix: for the `file:` scheme a browser reads `/` and
    # `\` alike, so `/\host\share` and `\/host/share` resolve to the same
    # remote address as the two even shapes.
    if len(stripped) >= 2 and stripped[0] in "/\\" and stripped[1] in "/\\":
        return "#"
    if stripped.startswith(("/", "#", "?")):
        return url
    if "://" not in stripped and ":" not in stripped:
        # likely relative URL like "photos/file.jpg"
        return url
    return "#"


# Types that differ only by the CSS class of a plain <span>. The forms that
# need more than that -- pre, blockquote, links, email, phone -- stay in the
# chain below.
_SIMPLE_SPAN_CLASSES: dict[TextType, str] = {
    TextType.bold: "bold",
    TextType.italic: "italic",
    TextType.underline: "underline",
    TextType.strikethrough: "strikethrough",
    TextType.code: "code",
    TextType.spoiler: "spoiler",
    TextType.mention: "mention",
    TextType.mention_name: "mention",
    TextType.hashtag: "hashtag",
    TextType.bot_command: "bot-command",
    TextType.cashtag: "cashtag",
}


def render_text_parts(parts: list[TextPart]) -> Markup:
    """Render TextPart list to HTML Markup (safe for autoescape templates)."""
    result = []
    for tp in parts:
        text = escape(tp.text)
        span_class = _SIMPLE_SPAN_CLASSES.get(tp.type)
        if span_class is not None:
            result.append(f'<span class="{span_class}">{text}</span>')
        elif tp.type == TextType.text:
            result.append(text.replace("\n", "<br>"))
        elif tp.type == TextType.pre:
            result.append(f'<pre class="pre">{text}</pre>')
        elif tp.type == TextType.blockquote:
            result.append(f'<div class="blockquote">{text}</div>')
        elif tp.type == TextType.url:
            href = escape(_safe_href(tp.text))
            result.append(
                f'<a class="url" href="{href}" target="_blank" rel="noopener noreferrer">{text}</a>'
            )
        elif tp.type == TextType.text_url:
            href = escape(_safe_href(tp.href or ""))
            result.append(
                f'<a class="text-url" href="{href}" target="_blank" rel="noopener noreferrer">{text}</a>'
            )
        elif tp.type == TextType.email:
            if _EMAIL_RE.match(tp.text):
                href = escape(_safe_href(f"mailto:{tp.text}"))
                result.append(f'<a href="{href}">{text}</a>')
            else:
                result.append(text)
        elif tp.type == TextType.phone:
            if _PHONE_RE.match(tp.text):
                href = escape(_safe_href(f"tel:{tp.text}"))
                result.append(f'<a href="{href}">{text}</a>')
            else:
                result.append(text)
        else:
            # custom_emoji included: types without markup of their own go out as text.
            result.append(text)
    return Markup("".join(result))


def _group_albums(messages: list[Message]) -> list[Message | list[Message]]:
    """Group messages by grouped_id into albums."""
    result = []
    album_buffer: list[Message] = []
    current_group_id = None

    for msg in messages:
        if msg.grouped_id is not None:
            if msg.grouped_id == current_group_id:
                album_buffer.append(msg)
            else:
                if album_buffer:
                    result.append(album_buffer if len(album_buffer) > 1 else album_buffer[0])
                album_buffer = [msg]
                current_group_id = msg.grouped_id
        else:
            if album_buffer:
                result.append(album_buffer if len(album_buffer) > 1 else album_buffer[0])
                album_buffer = []
                current_group_id = None
            result.append(msg)

    if album_buffer:
        result.append(album_buffer if len(album_buffer) > 1 else album_buffer[0])

    return result


def _relative_path(from_dir: Path, to_dir: Path) -> str:
    """Compute relative path from from_dir to to_dir.

    Why os.path.relpath: handles cross-drive paths on Windows by raising
    ValueError, which we fall back to absolute. Manual parent-walk could
    enter an infinite loop on Windows roots (C:\\ → C:\\).
    """
    try:
        return os.path.relpath(to_dir, start=from_dir).replace("\\", "/")
    except ValueError:
        return str(to_dir)


class _PathAnchors(NamedTuple):
    """The two paths a relative media path is measured against.

    Both are the same for every message of a chat, and both cost a filesystem
    call: `Path.cwd()` and `Path.resolve()` were made per message, so a chat of
    a hundred thousand messages asked the filesystem two hundred thousand times
    for one pair of values.
    """

    cwd: Path
    chat_root: Path

    @classmethod
    def for_chat(cls, chat_dir: Path) -> Self:
        return cls(Path.cwd(), chat_dir.resolve())


def _fix_media_path(msg: Message, chat_dir: Path, anchors: _PathAnchors) -> None:
    """Make media local_path relative to chat_dir for correct HTML references."""
    media = msg.media
    if media is None:
        return
    file_obj = getattr(media, "file", None)
    if file_obj is None or not file_obj.local_path:
        return
    p = Path(file_obj.local_path)
    if p.is_absolute():
        with contextlib.suppress(ValueError):
            file_obj.local_path = str(p.relative_to(chat_dir))
    else:
        # Relative path like "export_output/account/unfiled/Chat_123/photos/file.jpg"
        # Try to find chat_dir suffix in the path
        try:
            resolved = anchors.cwd / p
            file_obj.local_path = str(resolved.relative_to(anchors.chat_root))
        except ValueError:
            # Last resort: just keep the filename parts after the media subdir
            parts = p.parts
            for i, part in enumerate(parts):
                if part in MEDIA_SUBDIR_NAMES:
                    file_obj.local_path = str(Path(*parts[i:]))
                    return
