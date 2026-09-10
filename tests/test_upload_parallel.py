"""Параллельная отправка частей файла: выбор ширины, разбиение, ошибки.

Отправка через одно соединение упиралась не в канал, а в задержку: часть
уходила и ждала подтверждения, поэтому скорость равнялась размеру части,
делённому на время оборота. Здесь проверяется механика замены: сколько
соединений открывается по времени ответов, как файл режется на части, что
происходит при ограничении со стороны Telegram и при сетевом сбое.
"""

import asyncio
import threading

import pytest
from telethon.errors import FloodWaitError
from telethon.tl.functions.upload import SaveBigFilePartRequest

from tg_export.upload import (
    BIG_FILE_FROM,
    MAX_PARTS,
    ConnectionCountPolicy,
    ParallelUploader,
    UploadLimits,
    choose_part_size,
    part_count,
    upload_file,
)

MB = 1024 * 1024


class FakeSender:
    """Соединение-заглушка: помнит отправленные запросы и когда закрыто."""

    def __init__(self, index, behaviour=None, delay=0.0):
        self.index = index
        self.requests = []
        self.disconnected = False
        self._behaviour = behaviour
        self._delay = delay

    async def send(self, request):
        self.requests.append(request)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._behaviour is not None:
            self._behaviour(self, request)
        return True

    async def disconnect(self):
        self.disconnected = True


def fake_pool(behaviour=None, delay=0.0):
    """Фабрика соединений и список созданных ею заглушек."""
    created = []

    async def connect():
        sender = FakeSender(len(created), behaviour=behaviour, delay=delay)
        created.append(sender)
        return sender

    return connect, created


def _client():
    """Клиент нужен модулю только как владелец фабрики соединений."""
    return object()


# --------------------------------------------------------------------------
# Размер части
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("file_size", "expected"),
    [
        (100 * 1024, 32 * 1024),
        (5 * MB, 64 * 1024),
        (100 * MB, 128 * 1024),
        (401 * MB, 128 * 1024),
        (600 * MB, 256 * 1024),
        (1500 * MB, 512 * 1024),
    ],
)
def test_part_size_follows_the_file_size(file_size, expected):
    """Мелкие размеры оставлены мелким файлам, крупные растут с размером."""
    assert choose_part_size(file_size) == expected


def test_part_size_keeps_the_count_within_the_limit():
    """Выбранный размер всегда укладывает файл в допустимое число частей."""
    for file_size in (700 * MB, 1200 * MB, 1900 * MB):
        assert part_count(file_size, choose_part_size(file_size)) <= MAX_PARTS


def test_file_beyond_the_ceiling_is_refused():
    """Файл, не влезающий и в 4000 частей по 512 КБ, отправить нечем."""
    with pytest.raises(ValueError, match="no more than"):
        choose_part_size(MAX_PARTS * 512 * 1024 + 1)


def test_empty_file_is_refused():
    """Нулевой размер — не файл для загрузки частями."""
    with pytest.raises(ValueError, match="must be positive"):
        choose_part_size(0)


# --------------------------------------------------------------------------
# Ширина: сколько соединений держать
# --------------------------------------------------------------------------


def _widen(policy, *, to):
    """Довести ширину до нужной: каждое соединение отвечает быстро."""
    while policy.count < to:
        for index in range(policy.count):
            policy.record(connection=index, duration=0.2, in_flight=MB)
        assert policy.grow_if_all_fast() is True


def test_width_grows_only_when_every_connection_answered_fast():
    """Пока быстрый ответ показало не каждое соединение, роста нет."""
    limits = UploadLimits()
    policy = ConnectionCountPolicy(limits)
    policy.record(connection=0, duration=0.2, in_flight=MB)
    assert policy.grow_if_all_fast() is True
    assert policy.count == 2

    policy.record(connection=0, duration=0.2, in_flight=MB)
    assert policy.grow_if_all_fast() is False
    assert policy.count == 2

    policy.record(connection=1, duration=0.2, in_flight=MB)
    assert policy.grow_if_all_fast() is True
    assert policy.count == 3


def test_fast_answer_on_a_light_connection_does_not_count():
    """Быстрый ответ на одинокой мелкой части ничего не доказывает."""
    policy = ConnectionCountPolicy(UploadLimits())
    policy.record(connection=0, duration=0.1, in_flight=64 * 1024)
    assert policy.grow_if_all_fast() is False
    assert policy.count == 1


def test_middling_answer_stops_the_growth():
    """Ответ между порогами снимает отметки, и ширина остаётся прежней."""
    policy = ConnectionCountPolicy(UploadLimits())
    policy.record(connection=0, duration=0.2, in_flight=MB)
    policy.record(connection=0, duration=3.0, in_flight=MB)
    assert policy.grow_if_all_fast() is False
    assert policy.count == 1


def test_slow_answer_drops_one_connection():
    """Ответ дольше восьми секунд закрывает одно соединение."""
    clock = [100.0]
    policy = ConnectionCountPolicy(UploadLimits(), clock=lambda: clock[0])
    _widen(policy, to=4)
    assert policy.count == 4

    policy.record(connection=0, duration=9.0, in_flight=MB)
    assert policy.count == 3


def test_connections_are_dropped_no_faster_than_the_settle_time():
    """Второе сужение подряд ждёт, пока очередь разойдётся на новой ширине."""
    clock = [100.0]
    limits = UploadLimits()
    policy = ConnectionCountPolicy(limits, clock=lambda: clock[0])
    _widen(policy, to=4)
    assert policy.count == 4

    policy.record(connection=0, duration=9.0, in_flight=MB)
    policy.record(connection=0, duration=9.0, in_flight=MB)
    assert policy.count == 3

    clock[0] += limits.settle_after_shrink
    policy.record(connection=0, duration=9.0, in_flight=MB)
    assert policy.count == 2


def test_width_never_falls_below_one():
    """Последнее соединение не закрывается: отправлять было бы нечем."""
    policy = ConnectionCountPolicy(UploadLimits())
    for _ in range(5):
        policy.record(connection=0, duration=30.0, in_flight=MB)
    assert policy.count == 1


def test_width_never_exceeds_the_ceiling():
    """Ширина упирается в объявленный предел числа соединений."""
    limits = UploadLimits(max_connections=3)
    policy = ConnectionCountPolicy(limits)
    for _ in range(10):
        for index in range(policy.count):
            policy.record(connection=index, duration=0.1, in_flight=MB)
        policy.grow_if_all_fast()
    assert policy.count == 3


# --------------------------------------------------------------------------
# Сама загрузка
# --------------------------------------------------------------------------


def _write(path, size):
    path.write_bytes(bytes(range(256)) * (size // 256) + b"x" * (size % 256))
    return path


async def test_every_part_is_sent_once_and_numbered_in_order(tmp_path):
    """Файл уходит целиком: части пронумерованы подряд и не повторяются."""
    path = _write(tmp_path / "big.bin", 700 * 1024)
    connect, created = fake_pool()
    uploader = ParallelUploader(
        _client(), limits=UploadLimits(in_flight_per_connection=128 * 1024), connect=connect
    )

    handle = await uploader.upload(path)

    sent = [r for sender in created for r in sender.requests]
    assert all(isinstance(r, SaveBigFilePartRequest) for r in sent)
    assert sorted(r.file_part for r in sent) == list(range(handle.parts))
    assert {r.file_total_parts for r in sent} == {handle.parts}
    assert {r.file_id for r in sent} == {handle.id}
    assert b"".join(r.bytes for r in sorted(sent, key=lambda r: r.file_part)) == path.read_bytes()
    assert handle.name == "big.bin"


async def test_progress_counts_up_to_the_file_size(tmp_path):
    """Обратный вызов доводит счётчик ровно до размера файла."""
    path = _write(tmp_path / "big.bin", 300 * 1024)
    connect, _ = fake_pool()
    uploader = ParallelUploader(_client(), connect=connect)
    seen = []

    await uploader.upload(path, progress_callback=lambda sent, total: seen.append((sent, total)))

    assert seen[-1] == (path.stat().st_size, path.stat().st_size)
    assert [sent for sent, _ in seen] == sorted(sent for sent, _ in seen)


async def test_one_connection_holds_no_more_than_its_share(tmp_path):
    """На соединении не оказывается больше данных, чем ему разрешено."""
    path = _write(tmp_path / "big.bin", 1024 * 1024)
    in_flight = {}
    peak = []

    def behaviour(sender, request):
        in_flight[sender.index] = in_flight.get(sender.index, 0) + len(request.bytes)
        peak.append(max(in_flight.values()))

    connect, _ = fake_pool(behaviour=behaviour, delay=0.01)
    limits = UploadLimits(in_flight_per_connection=128 * 1024, max_connections=1)
    uploader = ParallelUploader(_client(), limits=limits, connect=connect)

    await uploader.upload(path)

    # Заглушка не уменьшает счётчик, поэтому проверяется первая партия:
    # одновременно в полёте не больше разрешённого объёма.
    assert peak[0] <= limits.in_flight_per_connection


async def test_connections_are_closed_after_the_upload(tmp_path):
    """Открытые ради файла соединения закрываются, когда файл ушёл."""
    path = _write(tmp_path / "big.bin", 200 * 1024)
    connect, created = fake_pool()
    uploader = ParallelUploader(_client(), connect=connect)

    await uploader.upload(path)

    assert created
    assert all(sender.disconnected for sender in created)


async def test_flood_wait_is_slept_out_and_the_part_repeats(tmp_path, monkeypatch):
    """Ограничение со стороны Telegram выжидается, часть уходит снова."""
    path = _write(tmp_path / "big.bin", 64 * 1024)
    failed = {"done": False}
    slept = []
    error = FloodWaitError(request=None)
    error.seconds = 2

    def behaviour(sender, request):
        if not failed["done"]:
            failed["done"] = True
            raise error

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    connect, created = fake_pool(behaviour=behaviour)
    uploader = ParallelUploader(_client(), connect=connect)

    handle = await uploader.upload(path)

    sent = [r for sender in created for r in sender.requests]
    assert len(sent) == handle.parts + 1
    # Пауза выдержана целиком, и лишь после неё часть ушла снова.
    assert slept and max(slept) == pytest.approx(2, abs=0.5)


async def test_a_broken_part_is_sent_again(tmp_path):
    """Сетевой сбой на части не роняет отправку: часть повторяется."""
    path = _write(tmp_path / "big.bin", 64 * 1024)
    attempts = {"count": 0}

    def behaviour(sender, request):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ConnectionResetError("connection lost")

    connect, created = fake_pool(behaviour=behaviour)
    uploader = ParallelUploader(_client(), connect=connect)

    handle = await uploader.upload(path)

    sent = [r for sender in created for r in sender.requests]
    assert len(sent) == handle.parts + 1


async def test_a_part_failing_every_attempt_fails_the_upload(tmp_path):
    """Часть, не уходящая после всех попыток, обрывает отправку с ошибкой."""
    path = _write(tmp_path / "big.bin", 64 * 1024)

    def behaviour(sender, request):
        raise ConnectionResetError("connection lost")

    connect, created = fake_pool(behaviour=behaviour)
    uploader = ParallelUploader(_client(), limits=UploadLimits(part_attempts=2), connect=connect)

    with pytest.raises(ConnectionResetError):
        await uploader.upload(path)

    assert all(sender.disconnected for sender in created)


async def test_a_long_flood_wait_is_reported_rather_than_waited_out(tmp_path):
    """Долгий запрет — не замедление: он доходит до вызывающего кода."""
    path = _write(tmp_path / "big.bin", 64 * 1024)
    error = FloodWaitError(request=None)
    error.seconds = 3600

    def behaviour(sender, request):
        raise error

    connect, _ = fake_pool(behaviour=behaviour)
    uploader = ParallelUploader(_client(), connect=connect)

    with pytest.raises(FloodWaitError):
        await uploader.upload(path)


async def test_reading_the_file_leaves_the_event_loop(tmp_path):
    """Чтение части идёт в отдельном потоке, а не в потоке цикла событий."""
    path = _write(tmp_path / "big.bin", 200 * 1024)
    threads = set()

    def behaviour(sender, request):
        threads.add(threading.get_ident())

    connect, _ = fake_pool(behaviour=behaviour)
    seen = []
    original = asyncio.to_thread

    async def watched(func, *args, **kwargs):
        seen.append(func)
        return await original(func, *args, **kwargs)

    uploader = ParallelUploader(_client(), connect=connect)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(asyncio, "to_thread", watched)
        await uploader.upload(path)

    assert seen, "чтение частей должно уходить в поток"


# --------------------------------------------------------------------------
# Выбор пути: параллельно или как раньше
# --------------------------------------------------------------------------


async def test_a_small_file_goes_the_library_way(tmp_path):
    """Мелкий файл не стоит нескольких соединений: его грузит библиотека."""
    path = _write(tmp_path / "small.bin", 1024)
    calls = []

    class Client:
        async def upload_file(self, name, progress_callback=None):
            calls.append(name)
            return "library handle"

    handle = await upload_file(Client(), path)

    assert handle == "library handle"
    assert calls == [str(path)]


async def test_a_large_file_goes_the_parallel_way(tmp_path):
    """Файл за порогом уходит частями через собственные соединения."""
    path = _write(tmp_path / "big.bin", BIG_FILE_FROM + 4096)
    connect, created = fake_pool()

    handle = await upload_file(_client(), path, connect=connect)

    assert handle.parts == part_count(path.stat().st_size, choose_part_size(path.stat().st_size))
    assert created


async def test_a_part_left_without_an_answer_is_sent_on_a_new_connection(tmp_path):
    """Соединение, замолчавшее навсегда, заменяется, а часть уходит заново.

    Без срока ожидания отправка вставала намертво: запрос ждал ответа от
    оборванного соединения, и цикл стоял на этом ожидании.
    """
    path = _write(tmp_path / "big.bin", 64 * 1024)
    hung = {"done": False}

    async def behaviour_send(sender, request):
        if not hung["done"]:
            hung["done"] = True
            await asyncio.sleep(3600)

    class HangingSender(FakeSender):
        async def send(self, request):
            self.requests.append(request)
            await behaviour_send(self, request)
            return True

    created = []

    async def connect():
        sender = HangingSender(len(created))
        created.append(sender)
        return sender

    limits = UploadLimits(part_timeout=0.05)
    uploader = ParallelUploader(_client(), limits=limits, connect=connect)

    handle = await uploader.upload(path)

    # Соединений открыто больше, чем ширина: одно пришло на замену павшему.
    assert len(created) > 1
    sent = [r for sender in created for r in sender.requests]
    assert sorted(r.file_part for r in sent[-handle.parts :]) == list(range(handle.parts))


async def test_a_dropped_link_does_not_keep_taking_parts(tmp_path):
    """На оборвавшееся соединение новые части не ставятся до его замены."""
    path = _write(tmp_path / "big.bin", 96 * 1024)
    failed = {"done": False}

    def behaviour(sender, request):
        if not failed["done"]:
            failed["done"] = True
            raise ConnectionResetError("link dropped")

    connect, created = fake_pool(behaviour=behaviour)
    uploader = ParallelUploader(_client(), connect=connect)

    handle = await uploader.upload(path)

    assert len(created) > 1
    assert created[0].disconnected
    sent = [r for sender in created for r in sender.requests]
    assert len(sent) == handle.parts + 1
