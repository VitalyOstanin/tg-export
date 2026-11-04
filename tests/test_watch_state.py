"""Проверки отметок слушателя и добора пропущенных сообщений."""

import asyncio
import io
import json
import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tg_export.watch import LiveStream, RecordSink, WatchState, missed_messages


def _state(tmp_path, **kwargs) -> WatchState:
    return WatchState(tmp_path / "watch.json", "testaccount", **kwargs)


class TestWatchState:
    def test_missing_file_starts_empty(self, tmp_path):
        """Отсутствующий файл отметок -- это пустые отметки, а не отказ."""
        state = WatchState.load(tmp_path / "watch.json", "testaccount")

        assert state.last_seen(111) is None

    def test_saved_marks_come_back(self, tmp_path):
        """Записанные отметки читаются следующим запуском."""
        state = _state(tmp_path)
        state.note(111, 980)
        state.note(-1001234567890, 4711)
        asyncio.run(state.save(force=True))

        again = WatchState.load(tmp_path / "watch.json", "testaccount")

        assert again.last_seen(111) == 980
        assert again.last_seen(-1001234567890) == 4711

    def test_file_is_private(self, tmp_path):
        """Файл отметок закрыт от других пользователей: он называет чаты."""
        state = _state(tmp_path)
        state.note(111, 980)
        asyncio.run(state.save(force=True))

        mode = stat.S_IMODE((tmp_path / "watch.json").stat().st_mode)

        assert mode & 0o077 == 0

    def test_stored_shape(self, tmp_path):
        """В файле лежат версия схемы, аккаунт и отметки по чатам."""
        state = _state(tmp_path)
        state.note(111, 980)
        asyncio.run(state.save(force=True))

        stored = json.loads((tmp_path / "watch.json").read_text(encoding="utf-8"))

        assert stored["version"] == 1
        assert stored["account"] == "testaccount"
        assert stored["chats"] == {"111": 980}

    def test_older_message_does_not_move_the_mark_back(self, tmp_path):
        """Отметка идёт только вперёд: правка старого сообщения её не сдвигает."""
        state = _state(tmp_path)
        state.note(111, 980)
        state.note(111, 974)

        assert state.last_seen(111) == 980

    def test_save_is_not_repeated_within_the_interval(self, tmp_path):
        """Между записями выдерживается интервал: шквал сообщений не идёт на диск."""
        state = _state(tmp_path)
        state.note(111, 980)

        assert asyncio.run(state.save(force=True)) is True

        state.note(111, 981)

        assert asyncio.run(state.save()) is False

    def test_damaged_file_starts_empty(self, tmp_path):
        """Испорченный файл отметок не мешает запуску -- слушание начинается заново."""
        path = tmp_path / "watch.json"
        path.write_text("{ не json", encoding="utf-8")

        state = WatchState.load(path, "testaccount")

        assert state.last_seen(111) is None


def _dialog(chat_id: int, top_message: int | None):
    """Диалог как его отдаёт Telethon: id, последнее сообщение и сущность чата.

    Не MagicMock: тот подставляет любой недостающий атрибут, и проверка
    «последнего сообщения нет» проходила бы через подставленную заглушку.
    """
    return SimpleNamespace(
        id=chat_id,
        title=f"Чат {chat_id}",
        message=SimpleNamespace(id=top_message) if top_message is not None else None,
        dialog=SimpleNamespace(top_message=top_message or 0),
        entity=SimpleNamespace(chat_id=chat_id),
    )


def _telethon_history(history, *, min_id=0, limit=None, reverse=False, **kwargs):
    """Выборка истории так, как её делает `iter_messages` Telethon 1.44.

    Сообщения новее `min_id`; без `reverse` -- от новых к старым, с ним -- от
    старых к новым, и `limit` отсекает хвост в этом порядке.
    """
    picked = [m for m in history if m.id > min_id]
    if not reverse:
        picked.reverse()
    return picked if limit is None else picked[:limit]


def _api(dialogs, messages_by_chat=None):
    """Заглушка API: заданные диалоги и история по чатам.

    Список диалогов отдаётся и целиком (`iter_dialogs`), и по перечню чатов
    (`peer_dialogs`); обращения к обоим записываются.
    """
    api = MagicMock()
    messages_by_chat = messages_by_chat or {}
    calls = []
    api.dialog_passes = 0
    api.peer_requests = []

    async def _iter_dialogs(archived=None):
        api.dialog_passes += 1
        for dialog in dialogs:
            yield dialog

    async def _peer_dialogs(chat_ids):
        api.peer_requests.append(list(chat_ids))
        for dialog in dialogs:
            if dialog.id in chat_ids:
                yield dialog

    def _iter_messages(entity, **kwargs):
        calls.append((entity, kwargs))

        async def _gen():
            for message in _telethon_history(messages_by_chat.get(id(entity), []), **kwargs):
                yield message

        return _gen()

    api.iter_dialogs = _iter_dialogs
    api.peer_dialogs = _peer_dialogs
    api.client = MagicMock()
    api.client.iter_messages = _iter_messages
    api.calls = calls
    return api


def _collect(api, state, **kwargs):
    async def _run():
        return [item async for item in missed_messages(api, state, **kwargs)]

    return asyncio.run(_run())


class TestMissedMessages:
    def test_chat_without_a_mark_is_not_replayed(self, tmp_path):
        """Чат, который ещё не слушали, историей не выгружается."""
        api = _api([_dialog(111, 980)])

        collected = _collect(api, _state(tmp_path), wanted=None, excluded=set(), limit=100)

        assert collected == []
        assert api.calls == []

    def test_unchanged_chat_is_not_requested(self, tmp_path):
        """Чат, у которого последнее сообщение совпадает с отметкой, не запрашивается."""
        state = _state(tmp_path)
        state.note(111, 980)
        api = _api([_dialog(111, 980)])

        collected = _collect(api, state, wanted=None, excluded=set(), limit=100)

        assert collected == []
        assert api.calls == []

    def test_changed_chat_is_asked_from_the_mark(self, tmp_path):
        """У разошедшегося чата история запрашивается начиная с отметки."""
        state = _state(tmp_path)
        state.note(111, 974)
        dialog = _dialog(111, 980)
        message = _history_message(975)
        api = _api([dialog], {id(dialog.entity): [message]})

        collected = _collect(api, state, wanted=None, excluded=set(), limit=50)

        assert collected == [(111, "Чат 111", message)]
        entity, kwargs = api.calls[0]
        assert entity is dialog.entity
        assert kwargs["min_id"] == 974

    def test_excluded_chat_is_skipped(self, tmp_path):
        """Исключённый чат не добирается, даже если в нём есть новое."""
        state = _state(tmp_path)
        state.note(111, 974)
        api = _api([_dialog(111, 980)])

        collected = _collect(api, state, wanted=None, excluded={111}, limit=100)

        assert collected == []
        assert api.calls == []

    def test_only_wanted_chats_are_asked(self, tmp_path):
        """При явном перечне чатов добор идёт только по ним."""
        state = _state(tmp_path)
        state.note(111, 974)
        state.note(222, 10)
        api = _api([_dialog(111, 980), _dialog(222, 20)])

        _collect(api, state, wanted={222}, excluded=set(), limit=100)

        assert len(api.calls) == 1

    def test_wanted_chats_are_asked_without_the_dialog_list(self, tmp_path):
        """При явном перечне чатов весь список диалогов не обходится.

        Диалоги нужных чатов берутся по перечню, причём только тех, у которых
        есть отметка: чат, который ещё не слушали, догонке не нужен.
        """
        state = _state(tmp_path)
        state.note(111, 10)
        dialog = _dialog(111, 11)
        message = _history_message(11)
        api = _api([dialog, _dialog(222, 20), _dialog(333, 30)], {id(dialog.entity): [message]})

        collected = _collect(api, state, wanted={111, 222}, excluded=set(), limit=100)

        assert collected == [(111, "Чат 111", message)]
        assert api.dialog_passes == 0
        assert api.peer_requests == [[111]]

    def test_wanted_chats_without_marks_ask_nothing(self, tmp_path):
        """Перечень из чатов без отметок не порождает ни одного запроса."""
        api = _api([_dialog(222, 20)])

        collected = _collect(api, _state(tmp_path), wanted={222}, excluded=set(), limit=100)

        assert collected == []
        assert api.dialog_passes == 0
        assert api.peer_requests == []


class TestCatchUpLimit:
    def _collect_ids(self, tmp_path, history, limit):
        state = _state(tmp_path)
        state.note(111, 10)
        dialog = _dialog(111, history[-1].id)
        api = _api([dialog], {id(dialog.entity): history})
        return [m.id for _, _, m in _collect(api, state, wanted=None, excluded=set(), limit=limit)]

    def test_limit_keeps_the_latest_messages(self, tmp_path):
        """При пропуске больше лимита выдаются последние N, по возрастанию id."""
        history = [_history_message(i) for i in range(11, 21)]

        assert self._collect_ids(tmp_path, history, 3) == [18, 19, 20]

    def test_skipped_messages_are_reported(self, tmp_path, caplog):
        """Отброшенное лимитом называется в журнале: чат и границы пропуска."""
        history = [_history_message(i) for i in range(11, 21)]

        with caplog.at_level("WARNING", logger="tg_export.watch"):
            self._collect_ids(tmp_path, history, 3)

        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "111" in warnings[0]
        assert "after 10" in warnings[0]
        assert "before 18" in warnings[0]

    def test_within_the_limit_nothing_is_reported(self, tmp_path, caplog):
        """Пропуск не больше лимита выдаётся целиком и без предупреждения."""
        history = [_history_message(i) for i in range(11, 14)]

        with caplog.at_level("WARNING", logger="tg_export.watch"):
            ids = self._collect_ids(tmp_path, history, 3)

        assert ids == [11, 12, 13]
        assert not [r for r in caplog.records if r.levelname == "WARNING"]


@pytest.mark.parametrize("top", [None, 0])
def test_dialog_without_a_last_message_is_skipped(tmp_path, top):
    """Диалог без последнего сообщения пропускается, а не роняет добор."""
    state = _state(tmp_path)
    state.note(111, 974)
    api = _api([_dialog(111, top)])

    collected = _collect(api, state, wanted=None, excluded=set(), limit=100)

    assert collected == []


def _live_event(chat_id: int, message_id: int, *, out: bool = False):
    """Живое событие нового сообщения, как его отдаёт обработчику Telethon."""

    async def _get_chat():
        return SimpleNamespace(title=f"Чат {chat_id}")

    return SimpleNamespace(
        chat_id=chat_id,
        message=_history_message(message_id, out=out),
        get_chat=_get_chat,
    )


def _history_message(message_id: int, *, out: bool = False):
    return SimpleNamespace(
        id=message_id,
        date=None,
        message=f"сообщение {message_id}",
        entities=None,
        media=None,
        action=None,
        out=out,
        file=None,
        sender=None,
        sender_id=None,
        reply_to=None,
    )


def _stream(tmp_path, api, state, *, outgoing=True):
    """Поток слушателя, пишущий записи JSON в строковый буфер."""
    out = io.StringIO()
    sink = RecordSink(out, as_json=True, log_path=None)
    stream = LiveStream(api, state, sink, wanted=None, excluded=set(), catchup_limit=100, outgoing=outgoing)
    return stream, out


def _emitted_ids(out) -> list[int]:
    return [json.loads(line)["id"] for line in out.getvalue().splitlines()]


def _api_with_live_arrival(dialog, history, on_dialogs, *, fail_after=None):
    """Заглушка API, в которой живое сообщение приходит посреди прохода по диалогам.

    `on_dialogs` вызывается до выдачи диалога -- так обработчик события
    срабатывает, пока догонка ещё не дошла до чата. `fail_after` обрывает
    историю ошибкой после указанного числа сообщений.
    """
    api = MagicMock()

    async def _iter_dialogs(archived=None):
        await on_dialogs()
        yield dialog

    def _iter_messages(entity, **kwargs):
        async def _gen():
            for count, message in enumerate(_telethon_history(history, **kwargs)):
                if fail_after is not None and count >= fail_after:
                    raise ConnectionError("связь оборвалась посреди догонки")
                yield message

        return _gen()

    api.iter_dialogs = _iter_dialogs
    api.client = MagicMock()
    api.client.iter_messages = _iter_messages
    return api


class TestCatchUpAgainstLiveMessages:
    @pytest.mark.asyncio
    async def test_live_message_during_catch_up_does_not_hide_the_gap(self, tmp_path):
        """Живое сообщение, пришедшее во время догонки, не сдвигает отметку за пропуск.

        Иначе догонка видит последнее сообщение чата не новее отметки, и
        сообщения между старой отметкой и живым теряются молча.
        """
        state = _state(tmp_path)
        state.note(111, 10)
        history = [_history_message(i) for i in range(11, 16)]
        holder = {}

        async def live_arrives():
            await holder["stream"].handle(_live_event(111, 15), kind="message")

        api = _api_with_live_arrival(_dialog(111, 15), history, live_arrives)
        stream, out = _stream(tmp_path, api, state)
        holder["stream"] = stream

        await stream.catch_up()

        assert sorted(_emitted_ids(out)) == [11, 12, 13, 14, 15]
        assert state.last_seen(111) == 15

    @pytest.mark.asyncio
    async def test_live_message_seen_during_catch_up_is_not_repeated(self, tmp_path):
        """Сообщение, уже показанное живым потоком, догонка повторно не выдаёт."""
        state = _state(tmp_path)
        state.note(111, 10)
        history = [_history_message(i) for i in range(11, 14)]
        holder = {}

        async def live_arrives():
            await holder["stream"].handle(_live_event(111, 12), kind="message")

        api = _api_with_live_arrival(_dialog(111, 13), history, live_arrives)
        stream, out = _stream(tmp_path, api, state)
        holder["stream"] = stream

        await stream.catch_up()

        assert _emitted_ids(out) == [12, 11, 13]

    @pytest.mark.asyncio
    async def test_failed_catch_up_keeps_the_mark_before_the_gap(self, tmp_path):
        """Оборванная догонка не отдаёт отметку живому сообщению: пропуск доберёт следующая."""
        state = _state(tmp_path)
        state.note(111, 10)
        history = [_history_message(i) for i in range(11, 16)]
        holder = {}

        async def live_arrives():
            await holder["stream"].handle(_live_event(111, 15), kind="message")

        api = _api_with_live_arrival(_dialog(111, 15), history, live_arrives, fail_after=1)
        stream, out = _stream(tmp_path, api, state)
        holder["stream"] = stream

        with pytest.raises(ConnectionError):
            await stream.catch_up()

        # History is read in full before any of it is shown -- the latest N
        # are only known once the read is over -- so a read cut short shows
        # nothing and the mark stays where the gap began.
        assert state.last_seen(111) == 10

        # The gap stays unfilled until a catch-up succeeds: a live message in
        # between does not move the mark over it either.
        await stream.handle(_live_event(111, 16), kind="message")
        assert state.last_seen(111) == 10

        async def nothing_arrives():
            return None

        history.append(_history_message(16))
        api_retry = _api_with_live_arrival(_dialog(111, 16), history, nothing_arrives)
        stream._api = api_retry
        await stream.catch_up()

        assert sorted(_emitted_ids(out)) == [11, 12, 13, 14, 15, 16]
        assert state.last_seen(111) == 16

    @pytest.mark.asyncio
    async def test_live_message_outside_a_catch_up_moves_the_mark(self, tmp_path):
        """Вне догонки живое сообщение сдвигает отметку сразу, как и раньше."""
        state = _state(tmp_path)
        state.note(111, 10)
        stream, out = _stream(tmp_path, MagicMock(), state)

        await stream.handle(_live_event(111, 11), kind="message")

        assert _emitted_ids(out) == [11]
        assert state.last_seen(111) == 11


class TestNoOutgoing:
    """`--no-outgoing`: свои сообщения не выдаются ни догонкой, ни живым потоком."""

    @pytest.mark.asyncio
    async def test_catch_up_leaves_out_own_messages_but_moves_the_mark(self, tmp_path):
        """Своё сообщение из истории не выдаётся, но отметка проходит через него.

        Иначе чат, где последнее сообщение своё, при каждом запуске выглядел
        бы разошедшимся с отметкой, и тот же интервал запрашивался бы снова.
        """
        state = _state(tmp_path)
        state.note(111, 10)
        history = [_history_message(11), _history_message(12, out=True)]

        async def nothing_arrives():
            return None

        api = _api_with_live_arrival(_dialog(111, 12), history, nothing_arrives)
        stream, out = _stream(tmp_path, api, state, outgoing=False)

        await stream.catch_up()

        assert _emitted_ids(out) == [11]
        assert state.last_seen(111) == 12

    @pytest.mark.asyncio
    async def test_live_own_message_moves_the_mark_silently(self, tmp_path):
        """Своё живое сообщение вне догонки не выдаётся, но сдвигает отметку."""
        state = _state(tmp_path)
        state.note(111, 10)
        stream, out = _stream(tmp_path, MagicMock(), state, outgoing=False)

        await stream.handle(_live_event(111, 11, out=True), kind="message")

        assert out.getvalue() == ""
        assert state.last_seen(111) == 11

    @pytest.mark.asyncio
    async def test_live_own_message_during_catch_up_does_not_hide_the_gap(self, tmp_path):
        """Своё живое сообщение во время догонки отметку за пропуск не уводит.

        Отметка встаёт на него только после того, как догонка выдала всё
        пропущенное до него.
        """
        state = _state(tmp_path)
        state.note(111, 10)
        history = [_history_message(i) for i in range(11, 15)] + [_history_message(15, out=True)]
        holder = {}

        async def live_arrives():
            await holder["stream"].handle(_live_event(111, 15, out=True), kind="message")
            assert state.last_seen(111) == 10

        api = _api_with_live_arrival(_dialog(111, 15), history, live_arrives)
        stream, out = _stream(tmp_path, api, state, outgoing=False)
        holder["stream"] = stream

        await stream.catch_up()

        assert _emitted_ids(out) == [11, 12, 13, 14]
        assert state.last_seen(111) == 15

    @pytest.mark.asyncio
    async def test_own_messages_are_shown_by_default(self, tmp_path):
        """Без `--no-outgoing` свои сообщения выдаются, как прежде."""
        state = _state(tmp_path)
        state.note(111, 10)
        stream, out = _stream(tmp_path, MagicMock(), state)

        await stream.handle(_live_event(111, 11, out=True), kind="message")

        assert _emitted_ids(out) == [11]


class TestDeletionTime:
    async def test_deletion_is_dated_in_utc(self, tmp_path, monkeypatch):
        """Время удаления пишется в UTC, как даты сообщений, а не в местном поясе."""
        import time
        from datetime import UTC, datetime

        import tg_export.watch as watch

        moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

        class _Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                local = moment.astimezone(tz)
                return local if tz is not None else local.replace(tzinfo=None)

        monkeypatch.setattr(watch, "datetime", _Clock)
        monkeypatch.setenv("TZ", "Etc/GMT-3")
        time.tzset()
        try:
            stream, out = _stream(tmp_path, MagicMock(), _state(tmp_path))

            await stream.handle(SimpleNamespace(chat_id=111, deleted_ids=[5]), kind="deleted")

            assert json.loads(out.getvalue())["date"] == watch.format_moment(moment)
        finally:
            monkeypatch.undo()
            time.tzset()


def _listening_api(listen):
    """Заглушка API для прогона жизненного цикла слушателя без Click.

    `listen` подменяет `run_until_disconnected`; колбэк переподключения
    Telethon -- отдельная заглушка, чтобы проверить, что его вернули на место.
    """
    api = MagicMock()

    async def _no_dialogs(archived=None):
        return
        yield  # pragma: no cover -- makes this an async generator

    api.iter_dialogs = _no_dialogs
    api.client = MagicMock()
    api.client.add_event_handler = MagicMock()
    api.client.run_until_disconnected = AsyncMock(side_effect=listen)
    api.original_reconnect_callback = AsyncMock()
    api.client._sender = SimpleNamespace(_auto_reconnect_callback=api.original_reconnect_callback)
    return api


def _logged_stream(tmp_path, api, state):
    """Поток слушателя с журналом в файле и отслеживаемым закрытием журнала."""
    sink = RecordSink(io.StringIO(), as_json=True, log_path=tmp_path / "watch.log")
    closing = AsyncMock(wraps=sink.aclose)
    sink.aclose = closing
    stream = LiveStream(api, state, sink, wanted=None, excluded=set(), catchup_limit=100, outgoing=True)
    return stream, sink, closing


class TestListenerLifecycle:
    async def test_failure_while_listening_still_saves_closes_and_restores(self, tmp_path):
        """Ошибка посреди прослушивания не теряет отметки, журнал и колбэк Telethon.

        Ошибка уходит вызывающему, но до этого колбэк переподключения
        возвращён, отметки записаны на диск, а журнал закрыт.
        """
        holder = {}

        async def listen():
            await holder["stream"].handle(_live_event(111, 42), kind="message")
            raise ConnectionError("связь оборвалась")

        api = _listening_api(listen)
        state = _state(tmp_path)
        stream, sink, closing = _logged_stream(tmp_path, api, state)
        holder["stream"] = stream
        said: list[str] = []

        with pytest.raises(ConnectionError):
            await stream.run(catchup=True, edits=True, announce=said.append)

        assert api.client._sender._auto_reconnect_callback is api.original_reconnect_callback
        stored = json.loads((tmp_path / "watch.json").read_text(encoding="utf-8"))
        assert stored["chats"] == {"111": 42}
        closing.assert_awaited()
        assert sink._log is None
        assert json.loads((tmp_path / "watch.log").read_text(encoding="utf-8"))["id"] == 42
        assert said == ["Watching every chat"]

    async def test_interrupt_is_a_quiet_stop(self, tmp_path):
        """Прерывание с клавиатуры -- штатная остановка: сообщение «Stopped», без ошибки."""

        async def listen():
            raise KeyboardInterrupt

        api = _listening_api(listen)
        stream, _, closing = _logged_stream(tmp_path, api, _state(tmp_path))
        said: list[str] = []

        await stream.run(catchup=False, edits=False, announce=said.append)

        assert said == ["Watching every chat", "Stopped"]
        assert api.client.add_event_handler.call_count == 1
        assert (tmp_path / "watch.json").exists()
        closing.assert_awaited()

    async def test_gap_is_open_before_the_first_event_can_arrive(self, tmp_path):
        """Пропуск открыт до подписки: живое сообщение не обгоняет догонку."""
        api = _listening_api(None)
        stream, _, _ = _logged_stream(tmp_path, api, _state(tmp_path))
        gaps_at_subscribe: list[int] = []
        api.client.add_event_handler.side_effect = lambda *a: gaps_at_subscribe.append(stream._open_gaps)

        await stream.run(catchup=True, edits=False, announce=lambda _: None)

        assert gaps_at_subscribe == [1]
