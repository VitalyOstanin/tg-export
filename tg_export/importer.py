"""Import existing exports (tdesktop or previous tg-export)."""

from __future__ import annotations

from pathlib import Path


def scan_tdesktop_export(export_path: Path) -> list[dict]:
    """Scan tdesktop export directory structure.

    Returns list of dicts: {path, size, chat_dir}.
    """
    files = []
    media_dirs = ["photos", "videos", "files", "voice_messages",
                  "video_messages", "stickers", "gifs"]

    for item in export_path.rglob("*"):
        if not item.is_file():
            continue
        # Check if in a known media subdir
        if any(part in media_dirs for part in item.parts):
            files.append({
                "path": str(item),
                "size": item.stat().st_size,
                "chat_dir": str(item.parent.parent),
            })

    files.sort(key=lambda f: f["size"])
    return files


def scan_tg_export(export_path: Path) -> list[dict]:
    """Read SQLite from previous tg-export run."""
    import aiosqlite
    import asyncio

    async def _scan():
        db_path = export_path / ".tg-export-state.db"
        if not db_path.exists():
            return []
        db = await aiosqlite.connect(db_path)
        db.row_factory = aiosqlite.Row
        try:
            async with db.execute("SELECT * FROM files WHERE status='done'") as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]
        finally:
            await db.close()

    return asyncio.run(_scan())
