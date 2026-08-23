import asyncio
from pathlib import Path

import pytest

from tg_export.config import MediaConfig
from tg_export.media import check_disk_space, check_skip_reason, media_subdir
from tg_export.models import DocumentMedia, FileInfo, MediaType, PhotoMedia


def test_check_skip_allowed_type():
    media = PhotoMedia(
        type=MediaType.photo,
        file=FileInfo(id=1, size=1000, name="photo.jpg", mime_type="image/jpeg", local_path=None),
        width=100,
        height=100,
    )
    cfg = MediaConfig(types=["photo", "video"], max_file_size_bytes=50 * 1024**2, concurrent_downloads=3)
    assert check_skip_reason(media, cfg) is None


def test_check_skip_disallowed_type():
    media = PhotoMedia(
        type=MediaType.photo,
        file=FileInfo(id=1, size=1000, name="photo.jpg", mime_type="image/jpeg", local_path=None),
        width=100,
        height=100,
    )
    cfg = MediaConfig(types=["document"], max_file_size_bytes=50 * 1024**2, concurrent_downloads=3)
    assert check_skip_reason(media, cfg) == "skipped_by_type"


def test_check_skip_file_too_large():
    media = DocumentMedia(
        type=MediaType.document,
        file=FileInfo(id=1, size=100 * 1024**2, name="big.zip", mime_type="application/zip", local_path=None),
        name="big.zip",
        mime_type="application/zip",
    )
    cfg = MediaConfig(types=["document"], max_file_size_bytes=50 * 1024**2, concurrent_downloads=3)
    assert check_skip_reason(media, cfg) == "skipped_by_size"


def test_check_skip_all_types():
    media = PhotoMedia(
        type=MediaType.photo,
        file=FileInfo(id=1, size=1000, name="p.jpg", mime_type="image/jpeg", local_path=None),
        width=100,
        height=100,
    )
    cfg = MediaConfig(types=["all"], max_file_size_bytes=50 * 1024**2, concurrent_downloads=3)
    assert check_skip_reason(media, cfg) is None


def test_media_subdir():
    assert media_subdir(MediaType.photo) == "photos"
    assert media_subdir(MediaType.video) == "videos"
    assert media_subdir(MediaType.document) == "files"
    assert media_subdir(MediaType.voice) == "voice_messages"
    assert media_subdir(MediaType.video_note) == "video_messages"
    assert media_subdir(MediaType.sticker) == "stickers"
    assert media_subdir(MediaType.gif) == "gifs"


def test_check_disk_space():
    assert check_disk_space(Path("/tmp"), min_free_bytes=1) is True
    assert check_disk_space(Path("/tmp"), min_free_bytes=10**18) is False


def _downloader(api, tmp_path):
    """MediaDownloader на подставных api и состоянии."""
    from unittest.mock import AsyncMock, MagicMock

    from tg_export.media import MediaDownloader

    state = MagicMock()
    state.get_file = AsyncMock(return_value=None)
    state.register_file = AsyncMock()
    state.find_file_by_id = AsyncMock(return_value=None)
    state.get_file_any_chat = AsyncMock(return_value=None)
    cfg = MediaConfig(types=["all"], max_file_size_bytes=50 * 1024**2, concurrent_downloads=3)
    dl = MediaDownloader(api, state, cfg, min_free_bytes=0)
    return dl


def _photo(file_id=1, name="photo.jpg"):
    return PhotoMedia(
        type=MediaType.photo,
        file=FileInfo(id=file_id, size=4, name=name, mime_type="image/jpeg", local_path=None),
        width=1,
        height=1,
    )


@pytest.mark.asyncio
async def test_failed_download_keeps_the_files_of_other_downloads(tmp_path, monkeypatch):
    """Уборка после отказа удаляла всё новое в общем каталоге чата, а снимок
    каталога снимался ещё до захвата семафора. С параллельными загрузками это
    означает удаление файлов, которые в это же время дописал сосед.
    """
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setattr("tg_export.media._MAX_DOWNLOAD_ATTEMPTS", 1)

    chat_dir = tmp_path / "chat"
    neighbour = chat_dir / "photos" / "neighbour.jpg"

    async def download_media(tl_message, target_dir, progress_cb=None):
        # Пока идёт эта загрузка, соседняя завершает свою и кладёт файл рядом.
        neighbour.parent.mkdir(parents=True, exist_ok=True)
        neighbour.write_bytes(b"kept")
        raise OSError("connection dropped")

    api = MagicMock()
    api.download_media = AsyncMock(side_effect=download_media)
    dl = _downloader(api, tmp_path)

    msg = MagicMock()
    msg.id = 10
    msg.file = None

    with pytest.raises(OSError):
        await dl.download(msg, _photo(), chat_dir, chat_id=1)

    assert neighbour.exists(), "уборка после отказа снесла файл соседней загрузки"


@pytest.mark.asyncio
async def test_failed_download_leaves_no_partial_of_its_own(tmp_path, monkeypatch):
    """Свой недокачанный файл после отказа остаться не должен."""
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setattr("tg_export.media._MAX_DOWNLOAD_ATTEMPTS", 1)

    chat_dir = tmp_path / "chat"

    async def download_media(tl_message, target_dir, progress_cb=None):
        (Path(target_dir) / "partial.jpg").write_bytes(b"half")
        raise OSError("connection dropped")

    api = MagicMock()
    api.download_media = AsyncMock(side_effect=download_media)
    dl = _downloader(api, tmp_path)

    msg = MagicMock()
    msg.id = 11
    msg.file = None

    with pytest.raises(OSError):
        await dl.download(msg, _photo(), chat_dir, chat_id=1)

    leftovers = list((chat_dir / "photos").rglob("*"))
    assert leftovers == [], leftovers


@pytest.mark.asyncio
async def test_successful_download_does_not_overwrite_a_different_file(tmp_path):
    """Совпадение имён не должно затирать чужой файл: до правки Telethon сам
    подбирал свободное имя, потому что видел целевой каталог."""
    from unittest.mock import AsyncMock, MagicMock

    chat_dir = tmp_path / "chat"
    photos = chat_dir / "photos"
    photos.mkdir(parents=True)
    (photos / "photo.jpg").write_bytes(b"older")

    async def download_media(tl_message, target_dir, progress_cb=None):
        path = Path(target_dir) / "photo.jpg"
        path.write_bytes(b"newer")
        return str(path)

    api = MagicMock()
    api.download_media = AsyncMock(side_effect=download_media)
    dl = _downloader(api, tmp_path)

    msg = MagicMock()
    msg.id = 12
    msg.file = None

    path, status = await dl.download(msg, _photo(), chat_dir, chat_id=1)

    assert status == "downloaded"
    assert (photos / "photo.jpg").read_bytes() == b"older"
    assert path is not None and Path(path).read_bytes() == b"newer"


@pytest.mark.asyncio
async def test_each_retry_leaves_a_trace_in_the_log(tmp_path, monkeypatch, caplog):
    """Бесшумные повторы неотличимы от зависшего процесса.

    Наружу доходило только исключение последней попытки, а причина предыдущих
    и величина задержки терялись целиком.
    """
    import logging
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setattr("tg_export.media._MAX_DOWNLOAD_ATTEMPTS", 3)
    monkeypatch.setattr("tg_export.media.asyncio.sleep", AsyncMock())

    api = MagicMock()
    api.download_media = AsyncMock(side_effect=ConnectionResetError("connection dropped"))
    dl = _downloader(api, tmp_path)

    msg = MagicMock()
    msg.id = 77
    msg.file = None

    with caplog.at_level(logging.DEBUG, logger="tg_export.media"), pytest.raises(ConnectionResetError):
        await dl.download(msg, _photo(), tmp_path / "chat", chat_id=5)

    attempts = [r for r in caplog.records if "attempt" in r.getMessage() and r.levelno == logging.DEBUG]
    assert len(attempts) == 2, f"нет записей о неудачных попытках: {[r.getMessage() for r in caplog.records]}"
    assert any("ConnectionResetError" in r.getMessage() for r in attempts)
    assert any(r.levelno == logging.WARNING and "77" in r.getMessage() for r in caplog.records), (
        "исчерпание попыток не поднято до WARNING"
    )


@pytest.mark.asyncio
async def test_a_telegram_server_error_is_retried_like_a_network_one(tmp_path, monkeypatch):
    """Отказы Telegram RPC -- те же временные сбои, что и обрыв сокета.

    Они наследуются от RPCError, а не от OSError, и потому не ретраились вовсе:
    один ServerError обрывал файл, хотя повтор дал бы результат.
    """
    from unittest.mock import AsyncMock, MagicMock

    from telethon.errors import ServerError

    monkeypatch.setattr("tg_export.media.asyncio.sleep", AsyncMock())

    calls = []

    async def download_media(tl_message, target_dir, progress_cb=None):
        calls.append(1)
        if len(calls) == 1:
            raise ServerError(request=None, message="INTERNAL")
        path = Path(target_dir) / "photo.jpg"
        path.write_bytes(b"data")
        return str(path)

    api = MagicMock()
    api.download_media = AsyncMock(side_effect=download_media)
    dl = _downloader(api, tmp_path)

    msg = MagicMock()
    msg.id = 78
    msg.file = None

    local_path, status = await dl.download(msg, _photo(), tmp_path / "chat", chat_id=5)

    assert status == "downloaded", f"ServerError не был повторён: {status}"
    assert len(calls) == 2
    assert local_path is not None and local_path.exists()


@pytest.mark.asyncio
async def test_reuse_replaces_a_truncated_leftover_instead_of_returning_it(tmp_path):
    """Совпадения имени недостаточно: обрыв прошлого прогона оставляет файл
    того же имени и меньшего размера, а путь переиспользования возвращал его
    как готовый и регистрировал в состоянии со статусом done.
    """
    from unittest.mock import AsyncMock, MagicMock

    src = tmp_path / "other_chat" / "photos" / "photo.jpg"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"full")

    chat_dir = tmp_path / "chat"
    dst = chat_dir / "photos" / "photo.jpg"
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b"tr")

    dl = _downloader(MagicMock(), tmp_path)
    dl.state.get_file_any_chat = AsyncMock(return_value={"chat_id": 2, "local_path": str(src)})

    result = await dl._try_link_intra_account(_photo(), chat_dir, chat_id=1)

    assert result == dst
    assert dst.read_bytes() == b"full"


@pytest.mark.asyncio
async def test_two_files_with_the_same_name_do_not_land_on_one_path(tmp_path, monkeypatch):
    """Замок берётся по file_id, а борьба идёт за имя в каталоге назначения.

    Два разных файла, у которых совпадает имя исходника, проходят проверку
    «место свободно» одновременно и оба пишут по одному пути: первый делает
    ссылку, второй копирует поверх неё. На диске остаётся одно содержимое, а в
    состоянии — две записи, ссылающиеся на него.
    """
    import threading
    from unittest.mock import AsyncMock, MagicMock

    import tg_export.media as media_module

    sources = {}
    for file_id, payload in ((1, b"first"), (2, b"second file")):
        src = tmp_path / f"other_{file_id}" / "photos" / "photo.jpg"
        src.parent.mkdir(parents=True)
        src.write_bytes(payload)
        sources[file_id] = src

    chat_dir = tmp_path / "chat"
    dl = _downloader(MagicMock(), tmp_path)

    async def get_file_any_chat(file_id):
        return {"chat_id": 2, "local_path": str(sources[file_id])}

    dl.state.get_file_any_chat = AsyncMock(side_effect=get_file_any_chat)

    # Барьер держит обе корутины после проверки «путь свободен» и до записи:
    # без него исход зависит от планировщика потоков.
    barrier = threading.Barrier(2, timeout=5)
    original = media_module._link_or_copy

    def link_or_copy(src, dst):
        barrier.wait()
        return original(src, dst)

    monkeypatch.setattr(media_module, "_link_or_copy", link_or_copy)

    def _media(file_id, size):
        m = _photo(file_id=file_id)
        m.file.size = size
        return m

    msg1, msg2 = MagicMock(id=11), MagicMock(id=12)
    results = await asyncio.gather(
        dl.download(msg1, _media(1, len(b"first")), chat_dir, 1),
        dl.download(msg2, _media(2, len(b"second file")), chat_dir, 1),
    )

    paths = [p for p, _status in results]
    assert paths[0] != paths[1], "оба файла записаны по одному пути"
    assert paths[0].read_bytes() == b"first"
    assert paths[1].read_bytes() == b"second file"


def test_a_long_name_fits_the_progress_column():
    """Ширина и срез были двумя несвязанными числами: 40 и 37.

    Правка одного из них делала строку длиннее колонки. Срез вычисляется из
    ширины, а тест сверяется с константой, а не с литералом.
    """
    from tg_export.media import _PROGRESS_NAME_WIDTH, _progress_name

    short = "photo_1.jpg"
    assert _progress_name(short) == short

    fitted = _progress_name("x" * 100)
    assert len(fitted) == _PROGRESS_NAME_WIDTH
    assert fitted.endswith("...")
