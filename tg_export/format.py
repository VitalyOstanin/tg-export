"""Shared human-readable formatting helpers."""

from __future__ import annotations


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
