"""Shared human-readable formatting helpers."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

# The wall-clock format of everything a person reads as a moment in time:
# message lists, session tables, the "generated at" line. The seconds variant
# is what the chat pages put into the title attribute of a timestamp. The rest
# of the file holds the other shapes a date takes -- a day, a clock, a month
# key -- so that none of them is written out at a call site.
MOMENT_FORMAT = "%Y-%m-%d %H:%M"
MOMENT_WITH_SECONDS_FORMAT = "%Y-%m-%d %H:%M:%S"

# The chat pages read as a conversation, so they date a message the way a
# person would say it and give the clock alone under each hour.
DAY_FORMAT = "%B %d, %Y"
TIME_FORMAT = "%H:%M"

# What a machine reads: the catalog date of the last message, the moment a
# catalog was generated, and the key a month page is named by.
DATE_FORMAT = "%Y-%m-%d"
ISO_MOMENT_FORMAT = "%Y-%m-%dT%H:%M:%S"
MONTH_KEY_FORMAT = "%Y-%m"
MONTH_LABEL_FORMAT = "%B %Y"

# Clock alone, for the line that says when a run started.
CLOCK_FORMAT = "%H:%M:%S"


# Everything below the space plus DEL. Telegram controls the text of a message
# and the title of a chat, and a terminal reads these bytes as commands: an
# escape sequence repaints the line, moves the cursor or sets a colour that
# stays after the command ends.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def strip_control_chars(text: str) -> str:
    """Text of a Telegram string with the control characters taken out.

    For lines printed to a terminal. The machine-readable output needs none of
    this -- `json.dumps` escapes what it writes -- and must not be touched: it
    carries the message as it was sent.
    """
    return _CONTROL_CHARS_RE.sub("", text)


def format_moment(
    value: datetime | int | float | None,
    *,
    fmt: str = MOMENT_FORMAT,
    missing: str = "",
) -> str:
    """Format a moment given as a datetime, a unix timestamp or nothing.

    Telegram hands the same field over in both shapes depending on the call,
    and the branch telling them apart was copied per call site.
    """
    if isinstance(value, datetime):
        return value.strftime(fmt)
    if value:
        return datetime.fromtimestamp(value).strftime(fmt)
    return missing


def format_size(size_bytes: float) -> str:
    """Format a byte count as a human-readable size (B / KB / MB / GB)."""
    if size_bytes < 1024:
        return f"{size_bytes:.0f} B"
    if size_bytes < 1024**2:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024**3:
        return f"{size_bytes / 1024**2:.1f} MB"
    return f"{size_bytes / 1024**3:.2f} GB"


def format_speed(bytes_count: float, elapsed_s: float) -> str:
    """Format a transfer rate, reusing the size ladder above."""
    if elapsed_s <= 0:
        return "-- B/s"
    return f"{format_size(bytes_count / elapsed_s)}/s"


def display_name(entity: Any, *, missing: str = "") -> str:
    """The name of a person on one line: ``first last``.

    Telethon keeps the two halves apart and either of them may be absent. The
    expression was written out in five places -- the converter, two commands
    and the exporter twice -- and the copies had already started to differ in
    what they put in place of a missing name.
    """
    first = getattr(entity, "first_name", "") or ""
    last = getattr(entity, "last_name", "") or ""
    return f"{first} {last}".strip() or missing
