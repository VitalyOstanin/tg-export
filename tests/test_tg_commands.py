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


def _invoke_tg_messages(args: list[str], text: str = "hello"):
    """Run `tg messages` against a mocked API returning a single message."""
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
    mock_msg.message = text
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
        return runner.invoke(main, ["tg", "messages", "123", *args])


class TestTgMessagesOutput:
    def test_msg_id_in_output(self):
        """tg messages output includes [msg_id] for each message."""
        result = _invoke_tg_messages(["-n", "1"])

        assert "[42]" in result.output
        assert "Alice" in result.output
        assert "hello" in result.output

    def test_text_truncated_to_200_chars_by_default(self):
        """Without options the text is cut to 200 characters."""
        text = "x" * 250

        result = _invoke_tg_messages(["-n", "1"], text=text)

        assert "x" * 200 in result.output
        assert "x" * 201 not in result.output

    def test_no_truncate_prints_full_text(self):
        """--no-truncate prints the message text in full."""
        text = "y" * 5000

        result = _invoke_tg_messages(["-n", "1", "--no-truncate"], text=text)

        assert text in result.output

    def test_truncate_sets_custom_length(self):
        """--truncate N cuts the text to N characters."""
        text = "z" * 100

        result = _invoke_tg_messages(["-n", "1", "--truncate", "10"], text=text)

        assert "z" * 10 in result.output
        assert "z" * 11 not in result.output

    def test_truncate_zero_prints_full_text(self):
        """--truncate 0 disables truncation."""
        text = "w" * 3000

        result = _invoke_tg_messages(["-n", "1", "--truncate", "0"], text=text)

        assert text in result.output

    def test_no_truncate_conflicts_with_truncate(self):
        """--no-truncate together with an explicit --truncate is a usage error."""
        result = _invoke_tg_messages(["-n", "1", "--no-truncate", "--truncate", "10"])

        assert result.exit_code == 2
        assert "--no-truncate" in result.output

    def test_negative_truncate_rejected(self):
        """Negative --truncate is a usage error."""
        result = _invoke_tg_messages(["-n", "1", "--truncate", "-5"])

        assert result.exit_code == 2


# ---------------------------------------------------------------------------
# TgApi: proxy / python-socks guard
# ---------------------------------------------------------------------------


def test_tgapi_raises_when_proxy_set_but_python_socks_missing():
    from tg_export.api import TgApi

    proxy = ("socks5", "127.0.0.1", 1080, True, None, None)
    with (
        patch("tg_export.api.importlib.util.find_spec", return_value=None),
        pytest.raises(RuntimeError, match="python-socks"),
    ):
        TgApi("/tmp/test.session", 1, "hash", proxy=proxy)


def test_tgapi_passes_proxy_to_client_when_python_socks_available():
    from tg_export.api import TgApi

    proxy = ("socks5", "127.0.0.1", 1080, True, None, None)
    with (
        patch("tg_export.api.importlib.util.find_spec", return_value=object()),
        patch("tg_export.api.FixedSQLiteSession"),
        patch("tg_export.api.TelegramClient") as mock_client,
    ):
        TgApi("/tmp/test.session", 1, "hash", proxy=proxy)

    assert mock_client.call_args.kwargs["proxy"] == proxy


def test_tgapi_no_proxy_does_not_check_python_socks():
    from tg_export.api import TgApi

    with (
        patch("tg_export.api.importlib.util.find_spec", return_value=None) as mock_find,
        patch("tg_export.api.FixedSQLiteSession"),
        patch("tg_export.api.TelegramClient") as mock_client,
    ):
        TgApi("/tmp/test.session", 1, "hash", proxy=None)

    mock_find.assert_not_called()
    assert "proxy" not in mock_client.call_args.kwargs


# ---------------------------------------------------------------------------
# CLI: --version
# ---------------------------------------------------------------------------


def test_cli_version_option():
    from importlib.metadata import version

    from click.testing import CliRunner

    from tg_export.cli import main

    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert version("tg-export") in result.output
