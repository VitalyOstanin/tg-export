"""Shared human-readable formatting helpers."""

from __future__ import annotations

from datetime import datetime

# One wall-clock format for everything a person reads: message lists, session
# tables, the "generated at" line. The seconds variant is what the chat pages
# put into the title attribute of a timestamp.
MOMENT_FORMAT = "%Y-%m-%d %H:%M"
MOMENT_WITH_SECONDS_FORMAT = "%Y-%m-%d %H:%M:%S"


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
