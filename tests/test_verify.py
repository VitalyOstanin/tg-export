"""Перекачивание битых файлов не должно уничтожать то, что уже есть.

Прежний порядок был «удалить, потом качать»: обрыв связи или Ctrl+C между
этими шагами оставлял файл удалённым, а запись в БД — со старым путём и
статусом partial.
"""

import asyncio
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
    """Каталог, оставшийся от убитого прогона, убирается перед следующим."""
    from tg_export.media import clean_staging

    stale = tmp_path / "chat" / ".tg-export-staging-abc"
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
        return [MagicMock(media=object(), id=msg_id) for msg_id in ids]

    api = MagicMock()
    api.client.get_messages = get_messages
    api.download_media = AsyncMock(return_value=None)

    await redownload_broken_files(api, AsyncMock(), entries, concurrency=3)

    assert len(calls) == 2, f"на два чата должно приходиться два запроса: {calls}"
    assert sorted(chat_id for chat_id, _ in calls) == [100, 101]
    assert sorted(len(ids) for _, ids in calls) == [3, 3]


@pytest.mark.asyncio
async def test_downloading_starts_before_every_message_has_been_fetched(tmp_path):
    """Все сообщения вычитывались до первой загрузки.

    Запрос идёт пачками по сто на чат, и на длинном списке битых файлов сеть
    простаивала столько кругов «запрос -- ответ», сколько пачек, прежде чем
    начать качать хоть что-то.
    """
    import asyncio

    from tg_export.verify import redownload_broken_files

    entries = [_broken(i, 100 + i, 200 + i, tmp_path / f"f{i}.jpg") for i in range(3)]
    for entry in entries:
        Path(entry["local_path"]).write_bytes(b"partial")

    events: list[str] = []

    async def get_messages(chat_id, ids):
        events.append("fetch")
        await asyncio.sleep(0.01)
        return [MagicMock(media=object(), id=msg_id) for msg_id in ids]

    async def download_media(tl_msg, staging):
        events.append("download")
        await asyncio.sleep(0)
        return None

    api = MagicMock()
    api.client.get_messages = get_messages
    api.download_media = download_media

    await redownload_broken_files(api, AsyncMock(), entries, concurrency=3)

    assert events.count("fetch") == 3, f"по запросу на чат: {events}"
    last_fetch = len(events) - 1 - events[::-1].index("fetch")
    assert events.index("download") < last_fetch, f"порядок событий: {events}"


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
    api.client.get_messages = AsyncMock(
        return_value=[MagicMock(media=object(), id=e["msg_id"]) for e in entries]
    )
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


@pytest.mark.asyncio
async def test_a_transient_failure_of_a_redownload_is_retried(tmp_path, monkeypatch):
    """Перекачивание шло мимо политики повторов и теряло файл на первом обрыве.

    Экспорт различает преходящие и непреходящие отказы и повторяет первые, а
    починка вызывала транспорт напрямую: обрыв связи или ответ сервера об
    ожидании превращали битый файл в отказ прохода, задача которого -- этот
    файл восстановить.
    """
    from tg_export.verify import RedownloadResult, redownload_broken_file

    monkeypatch.setattr("tg_export.media.asyncio.sleep", AsyncMock())

    existing = tmp_path / "photo.jpg"
    existing.write_bytes(b"partial")

    attempts = []

    async def flaky_download(tl_msg, target_dir, *args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("reset by peer")
        path = Path(target_dir) / "photo.jpg"
        path.write_bytes(b"whole")
        return str(path)

    api = MagicMock()
    api.client.get_messages = AsyncMock(return_value=MagicMock(media=object()))
    api.download_media = flaky_download
    state = AsyncMock()

    result, final = await redownload_broken_file(api, state, _entry(existing))

    assert result is RedownloadResult.replaced
    assert len(attempts) == 2, "преходящий отказ не был повторён"
    assert final is not None and final.read_bytes() == b"whole"


@pytest.mark.asyncio
async def test_a_permanent_failure_of_a_redownload_is_not_retried(tmp_path, monkeypatch):
    """Нехватка места отвечает одинаково на каждой попытке.

    Повтор в этом случае стоит файлу полной паузы отката и превращает
    заполненный диск в долгий холостой проход по каталогу.
    """
    import errno

    from tg_export.verify import redownload_broken_file

    monkeypatch.setattr("tg_export.media.asyncio.sleep", AsyncMock())

    existing = tmp_path / "photo.jpg"
    existing.write_bytes(b"partial")

    attempts = []

    async def full_disk(tl_msg, target_dir, *args, **kwargs):
        attempts.append(1)
        raise OSError(errno.ENOSPC, "No space left on device")

    api = MagicMock()
    api.client.get_messages = AsyncMock(return_value=MagicMock(media=object()))
    api.download_media = full_disk
    state = AsyncMock()

    with pytest.raises(OSError):
        await redownload_broken_file(api, state, _entry(existing))

    assert len(attempts) == 1, "непреходящий отказ был повторён"


@pytest.mark.asyncio
async def test_a_failure_of_the_verify_phase_leaves_the_run_with_its_summary(tmp_path):
    """Отказ фазы проверки уносил сводку, индекс и весь список ошибок прогона.

    Соседняя фаза -- выгрузка общих данных -- свой отказ записывает в
    `stats.errors`, потому что по этому списку считается код возврата. У
    проверки такого обработчика не было: отказ базы или сети выходил из
    `run` целиком, и многочасовой экспорт, уже записанный на диск, выглядел
    как полностью упавшая команда.
    """
    from tg_export.exporter import Exporter

    api = MagicMock()
    state = MagicMock()
    state.get_files_to_verify = AsyncMock(side_effect=RuntimeError("database is locked"))

    config = MagicMock()
    config.output.path = str(tmp_path / "out")
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

    stats = await exporter.run(dry_run=True, verify=True, chat_list=[])

    assert any("verify" in error for error in stats.errors), (
        f"отказ фазы проверки не попал в список ошибок прогона: {stats.errors}"
    )


@pytest.mark.asyncio
async def test_an_unknown_outcome_is_not_counted_as_a_re_downloaded_file(tmp_path, monkeypatch):
    """Последняя ветка разбора исходов проверяла «не None», а не член перечисления.

    Перечисление исходов заведено ровно затем, чтобы новый исход нельзя было
    добавить в одном месте и пропустить в другом; «не None» засчитывал бы
    четвёртый исход в перекачанные -- молча, в обоих местах разбора.
    """
    from tg_export import verify as verify_module
    from tg_export.exporter import Exporter, ExportStats

    broken = tmp_path / "photo.jpg"
    broken.write_bytes(b"partial")

    async def unknown_outcome(*_args, **_kwargs):
        return [verify_module.RedownloadOutcome(_entry(broken), result="still_broken")]  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr("tg_export.exporter.redownload_broken_files", unknown_outcome)

    state = MagicMock()
    state.get_files_to_verify = AsyncMock(return_value=[_entry(broken)])

    config = MagicMock()
    config.defaults.media.concurrent_downloads = 2

    exporter = Exporter(  # pyright: ignore[reportArgumentType]
        api=MagicMock(),
        state=state,
        config=config,
        renderer=MagicMock(),
        downloader=MagicMock(),
        account="acc",
        quiet=True,
    )
    stats = ExportStats()

    await exporter._verify_files(stats)

    assert stats.errors, "неизвестный исход перекачивания прошёл как успешный"


@pytest.mark.asyncio
async def test_the_verify_command_fails_when_a_file_stays_broken(tmp_path, monkeypatch):
    """Справочник обещает код 1, когда файл починить не удалось.

    Обещание держалось только кодом: команда `verify` не исполнялась ни одним
    тестом, поэтому изменение исхода прохода не отразилось бы нигде.
    """
    import contextlib

    from tg_export.cli import common as cli_common
    from tg_export.cli import export as cli_export
    from tg_export.errors import EXIT_FAILURE, EXIT_OK
    from tg_export.verify import RedownloadOutcome, RedownloadResult

    broken = tmp_path / "photo.jpg"
    broken.write_bytes(b"partial")

    state = MagicMock()
    state.get_files_to_verify = AsyncMock(return_value=[_entry(broken)])

    @contextlib.asynccontextmanager
    async def fake_state(*_a, **_k):
        yield state, tmp_path, "acc"

    @contextlib.asynccontextmanager
    async def fake_api(*_a, **_k):
        yield MagicMock(), "acc"

    monkeypatch.setattr(cli_common, "opened_state_if_any", fake_state)
    monkeypatch.setattr(cli_common, "connected_api", fake_api)
    monkeypatch.setattr(cli_common, "resolve_output", lambda *_a, **_k: (None, MagicMock(), tmp_path))

    outcome = RedownloadOutcome(_entry(broken), result=RedownloadResult.nothing_downloaded)
    monkeypatch.setattr("tg_export.cli.export.redownload_broken_files", AsyncMock(return_value=[outcome]))

    assert await cli_export._verify_files("acc", None, None) == EXIT_FAILURE

    repaired = RedownloadOutcome(_entry(broken), result=RedownloadResult.replaced, path=broken)
    monkeypatch.setattr("tg_export.cli.export.redownload_broken_files", AsyncMock(return_value=[repaired]))

    assert await cli_export._verify_files("acc", None, None) == EXIT_OK


@pytest.mark.asyncio
async def test_a_message_answered_under_another_id_is_not_attached_to_the_record(tmp_path):
    """Ответ сопоставлялся с запросом по порядку, а не по идентификатору сообщения.

    Сдвиг в ответе приписал бы файл одного сообщения записи другого -- молча:
    оба из одного чата, и по типу медиа отличить их нечем.
    """
    from tg_export.verify import fetch_broken_messages

    entries = [_broken(i, 100, 200 + i, tmp_path / f"f{i}.jpg") for i in range(2)]

    async def get_messages(chat_id, ids):
        # Telegram ответил одним сообщением из двух запрошенных.
        return [MagicMock(media=object(), id=201)]

    api = MagicMock()
    api.client.get_messages = get_messages

    found_all = {}
    async for _, found in fetch_broken_messages(api, list(enumerate(entries))):
        found_all.update(found)

    assert set(found_all) == {(100, 201)}, found_all


@pytest.mark.asyncio
async def test_a_failed_batch_stops_the_downloads_it_already_started(tmp_path):
    """Задачи переживали отказ запроса и продолжали писать в закрываемое состояние.

    Вызывающий закрывает базу состояния в своём `finally`, поэтому загрузки,
    оставшиеся работать после исключения, писали бы в закрытое соединение.
    """
    from tg_export.verify import redownload_broken_files

    entries = [_broken(0, 100, 200, tmp_path / "a.jpg"), _broken(1, 101, 201, tmp_path / "b.jpg")]
    for entry in entries:
        Path(entry["local_path"]).write_bytes(b"partial")

    calls = 0

    async def get_messages(chat_id, ids):
        nonlocal calls
        calls += 1
        if calls > 1:
            # Пауза, чтобы загрузки первой пачки успели начаться: отменять
            # нечего, пока задача не дошла до первого await.
            await asyncio.sleep(0.01)
            raise RuntimeError("сеть отказала на второй пачке")
        return [MagicMock(media=object(), id=msg_id) for msg_id in ids]

    cancelled = 0

    async def download_media(tl_msg, staging):
        nonlocal cancelled
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled += 1
            raise

    api = MagicMock()
    api.client.get_messages = get_messages
    api.download_media = download_media

    with pytest.raises(RuntimeError):
        await redownload_broken_files(api, AsyncMock(), entries, concurrency=2)

    assert cancelled == 1, "загрузка первой пачки осталась работать после отказа второй"


@pytest.mark.asyncio
async def test_not_every_broken_file_gets_a_task_at_once(tmp_path):
    """Задача заводилась на каждый битый файл сразу и держала своё сообщение.

    Семафор ограничивает то, что выполняется, а не то, что создано: чат с
    десятками тысяч битых файлов держал столько же задач одновременно.
    """
    from tg_export.verify import redownload_broken_files

    entries = [_broken(i, 100, 200 + i, tmp_path / f"f{i}.jpg") for i in range(40)]
    for entry in entries:
        Path(entry["local_path"]).write_bytes(b"partial")

    peak_tasks = 0

    async def download_media(tl_msg, staging):
        nonlocal peak_tasks
        peak_tasks = max(peak_tasks, len(asyncio.all_tasks()))
        await asyncio.sleep(0)
        return None

    api = MagicMock()
    api.client.get_messages = AsyncMock(
        return_value=[MagicMock(media=object(), id=e["msg_id"]) for e in entries]
    )
    api.download_media = download_media

    await redownload_broken_files(api, AsyncMock(), entries, concurrency=1)

    assert peak_tasks < len(entries), f"одновременно живых задач: {peak_tasks}"


@pytest.mark.asyncio
async def test_files_the_stopped_pass_never_touched_are_not_errors(tmp_path, monkeypatch):
    """Ctrl+C во время `run --verify` давал по ложной ошибке на каждый нетронутый файл.

    Исход «прохода не было» попадал в ветку неизвестного исхода, и сводка
    сообщала «N files still have issues» про файлы, к которым не обращались.
    """
    import tg_export.verify as verify_module
    from tg_export.exporter import Exporter, ExportStats

    broken = tmp_path / "photo.jpg"
    broken.write_bytes(b"partial")

    async def stopped_pass(*_args, **_kwargs):
        return [verify_module.RedownloadOutcome(_entry(broken))]

    monkeypatch.setattr("tg_export.exporter.redownload_broken_files", stopped_pass)

    state = MagicMock()
    state.get_files_to_verify = AsyncMock(return_value=[_entry(broken)])

    config = MagicMock()
    config.defaults.media.concurrent_downloads = 2

    exporter = Exporter(  # pyright: ignore[reportArgumentType]
        api=MagicMock(),
        state=state,
        config=config,
        renderer=MagicMock(),
        downloader=MagicMock(),
        account="acc",
        quiet=True,
    )
    stats = ExportStats()

    await exporter._verify_files(stats)

    assert not stats.errors, stats.errors
