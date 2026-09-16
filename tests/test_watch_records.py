"""Проверки записей слушателя, подписки на события и вывода потока."""

import asyncio
import io
import json
import stat
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tg_export.watch import (
    RecordSink,
    connection_watchdog,
    deletion_record,
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
        "media": None,
        "action": None,
        "out": False,
        "file": None,
        "sender": SimpleNamespace(first_name="Иван", last_name="Иванов"),
        "sender_id": 12345,
        "reply_to_msg_id": 4708,
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
            "media": None,
            "file": None,
            "action": None,
            "outgoing": False,
            "reply_to": 4708,
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
        """Отказ от своих сообщений выражается фильтром входящих."""
        client = MagicMock()

        subscribe(client, MagicMock(), wanted=None, excluded=set(), edits=True, outgoing=False)

        builder = client.add_event_handler.call_args_list[0].args[1]
        assert builder.incoming is True


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


class TestConnectionWatchdog:
    @pytest.mark.asyncio
    async def test_recovered_connection_triggers_the_catch_up(self):
        """Восстановление связи запускает добор -- библиотека его не делает."""
        states = [True, False, False, True]
        client = MagicMock()
        client.is_connected.side_effect = lambda: states.pop(0) if states else True
        calls = []

        async def on_reconnect():
            calls.append(1)

        task = asyncio.create_task(connection_watchdog(client, on_reconnect, interval=0))
        await asyncio.sleep(0.05)
        task.cancel()

        assert calls, "добор после восстановления связи не запущен"

    @pytest.mark.asyncio
    async def test_steady_connection_is_left_alone(self):
        """Пока связь держится, добор не повторяется."""
        client = MagicMock()
        client.is_connected.return_value = True
        calls = []

        async def on_reconnect():
            calls.append(1)

        task = asyncio.create_task(connection_watchdog(client, on_reconnect, interval=0))
        await asyncio.sleep(0.05)
        task.cancel()

        assert calls == []
