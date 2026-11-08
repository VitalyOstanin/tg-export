"""Удаление сообщений: `tg delete` и номера сообщений в выводе `tg send`."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner
from telethon.errors.rpcerrorlist import MessageDeleteForbiddenError

from tg_export.cli import common as cli_common
from tg_export.cli import tg as cli_tg

SELF_CHAT = SimpleNamespace(first_name="Тестовый", last_name=None, is_self=True)


def _message(i, *, text="", grouped_id=None, out=True, file_name=None):
    return SimpleNamespace(
        id=i,
        date=datetime(2030, 6, 1, 9, 0, tzinfo=UTC) + timedelta(minutes=i),
        message=text or f"текст {i}",
        entities=None,
        media=None,
        action=None,
        sender=None,
        sender_id=None,
        reply_to=None,
        out=out,
        grouped_id=grouped_id,
        file=SimpleNamespace(name=file_name, size=10) if file_name else None,
    )


class _Chat:
    """Подставной чат: сообщения по номерам, чтение и удаление как у Telethon."""

    def __init__(self, messages, *, refuse=None, keep=()):
        self.messages = {m.id: m for m in messages}
        self.refuse = refuse
        self.keep = set(keep)
        self.deleted: list[tuple[list[int], bool]] = []

    async def get_messages(self, _entity, ids):
        return [self.messages.get(i) for i in ids]

    async def delete_messages(self, _entity, ids, revoke=True):
        if self.refuse is not None:
            raise self.refuse
        self.deleted.append((list(ids), revoke))
        for i in ids:
            if i not in self.keep:
                self.messages.pop(i, None)


def _run(chat, ids, *, entity=SELF_CHAT, revoke=False, whole_album=False, skip_confirm=True):
    api = MagicMock()
    api.client = chat

    @contextlib.asynccontextmanager
    async def connected(_account):
        yield api, "acc"

    with (
        patch("tg_export.cli.common.connected_api", connected),
        patch.object(cli_tg, "resolve_chat", AsyncMock(return_value=entity)),
    ):
        return asyncio.run(
            cli_tg._tg_delete(
                "me", ids, "acc", revoke=revoke, whole_album=whole_album, skip_confirm=skip_confirm
            )
        )


def test_deletes_the_named_messages_only_for_me_by_default():
    """По умолчанию удаляются названные сообщения и только у себя (revoke=False)."""
    chat = _Chat([_message(1), _message(2), _message(3)])

    code = _run(chat, [3, 1])

    assert code == 0
    assert chat.deleted == [([1, 3], False)]
    assert set(chat.messages) == {2}


def test_for_everyone_revokes():
    """`--for-everyone` удаляет у обеих сторон (revoke=True)."""
    chat = _Chat([_message(1)])

    _run(chat, [1], revoke=True)

    assert chat.deleted == [([1], True)]


def test_preview_lists_what_is_deleted(capsys):
    """Перед удалением выводится каждое сообщение: номер, начало текста, имя файла."""
    chat = _Chat([_message(4, text="первая строка", file_name="отчёт.pdf")])

    _run(chat, [4])

    err = capsys.readouterr().err
    assert "[4]" in err
    assert "первая строка" in err
    assert "отчёт.pdf" in err
    assert "2030-06-01" in err


def test_missing_id_deletes_nothing(capsys):
    """Номер, которого нет в чате, -- ошибка, и не удаляется ничего: опечатка может быть не одна."""
    chat = _Chat([_message(1)])

    code = _run(chat, [1, 9])

    assert code == 1
    assert chat.deleted == []
    assert "no message 9" in capsys.readouterr().err


def test_answer_no_deletes_nothing(capsys):
    """Отказ в подтверждении ничего не удаляет."""
    chat = _Chat([_message(1)])

    with patch.object(cli_tg, "confirm", return_value=False) as asked:
        code = _run(chat, [1], skip_confirm=False)

    assert asked.called
    assert code == 0
    assert chat.deleted == []
    assert "Cancelled" in capsys.readouterr().err


def test_yes_skips_the_question():
    """С `--yes` вопрос не задаётся."""
    chat = _Chat([_message(1)])

    with patch.object(cli_tg, "confirm") as asked:
        _run(chat, [1], skip_confirm=True)

    assert not asked.called
    assert chat.deleted


def test_part_of_an_album_is_deleted_alone_with_a_warning(capsys):
    """Часть альбома удаляется одна, и вывод называет остальные части и `--whole-album`."""
    chat = _Chat([_message(10, grouped_id=7), _message(11, grouped_id=7), _message(12, grouped_id=7)])

    code = _run(chat, [11])

    assert code == 0
    assert chat.deleted == [([11], False)]
    err = capsys.readouterr().err
    assert "album" in err
    assert "10, 11, 12" in err
    assert "--whole-album" in err


def test_whole_album_takes_every_part():
    """`--whole-album` добавляет к названной части все части её альбома."""
    chat = _Chat([_message(9), _message(10, grouped_id=7), _message(11, grouped_id=7), _message(13)])

    _run(chat, [11], whole_album=True)

    assert chat.deleted == [([10, 11], False)]


def test_forbidden_deletion_is_explained(capsys):
    """Отказ Telegram в праве удалить объясняется, код возврата 1."""
    chat = _Chat([_message(1, out=False)], refuse=MessageDeleteForbiddenError(None))

    code = _run(chat, [1], entity=SimpleNamespace(title="Группа", megagroup=True))

    assert code == 1
    assert "rights" in capsys.readouterr().err


def test_message_left_in_place_is_reported(capsys):
    """Если после удаления сообщение всё ещё читается, это ошибка с его номером."""
    chat = _Chat([_message(1), _message(2)], keep={2})

    code = _run(chat, [1, 2])

    assert code == 1
    assert "2" in capsys.readouterr().err.split("still")[-1]


def test_foreign_message_is_marked(capsys):
    """Чужое сообщение помечено в списке: удалить его может не хватить прав."""
    chat = _Chat([_message(1, out=False)])

    _run(chat, [1], entity=SimpleNamespace(first_name="Собеседник", last_name=None))

    assert "not yours" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("entity", "revoke", "expected"),
    [
        (SELF_CHAT, False, "Saved Messages"),
        (SimpleNamespace(first_name="Собеседник", last_name=None), False, "only for you"),
        (SimpleNamespace(first_name="Собеседник", last_name=None), True, "for everyone"),
        (SimpleNamespace(title="Канал", broadcast=True), False, "for everyone"),
    ],
)
def test_preview_says_for_whom(capsys, entity, revoke, expected):
    """Вывод говорит, у кого исчезнут сообщения: в группах и каналах -- у всех независимо от флага."""
    _run(_Chat([_message(1)]), [1], entity=entity, revoke=revoke)

    assert expected in capsys.readouterr().err


def test_needs_ids():
    """Без номеров сообщений -- ошибка формы вызова."""
    assert CliRunner().invoke(cli_tg.tg, ["delete", "me"]).exit_code == 2


def test_help_names_the_default():
    """Справка называет поведение по умолчанию и необратимость."""
    out = CliRunner().invoke(cli_tg.tg, ["delete", "--help"]).output
    assert "--for-everyone" in out
    assert "--yes" in out
    assert "cannot be undone" in out


# ---------------------------------------------------------------------------
# tg send: номера отправленных сообщений
# ---------------------------------------------------------------------------


def test_send_prints_the_ids(capsys):
    """`tg send` печатает номера отправленных сообщений, чтобы их можно было сразу удалить."""
    api = MagicMock()
    api.client = AsyncMock()
    api.client.send_message.return_value = SimpleNamespace(id=55)

    @contextlib.asynccontextmanager
    async def connected(_account):
        yield api, "acc"

    with patch("tg_export.cli.common.connected_api", connected), patch.object(cli_common, "_QUIET", False):
        code = asyncio.run(cli_tg._tg_send("acc", ["me"], "привет", None))

    assert code == 0
    assert "sent to me (id 55)" in capsys.readouterr().err
