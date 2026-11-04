"""Проверки команды `tg watch`: отбор чатов, отказы и завершение."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from tg_export.cli import main
from tg_export.errors import ChatResolveError, ProcessLockError


def _run(args, *, api=None, connect_error=None, tmp_path=None):
    """Выполнить `tg watch` поверх заглушки API."""
    api = api or _api()

    with (
        patch("tg_export.cli.common.account_manager") as manager,
        patch("tg_export.api.TgApi", return_value=api),
    ):
        mgr = MagicMock()
        mgr.resolve_account.return_value = "testaccount"
        mgr.load_credentials.return_value = ("id", "hash")
        mgr.load_proxy.return_value = None
        mgr.session_path.return_value = "/tmp/test.session"
        mgr.watch_state_path.return_value = (tmp_path or MagicMock()) and (
            tmp_path / "watch.json" if tmp_path else "/tmp/watch.json"
        )
        manager.return_value = mgr
        if connect_error is not None:
            api.__aenter__.side_effect = connect_error

        result = CliRunner().invoke(main, ["tg", "watch", *args])
    return result, api


def _channel():
    """Канал как его отдаёт Telegram: по нему считается маркированный идентификатор."""
    from datetime import datetime

    from telethon.tl.types import Channel, ChatPhotoEmpty

    return Channel(id=1234567890, title="Чат", photo=ChatPhotoEmpty(), date=datetime.now())


def _api(entity=None):
    api = AsyncMock()
    api.client = MagicMock()
    api.client.get_entity = AsyncMock(return_value=entity or SimpleNamespace(id=111, title="Чат"))
    api.client.run_until_disconnected = AsyncMock(return_value=None)
    api.client.is_connected = MagicMock(return_value=True)
    api.client.add_event_handler = MagicMock()

    async def _no_dialogs(archived=None):
        return
        yield  # pragma: no cover -- makes this an async generator

    api.iter_dialogs = _no_dialogs
    return api


class TestWatchArguments:
    def test_chats_and_exclusions_together_are_refused(self, tmp_path):
        """Перечень чатов и исключения вместе -- противоречивый отбор."""
        result, api = _run(["111", "--exclude", "222"], tmp_path=tmp_path)

        assert result.exit_code == 2
        api.client.add_event_handler.assert_not_called()

    def test_listens_until_disconnected(self, tmp_path):
        """Без аргументов команда слушает все чаты до разрыва соединения."""
        result, api = _run(["--no-catchup"], tmp_path=tmp_path)

        assert result.exit_code == 0, result.output
        api.client.run_until_disconnected.assert_awaited_once()
        assert api.client.add_event_handler.call_count == 3

    def test_marks_are_saved_on_exit(self, tmp_path):
        """При завершении отметки записываются на диск."""
        _run(["--no-catchup"], tmp_path=tmp_path)

        assert (tmp_path / "watch.json").exists()


class TestWatchRefusals:
    def test_busy_session_is_reported(self, tmp_path):
        """Занятая другим процессом сессия -- отказ с ненулевым кодом."""
        result, _ = _run(
            ["--no-catchup"],
            connect_error=ProcessLockError("session busy"),
            tmp_path=tmp_path,
        )

        assert result.exit_code == 1
        # Точку входа занимает общий обработчик доменных ошибок: он печатает
        # короткое сообщение и отдаёт этот же код, а CliRunner отдаёт саму ошибку.
        assert isinstance(result.exception, ProcessLockError)

    def test_unresolvable_chat_stops_before_listening(self, tmp_path):
        """Чат, который не открывается, останавливает команду до подписки."""
        api = _api()
        api.client.get_entity = AsyncMock(side_effect=ChatResolveError("no such chat"))

        result, api = _run(["@nosuchchat", "--no-catchup"], api=api, tmp_path=tmp_path)

        assert result.exit_code == 1
        api.client.add_event_handler.assert_not_called()


class TestWatchSelection:
    def test_named_chat_becomes_the_white_list(self, tmp_path):
        """Названный чат разрешается в маркированный идентификатор и уходит в подписку."""
        api = _api(_channel())

        result, api = _run(["@examplechat", "--no-catchup"], api=api, tmp_path=tmp_path)

        assert result.exit_code == 0, result.output
        builder = api.client.add_event_handler.call_args_list[0].args[1]
        assert builder.chats == [-1001234567890]
        assert builder.blacklist_chats is False

    def test_excluded_chat_becomes_the_black_list(self, tmp_path):
        """Исключённый чат уходит в подписку чёрным списком."""
        api = _api(_channel())

        result, api = _run(["--exclude", "@examplechat", "--no-catchup"], api=api, tmp_path=tmp_path)

        assert result.exit_code == 0, result.output
        builder = api.client.add_event_handler.call_args_list[0].args[1]
        assert builder.chats == [-1001234567890]
        assert builder.blacklist_chats is True

    def test_refusal_names_the_chat(self, tmp_path):
        """Отказ называет чат, который не открылся, а не просто падает."""
        api = _api()
        api.client.get_entity = AsyncMock(side_effect=ChatResolveError("no chat named 'nosuchchat'"))

        result, _ = _run(["@nosuchchat", "--no-catchup"], api=api, tmp_path=tmp_path)

        assert result.exit_code == 1
        assert "nosuchchat" in result.output


def _api_with_reconnect():
    """Заглушка API, в которой Telethon один раз переподключается посреди прослушивания.

    Переподключение -- это вызов колбэка, который отправитель Telethon
    запускает после каждого успешного автоматического переподключения.
    """
    api = _api()
    original = AsyncMock()
    api.client._sender = SimpleNamespace(_auto_reconnect_callback=original)
    api.dialog_passes = 0

    async def _counted_dialogs(archived=None):
        api.dialog_passes += 1
        return
        yield  # pragma: no cover -- makes this an async generator

    async def _listen():
        await api.client._sender._auto_reconnect_callback()

    api.iter_dialogs = _counted_dialogs
    api.client.run_until_disconnected = AsyncMock(side_effect=_listen)
    api.original_reconnect_callback = original
    return api


class TestWatchReconnect:
    def test_reconnect_asks_for_what_was_missed(self, tmp_path):
        """После автоматического переподключения догонка запускается сразу, без опроса связи."""
        api = _api_with_reconnect()

        result, api = _run([], api=api, tmp_path=tmp_path)

        assert result.exit_code == 0, result.output
        assert api.dialog_passes == 2
        api.original_reconnect_callback.assert_awaited_once()

    def test_no_catchup_leaves_reconnects_alone(self, tmp_path):
        """С --no-catchup пропущенное не добирается и после переподключения."""
        api = _api_with_reconnect()

        result, api = _run(["--no-catchup"], api=api, tmp_path=tmp_path)

        assert result.exit_code == 0, result.output
        assert api.dialog_passes == 0
        api.original_reconnect_callback.assert_awaited_once()

    def test_reconnect_hook_is_removed_on_exit(self, tmp_path):
        """По завершении колбэк переподключения Telethon возвращается прежним."""
        api = _api_with_reconnect()

        _run([], api=api, tmp_path=tmp_path)

        assert api.client._sender._auto_reconnect_callback is api.original_reconnect_callback


def _seed_marks(tmp_path, marks):
    """Файл отметок, как его оставил прошлый запуск."""
    (tmp_path / "watch.json").write_text(
        json.dumps({"version": 1, "account": "testaccount", "chats": {str(k): v for k, v in marks.items()}}),
        encoding="utf-8",
    )


def _history_api(chat_id, history, entity=None):
    """Заглушка API, у которой в чате `chat_id` есть история после отметки."""
    api = _api(entity)
    dialog = SimpleNamespace(
        id=chat_id,
        title="alfa",
        message=SimpleNamespace(id=history[-1].id),
        dialog=SimpleNamespace(top_message=history[-1].id),
        entity=SimpleNamespace(),
    )
    api.dialog_passes = 0
    api.peer_requests = []

    async def _dialogs(archived=None):
        api.dialog_passes += 1
        yield dialog

    async def _peer_dialogs(chat_ids):
        api.peer_requests.append(list(chat_ids))
        yield dialog

    def _iter_messages(entity, *, min_id=0, limit=None, reverse=False):
        async def _gen():
            picked = [m for m in history if m.id > min_id]
            if not reverse:
                picked.reverse()
            for message in picked if limit is None else picked[:limit]:
                yield message

        return _gen()

    api.iter_dialogs = _dialogs
    api.peer_dialogs = _peer_dialogs
    api.client.iter_messages = _iter_messages
    return api


def _message(message_id, *, out=False):
    return SimpleNamespace(
        id=message_id,
        date=None,
        message="beta",
        entities=None,
        media=None,
        action=None,
        out=out,
        file=None,
        sender=None,
        sender_id=None,
        reply_to=None,
    )


class TestWatchCatchUp:
    def test_no_outgoing_reaches_the_catch_up(self, tmp_path):
        """С --no-outgoing догонка не выдаёт своё сообщение, но отметку сдвигает."""
        _seed_marks(tmp_path, {111: 10})
        api = _history_api(111, [_message(11, out=True)])

        result, _ = _run(["--no-outgoing", "--json"], api=api, tmp_path=tmp_path)

        assert result.exit_code == 0, result.output
        assert result.stdout == ""
        saved = json.loads((tmp_path / "watch.json").read_text(encoding="utf-8"))
        assert saved["chats"] == {"111": 11}

    def test_named_chat_is_caught_up_without_the_dialog_list(self, tmp_path):
        """Для названного чата догонка берёт его диалог напрямую, не обходя весь список."""
        _seed_marks(tmp_path, {-1001234567890: 10})
        api = _history_api(-1001234567890, [_message(11)], entity=_channel())

        result, api = _run(["@examplechat", "--json"], api=api, tmp_path=tmp_path)

        assert result.exit_code == 0, result.output
        assert api.dialog_passes == 0
        assert api.peer_requests == [[-1001234567890]]
        record = json.loads(result.stdout)
        assert record["id"] == 11
        assert record["chat"] == "alfa"
