"""Проверки единого разбора идентификатора чата для группы команд tg."""

import pytest

from tg_export.cli.chatref import parse_chat_ref


class TestParseChatRef:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("123456789", 123456789),
            ("-1001234567890", -1001234567890),
            ("@examplechat", "examplechat"),
            ("examplechat", "examplechat"),
            ("https://t.me/examplechat", "examplechat"),
            ("https://t.me/examplechat/1234", "examplechat"),
            ("http://t.me/examplechat", "examplechat"),
            ("t.me/examplechat", "examplechat"),
            ("https://telegram.me/examplechat", "examplechat"),
            ("  @examplechat  ", "examplechat"),
        ],
    )
    def test_accepted_forms(self, value, expected):
        """Каждая поддержанная форма приводится к числу или к имени без собаки."""
        assert parse_chat_ref(value) == expected

    def test_private_link_becomes_channel_id(self):
        """Ссылка на приватную супергруппу приводится к её идентификатору."""
        assert parse_chat_ref("https://t.me/c/1234567890/42") == -1001234567890

    @pytest.mark.parametrize(
        "value",
        [
            "https://t.me/+AbCdEfGh",
            "https://t.me/joinchat/AbCdEfGh",
            "t.me/+AbCdEfGh",
        ],
    )
    def test_invite_links_refused(self, value):
        """Ссылка-приглашение не разрешается в чат и отвергается с подсказкой."""
        with pytest.raises(ValueError, match="invite link"):
            parse_chat_ref(value)

    @pytest.mark.parametrize("value", ["", "   ", "два слова", "name/with/slashes"])
    def test_malformed_refused(self, value):
        """Пустая строка и строка, не похожая на имя, отвергаются."""
        with pytest.raises(ValueError):
            parse_chat_ref(value)


def _invoke_messages(chat_arg: str):
    """Выполнить `tg messages <chat_arg>` поверх заглушки API, отдающей пустой чат."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from click.testing import CliRunner

    from tg_export.cli import main

    entity = MagicMock()
    entity.title = "Test Chat"

    async def _no_messages(*a, **kw):
        return
        yield  # pragma: no cover -- makes the function an async generator

    api = AsyncMock()
    api.client.get_entity.return_value = entity
    api.client.iter_messages = _no_messages

    with (
        patch("tg_export.cli.common.account_manager") as manager,
        patch("tg_export.api.TgApi", return_value=api),
    ):
        mgr = MagicMock()
        mgr.resolve_account.return_value = "test"
        mgr.load_credentials.return_value = ("id", "hash")
        mgr.load_proxy.return_value = None
        mgr.session_path.return_value = "/tmp/test.session"
        manager.return_value = mgr

        result = CliRunner().invoke(main, ["tg", "messages", chat_arg])
        return result, api


class TestChatRefInCommands:
    @pytest.mark.parametrize(
        ("argument", "resolved"),
        [
            ("123456789", 123456789),
            ("@examplechat", "examplechat"),
            ("https://t.me/examplechat/1234", "examplechat"),
        ],
    )
    def test_messages_resolves_every_form(self, argument, resolved):
        """Команда доводит до Telegram разобранную форму, а не исходную строку."""
        result, api = _invoke_messages(argument)

        assert result.exit_code == 0, result.output
        api.client.get_entity.assert_awaited_once_with(resolved)

    def test_malformed_argument_is_a_usage_error(self):
        """Негодная форма отвергается разбором аргументов, до обращения к сети."""
        result, api = _invoke_messages("два слова")

        assert result.exit_code == 2
        api.client.get_entity.assert_not_awaited()
