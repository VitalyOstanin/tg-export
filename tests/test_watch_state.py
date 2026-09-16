"""Проверки отметок слушателя и добора пропущенных сообщений."""

import asyncio
import json
import stat
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tg_export.watch import WatchState, missed_messages


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


def _api(dialogs, messages_by_chat=None):
    """Заглушка API: заданные диалоги и история по чатам."""
    api = MagicMock()
    messages_by_chat = messages_by_chat or {}
    calls = []

    async def _iter_dialogs(archived=None):
        for dialog in dialogs:
            yield dialog

    def _iter_messages(entity, **kwargs):
        calls.append((entity, kwargs))

        async def _gen():
            for message in messages_by_chat.get(id(entity), []):
                yield message

        return _gen()

    api.iter_dialogs = _iter_dialogs
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
        message = MagicMock()
        api = _api([dialog], {id(dialog.entity): [message]})

        collected = _collect(api, state, wanted=None, excluded=set(), limit=50)

        assert collected == [(111, "Чат 111", message)]
        entity, kwargs = api.calls[0]
        assert entity is dialog.entity
        assert kwargs["min_id"] == 974
        assert kwargs["limit"] == 50
        assert kwargs["reverse"] is True

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


@pytest.mark.parametrize("top", [None, 0])
def test_dialog_without_a_last_message_is_skipped(tmp_path, top):
    """Диалог без последнего сообщения пропускается, а не роняет добор."""
    state = _state(tmp_path)
    state.note(111, 974)
    api = _api([_dialog(111, top)])

    collected = _collect(api, state, wanted=None, excluded=set(), limit=100)

    assert collected == []
