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
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from tg_export.config import OutputConfig
from tg_export.format import format_size
from tg_export.media import MEDIA_SUBDIRS
from tg_export.models import (
    Chat,
    Message,
    TextPart,
    TextType,
)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

JOIN_WITHIN_SECONDS = 900  # 15 minutes


class HtmlRenderer:
    def __init__(self, output_dir: Path, config: OutputConfig):
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

    def setup(self):
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
            if key == "0000-00":
                label = "Unknown date"
            else:
                try:
                    dt = datetime.strptime(key, "%Y-%m")
                    label = dt.strftime("%B %Y")
                except ValueError:
                    label = key
            pages_info.append({"key": key, "filename": filename, "label": label})
        return pages_info

    @staticmethod
    def _build_items(processed: list[Any]) -> list[dict[str, Any]]:
        items: list[dict] = []
        prev_msg = None
        prev_date = None
        for entry in processed:
            if isinstance(entry, list):
                first = entry[0]
                msg_date = first.date.date() if first.date else None
                if msg_date != prev_date:
                    items.append(
                        {
                            "type": "date_separator",
                            "date": first.date.strftime("%B %d, %Y") if first.date else "",
                        }
                    )
                    prev_date = msg_date
                items.append(
                    {
                        "type": "album",
                        "msgs": entry,
                        "author": first.from_name or "Unknown",
                        "author_initial": (first.from_name or "?")[0].upper(),
                        "time": first.date.strftime("%H:%M") if first.date else "",
                        "full_date": first.date.strftime("%Y-%m-%d %H:%M:%S") if first.date else "",
                    }
                )
                prev_msg = entry[-1]
            else:
                msg = entry
                msg_date = msg.date.date() if msg.date else None
                if msg_date != prev_date:
                    items.append(
                        {
                            "type": "date_separator",
                            "date": msg.date.strftime("%B %d, %Y") if msg.date else "",
                        }
                    )
                    prev_date = msg_date

                if msg.action:
                    items.append({"type": "service", "msg": msg})
                else:
                    joined = is_joined(msg, prev_msg)
                    items.append(
                        {
                            "type": "message",
                            "msg": msg,
                            "joined": joined,
                            "author": msg.from_name or "Unknown",
                            "author_initial": (msg.from_name or "?")[0].upper(),
                            "time": msg.date.strftime("%H:%M") if msg.date else "",
                            "full_date": msg.date.strftime("%Y-%m-%d %H:%M:%S") if msg.date else "",
                        }
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
    ):
        pinfo = pages_info[page_idx]
        prev_href = pages_info[page_idx - 1]["filename"] if page_idx > 0 else None
        next_href = pages_info[page_idx + 1]["filename"] if page_idx < len(pages_info) - 1 else None
        html = template.render(
            title=f"{chat.name} - {pinfo['label']} - tg-export",
            css_path=f"{rel}/css/style.css",
            js_path=f"{rel}/js/script.js",
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

    def render_chat(self, chat: Chat, messages: list[Message], chat_dir: Path):
        """Render chat split by month with TOC and prev/next navigation.

        In-memory variant: requires the full message list.
        Prefer render_chat_streaming for large chats.
        """
        chat_dir.mkdir(parents=True, exist_ok=True)

        for old in chat_dir.glob("messages*.html"):
            old.unlink()

        for msg in messages:
            _fix_media_path(msg, chat_dir)

        processed = _group_albums(messages)

        monthly: dict[str, list] = {}
        for entry in processed:
            first_msg = entry[0] if isinstance(entry, list) else entry
            key = first_msg.date.strftime("%Y-%m") if first_msg.date else "0000-00"
            monthly.setdefault(key, []).append(entry)

        if not monthly:
            monthly = {"0000-00": []}

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
    ):
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
            month_keys = ["0000-00"]
        pages_info = self._build_pages_info(sorted(month_keys))
        rel = _relative_path(chat_dir, self.output_dir)
        template = self.env.get_template("chat.html.j2")

        aborted = False
        for page_idx, pinfo in enumerate(pages_info):
            if should_stop and should_stop():
                aborted = True
                break
            messages = load_month(pinfo["key"])
            for msg in messages:
                _fix_media_path(msg, chat_dir)
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
    ):
        """Render main index page.

        folders_list: list of {name, href, chats} dicts.
        """
        template = self.env.get_template("index.html.j2")
        html = template.render(
            title="Telegram Export - tg-export",
            css_path="css/style.css",
            js_path="js/script.js",
            generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
            folders_list=folders_list,
            unfiled=unfiled,
            sections=sections,
        )
        (self.output_dir / "index.html").write_text(html, encoding="utf-8")

    def render_folder_index(self, folder_name: str, chats: list[dict]):
        """Render per-folder index page with chat list."""
        from tg_export.exporter import sanitize_name

        folder_dir = self.output_dir / "folders" / sanitize_name(folder_name)
        folder_dir.mkdir(parents=True, exist_ok=True)
        rel = _relative_path(folder_dir, self.output_dir)

        template = self.env.get_template("folder_index.html.j2")
        html = template.render(
            title=f"{folder_name} - tg-export",
            css_path=f"{rel}/css/style.css",
            js_path=f"{rel}/js/script.js",
            folder_name=folder_name,
            index_href=f"{rel}/index.html",
            chats=chats,
        )
        (folder_dir / "index.html").write_text(html, encoding="utf-8")

    def render_personal_info(self, user_data: dict[str, Any]):
        """Render personal information page."""
        template = self.env.get_template("personal_info.html.j2")
        html = template.render(
            title="Personal Information - tg-export",
            css_path="css/style.css",
            js_path="js/script.js",
            index_href="index.html",
            **user_data,
        )
        (self.output_dir / "personal_info.html").write_text(html, encoding="utf-8")

    def render_contacts(self, contacts: list[dict], frequent: list[dict]):
        """Render contacts page."""
        template = self.env.get_template("contacts.html.j2")
        html = template.render(
            title="Contacts - tg-export",
            css_path="css/style.css",
            js_path="js/script.js",
            index_href="index.html",
            contacts=contacts,
            frequent=frequent,
        )
        (self.output_dir / "contacts.html").write_text(html, encoding="utf-8")

    def render_sessions(self, app_sessions: list[dict], web_sessions: list[dict]):
        """Render sessions page."""
        template = self.env.get_template("sessions.html.j2")
        html = template.render(
            title="Active Sessions - tg-export",
            css_path="css/style.css",
            js_path="js/script.js",
            index_href="index.html",
            app_sessions=app_sessions,
            web_sessions=web_sessions,
        )
        (self.output_dir / "sessions.html").write_text(html, encoding="utf-8")

    def render_userpics(self, photos: list[dict]):
        """Render profile photos gallery page."""
        template = self.env.get_template("userpics.html.j2")
        html = template.render(
            title="Profile Photos - tg-export",
            css_path="css/style.css",
            js_path="js/script.js",
            index_href="index.html",
            photos=photos,
        )
        (self.output_dir / "userpics.html").write_text(html, encoding="utf-8")

    def render_stories(self, stories: list[dict]):
        """Render stories page."""
        template = self.env.get_template("stories.html.j2")
        html = template.render(
            title="Stories - tg-export",
            css_path="css/style.css",
            js_path="js/script.js",
            index_href="index.html",
            stories=stories,
        )
        (self.output_dir / "stories.html").write_text(html, encoding="utf-8")

    def render_other_data(self, data: dict[str, Any]):
        """Render other data page."""
        template = self.env.get_template("other_data.html.j2")
        html = template.render(
            title="Other Data - tg-export",
            css_path="css/style.css",
            js_path="js/script.js",
            index_href="index.html",
            **data,
        )
        (self.output_dir / "other_data.html").write_text(html, encoding="utf-8")

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
    if stripped.startswith("/") or stripped.startswith("#") or stripped.startswith("?"):
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
        elif tp.type == TextType.custom_emoji:
            result.append(text)
        else:
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


def _fix_media_path(msg: Message, chat_dir: Path):
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
            resolved = Path.cwd() / p
            file_obj.local_path = str(resolved.relative_to(chat_dir.resolve()))
        except ValueError:
            # Last resort: just keep the filename parts after the media subdir
            media_subdirs = set(MEDIA_SUBDIRS.values())
            parts = p.parts
            for i, part in enumerate(parts):
                if part in media_subdirs:
                    file_obj.local_path = str(Path(*parts[i:]))
                    return
