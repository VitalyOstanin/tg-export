"""Tests for tg subcommands: messages (msg_id display) and download (dedup)."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tg_export.cli import _download_if_new

# ---------------------------------------------------------------------------
# _download_if_new: deduplication logic
# ---------------------------------------------------------------------------


@pytest.fixture()
def out_dir(tmp_path):
    return tmp_path


def _make_client(out_dir: Path, filename: str, content: bytes) -> AsyncMock:
    """Mock client whose download_media writes a file and returns its path."""
    client = AsyncMock()

    async def _download(msg, *, file):
        p = Path(file) / filename
        # Telethon adds (1) suffix if file exists
        n = 0
        while p.exists():
            n += 1
            p = Path(file) / f"{p.stem} ({n}){p.suffix}"
        p.write_bytes(content)
        return str(p)

    client.download_media.side_effect = _download
    return client


class TestDownloadIfNew:
    def test_first_download_succeeds(self, out_dir):
        client = _make_client(out_dir, "doc.pdf", b"A" * 100)
        downloaded = set()
        msg = MagicMock()

        result = asyncio.run(_download_if_new(client, msg, out_dir, downloaded))

        assert result is not None
        assert Path(result).name == "doc.pdf"
        assert (out_dir / "doc.pdf").exists()
        assert len(downloaded) == 1

    def test_duplicate_same_size_removed(self, out_dir):
        """Second download of same-size file is detected and removed."""
        content = b"A" * 100
        # Pre-existing file
        existing = out_dir / "doc.pdf"
        existing.write_bytes(content)
        downloaded = {existing}

        client = _make_client(out_dir, "doc.pdf", content)
        msg = MagicMock()

        result = asyncio.run(_download_if_new(client, msg, out_dir, downloaded))

        assert result is None
        # Only original file remains, Telethon's (1) copy was removed
        files = list(out_dir.iterdir())
        assert len(files) == 1
        assert files[0].name == "doc.pdf"

    def test_different_size_kept(self, out_dir):
        """File with different size is NOT considered a duplicate."""
        existing = out_dir / "doc.pdf"
        existing.write_bytes(b"A" * 100)
        downloaded = {existing}

        client = _make_client(out_dir, "doc.pdf", b"B" * 200)
        msg = MagicMock()

        result = asyncio.run(_download_if_new(client, msg, out_dir, downloaded))

        assert result is not None
        assert len(downloaded) == 2
        files = list(out_dir.iterdir())
        assert len(files) == 2

    def test_download_returns_none(self, out_dir):
        """If client.download_media returns None, _download_if_new returns None."""
        client = AsyncMock()
        client.download_media.return_value = None
        downloaded = set()
        msg = MagicMock()

        result = asyncio.run(_download_if_new(client, msg, out_dir, downloaded))

        assert result is None
        assert len(downloaded) == 0

    def test_preexisting_files_prevent_duplicates(self, out_dir):
        """downloaded set initialized from existing dir files blocks dupes."""
        content = b"X" * 50
        (out_dir / "a.docx").write_bytes(content)
        (out_dir / "b.docx").write_bytes(content)
        downloaded = {f for f in out_dir.iterdir() if f.is_file()}

        # Try downloading same-size file
        client = _make_client(out_dir, "c.docx", content)
        msg = MagicMock()

        result = asyncio.run(_download_if_new(client, msg, out_dir, downloaded))

        assert result is None


# ---------------------------------------------------------------------------
# tg messages: msg_id in output
# ---------------------------------------------------------------------------


class TestTgMessagesOutput:
    def test_msg_id_in_output(self):
        """tg messages output includes [msg_id] for each message."""
        from click.testing import CliRunner

        from tg_export.cli import main

        mock_entity = MagicMock()
        mock_entity.title = "Test Chat"

        mock_msg = MagicMock()
        mock_msg.id = 42
        mock_msg.date = MagicMock()
        mock_msg.date.strftime.return_value = "2026-01-01 12:00"
        mock_msg.sender = MagicMock()
        mock_msg.sender.first_name = "Alice"
        mock_msg.sender.last_name = ""
        mock_msg.message = "hello"
        mock_msg.media = None
        mock_msg.action = None

        async def _fake_iter(*a, **kw):
            yield mock_msg

        mock_api = AsyncMock()
        mock_api.client.get_entity.return_value = mock_entity
        mock_api.client.iter_messages = _fake_iter
        mock_api.connect = AsyncMock()
        mock_api.disconnect = AsyncMock()

        with patch("tg_export.cli._mgr") as mock_mgr, patch("tg_export.api.TgApi", return_value=mock_api):
            mgr = MagicMock()
            mgr.resolve_account.return_value = "test"
            mgr.load_credentials.return_value = ("id", "hash")
            mgr.load_proxy.return_value = None
            mgr.session_path.return_value = "/tmp/test.session"
            mock_mgr.return_value = mgr

            runner = CliRunner()
            result = runner.invoke(main, ["tg", "messages", "123", "-n", "1"])

        assert "[42]" in result.output
        assert "Alice" in result.output
        assert "hello" in result.output
