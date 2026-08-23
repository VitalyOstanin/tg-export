"""Перекачивание битых файлов не должно уничтожать то, что уже есть.

Прежний порядок был «удалить, потом качать»: обрыв связи или Ctrl+C между
этими шагами оставлял файл удалённым, а запись в БД — со старым путём и
статусом partial.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


def _entry(path: Path) -> dict:
    return {
        "file_id": 1,
        "chat_id": 100,
        "msg_id": 200,
        "expected_size": 1000,
        "actual_size": 10,
        "local_path": str(path),
        "status": "partial",
    }


@pytest.mark.asyncio
async def test_failed_redownload_keeps_the_existing_file(tmp_path):
    from tg_export.verify import redownload_broken_file

    existing = tmp_path / "photo.jpg"
    existing.write_bytes(b"partial")

    api = MagicMock()
    api.client.get_messages = AsyncMock(return_value=MagicMock(media=object()))
    api.download_media = AsyncMock(side_effect=RuntimeError("connection lost"))
    state = AsyncMock()

    with pytest.raises(RuntimeError):
        await redownload_broken_file(api, state, _entry(existing))

    assert existing.read_bytes() == b"partial"
    state.register_file.assert_not_called()


@pytest.mark.asyncio
async def test_empty_download_keeps_the_existing_file(tmp_path):
    from tg_export.verify import RedownloadResult, redownload_broken_file

    existing = tmp_path / "photo.jpg"
    existing.write_bytes(b"partial")

    api = MagicMock()
    api.client.get_messages = AsyncMock(return_value=MagicMock(media=object()))
    api.download_media = AsyncMock(return_value=None)
    state = AsyncMock()

    result, _ = await redownload_broken_file(api, state, _entry(existing))
    assert result is RedownloadResult.nothing_downloaded
    assert existing.read_bytes() == b"partial"
    state.register_file.assert_not_called()


@pytest.mark.asyncio
async def test_successful_redownload_replaces_the_file(tmp_path):
    from tg_export.verify import RedownloadResult, redownload_broken_file

    existing = tmp_path / "photo.jpg"
    existing.write_bytes(b"partial")

    async def fake_download(tl_msg, target_dir, *args, **kwargs):
        out = Path(target_dir) / "photo.jpg"
        out.write_bytes(b"complete-content")
        return str(out)

    api = MagicMock()
    api.client.get_messages = AsyncMock(return_value=MagicMock(media=object()))
    api.download_media = AsyncMock(side_effect=fake_download)
    state = AsyncMock()

    result, final_path = await redownload_broken_file(api, state, _entry(existing))
    assert result is RedownloadResult.replaced and final_path == existing
    assert existing.read_bytes() == b"complete-content"
    assert state.register_file.await_count == 1
    kwargs = state.register_file.await_args.kwargs
    assert kwargs["status"] == "done"
    assert kwargs["actual_size"] == len(b"complete-content")
    # После замены во временном каталоге ничего не остаётся.
    assert [p.name for p in tmp_path.iterdir()] == ["photo.jpg"]


@pytest.mark.asyncio
async def test_missing_message_is_skipped_without_touching_the_file(tmp_path):
    from tg_export.verify import RedownloadResult, redownload_broken_file

    existing = tmp_path / "photo.jpg"
    existing.write_bytes(b"partial")

    api = MagicMock()
    api.client.get_messages = AsyncMock(return_value=None)
    api.download_media = AsyncMock()
    state = AsyncMock()

    result, _ = await redownload_broken_file(api, state, _entry(existing))
    assert result is RedownloadResult.no_media
    assert existing.read_bytes() == b"partial"
    api.download_media.assert_not_called()


def test_stale_staging_dirs_are_cleaned(tmp_path):
    from tg_export.verify import clean_staging

    stale = tmp_path / "chat" / ".tg-export-verify-abc"
    stale.mkdir(parents=True)
    (stale / "leftover.bin").write_bytes(b"x")
    keep = tmp_path / "chat" / "photo.jpg"
    keep.write_bytes(b"y")

    clean_staging(tmp_path)

    assert not stale.exists()
    assert keep.exists()


@pytest.mark.asyncio
async def test_the_run_verify_pass_keeps_the_file_when_the_download_fails(tmp_path):
    """`run --verify` шёл своим путём, где файл удалялся до скачивания замены."""
    from tg_export.exporter import Exporter, ExportStats

    existing = tmp_path / "photo.jpg"
    existing.write_bytes(b"partial")

    api = MagicMock()
    api.client.get_messages = AsyncMock(return_value=MagicMock(media=object()))
    api.download_media = AsyncMock(side_effect=RuntimeError("connection lost"))
    state = MagicMock()
    state.get_files_to_verify = AsyncMock(return_value=[_entry(existing)])
    state.register_file = AsyncMock()
    state.commit = AsyncMock()

    config = MagicMock()
    config.defaults.media.concurrent_downloads = 2

    exporter = Exporter(  # pyright: ignore[reportArgumentType]
        api=api,
        state=state,
        config=config,
        renderer=MagicMock(),
        downloader=MagicMock(),
        account="acc",
        quiet=True,
    )
    stats = ExportStats()

    await exporter._verify_files(stats)

    assert existing.read_bytes() == b"partial", "битый файл удалён, замена не скачалась"
    assert stats.errors, "неудачное перекачивание не попало в сводку"
    state.register_file.assert_not_called()


def _broken(file_id: int, chat_id: int, msg_id: int, path: Path) -> dict:
    return {
        "file_id": file_id,
        "chat_id": chat_id,
        "msg_id": msg_id,
        "expected_size": 1000,
        "actual_size": 10,
        "local_path": str(path),
        "status": "partial",
    }


@pytest.mark.asyncio
async def test_broken_files_are_asked_for_in_batches_per_chat(tmp_path):
    """Каждому битому файлу доставался свой круг «запрос -- ответ» к серверу.

    Telegram принимает список идентификаторов, а список битых файлов содержит
    и чат, и сообщение: чат опрашивается одним запросом на пачку.
    """
    from tg_export.verify import redownload_broken_files

    entries = [_broken(i, 100 + i % 2, 200 + i, tmp_path / f"f{i}.jpg") for i in range(6)]
    for entry in entries:
        Path(entry["local_path"]).write_bytes(b"partial")

    calls: list[tuple[int, list[int]]] = []

    async def get_messages(chat_id, ids: list[int]):
        calls.append((chat_id, ids))
        return [MagicMock(media=object()) for _ in ids]

    api = MagicMock()
    api.client.get_messages = get_messages
    api.download_media = AsyncMock(return_value=None)

    await redownload_broken_files(api, AsyncMock(), entries, concurrency=3)

    assert len(calls) == 2, f"на два чата должно приходиться два запроса: {calls}"
    assert sorted(chat_id for chat_id, _ in calls) == [100, 101]
    assert sorted(len(ids) for _, ids in calls) == [3, 3]


@pytest.mark.asyncio
async def test_broken_files_are_downloaded_with_the_configured_parallelism(tmp_path):
    """Скачивание шло строго по одному: настройка concurrent_downloads к пути
    проверки не применялась, и сеть простаивала на каждом круге."""
    import asyncio

    from tg_export.verify import redownload_broken_files

    entries = [_broken(i, 100, 200 + i, tmp_path / f"f{i}.jpg") for i in range(6)]
    for entry in entries:
        Path(entry["local_path"]).write_bytes(b"partial")

    running = 0
    peak = 0

    async def download_media(tl_msg, staging):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return None

    api = MagicMock()
    api.client.get_messages = AsyncMock(return_value=[MagicMock(media=object()) for _ in entries])
    api.download_media = download_media

    await redownload_broken_files(api, AsyncMock(), entries, concurrency=3)

    assert peak == 3, f"одновременных загрузок: {peak}"


@pytest.mark.asyncio
async def test_parallel_redownload_does_not_put_two_files_on_one_path(tmp_path):
    """Две битые записи с разными именами не должны схлопнуться в один файл.

    Экспорт развёл одноимённые файлы как `photo.jpg` и `photo (1).jpg`. При
    перекачивании каждый скачивается в пустой временный каталог, и Telethon в
    обоих случаях выбирает базовое имя, поэтому имя в целевом каталоге надо
    выбирать так же, как это делает выгрузка, — через общий реестр.
    """
    import asyncio

    from tg_export.verify import redownload_broken_files

    first = tmp_path / "photo.jpg"
    second = tmp_path / "photo (1).jpg"
    first.write_bytes(b"partial-1")
    second.write_bytes(b"partial-2")

    payloads = {200: b"content-of-200", 201: b"content-of-201"}

    async def fake_download(tl_msg, target_dir, *args, **kwargs):
        out = Path(target_dir) / "photo.jpg"
        # Уступить управление, чтобы обе корутины оказались между скачиванием
        # и переносом одновременно.
        await asyncio.sleep(0)
        out.write_bytes(payloads[tl_msg.id])
        return str(out)

    messages = [MagicMock(media=object(), id=msg_id) for msg_id in (200, 201)]
    api = MagicMock()
    api.client.get_messages = AsyncMock(return_value=messages)
    api.download_media = AsyncMock(side_effect=fake_download)
    state = AsyncMock()

    entries = [
        {**_entry(first), "file_id": 1, "msg_id": 200},
        {**_entry(second), "file_id": 2, "msg_id": 201},
    ]
    await redownload_broken_files(api, state, entries, concurrency=2)

    registered = {call.kwargs["local_path"] for call in state.register_file.await_args_list}
    assert len(registered) == 2, f"обе записи указывают на один путь: {registered}"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["photo (1).jpg", "photo.jpg"]
    assert {first.read_bytes(), second.read_bytes()} == set(payloads.values())


@pytest.mark.asyncio
async def test_replacement_under_a_new_name_removes_the_old_file(tmp_path):
    """Если Telethon дал файлу другое имя, прежний файл удаляется после замены."""
    from tg_export.verify import RedownloadResult, redownload_broken_file

    existing = tmp_path / "photo.jpg"
    existing.write_bytes(b"partial")

    async def fake_download(tl_msg, target_dir, *args, **kwargs):
        out = Path(target_dir) / "video.mp4"
        out.write_bytes(b"complete")
        return str(out)

    api = MagicMock()
    api.client.get_messages = AsyncMock(return_value=MagicMock(media=object()))
    api.download_media = AsyncMock(side_effect=fake_download)
    state = AsyncMock()

    result, final_path = await redownload_broken_file(api, state, _entry(existing))

    assert result is RedownloadResult.replaced
    assert final_path == tmp_path / "video.mp4"
    assert not existing.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["video.mp4"]


@pytest.mark.asyncio
async def test_a_stopped_pass_downloads_nothing(tmp_path):
    """При запросе остановки проход не качает ни одного файла и не трогает диск."""
    from tg_export.verify import redownload_broken_files

    existing = tmp_path / "photo.jpg"
    existing.write_bytes(b"partial")

    api = MagicMock()
    api.client.get_messages = AsyncMock(return_value=[MagicMock(media=object(), id=200)])
    api.download_media = AsyncMock()
    state = AsyncMock()

    outcomes = await redownload_broken_files(
        api, state, [_entry(existing)], concurrency=2, should_stop=lambda: True
    )

    assert [outcome.result for outcome in outcomes] == [None]
    api.client.get_messages.assert_not_called()
    api.download_media.assert_not_called()
    assert existing.read_bytes() == b"partial"
