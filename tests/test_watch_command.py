"""Проверки команды `tg watch`: отбор чатов, отказы и завершение."""

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
