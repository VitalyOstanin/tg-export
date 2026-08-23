"""Проверки подкоманд tg: messages (показ msg_id) и download (защита от дублей)."""

import asyncio
import contextlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tg_export.cli import common as cli_common
from tg_export.cli import tg as cli_tg
from tg_export.cli.tg import _download_if_new

# ---------------------------------------------------------------------------
# _download_if_new: deduplication logic
# ---------------------------------------------------------------------------


@pytest.fixture()
def out_dir(tmp_path):
    return tmp_path


def _make_client(out_dir: Path, filename: str, content: bytes) -> AsyncMock:
    """Клиент-заглушка, у которого download_media пишет файл и возвращает его путь."""
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
        """Повторная загрузка файла того же размера распознаётся и удаляется."""
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
        """Файл другого размера дублем НЕ считается."""
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
        """Если client.download_media вернул None, _download_if_new тоже возвращает None."""
        client = AsyncMock()
        client.download_media.return_value = None
        downloaded = set()
        msg = MagicMock()

        result = asyncio.run(_download_if_new(client, msg, out_dir, downloaded))

        assert result is None
        assert len(downloaded) == 0

    def test_preexisting_files_prevent_duplicates(self, out_dir):
        """Набор скачанного, собранный по уже лежащим в каталоге файлам, отсекает дубли."""
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


def _invoke_tg_messages(args: list[str], text: str = "hello", media: str | None = None):
    """Выполнить `tg messages` поверх заглушки API, отдающей одно сообщение."""
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
    if media is None:
        mock_msg.media = None
    else:
        mock_msg.media = type(f"MessageMedia{media}", (), {})()
    mock_msg.action = None

    async def _fake_iter(*a, **kw):
        yield mock_msg

    mock_api = AsyncMock()
    mock_api.client.get_entity.return_value = mock_entity
    mock_api.client.iter_messages = _fake_iter
    mock_api.connect = AsyncMock()
    mock_api.disconnect = AsyncMock()

    with patch("tg_export.cli.common._mgr") as mock_mgr, patch("tg_export.api.TgApi", return_value=mock_api):
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
        """В выводе `tg messages` у каждого сообщения есть [msg_id]."""
        result = _invoke_tg_messages(["-n", "1"])

        assert "[42]" in result.output
        assert "Alice" in result.output
        assert "hello" in result.output

    def test_text_truncated_to_200_chars_by_default(self):
        """Без опций текст обрезается до 200 знаков."""
        text = "x" * 250

        result = _invoke_tg_messages(["-n", "1"], text=text)

        assert "x" * 200 in result.output
        assert "x" * 201 not in result.output

    def test_no_truncate_prints_full_text(self):
        """--no-truncate печатает текст сообщения целиком."""
        text = "y" * 5000

        result = _invoke_tg_messages(["-n", "1", "--no-truncate"], text=text)

        assert text in result.output

    def test_truncate_sets_custom_length(self):
        """--truncate N обрезает текст до N знаков."""
        text = "z" * 100

        result = _invoke_tg_messages(["-n", "1", "--truncate", "10"], text=text)

        assert "z" * 10 in result.output
        assert "z" * 11 not in result.output

    def test_truncate_zero_prints_full_text(self):
        """--truncate 0 отключает обрезку."""
        text = "w" * 3000

        result = _invoke_tg_messages(["-n", "1", "--truncate", "0"], text=text)

        assert text in result.output

    def test_no_truncate_conflicts_with_truncate(self):
        """--no-truncate вместе с явным --truncate -- ошибка формы вызова."""
        result = _invoke_tg_messages(["-n", "1", "--no-truncate", "--truncate", "10"])

        assert result.exit_code == 2
        assert "--no-truncate" in result.output

    def test_negative_truncate_rejected(self):
        """Отрицательный --truncate -- ошибка формы вызова."""
        result = _invoke_tg_messages(["-n", "1", "--truncate", "-5"])

        assert result.exit_code == 2


class TestTgMessagesJson:
    """`--json` был у пяти запросных команд, а здесь вывод разбирался регулярным выражением.

    Текст сообщения может содержать переводы строк, поэтому построчный разбор
    формата `  <дата>  [<id>]  <отправитель>: <текст>` ломается на первом же
    многострочном сообщении.
    """

    def test_json_puts_an_array_of_messages_into_stdout_alone(self):
        import json

        result = _invoke_tg_messages(["-n", "1", "--json"])

        payload = json.loads(result.stdout)
        assert isinstance(payload, list) and len(payload) == 1, payload
        entry = payload[0]
        assert entry["id"] == 42
        assert entry["sender"] == "Alice"
        assert entry["text"] == "hello"
        assert entry["media"] is None
        assert entry["date"]

    def test_json_keeps_the_text_whole_by_default(self):
        """Обрезка нужна терминалу, а не разбирающей программе."""
        import json

        text = "x" * 250

        result = _invoke_tg_messages(["-n", "1", "--json"], text=text)

        assert json.loads(result.stdout)[0]["text"] == text

    def test_json_keeps_the_media_type_in_its_own_field(self):
        """В строке для терминала тип медиа приписан к тексту; разбирающей программе
        нужен сам текст и тип отдельно."""
        import json

        result = _invoke_tg_messages(["-n", "1", "--json"], media="Photo")

        entry = json.loads(result.stdout)[0]
        assert entry["media"] == "Photo"
        assert entry["text"] == "hello", entry

    def test_json_still_cuts_when_asked_to(self):
        import json

        result = _invoke_tg_messages(["-n", "1", "--json", "--truncate", "10"], text="z" * 100)

        assert json.loads(result.stdout)[0]["text"] == "z" * 10


# ---------------------------------------------------------------------------
# TgApi: proxy / python-socks guard
# ---------------------------------------------------------------------------


def test_tgapi_raises_when_proxy_set_but_python_socks_missing(tmp_path):
    from tg_export.api import TgApi

    proxy = ("socks5", "127.0.0.1", 1080, True, None, None)
    with (
        patch("tg_export.api.importlib.util.find_spec", return_value=None),
        pytest.raises(RuntimeError, match="python-socks"),
    ):
        TgApi(str(tmp_path / "test.session"), 1, "hash", proxy=proxy)


def test_the_missing_proxy_hint_names_an_extra_the_project_actually_declares(tmp_path):
    """Подсказка в ошибке должна называть extra, который есть в pyproject.

    Переименование или удаление extra оставляет текст ошибки прежним, и
    пользователь выполняет команду, которая ничего не ставит. Тест сверяет имя
    из подсказки с объявленными extras, а не с константой в тесте.
    """
    import tomllib

    from tg_export.api import TgApi

    root = Path(__file__).resolve().parent.parent
    with (root / "pyproject.toml").open("rb") as fh:
        extras = set(tomllib.load(fh)["project"]["optional-dependencies"])
    assert extras, "в pyproject не объявлено ни одного extra — проверять нечего"

    proxy = ("socks5", "127.0.0.1", 1080, True, None, None)
    with (
        patch("tg_export.api.importlib.util.find_spec", return_value=None),
        pytest.raises(RuntimeError) as excinfo,
    ):
        TgApi(str(tmp_path / "test.session"), 1, "hash", proxy=proxy)

    message = str(excinfo.value)
    named = {name for name in extras if f"--extra {name}" in message and f"tg-export[{name}]" in message}
    assert named, f"подсказка не называет ни один объявленный extra {sorted(extras)}: {message}"


def test_tgapi_passes_proxy_to_client_when_python_socks_available(tmp_path):
    from tg_export.api import TgApi

    proxy = ("socks5", "127.0.0.1", 1080, True, None, None)
    with (
        patch("tg_export.api.importlib.util.find_spec", return_value=object()),
        patch("tg_export.api.FixedSQLiteSession"),
        patch("tg_export.api.TelegramClient") as mock_client,
    ):
        TgApi(str(tmp_path / "test.session"), 1, "hash", proxy=proxy)

    assert mock_client.call_args.kwargs["proxy"] == proxy


def test_tgapi_no_proxy_does_not_check_python_socks(tmp_path):
    from tg_export.api import TgApi

    with (
        patch("tg_export.api.importlib.util.find_spec", return_value=None) as mock_find,
        patch("tg_export.api.FixedSQLiteSession"),
        patch("tg_export.api.TelegramClient") as mock_client,
    ):
        TgApi(str(tmp_path / "test.session"), 1, "hash", proxy=None)

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
        from tg_export.cli.tg import _tg_send

        files = []
        for i in range(file_count):
            f = tmp_path / f"pic{i}.jpg"
            f.write_bytes(b"data" * (i + 1))
            files.append(str(f))

        api = AsyncMock()
        api.client = AsyncMock()

        @contextlib.asynccontextmanager
        async def fake_connected_api(_account_name):
            yield api, "acc"

        with (
            patch("tg_export.cli.common._connected_api", fake_connected_api),
            patch.object(cli_common, "_QUIET", True),
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

    monkeypatch.setattr(cli_tg, "_upload_progress", fake_progress)

    client = MagicMock()
    client.send_file = AsyncMock()

    await cli_tg._send_files(client, 1, [tricky], "text", True)

    assert descriptions == ["\\[draft]report.txt"], descriptions


def test_rich_swallows_an_unescaped_file_name():
    """Проверка того, что экранирование действительно требуется: без него имя
    печатается искажённым, с ним -- дословно."""
    import io

    from rich.console import Console

    from tg_export.exporter import file_progress_description

    raw = io.StringIO()
    Console(file=raw, width=80, force_terminal=False).print("[draft]report.txt")
    assert raw.getvalue() == "report.txt\n"

    escaped = io.StringIO()
    Console(file=escaped, width=80, force_terminal=False).print(
        file_progress_description("[draft]report.txt")
    )
    assert escaped.getvalue() == "[draft]report.txt\n"


def test_upload_progress_is_silent_without_a_terminal(monkeypatch):
    """Экспортёр рисует живой прогресс только при `console.is_terminal`, а
    прогресс отправки такой проверки не имел: в конвейере и в файле журнала
    оседали перерисовки бара."""
    import tg_export.console as console_module

    fake_console = MagicMock()
    fake_console.is_terminal = False
    monkeypatch.setattr(console_module, "console", fake_console)

    with cli_tg._upload_progress(by_bytes=True) as progress:
        assert progress is None


def test_upload_progress_shows_percentage_in_both_modes(monkeypatch):
    """Итоговый вид бара не должен зависеть от режима: побайтовый показывал
    байты и скорость, альбомный -- проценты, и одна и та же команда выглядела
    по-разному в зависимости от числа файлов."""
    import io

    from rich.console import Console
    from rich.progress import TaskProgressColumn

    import tg_export.console as console_module

    monkeypatch.setattr(console_module, "console", Console(file=io.StringIO(), force_terminal=True))

    for by_bytes in (True, False):
        with cli_tg._upload_progress(by_bytes=by_bytes) as progress:
            assert progress is not None
            kinds = [type(c) for c in progress.columns]
            assert TaskProgressColumn in kinds, (by_bytes, kinds)


@pytest.mark.asyncio
async def test_album_progress_never_passes_the_file_count(tmp_path, monkeypatch):
    """Telethon режет альбом на группы по 10 и передаёт в колбэк остаток списка
    как total (25 -> 15 -> 5), тогда как sent считается от начала альбома.
    Колбэк перетирал этим значением свой total, и для более чем десяти файлов
    прогресс уходил за 100%."""
    import contextlib
    from unittest.mock import MagicMock

    files = []
    for i in range(25):
        f = tmp_path / f"f{i}.jpg"
        f.write_bytes(b"x")
        files.append(f)

    updates = []
    totals = {}

    class _Progress:
        def add_task(self, description, total=None, **kwargs):
            totals[1] = total
            return 1

        def update(self, task, completed=None, total=None, **kwargs):
            if total is not None:
                totals[task] = total
            if completed is not None:
                updates.append(completed)

        def remove_task(self, *a, **k):
            pass

    @contextlib.contextmanager
    def fake_progress(by_bytes):
        yield _Progress()

    monkeypatch.setattr(cli_tg, "_upload_progress", fake_progress)

    async def send_file(recipient, paths, caption=None, force_document=False, progress_callback=None):
        # Воспроизводит нарезку Telethon: sent -- сквозной, total -- остаток.
        assert progress_callback is not None
        sent_count = 0
        remaining = len(paths)
        while remaining:
            chunk = min(10, remaining)
            for i in range(1, chunk + 1):
                progress_callback(sent_count + i, remaining)
            sent_count += 10
            remaining -= chunk

    client = MagicMock()
    client.send_file = send_file

    await cli_tg._send_files(client, 1, files, "text", False)

    assert totals[1] == 25, totals
    assert max(updates) <= 25, max(updates)


# ---------------------------------------------------------------------------
# tg info: разделение потоков и машиночитаемый вывод
# ---------------------------------------------------------------------------


def _invoke_tg_info(args: list[str], *, chat_ids=("123",), fail_on=(), calls: list | None = None):
    """Запустить `tg info` на подставном API; чаты из fail_on отвечают ошибкой.

    `calls` собирает аргументы каждого обращения к get_entity: по ним видно,
    разрешаются ли идентификаторы одним запросом или по одному.
    """
    from click.testing import CliRunner

    from tg_export.cli import main

    def one_entity(cid):
        if cid in fail_on:
            raise RuntimeError("chat unavailable")
        entity = MagicMock()
        entity.title = f"Chat {cid}"
        return entity

    def get_entity(cid):
        if calls is not None:
            calls.append(cid)
        if isinstance(cid, list):
            # Telethon разрешает список одним запросом, и отказ по любому из
            # идентификаторов завершает весь запрос ошибкой.
            return [one_entity(c) for c in cid]
        return one_entity(cid)

    history = MagicMock()
    history.count = 7
    history.messages = []

    mock_api = AsyncMock()
    mock_api.client.get_entity = AsyncMock(side_effect=get_entity)
    mock_api.client = AsyncMock(return_value=history, get_entity=AsyncMock(side_effect=get_entity))
    mock_api.connect = AsyncMock()
    mock_api.disconnect = AsyncMock()

    with patch("tg_export.cli.common._mgr") as mock_mgr, patch("tg_export.api.TgApi", return_value=mock_api):
        mgr = MagicMock()
        mgr.resolve_account.return_value = "test"
        mgr.load_credentials.return_value = ("id", "hash")
        mgr.load_proxy.return_value = None
        mgr.session_path.return_value = "/tmp/test.session"
        mock_mgr.return_value = mgr

        return CliRunner().invoke(main, ["tg", "info", *chat_ids, *args])


def test_tg_info_json_goes_to_stdout_alone():
    """Единственный способ получить машинный результат -- запись в файл; в пайп ничего.

    Остальные запросные команды (`account list`, `auth check`, `state show`)
    отдают JSON в stdout по флагу --json.
    """
    import json

    result = _invoke_tg_info(["--json"], chat_ids=("123", "456"))

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert [entry["id"] for entry in payload] == [123, 456]
    assert all(entry["messages"] == 7 for entry in payload)


def test_tg_info_keeps_the_counter_out_of_the_data_stream():
    """`tg info ... | grep` получал вперемешку данные и служебные строки."""
    result = _invoke_tg_info([], chat_ids=("123", "456"))

    assert result.exit_code == 0, result.stderr
    assert "[1/2]" not in result.stdout, result.stdout
    assert "Chat 123" in result.stdout
    assert "[1/2]" in result.stderr


def test_tg_info_without_any_chat_id_refuses_to_run():
    """Вызов, который не из чего собрать, -- отказ разобрать команду, а не успех.

    Ветка печатала подсказку и возвращала None, что вызывающий трактовал как
    нулевой код: под `--quiet` команда молчала и завершалась нулём, то есть
    скрипт не отличал её от успешной работы. Соседняя `tg send` на такую же
    ситуацию отвечает отказом.
    """
    result = _invoke_tg_info([], chat_ids=())

    assert result.exit_code == 2, f"код возврата {result.exit_code}, вывод: {result.stderr}"
    assert "Usage:" in result.stderr


def test_tg_info_prints_an_empty_document_when_the_filter_matched_nothing(tmp_path):
    """Фильтр, не подобравший ни одного чата, -- законный пустой результат.

    Под `--json` stdout оставался пустым: это не JSON-документ, и `jq` на нём
    падает с ошибкой разбора, хотя контракт `--json` обещает в stdout только
    документ.
    """
    import json

    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps([{"id": 1, "type": "personal"}]), encoding="utf-8")

    result = _invoke_tg_info(["--from-catalog", str(catalog), "--type", "channel", "--json"], chat_ids=())

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == []


def test_tg_info_reports_a_failed_chat_on_stderr():
    """Строка ERROR в stdout ломала машинную обработку вывода."""
    result = _invoke_tg_info([], chat_ids=("123", "456"), fail_on=(456,))

    assert result.exit_code == 1
    assert "ERROR" not in result.stdout
    assert "chat unavailable" in result.stderr


def test_upload_progress_yields_nothing_under_a_running_live(monkeypatch):
    """Рисует только верхний живой виджет console; остальные rich держит в стеке.

    Экспорт держит `Live` на общем console, прогресс отправки рисуется на нём
    же. Сегодня пути не пересекаются, но переиспользование `_send_files` внутри
    экспорта дало бы прогресс-бар, который стоит обновлений и ничего не
    показывает.
    """
    import io

    from rich.console import Console
    from rich.live import Live

    import tg_export.cli.tg as cli_tg
    import tg_export.console as console_module

    test_console = Console(file=io.StringIO(), force_terminal=True)
    monkeypatch.setattr(console_module, "console", test_console)

    with (
        Live(console=test_console, redirect_stdout=False, redirect_stderr=False),
        cli_tg._upload_progress(by_bytes=True) as progress,
    ):
        assert progress is None


def _preview_message(text=None, media=None, action=None, first="Ann", last="Lee"):
    msg = MagicMock()
    msg.message = text
    msg.media = media
    msg.action = action
    msg.sender = MagicMock(first_name=first, last_name=last)
    return msg


def test_the_preview_of_a_message_is_built_in_one_place():
    """Сборка пары (отправитель, текст) была скопирована в `messages` и в `info`.

    Копии разошлись: одна резала текст по `DEFAULT_MESSAGE_TEXT_LENGTH`, другая
    литералом 200, и правка настройки меняла вывод только одной команды.
    """
    from tg_export.cli.tg import _message_preview

    assert _message_preview(_preview_message("hello")) == ("Ann Lee", "hello")
    assert _message_preview(_preview_message(None, first="", last="")) == ("", "")

    photo = MagicMock()
    photo.__class__.__name__ = "MessageMediaPhoto"
    assert _message_preview(_preview_message("cat", media=photo))[1] == "[Photo] cat"
    assert _message_preview(_preview_message(None, media=photo))[1] == "[Photo]"

    created = MagicMock()
    created.__class__.__name__ = "MessageActionChatCreate"
    assert _message_preview(_preview_message("ignored", action=created))[1] == "(ChatCreate)"


def test_the_preview_is_cut_at_the_configured_length():
    """Длину задаёт настройка модуля, а не литерал в одной из команд."""
    from tg_export.cli.tg import _message_preview

    long_text = "x" * (cli_common.DEFAULT_MESSAGE_TEXT_LENGTH + 50)
    _, cut = _message_preview(_preview_message(long_text))

    assert len(cut) == cli_common.DEFAULT_MESSAGE_TEXT_LENGTH
    assert _message_preview(_preview_message(long_text), truncate=0)[1] == long_text


def test_tg_info_resolves_the_whole_catalog_in_one_request():
    """`--from-catalog --type` заведён ради пакетного опроса, а исполнялся поштучно.

    Идентификатора, которого нет в кеше сессии, `get_entity` стоит запроса к
    серверу: на каталоге в сотни чатов это столько же круговых задержек, идущих
    одна за другой.
    """
    calls: list = []

    result = _invoke_tg_info(["--json"], chat_ids=("123", "456", "789"), calls=calls)

    assert result.exit_code == 0, result.output
    assert calls == [[123, 456, 789]], f"идентификаторы разрешались по одному: {calls}"


def test_tg_info_still_reports_the_chat_that_failed_alone():
    """Пакетное разрешение падает целиком, поэтому остаётся поштучный проход.

    Отказ по одному чату не должен превращаться в отказ по всему каталогу.
    """
    calls: list = []

    result = _invoke_tg_info(["--json"], chat_ids=("123", "456"), fail_on=(456,), calls=calls)

    assert result.exit_code != 0, result.output
    assert '"id": 123' in result.output and '"error"' in result.output, result.output
    assert calls[0] == [123, 456], calls
    assert calls[1:] == [123, 456], f"после отказа пакета нужен поштучный проход: {calls}"


@pytest.mark.asyncio
async def test_download_compares_only_with_what_this_call_downloaded(monkeypatch, tmp_path):
    """Набор «уже скачанного» собирался из всего содержимого каталога.

    По умолчанию `--output .`, то есть текущий каталог, и в набор попадали
    посторонние файлы вместе с только что записанным `<msg_id>.txt`. Совпадение
    размера и первых 64 КБ приводило к тихому удалению скачанного медиа: вывод
    не содержал о нём ни строки.
    """
    import contextlib

    from tg_export.cli import common as cli_common
    from tg_export.cli import tg as cli_tg

    content = b"A" * 100
    (tmp_path / "unrelated.bin").write_bytes(content)

    api = MagicMock()
    api.client = _make_client(tmp_path, "media.bin", content)
    api.client.get_messages = AsyncMock(return_value=MagicMock(text=None, grouped_id=None))

    @contextlib.asynccontextmanager
    async def fake(_account_name):
        yield api, "me"

    monkeypatch.setattr(cli_common, "_connected_api", fake)

    code = await cli_tg._tg_download("acc", 1, 2, tmp_path)

    assert code == 0
    assert (tmp_path / "media.bin").exists(), "скачанный файл удалён из-за постороннего файла каталога"
