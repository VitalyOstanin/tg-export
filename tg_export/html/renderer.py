"""HTML renderer with Jinja2 templates."""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from tg_export.models import (
    Message, TextPart, TextType, Media, MediaType,
    PhotoMedia, DocumentMedia, ContactMedia, GeoMedia, VenueMedia,
    PollMedia, Chat,
)
from tg_export.config import OutputConfig


TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

JOIN_WITHIN_SECONDS = 900  # 15 minutes


class HtmlRenderer:
    def __init__(self, output_dir: Path, config: OutputConfig):
        self.output_dir = output_dir
        self.config = config
        self.env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=False,
        )

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

    def render_message(self, msg: Message, prev_msg: Message | None) -> str:
        """Render a single message block."""
        if msg.action:
            return self._render_service(msg)

        joined = is_joined(msg, prev_msg)
        author = msg.from_name or "Unknown"
        initial = author[0].upper() if author else "?"

        parts = []
        parts.append(f'<div class="message{" joined" if joined else ""}" id="message{msg.id}">')
        parts.append('<div class="message-wrap">')
        parts.append(f'<div class="author-avatar">{escape(initial)}</div>')
        parts.append('<div class="message-body">')

        if not joined:
            time_str = msg.date.strftime("%H:%M") if msg.date else ""
            parts.append(f'<div class="author">{escape(author)}<span class="timestamp">{time_str}</span></div>')
        else:
            time_str = msg.date.strftime("%H:%M") if msg.date else ""
            parts.append(f'<span class="timestamp">{time_str}</span>')

        if msg.reply_to_msg_id:
            parts.append(self._render_reply(msg))

        if msg.forwarded_from:
            fwd_name = msg.forwarded_from.from_name or "Unknown"
            parts.append(f'<div class="forward-block"><span class="forward-from">Forwarded from {escape(fwd_name)}</span></div>')

        if msg.media:
            parts.append(self._render_media(msg.media))

        if msg.text:
            parts.append(f'<div class="text">{render_text_parts(msg.text)}</div>')

        if msg.reactions:
            parts.append(self._render_reactions(msg))

        if msg.inline_buttons:
            parts.append(self._render_buttons(msg))

        parts.append('</div></div></div>')
        return "\n".join(parts)

    def render_album(self, msgs: list[Message]) -> str:
        """Render grouped messages as one album block."""
        first = msgs[0]
        author = first.from_name or "Unknown"
        initial = author[0].upper() if author else "?"
        time_str = first.date.strftime("%H:%M") if first.date else ""

        media_html = []
        for m in msgs:
            if m.media:
                media_html.append(self._render_media(m.media))

        # Text from last message (tdesktop convention)
        text_html = ""
        for m in reversed(msgs):
            if m.text:
                text_html = render_text_parts(m.text)
                break

        reactions_html = ""
        for m in reversed(msgs):
            if m.reactions:
                reactions_html = self._render_reactions(m)
                break

        parts = [
            f'<div class="message" id="message{first.id}">',
            '<div class="message-wrap">',
            f'<div class="author-avatar">{escape(initial)}</div>',
            '<div class="message-body">',
            f'<div class="author">{escape(author)}<span class="timestamp">{time_str}</span></div>',
            '<div class="album">',
            "\n".join(media_html),
            '</div>',
        ]
        if text_html:
            parts.append(f'<div class="text">{text_html}</div>')
        if reactions_html:
            parts.append(reactions_html)
        parts.append('</div></div></div>')
        return "\n".join(parts)

    def render_chat(self, chat: Chat, messages: list[Message], chat_dir: Path):
        """Render chat split by month with TOC and prev/next navigation."""
        chat_dir.mkdir(parents=True, exist_ok=True)

        # Clean up old HTML files (from previous renders)
        for old in chat_dir.glob("messages*.html"):
            old.unlink()

        # Fix media paths: make local_path relative to chat_dir
        for msg in messages:
            _fix_media_path(msg, chat_dir)

        # Group albums
        processed = _group_albums(messages)

        # Split by month
        monthly: dict[str, list] = {}  # "YYYY-MM" -> items
        for entry in processed:
            first_msg = entry[0] if isinstance(entry, list) else entry
            if first_msg.date:
                key = first_msg.date.strftime("%Y-%m")
            else:
                key = "0000-00"
            monthly.setdefault(key, []).append(entry)

        if not monthly:
            monthly = {"0000-00": []}

        month_keys = sorted(monthly.keys())

        # Build page info: (month_key, filename, label)
        pages_info = []
        for key in month_keys:
            filename = f"messages_{key}.html"
            # Human-readable label
            if key == "0000-00":
                label = "Unknown date"
            else:
                try:
                    dt = datetime.strptime(key, "%Y-%m")
                    label = dt.strftime("%B %Y")
                except ValueError:
                    label = key
            pages_info.append({"key": key, "filename": filename, "label": label})

        # Relative path from chat_dir to output root for CSS/JS
        rel = _relative_path(chat_dir, self.output_dir)

        template = self.env.get_template("chat.html.j2")

        for page_idx, pinfo in enumerate(pages_info):
            page_items = monthly[pinfo["key"]]

            # Build render items
            items = []
            prev_msg = None
            prev_date = None

            for entry in page_items:
                if isinstance(entry, list):
                    # Album
                    first = entry[0]
                    msg_date = first.date.date() if first.date else None
                    if msg_date != prev_date:
                        items.append({
                            "type": "date_separator",
                            "date": first.date.strftime("%B %d, %Y") if first.date else "",
                        })
                        prev_date = msg_date
                    items.append({
                        "type": "album",
                        "msgs": entry,
                        "author": first.from_name or "Unknown",
                        "author_initial": (first.from_name or "?")[0].upper(),
                        "time": first.date.strftime("%H:%M") if first.date else "",
                        "full_date": first.date.strftime("%Y-%m-%d %H:%M:%S") if first.date else "",
                        "media_html": [self._render_media(m.media) for m in entry if m.media],
                        "text_html": next((render_text_parts(m.text) for m in reversed(entry) if m.text), ""),
                        "reactions_html": next((self._render_reactions(m) for m in reversed(entry) if m.reactions), ""),
                    })
                    prev_msg = entry[-1]
                else:
                    msg = entry
                    msg_date = msg.date.date() if msg.date else None
                    if msg_date != prev_date:
                        items.append({
                            "type": "date_separator",
                            "date": msg.date.strftime("%B %d, %Y") if msg.date else "",
                        })
                        prev_date = msg_date

                    if msg.action:
                        items.append({
                            "type": "service",
                            "msg": msg,
                            "html": self._render_service_text(msg),
                        })
                    else:
                        joined = is_joined(msg, prev_msg)
                        items.append({
                            "type": "message",
                            "msg": msg,
                            "joined": joined,
                            "author": msg.from_name or "Unknown",
                            "author_initial": (msg.from_name or "?")[0].upper(),
                            "time": msg.date.strftime("%H:%M") if msg.date else "",
                            "full_date": msg.date.strftime("%Y-%m-%d %H:%M:%S") if msg.date else "",
                            "reply_html": self._render_reply(msg) if msg.reply_to_msg_id else "",
                            "forward_html": self._render_forward(msg) if msg.forwarded_from else "",
                            "media_html": self._render_media(msg.media) if msg.media else "",
                            "text_html": render_text_parts(msg.text) if msg.text else "",
                            "buttons_html": self._render_buttons(msg) if msg.inline_buttons else "",
                            "reactions_html": self._render_reactions(msg) if msg.reactions else "",
                        })
                    prev_msg = msg

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

        # Write messages.html as redirect to first month
        if pages_info:
            redirect_html = f'<!DOCTYPE html><html><head><meta http-equiv="refresh" content="0;url={pages_info[0]["filename"]}"></head></html>'
            (chat_dir / "messages.html").write_text(redirect_html, encoding="utf-8")

    def render_index(self, folders_list: list[dict], unfiled: list, sections: list):
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

    def render_personal_info(self, user_data: dict):
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

    def render_other_data(self, data: dict):
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

    def _render_service(self, msg: Message) -> str:
        return f'<div class="message service" id="message{msg.id}"><span class="service-text">{self._render_service_text(msg)}</span></div>'

    def _render_service_text(self, msg: Message) -> str:
        if msg.action:
            action_type = msg.action.type
            author = escape(msg.from_name or "Someone")
            if action_type == "ActionChatCreate":
                return f"{author} created the group"
            if action_type == "ActionChatEditTitle":
                return f"{author} changed the group name"
            if action_type == "ActionPinMessage":
                return f"{author} pinned a message"
            if action_type == "ActionPhoneCall":
                return f"{author} made a call"
            if action_type == "ActionContactSignUp":
                return f"{author} joined Telegram"
            if action_type == "ActionScreenshotTaken":
                return f"{author} took a screenshot"
            return f"{author}: {action_type}"
        return ""

    def _render_reply(self, msg: Message) -> str:
        return f'<div class="reply-block" data-msg-id="{msg.reply_to_msg_id}"><span class="reply-author">Reply</span></div>'

    def _render_forward(self, msg: Message) -> str:
        fwd_name = escape(msg.forwarded_from.from_name or "Unknown") if msg.forwarded_from else "Unknown"
        return f'<div class="forward-block"><span class="forward-from">Forwarded from {fwd_name}</span></div>'

    def _render_media(self, media: Media | None) -> str:
        if media is None:
            return ""

        if isinstance(media, PhotoMedia):
            if media.file and media.file.local_path:
                return f'<div class="media-block"><img src="{escape(media.file.local_path)}" alt="photo"></div>'
            return '<div class="media-block"><div class="file-block"><div class="file-icon">IMG</div><div class="file-info"><div class="file-name">Photo</div></div></div></div>'

        if isinstance(media, DocumentMedia):
            name = escape(media.name or "File")
            size = _format_size(media.file.size) if media.file else ""
            if media.type == MediaType.voice:
                dur = f" ({media.duration}s)" if media.duration else ""
                return f'<div class="media-block"><div class="voice">Voice message{dur}</div></div>'
            if media.type == MediaType.video_note:
                dur = f" ({media.duration}s)" if media.duration else ""
                return f'<div class="media-block"><div class="video-note">Video message{dur}</div></div>'
            if media.type == MediaType.sticker:
                emoji = escape(media.sticker_emoji or "")
                return f'<div class="media-block"><div class="sticker">{emoji} Sticker</div></div>'
            if media.type == MediaType.video:
                if media.file and media.file.local_path:
                    return f'<div class="media-block"><video controls src="{escape(media.file.local_path)}"></video></div>'
                return f'<div class="media-block"><div class="file-block"><div class="file-icon">VID</div><div class="file-info"><div class="file-name">{name}</div><div class="file-size">{size}</div></div></div></div>'
            # Generic file
            ext = name.rsplit(".", 1)[-1].upper()[:4] if "." in name else "FILE"
            href = f' href="{escape(media.file.local_path)}"' if media.file and media.file.local_path else ""
            return f'<div class="media-block"><a{href} class="file-block"><div class="file-icon">{ext}</div><div class="file-info"><div class="file-name">{name}</div><div class="file-size">{size}</div></div></a></div>'

        if isinstance(media, ContactMedia):
            name = escape(f"{media.first_name} {media.last_name}".strip())
            phone = escape(media.phone)
            return f'<div class="media-block"><div class="contact-card">{name}<br>{phone}</div></div>'

        if isinstance(media, GeoMedia):
            return f'<div class="media-block"><div class="geo-card">Location: {media.latitude:.5f}, {media.longitude:.5f}</div></div>'

        if isinstance(media, VenueMedia):
            return f'<div class="media-block"><div class="venue-card">{escape(media.title)}<br>{escape(media.address)}</div></div>'

        if isinstance(media, PollMedia):
            parts = ['<div class="media-block"><div class="poll-block">']
            q = render_text_parts(media.question) if media.question else "Poll"
            parts.append(f'<div class="poll-question">{q}</div>')
            for ans in media.answers:
                ans_text = render_text_parts(ans.text)
                parts.append(f'<div class="poll-answer"><span>{ans_text}</span><span class="poll-votes">{ans.voters} votes</span></div>')
            parts.append('</div></div>')
            return "\n".join(parts)

        return f'<div class="media-block"><div class="file-block"><div class="file-icon">?</div><div class="file-info"><div class="file-name">{media.type.value}</div></div></div></div>'

    def _render_reactions(self, msg: Message) -> str:
        if not msg.reactions:
            return ""
        parts = ['<div class="reactions">']
        for r in msg.reactions:
            label = r.emoji or "star"
            parts.append(f'<span class="reaction">{label} <span class="count">{r.count}</span></span>')
        parts.append('</div>')
        return "\n".join(parts)

    def _render_buttons(self, msg: Message) -> str:
        if not msg.inline_buttons:
            return ""
        parts = ['<div class="inline-buttons">']
        for row in msg.inline_buttons:
            parts.append('<div class="btn-row">')
            for btn in row:
                text = escape(btn.text)
                if btn.data and btn.type.value == "url":
                    parts.append(f'<a class="btn" href="{escape(btn.data)}" target="_blank">{text}</a>')
                else:
                    parts.append(f'<span class="btn">{text}</span>')
            parts.append('</div>')
        parts.append('</div>')
        return "\n".join(parts)


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


def render_text_parts(parts: list[TextPart]) -> str:
    """Render TextPart list to HTML string."""
    result = []
    for tp in parts:
        text = escape(tp.text)
        if tp.type == TextType.text:
            result.append(text.replace("\n", "<br>"))
        elif tp.type == TextType.bold:
            result.append(f'<span class="bold">{text}</span>')
        elif tp.type == TextType.italic:
            result.append(f'<span class="italic">{text}</span>')
        elif tp.type == TextType.underline:
            result.append(f'<span class="underline">{text}</span>')
        elif tp.type == TextType.strikethrough:
            result.append(f'<span class="strikethrough">{text}</span>')
        elif tp.type == TextType.code:
            result.append(f'<span class="code">{text}</span>')
        elif tp.type == TextType.pre:
            result.append(f'<pre class="pre">{text}</pre>')
        elif tp.type == TextType.blockquote:
            result.append(f'<div class="blockquote">{text}</div>')
        elif tp.type == TextType.spoiler:
            result.append(f'<span class="spoiler">{text}</span>')
        elif tp.type == TextType.url:
            result.append(f'<a class="url" href="{text}" target="_blank">{text}</a>')
        elif tp.type == TextType.text_url:
            href = escape(tp.href) if tp.href else "#"
            result.append(f'<a class="text-url" href="{href}" target="_blank">{text}</a>')
        elif tp.type == TextType.mention:
            result.append(f'<span class="mention">{text}</span>')
        elif tp.type == TextType.mention_name:
            result.append(f'<span class="mention">{text}</span>')
        elif tp.type == TextType.hashtag:
            result.append(f'<span class="hashtag">{text}</span>')
        elif tp.type == TextType.email:
            result.append(f'<a href="mailto:{text}">{text}</a>')
        elif tp.type == TextType.phone:
            result.append(f'<a href="tel:{text}">{text}</a>')
        elif tp.type == TextType.bot_command:
            result.append(f'<span class="bot-command">{text}</span>')
        elif tp.type == TextType.cashtag:
            result.append(f'<span class="cashtag">{text}</span>')
        elif tp.type == TextType.custom_emoji:
            result.append(text)
        else:
            result.append(text)
    return "".join(result)


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
    """Compute relative path from from_dir to to_dir."""
    try:
        return str(to_dir.relative_to(from_dir))
    except ValueError:
        # Count levels up
        parts = []
        current = from_dir
        while True:
            try:
                rel = to_dir.relative_to(current)
                return "/".join(parts) + ("/" if parts else "") + str(rel)
            except ValueError:
                parts.append("..")
                current = current.parent
                if current == current.parent:
                    break
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
        try:
            file_obj.local_path = str(p.relative_to(chat_dir))
        except ValueError:
            pass
    else:
        # Relative path like "export_output/account/unfiled/Chat_123/photos/file.jpg"
        # Try to find chat_dir suffix in the path
        try:
            resolved = Path.cwd() / p
            file_obj.local_path = str(resolved.relative_to(chat_dir.resolve()))
        except ValueError:
            # Last resort: just keep the filename parts after the media subdir
            parts = p.parts
            for i, part in enumerate(parts):
                if part in ("photos", "videos", "files", "voice_messages",
                            "video_messages", "stickers", "gifs"):
                    file_obj.local_path = str(Path(*parts[i:]))
                    return


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024**2:
        return f"{size / 1024:.1f} KB"
    if size < 1024**3:
        return f"{size / 1024**2:.1f} MB"
    return f"{size / 1024**3:.1f} GB"
