"""Блокирующий ввод-вывод не должен выполняться в потоке цикла событий.

Пока корутина занята синхронным чтением SQLite, копированием файла или
рендером страницы, цикл событий не обслуживает ничего другого: ни соседние
загрузки, ни соединение с Telegram, ни обработку сигнала. Проверки замеряют,
в каком потоке фактически выполняется тяжёлая часть.
"""

import shutil
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tg_export.cli import export as cli_export
from tg_export.config import MediaConfig
from tg_export.media import MediaDownloader
from tg_export.models import FileInfo, MediaType, PhotoMedia


def _downloader(**kwargs):
    state = MagicMock()
    state.get_file = AsyncMock(return_value=None)
    state.get_file_any_chat = AsyncMock(return_value=None)
    state.register_file = AsyncMock()
    cfg = MediaConfig(types=["all"], max_file_size_bytes=50 * 1024**2, concurrent_downloads=3)
    return MediaDownloader(MagicMock(), state, cfg, min_free_bytes=0, **kwargs)


def _photo(file_id=1, size=4):
    return PhotoMedia(
        type=MediaType.photo,
        file=FileInfo(id=file_id, size=size, name="p.jpg", mime_type="image/jpeg", local_path=None),
        width=1,
        height=1,
    )


@pytest.mark.asyncio
async def test_sibling_lookup_leaves_the_event_loop(tmp_path, monkeypatch):
    """Чтение соседней базы состояния идёт синхронным sqlite3 с ожиданием
    занятого писателя до 30 секунд -- всё это время цикл событий стоит."""
    seen = {}

    def fake_lookup(self, db_path, file_id):
        seen["thread"] = threading.get_ident()
        return None

    monkeypatch.setattr("tg_export.media._SiblingReaders.lookup", fake_lookup)

    dl = _downloader(sibling_db_paths=[tmp_path / "sibling.db"])
    await dl._try_link_sibling(_photo(), tmp_path / "chat")

    assert seen["thread"] != threading.get_ident()


@pytest.mark.asyncio
async def test_tdesktop_copy_leaves_the_event_loop(tmp_path, monkeypatch):
    """Копирование файла из выгрузки tdesktop -- это чтение и запись целого
    файла; в потоке цикла оно останавливает всё остальное."""
    src = tmp_path / "src.jpg"
    src.write_bytes(b"data")
    seen = {}
    real_copy = shutil.copy2

    def fake_copy(a, b, *args, **kwargs):
        seen["thread"] = threading.get_ident()
        return real_copy(a, b, *args, **kwargs)

    monkeypatch.setattr("tg_export.media.shutil.copy2", fake_copy)

    index = MagicMock()
    index.find_file = lambda msg_id: src
    dl = _downloader(tdesktop_indexes=[index])

    msg = MagicMock()
    msg.id = 5
    result = await dl._try_import_tdesktop(msg, _photo(), tmp_path / "chat")

    assert result is not None
    assert seen["thread"] != threading.get_ident()


@pytest.mark.asyncio
async def test_intra_account_link_leaves_the_event_loop(tmp_path, monkeypatch):
    """Связывание файла между чатами делает системный вызов и, при отказе,
    копирует файл целиком."""
    src_dir = tmp_path / "other"
    src_dir.mkdir()
    src = src_dir / "p.jpg"
    src.write_bytes(b"data")

    seen = {}
    real_link = __import__("os").link

    def fake_link(a, b):
        seen["thread"] = threading.get_ident()
        return real_link(a, b)

    monkeypatch.setattr("tg_export.media.os.link", fake_link)

    dl = _downloader()
    dl.state.get_file_any_chat = AsyncMock(return_value={"chat_id": 2, "local_path": str(src)})

    result = await dl._try_link_intra_account(_photo(), tmp_path / "chat", chat_id=1)

    assert result is not None
    assert seen["thread"] != threading.get_ident()


@pytest.mark.asyncio
async def test_index_render_leaves_the_event_loop(tmp_path):
    """Рендер индекса идёт при живом соединении с Telegram: пока шаблон
    собирается, соединение не обслуживается. Рендер чата уже вынесен в поток."""
    seen = {}
    renderer = MagicMock()

    def render_index(**kwargs):
        seen["thread"] = threading.get_ident()

    renderer.render_index = render_index

    cfg = MagicMock()
    for flag in ("personal_info", "contacts", "sessions", "userpics", "stories", "other_data"):
        setattr(cfg, flag, False)
    cfg.profile_music = False

    state = MagicMock()
    state.count_messages = AsyncMock(return_value=0)
    state.message_counts = AsyncMock(return_value={})

    await cli_export._render_index(renderer, [], cfg, state)

    assert seen["thread"] != threading.get_ident()


@pytest.mark.asyncio
async def test_global_data_render_leaves_the_event_loop():
    """То же для страниц глобальных данных."""
    from tg_export.exporter import Exporter

    seen = {}
    renderer = MagicMock()

    def render_contacts(contacts, frequent):
        seen["thread"] = threading.get_ident()

    renderer.render_contacts = render_contacts

    api = MagicMock()
    api.get_contacts = AsyncMock(return_value=MagicMock(users=[]))
    api.get_top_peers = AsyncMock(return_value=None)

    exporter = Exporter(
        api=api,
        state=MagicMock(),
        config=MagicMock(),
        renderer=renderer,
        downloader=MagicMock(),
        account="test",
        quiet=True,
    )

    await exporter._export_contacts()

    assert seen["thread"] != threading.get_ident()


@pytest.mark.asyncio
async def test_disk_space_is_not_checked_for_every_file(tmp_path, monkeypatch):
    """shutil.disk_usage вызывался на каждый файл. Проверка нужна, но не
    настолько часто: между вызовами свободное место меняется на величину
    скачанного, а не скачком."""
    calls = []
    real_usage = shutil.disk_usage

    def counting_usage(path):
        calls.append(path)
        return real_usage(path)

    monkeypatch.setattr("tg_export.media.shutil.disk_usage", counting_usage)

    dl = _downloader()
    chat_dir = tmp_path / "chat"
    chat_dir.mkdir()
    for _ in range(10):
        assert dl._has_free_space(chat_dir) is True

    assert len(calls) == 1, calls


@pytest.mark.asyncio
async def test_staging_cleanup_leaves_the_event_loop(tmp_path, monkeypatch):
    """Уборка staging обходит всё дерево выгрузки рекурсивно, со `stat` на запись.

    Выгрузка держит сотни тысяч файлов, и такой обход занимает секунды. Он
    идёт при живом соединении с Telegram, поэтому в потоке цикла останавливает
    и обмен с сервером, и обработчик сигнала: Ctrl+C в это время не отвечает.
    """
    from tg_export.exporter import Exporter, ExportStats

    seen = {}
    real_rglob = Path.rglob

    def watching_rglob(self, pattern):
        seen["thread"] = threading.get_ident()
        return real_rglob(self, pattern)

    monkeypatch.setattr("tg_export.verify.Path.rglob", watching_rglob)

    broken = {
        "file_id": 1,
        "chat_id": 100,
        "msg_id": 200,
        "expected_size": 1000,
        "actual_size": 10,
        "local_path": str(tmp_path / "out" / "photo.jpg"),
        "status": "partial",
    }
    state = MagicMock()
    state.get_files_to_verify = AsyncMock(return_value=[broken])
    monkeypatch.setattr("tg_export.exporter.redownload_broken_files", AsyncMock(return_value=[]))

    config = MagicMock()
    config.output.path = str(tmp_path / "out")

    exporter = Exporter(  # pyright: ignore[reportArgumentType]
        api=MagicMock(),
        state=state,
        config=config,
        renderer=MagicMock(),
        downloader=MagicMock(),
        account="acc",
        quiet=True,
    )

    await exporter._verify_files(ExportStats())

    assert seen["thread"] != threading.get_ident()


@pytest.mark.asyncio
async def test_closing_the_sibling_readers_leaves_the_event_loop(tmp_path, monkeypatch):
    """Закрытие читателей соседних баз ждёт ту же блокировку, что держит запрос.

    Запрос к соседней базе удерживает блокировку на всё своё время, а
    соединение открыто с ожиданием занятого писателя до 30 секунд. Закрытие
    берёт ту же блокировку, и в потоке цикла это синхронное ожидание: пока оно
    идёт, не обслуживаются ни оставшиеся корутины, ни сигналы.
    """
    seen = {}

    def watching_close(self):
        seen["thread"] = threading.get_ident()

    monkeypatch.setattr("tg_export.media._SiblingReaders.close", watching_close)

    dl = _downloader(sibling_db_paths=[tmp_path / "sibling.db"])
    await cli_export._close_downloader(dl)

    assert seen["thread"] != threading.get_ident()


@pytest.mark.asyncio
async def test_intra_account_target_dir_is_prepared_off_the_loop(tmp_path, monkeypatch):
    """Подготовка каталога назначения тоже блокирующая работа.

    Кроме самого связывания файла, путь между чатами создаёт подкаталог типа
    медиа и подбирает свободное имя, обходя каталог вызовами stat. В выгрузке
    с тысячами файлов в одном подкаталоге это заметная работа, и в потоке
    цикла она останавливает соседние загрузки так же, как копирование.
    """
    src_dir = tmp_path / "other"
    src_dir.mkdir()
    src = src_dir / "p.jpg"
    src.write_bytes(b"data")

    seen = {}
    real_mkdir = Path.mkdir

    def watching_mkdir(self, *args, **kwargs):
        seen["thread"] = threading.get_ident()
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr("tg_export.media.Path.mkdir", watching_mkdir)

    dl = _downloader()
    dl.state.get_file_any_chat = AsyncMock(return_value={"chat_id": 2, "local_path": str(src)})

    result = await dl._try_link_intra_account(_photo(), tmp_path / "chat", chat_id=1)

    assert result is not None
    assert seen["thread"] != threading.get_ident()


@pytest.mark.asyncio
async def test_the_download_path_prepares_its_directories_off_the_loop(tmp_path, monkeypatch):
    """Создание каталогов, уборка staging и mkdtemp шли в потоке цикла событий.

    Проект держит правило «файловые вызовы -- в поток» и следит за ним для
    пути между чатами, но основной путь загрузки под него не попал: каждый
    файл платил тремя обращениями к файловой системе, останавливая соседние
    загрузки того же чата.
    """
    import tempfile as tempfile_module

    seen = {}
    real_mkdtemp = tempfile_module.mkdtemp

    def watching_mkdtemp(*args, **kwargs):
        seen["thread"] = threading.get_ident()
        return real_mkdtemp(*args, **kwargs)

    monkeypatch.setattr("tg_export.media.tempfile.mkdtemp", watching_mkdtemp)

    dl = _downloader()
    dl.api.download_media = AsyncMock(return_value=None)

    await dl.download(MagicMock(id=5), _photo(), tmp_path / "chat", chat_id=1)

    assert seen["thread"] != threading.get_ident()
