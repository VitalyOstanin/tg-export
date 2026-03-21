"""Import existing exports (tdesktop HTML or previous tg-export)."""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Media subdirs in tdesktop HTML export
_MEDIA_SUBDIRS = {"photos", "files", "video_files", "voice_messages",
                  "video_messages", "stickers", "gifs"}

# Regex patterns for tdesktop HTML parsing
_MSG_ID_RE = re.compile(r'id="message(\d+)"')
_HREF_RE = re.compile(r'href="[^"]*?/chats/[^"]*?/(' + '|'.join(_MEDIA_SUBDIRS) + r')/([^"]+)"')


class TdesktopIndex:
    """Lazy per-chat index of tdesktop HTML export.

    Pre-scans chat directories to build chat_name -> chat_dir mapping.
    Per-chat media index (msg_id -> file_path) is built on demand.
    """

    def __init__(self, export_path: Path):
        self.export_path = export_path
        self._chat_map: dict[str, Path] | None = None  # chat_name -> chat_dir
        self._current_chat_dir: Path | None = None
        self._current_index: dict[int, list[Path]] | None = None  # msg_id -> [file_paths]

    def _ensure_chat_map(self):
        """Build chat_name -> chat_dir mapping from tdesktop export."""
        if self._chat_map is not None:
            return

        self._chat_map = {}
        chats_dir = self.export_path / "chats"
        if not chats_dir.is_dir():
            logger.warning("tdesktop export has no chats/ directory: %s", self.export_path)
            return

        for chat_dir in sorted(chats_dir.iterdir()):
            if not chat_dir.is_dir() or not chat_dir.name.startswith("chat_"):
                continue
            msg_html = chat_dir / "messages.html"
            if not msg_html.exists():
                continue
            name = _extract_chat_name(msg_html)
            if name:
                self._chat_map[name] = chat_dir
                logger.debug("tdesktop chat: %s -> %s", name, chat_dir.name)

        logger.info("tdesktop index: %d chats in %s", len(self._chat_map), self.export_path)

    def find_chat_dir(self, chat_name: str) -> Path | None:
        """Find tdesktop chat directory by chat name."""
        self._ensure_chat_map()
        return self._chat_map.get(chat_name)

    def get_chat_names(self) -> list[str]:
        """Return list of indexed chat names."""
        self._ensure_chat_map()
        return list(self._chat_map.keys())

    def load_chat_index(self, chat_name: str) -> bool:
        """Build msg_id -> file_paths index for a specific chat.

        Returns True if chat was found and indexed.
        """
        chat_dir = self.find_chat_dir(chat_name)
        if chat_dir is None:
            self._current_chat_dir = None
            self._current_index = None
            return False

        if self._current_chat_dir == chat_dir:
            return True  # already loaded

        self._current_chat_dir = chat_dir
        self._current_index = _parse_chat_media(chat_dir)
        logger.info("tdesktop index for '%s': %d messages with media",
                     chat_name, len(self._current_index))
        return True

    def unload_chat_index(self):
        """Free memory for current chat index."""
        self._current_chat_dir = None
        self._current_index = None

    def find_file(self, msg_id: int) -> Path | None:
        """Find file path for a message in the current chat index.

        Returns absolute path to the file, or None.
        Only returns the first (primary) file for a message.
        """
        if self._current_index is None:
            return None
        files = self._current_index.get(msg_id)
        if not files:
            return None
        # Return first existing file
        for f in files:
            if f.exists():
                return f
        return None

    def find_files(self, msg_id: int) -> list[Path]:
        """Find all file paths for a message (e.g. album with multiple photos)."""
        if self._current_index is None:
            return []
        return self._current_index.get(msg_id, [])


def _extract_chat_name(msg_html: Path) -> str | None:
    """Extract chat name from tdesktop messages.html header.

    Reads only first ~50 lines to find the chat name in the header.
    """
    try:
        with open(msg_html, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i > 50:
                    break
                if "text bold" in line:
                    # Next line contains the chat name
                    name_line = next(f, "").strip()
                    if name_line:
                        return name_line
    except (OSError, StopIteration):
        pass
    return None


def _parse_chat_media(chat_dir: Path) -> dict[int, list[Path]]:
    """Parse all messages*.html in a chat dir to build msg_id -> [file_paths] index.

    Scans for id="messageNNN" and href=".../(photos|files|...)/filename" pairs.
    Files are resolved relative to the export root (chat_dir/../../).
    """
    index: dict[int, list[Path]] = {}

    # Collect all message HTML files in order
    html_files = [chat_dir / "messages.html"]
    for i in range(2, 10000):
        f = chat_dir / f"messages{i}.html"
        if not f.exists():
            break
        html_files.append(f)

    for html_file in html_files:
        current_msg_id = None
        try:
            with open(html_file, encoding="utf-8") as f:
                for line in f:
                    # Check for message id
                    m = _MSG_ID_RE.search(line)
                    if m:
                        current_msg_id = int(m.group(1))

                    # Check for media href
                    if current_msg_id is not None:
                        m = _HREF_RE.search(line)
                        if m:
                            subdir = m.group(1)
                            filename = m.group(2)
                            # Skip thumbnails
                            if "_thumb" in filename:
                                continue
                            file_path = chat_dir / subdir / filename
                            if current_msg_id not in index:
                                index[current_msg_id] = []
                            index[current_msg_id].append(file_path)
        except OSError as e:
            logger.warning("Error reading %s: %s", html_file, e)

    return index


def build_tdesktop_indexes(import_entries: list) -> list[TdesktopIndex]:
    """Build TdesktopIndex instances from config import_existing entries."""
    indexes = []
    for entry in import_entries:
        if entry.type != "tdesktop":
            continue
        path = Path(entry.path).expanduser()
        if not path.is_dir():
            logger.warning("tdesktop export not found: %s", path)
            continue
        indexes.append(TdesktopIndex(path))
    return indexes
