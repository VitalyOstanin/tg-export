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


# ---------------------------------------------------------------------------
# _tg_send: attachment mode and progress
# ---------------------------------------------------------------------------


class TestTgSend:
    @staticmethod
    def _run_send(tmp_path, file_count, as_document, text="caption"):
        from tg_export import cli
        from tg_export.cli import _tg_send

        files = []
        for i in range(file_count):
            f = tmp_path / f"pic{i}.jpg"
            f.write_bytes(b"data" * (i + 1))
            files.append(str(f))

        api = AsyncMock()
        api.client = AsyncMock()

        with (
            patch("tg_export.cli._connect_tg", AsyncMock(return_value=(api, "acc"))),
            patch.object(cli, "_QUIET", True),
        ):
            asyncio.run(_tg_send("acc", [123], text, files, as_document))

        return api.client.send_file

    def test_single_file_sent_as_document_when_flag_set(self, tmp_path):
        send_file = self._run_send(tmp_path, 1, True)

        assert send_file.await_count == 1
        assert send_file.await_args.kwargs["force_document"] is True

    def test_documents_are_sent_one_by_one(self, tmp_path):
        send_file = self._run_send(tmp_path, 3, True)

        assert send_file.await_count == 3
        assert all(call.kwargs["force_document"] is True for call in send_file.await_args_list)
        assert all(isinstance(call.args[1], str) for call in send_file.await_args_list)

    def test_caption_goes_to_the_first_document_only(self, tmp_path):
        send_file = self._run_send(tmp_path, 3, True, text="hello")
        captions = [call.kwargs["caption"] for call in send_file.await_args_list]

        assert captions == ["hello", "", ""]

    def test_photos_keep_album_grouping(self, tmp_path):
        send_file = self._run_send(tmp_path, 3, False)

        assert send_file.await_count == 1
        assert send_file.await_args.kwargs["force_document"] is False
        assert len(send_file.await_args.args[1]) == 3

    def test_progress_callback_is_passed(self, tmp_path):
        send_file = self._run_send(tmp_path, 2, True)

        assert all(callable(call.kwargs["progress_callback"]) for call in send_file.await_args_list)


@pytest.mark.asyncio
async def test_send_files_escapes_the_file_name_for_rich(tmp_path, monkeypatch):
    """Имя файла попадает в описание задачи rich, а rich разбирает его как
    разметку: `[draft]report.txt` печатается как report.txt -- квадратные скобки
    съедаются вместе с содержимым. В проекте для этого есть
    file_progress_description() с escape(), которой пользуется загрузчик
    экспорта.
    """
    import contextlib
    from unittest.mock import AsyncMock, MagicMock

    import tg_export.cli as cli

    tricky = tmp_path / "[draft]report.txt"
    tricky.write_bytes(b"data")

    descriptions = []

    class _Progress:
        def add_task(self, description, **kwargs):
            descriptions.append(description)
            return len(descriptions)

        def update(self, *a, **k):
            pass

        def remove_task(self, *a, **k):
            pass

    @contextlib.contextmanager
    def fake_progress(by_bytes):
        yield _Progress()

    monkeypatch.setattr(cli, "_upload_progress", fake_progress)

    client = MagicMock()
    client.send_file = AsyncMock()

    await cli._send_files(client, 1, [tricky], "text", True)

    assert descriptions == ["\\[draft]report.txt"], descriptions


def test_rich_swallows_an_unescaped_file_name():
    """Проверка того, что экранирование действительно требуется: без него имя
    печатается искажённым, с ним -- дословно."""
    import io

    from rich.console import Console

    from tg_export.exporter import file_progress_description

    console = Console(file=io.StringIO(), width=80, force_terminal=False)
    console.print("[draft]report.txt")
    assert console.file.getvalue() == "report.txt\n"

    console = Console(file=io.StringIO(), width=80, force_terminal=False)
    console.print(file_progress_description("[draft]report.txt"))
    assert console.file.getvalue() == "[draft]report.txt\n"
