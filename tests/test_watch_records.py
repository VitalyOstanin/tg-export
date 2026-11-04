"""Проверки записей слушателя, подписки на события и вывода потока."""

import asyncio
import io
import json
import os
import stat
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from telethon.tl.types import MessageReplyHeader

from tg_export.watch import (
    RecordSink,
    deletion_record,
    follow_reconnects,
    message_record,
    subscribe,
    text_line,
)

MOMENT = datetime(2026, 9, 16, 12, 3, 41)


def _message(**kwargs):
    defaults = {
        "id": 4711,
        "date": MOMENT,
        "message": "смотри вложение",
        "entities": None,
        "media": None,
        "action": None,
        "out": False,
        "file": None,
        "sender": SimpleNamespace(first_name="Иван", last_name="Иванов"),
        "sender_id": 12345,
        "reply_to": MessageReplyHeader(reply_to_msg_id=4708),
    }
    return SimpleNamespace(**{**defaults, **kwargs})


class TestMessageRecord:
    def test_carries_the_fields_a_reader_acts_on(self):
        """Запись называет чат, отправителя, текст и вид события."""
        record = message_record(-1001234567890, "Рабочая группа", _message())

        assert record == {
            "event": "message",
            "chat_id": -1001234567890,
            "chat": "Рабочая группа",
            "id": 4711,
            "date": "2026-09-16 12:03",
            "sender_id": 12345,
            "sender": "Иван Иванов",
            "text": "смотри вложение",
            "links": [],
            "media": None,
            "file": None,
            "action": None,
            "outgoing": False,
            "reply_to": 4708,
            "reply_to_peer": None,
        }

    def test_media_is_named_by_its_type(self):
        """Тип вложения и его файл попадают в запись, сам файл не скачивается."""
        media = type("MessageMediaDocument", (), {})()
        file = SimpleNamespace(name="отчёт.pdf", size=248000)

        record = message_record(111, "Чат", _message(media=media, file=file))

        assert record["media"] == "Document"
        assert record["file"] == {"name": "отчёт.pdf", "size": 248000}

    def test_service_message_names_its_action(self):
        """Служебное сообщение отмечается действием, а не пустым текстом."""
        action = type("MessageActionChatJoinedByLink", (), {})()

        record = message_record(111, "Чат", _message(message="", action=action))

        assert record["action"] == "ChatJoinedByLink"

    def test_edit_is_marked_as_such(self):
        """Правка отличима от нового сообщения по виду события."""
        record = message_record(111, "Чат", _message(), event="edited")

        assert record["event"] == "edited"

    def test_outgoing_is_marked(self):
        """Своё отправленное сообщение помечено, иначе отвечающий зациклится."""
        record = message_record(111, "Чат", _message(out=True))

        assert record["outgoing"] is True

    def test_links_under_words_carry_their_addresses(self):
        """Адрес ссылки, привязанной к словам, в тексте не виден -- он идёт полем `links`."""
        from telethon.tl.types import MessageEntityTextUrl

        entities = [MessageEntityTextUrl(offset=10, length=4, url="https://example.com")]

        record = message_record(111, "Чат", _message(message="читай эту beta", entities=entities))

        assert record["text"] == "читай эту beta"
        assert record["links"] == [{"offset": 10, "length": 4, "text": "beta", "url": "https://example.com"}]


class TestDeletionRecord:
    def test_lists_the_deleted_ids(self):
        """Удаление называет список идентификаторов и время события."""
        record = deletion_record(-1001234567890, "Рабочая группа", [4711, 4712], MOMENT)

        assert record["event"] == "deleted"
        assert record["ids"] == [4711, 4712]
        assert record["chat_id"] == -1001234567890
        assert record["date"] == "2026-09-16 12:03"

    def test_chat_may_be_unknown(self):
        """В личной переписке Telegram не называет чат удаления -- поле пустое."""
        record = deletion_record(None, None, [4711], MOMENT)

        assert record["chat_id"] is None


class TestTextLine:
    def test_message_line_reads_like_tg_messages(self):
        """Строка для терминала совпадает по виду с выводом `tg messages`."""
        line = text_line(message_record(111, "Чат", _message()))

        assert "2026-09-16 12:03" in line
        assert "[4711]" in line
        assert "Иван Иванов" in line
        assert "смотри вложение" in line
        assert "[4711] ↪ 4708  " in line

    def test_message_line_shows_the_address_of_a_link(self):
        """В терминале адрес ссылки встаёт в скобках за её словами."""
        from telethon.tl.types import MessageEntityTextUrl

        entities = [MessageEntityTextUrl(offset=10, length=4, url="https://example.com")]

        line = text_line(message_record(111, "Чат", _message(message="читай эту beta", entities=entities)))

        assert line.endswith("читай эту beta (https://example.com)")

    def test_deletion_line_says_what_went(self):
        """Строка удаления называет идентификаторы удалённых сообщений."""
        line = text_line(deletion_record(111, "Чат", [4711, 4712], MOMENT))

        assert "4711" in line
        assert "4712" in line


class TestSubscribe:
    def test_named_chats_become_a_white_list(self):
        """Перечень чатов уходит в подписку как белый список."""
        client = MagicMock()

        subscribe(client, MagicMock(), wanted={111, 222}, excluded=set(), edits=True, outgoing=True)

        builder = client.add_event_handler.call_args_list[0].args[1]
        assert set(builder.chats) == {111, 222}
        assert builder.blacklist_chats is False

    def test_exclusions_become_a_black_list(self):
        """Исключения уходят в подписку как чёрный список."""
        client = MagicMock()

        subscribe(client, MagicMock(), wanted=None, excluded={444}, edits=True, outgoing=True)

        builder = client.add_event_handler.call_args_list[0].args[1]
        assert set(builder.chats) == {444}
        assert builder.blacklist_chats is True

    def test_edits_and_deletions_are_optional(self):
        """Без правок и удалений подписка ставит один обработчик вместо трёх."""
        client = MagicMock()

        subscribe(client, MagicMock(), wanted=None, excluded=set(), edits=False, outgoing=True)

        assert client.add_event_handler.call_count == 1

    def test_all_three_events_by_default(self):
        """По умолчанию слушаются новые сообщения, правки и удаления."""
        client = MagicMock()

        subscribe(client, MagicMock(), wanted=None, excluded=set(), edits=True, outgoing=True)

        assert client.add_event_handler.call_count == 3

    def test_outgoing_can_be_left_out(self):
        """Без своих сообщений правки фильтруются по входящим, а новые сообщения -- нет.

        Свои новые сообщения доходят до обработчика: он их не печатает, но
        сдвигает по ним отметку чата, иначе догонка запрашивала бы их снова.
        """
        client = MagicMock()

        subscribe(client, MagicMock(), wanted=None, excluded=set(), edits=True, outgoing=False)

        new_messages = client.add_event_handler.call_args_list[0].args[1]
        edits = client.add_event_handler.call_args_list[1].args[1]
        assert new_messages.incoming is None
        assert new_messages.outgoing is None
        assert edits.incoming is True


class TestRecordSink:
    def test_json_line_per_record(self, tmp_path):
        """В машинном режиме каждая запись -- одна строка JSON."""
        stream = io.StringIO()
        sink = RecordSink(stream, as_json=True, log_path=None)

        asyncio.run(sink.emit(message_record(111, "Чат", _message())))
        asyncio.run(sink.emit(message_record(111, "Чат", _message(id=4712))))

        lines = stream.getvalue().splitlines()
        assert [json.loads(line)["id"] for line in lines] == [4711, 4712]

    def test_terminal_mode_writes_lines(self, tmp_path):
        """В терминальном режиме пишется строка для чтения человеком."""
        stream = io.StringIO()
        sink = RecordSink(stream, as_json=False, log_path=None)

        asyncio.run(sink.emit(message_record(111, "Чат", _message())))

        assert "[4711]" in stream.getvalue()

    def test_log_keeps_json_even_in_terminal_mode(self, tmp_path):
        """Журнал всегда машинный: его читает программа, а не человек."""
        log = tmp_path / "live.ndjson"
        sink = RecordSink(io.StringIO(), as_json=False, log_path=log)

        asyncio.run(sink.emit(message_record(111, "Чат", _message())))

        assert json.loads(log.read_text(encoding="utf-8").strip())["id"] == 4711

    def test_log_is_private(self, tmp_path):
        """Журнал закрыт от других пользователей: в нём переписка."""
        log = tmp_path / "live.ndjson"
        sink = RecordSink(io.StringIO(), as_json=False, log_path=log)

        asyncio.run(sink.emit(message_record(111, "Чат", _message())))

        assert stat.S_IMODE(log.stat().st_mode) & 0o077 == 0

    def test_log_appends(self, tmp_path):
        """Журнал дописывается, а не переписывается: прошлый запуск сохраняется."""
        log = tmp_path / "live.ndjson"
        log.write_text('{"event":"message","id":1}\n', encoding="utf-8")
        sink = RecordSink(io.StringIO(), as_json=False, log_path=log)

        asyncio.run(sink.emit(message_record(111, "Чат", _message())))

        assert len(log.read_text(encoding="utf-8").splitlines()) == 2

    def test_existing_loose_log_is_tightened(self, tmp_path):
        """Журнал, созданный раньше с правами для всех, закрывается перед дописыванием:
        переписка не должна оставаться читаемой другим пользователям."""
        log = tmp_path / "live.ndjson"
        log.write_text('{"event":"message","id":1}\n', encoding="utf-8")
        log.chmod(0o644)
        sink = RecordSink(io.StringIO(), as_json=False, log_path=log)

        async def run():
            await sink.emit(message_record(111, "Чат", _message()))
            await sink.aclose()

        asyncio.run(run())

        assert stat.S_IMODE(log.stat().st_mode) & 0o077 == 0

    def test_log_is_opened_once_for_many_records(self, tmp_path, monkeypatch):
        """Файл журнала открывается один раз на весь запуск, а не на каждую запись."""
        log = tmp_path / "live.ndjson"
        opened = []
        real_open = os.open

        def counting_open(path, *args, **kwargs):
            opened.append(path)
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("tg_export.watch.os.open", counting_open)
        sink = RecordSink(io.StringIO(), as_json=True, log_path=log)

        async def run():
            for message_id in (4711, 4712, 4713):
                await sink.emit(message_record(111, "Чат", _message(id=message_id)))
            await sink.aclose()

        asyncio.run(run())

        assert len(opened) == 1
        assert len(log.read_text(encoding="utf-8").splitlines()) == 3

    def test_log_lines_reach_the_file_before_closing(self, tmp_path):
        """Каждая запись попадает в файл сразу: читатель журнала видит её, не дожидаясь
        остановки слушателя."""
        log = tmp_path / "live.ndjson"
        sink = RecordSink(io.StringIO(), as_json=True, log_path=log)

        async def run():
            await sink.emit(message_record(111, "Чат", _message()))
            seen = log.read_text(encoding="utf-8")
            await sink.aclose()
            return seen

        assert json.loads(asyncio.run(run()).strip())["id"] == 4711

    def test_aclose_without_log_is_harmless(self):
        """Закрытие без журнала ничего не требует и может повторяться."""
        sink = RecordSink(io.StringIO(), as_json=True, log_path=None)

        async def run():
            await sink.aclose()
            await sink.aclose()

        asyncio.run(run())

    def test_stdout_is_written_off_the_event_loop(self):
        """Вывод пишется не в потоке цикла событий: медленный читатель конвейера
        не должен останавливать обработку обновлений и ping соединения."""
        loop_thread = threading.get_ident()
        writers = []

        class Recording(io.StringIO):
            def write(self, text):
                writers.append(threading.get_ident())
                return super().write(text)

        sink = RecordSink(Recording(), as_json=True, log_path=None)

        asyncio.run(sink.emit(message_record(111, "Чат", _message())))

        assert writers
        assert loop_thread not in writers

    def test_concurrent_records_keep_their_order(self, tmp_path):
        """Записи, пришедшие одновременно из разных обработчиков, выходят в порядке
        поступления -- и в вывод, и в журнал."""
        log = tmp_path / "live.ndjson"
        stream = io.StringIO()
        sink = RecordSink(stream, as_json=True, log_path=log)
        ids = list(range(5000, 5040))

        async def run():
            await asyncio.gather(*(sink.emit(message_record(111, "Чат", _message(id=i))) for i in ids))
            await sink.aclose()

        asyncio.run(run())

        assert [json.loads(line)["id"] for line in stream.getvalue().splitlines()] == ids
        assert [json.loads(line)["id"] for line in log.read_text(encoding="utf-8").splitlines()] == ids


class TestFollowReconnects:
    @pytest.mark.asyncio
    async def test_reconnect_runs_the_catch_up_before_the_library_callback(self):
        """Колбэк переподключения сначала запускает добор, затем прежний колбэк Telethon."""
        calls = []

        async def original():
            calls.append("telethon")

        async def on_reconnect():
            calls.append("catch-up")

        client = SimpleNamespace(_sender=SimpleNamespace(_auto_reconnect_callback=original))

        restore = follow_reconnects(client, on_reconnect)
        await client._sender._auto_reconnect_callback()
        restore()

        assert calls == ["catch-up", "telethon"]
        assert client._sender._auto_reconnect_callback is original

    @pytest.mark.asyncio
    async def test_failed_catch_up_still_runs_the_library_callback(self):
        """Упавший добор не отменяет запрос, которым Telethon возобновляет обновления."""
        calls = []

        async def original():
            calls.append("telethon")

        async def on_reconnect():
            raise ConnectionError("связь оборвалась посреди догонки")

        client = SimpleNamespace(_sender=SimpleNamespace(_auto_reconnect_callback=original))

        follow_reconnects(client, on_reconnect)
        await client._sender._auto_reconnect_callback()

        assert calls == ["telethon"]

    def test_client_without_the_hook_is_reported_not_broken(self, caplog):
        """Клиент без колбэка переподключения не роняет команду: добор после разрыва недоступен."""

        async def on_reconnect():
            return None

        with caplog.at_level("WARNING", logger="tg_export.watch"):
            restore = follow_reconnects(SimpleNamespace(), on_reconnect)
        restore()

        assert "reconnect" in caplog.text


class TestTelethonReconnectContract:
    """Добор после разрыва опирается на внутренности Telethon; проверки ловят их смену."""

    def test_sender_carries_the_reconnect_callback(self):
        """Отправитель клиента хранит колбэк, вызываемый после переподключения."""
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        client = TelegramClient(StringSession(), 1, "hash")

        assert callable(client._sender._auto_reconnect_callback)

    def test_callback_runs_after_every_successful_reconnect(self):
        """`_reconnect` вызывает колбэк после успешного подключения."""
        import inspect

        from telethon.network.mtprotosender import MTProtoSender

        source = inspect.getsource(MTProtoSender._reconnect)

        assert "self._auto_reconnect_callback()" in source

    def test_is_connected_does_not_drop_during_a_reconnect(self):
        """`is_connected` не сбрасывается при автопереподключении -- опрос его разрыва не видит.

        Флаг гасит только окончательное отключение, после которого
        `run_until_disconnected` возвращается и слушатель завершается.
        """
        import inspect

        from telethon.network.mtprotosender import MTProtoSender

        assert "_user_connected = False" not in inspect.getsource(MTProtoSender._reconnect)
        assert "_user_connected = False" in inspect.getsource(MTProtoSender._disconnect)
