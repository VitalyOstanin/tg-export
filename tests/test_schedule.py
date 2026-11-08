"""Отложенная отправка: разбор времени `--schedule`, границы Telegram и команды `tg scheduled`."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner
from telethon.errors.rpcerrorlist import (
    ScheduleBotNotAllowedError,
    ScheduleDateTooLateError,
    ScheduleTooMuchError,
)

from tg_export.cli import common as cli_common
from tg_export.cli import tg as cli_tg
from tg_export.schedule import (
    MAX_LEAD,
    check_schedule,
    describe,
    explain_refusal,
    parse_schedule,
)

TZ = timezone(timedelta(hours=3))
NOW = datetime(2030, 5, 10, 14, 30, tzinfo=TZ)


class TestParse:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("+30m", NOW + timedelta(minutes=30)),
            ("+2h", NOW + timedelta(hours=2)),
            ("+1d12h", NOW + timedelta(days=1, hours=12)),
            ("+1W", NOW + timedelta(weeks=1)),
            ("+90s", NOW + timedelta(seconds=90)),
        ],
    )
    def test_relative_time_counts_from_now(self, value, expected):
        """`+30m` и сочетания единиц отсчитываются от текущего момента."""
        assert parse_schedule(value, NOW) == expected

    def test_bare_time_later_today_stays_today(self):
        """Время суток, ещё не наступившее сегодня, означает сегодня."""
        assert parse_schedule("18:05", NOW) == datetime(2030, 5, 10, 18, 5, tzinfo=TZ)

    def test_bare_time_already_passed_means_tomorrow(self):
        """Прошедшее сегодня время означает то же время завтра, а не момент в прошлом."""
        assert parse_schedule("9:00", NOW) == datetime(2030, 5, 11, 9, 0, tzinfo=TZ)

    def test_tomorrow_is_tomorrow_even_when_the_time_is_ahead_today(self):
        """`tomorrow 18:00` -- завтра, хотя 18:00 сегодня ещё впереди."""
        assert parse_schedule("tomorrow 18:00", NOW) == datetime(2030, 5, 11, 18, 0, tzinfo=TZ)

    def test_iso_without_zone_takes_the_local_one(self):
        """ISO 8601 без пояса понимается в местном поясе."""
        assert parse_schedule("2030-05-12 08:15", NOW) == datetime(2030, 5, 12, 8, 15, tzinfo=TZ)

    def test_iso_with_zone_keeps_it(self):
        """Явно указанный пояс сохраняется: это тот же момент, а не местное время."""
        moment = parse_schedule("2030-05-12T08:15+00:00", NOW)
        assert moment == datetime(2030, 5, 12, 11, 15, tzinfo=TZ)

    @pytest.mark.parametrize("value", ["2030-05-12", "", "soon", "+", "+5x", "25:00", "tomorrow"])
    def test_unclear_values_are_refused(self, value):
        """Дата без времени и непонятные строки отвергаются, а не угадываются."""
        with pytest.raises(ValueError):
            parse_schedule(value, NOW)


class TestBounds:
    def test_past_is_refused(self):
        """Момент в прошлом запланировать нельзя."""
        with pytest.raises(ValueError, match="in the past"):
            check_schedule(NOW - timedelta(minutes=1), NOW)

    def test_less_than_ten_seconds_is_refused(self):
        """Ближе 10 секунд Telegram отправляет сразу, поэтому такой момент отвергается."""
        with pytest.raises(ValueError, match="at once"):
            check_schedule(NOW + timedelta(seconds=5), NOW)

    def test_more_than_a_year_is_refused(self):
        """Дальше года Telegram отвечает SCHEDULE_DATE_TOO_LATE; граница проверяется заранее."""
        with pytest.raises(ValueError, match="365 days"):
            check_schedule(NOW + MAX_LEAD + timedelta(minutes=1), NOW)

    def test_inside_the_bounds_passes(self):
        """Момент внутри границ принимается."""
        check_schedule(NOW + timedelta(seconds=11), NOW)
        check_schedule(NOW + MAX_LEAD, NOW)


class TestRefusals:
    @pytest.mark.parametrize(
        ("error", "hint"),
        [
            (ScheduleTooMuchError(None), "tg scheduled list"),
            (ScheduleDateTooLateError(None), "a year"),
            (ScheduleBotNotAllowedError(None), "does not allow"),
        ],
    )
    def test_refusals_of_telegram_are_explained(self, error, hint):
        """Отказы Telegram переводятся в объяснение с тем, что делать дальше."""
        explanation = explain_refusal(error)
        assert explanation is not None
        assert hint in explanation

    def test_other_errors_are_left_alone(self):
        """Прочие ошибки не подменяются: их текст печатается как есть."""
        assert explain_refusal(RuntimeError("boom")) is None


# ---------------------------------------------------------------------------
# tg send --schedule
# ---------------------------------------------------------------------------

FUTURE = datetime.now().astimezone() + timedelta(hours=2)


def _fake_api():
    api = MagicMock()
    api.client = AsyncMock()

    @contextlib.asynccontextmanager
    async def connected(_account):
        yield api, "acc"

    return api, connected


def _run_send(
    api, connected, recipients, text, files=None, as_document=False, schedule: datetime | None = FUTURE
):
    with (
        patch("tg_export.cli.common.connected_api", connected),
        patch.object(cli_common, "_QUIET", True),
    ):
        return asyncio.run(cli_tg._tg_send("acc", recipients, text, files, as_document, schedule=schedule))


def test_text_is_scheduled_for_every_recipient():
    """Текст уходит каждому получателю с одним и тем же моментом отправки."""
    api, connected = _fake_api()
    api.client.send_message.return_value = SimpleNamespace(id=7)

    code = _run_send(api, connected, [101, 202], "привет")

    assert code == 0
    calls = api.client.send_message.await_args_list
    assert [c.args[0] for c in calls] == [101, 202]
    assert all(c.kwargs["schedule"] == FUTURE for c in calls)


def test_files_and_caption_are_scheduled(tmp_path):
    """Документы с подписью планируются все: подпись у первого, момент у каждого."""
    files = []
    for i in range(2):
        f = tmp_path / f"doc{i}.bin"
        f.write_bytes(b"x" * (i + 1))
        files.append(str(f))
    api, connected = _fake_api()

    code = _run_send(api, connected, [101], "подпись", files=files, as_document=True)

    assert code == 0
    calls = api.client.send_file.await_args_list
    assert len(calls) == 2
    assert [c.kwargs["caption"] for c in calls] == ["подпись", ""]
    assert all(c.kwargs["schedule"] == FUTURE for c in calls)


def test_album_is_scheduled_in_one_call(tmp_path):
    """Альбом сжатых фото планируется одним вызовом с моментом отправки."""
    files = []
    for i in range(3):
        f = tmp_path / f"pic{i}.jpg"
        f.write_bytes(b"x")
        files.append(str(f))
    api, connected = _fake_api()

    _run_send(api, connected, [101], None, files=files)

    assert api.client.send_file.await_count == 1
    assert api.client.send_file.await_args.kwargs["schedule"] == FUTURE


def test_without_schedule_nothing_is_scheduled():
    """Без `--schedule` в Telegram уходит schedule=None -- обычная отправка."""
    api, connected = _fake_api()

    _run_send(api, connected, [101], "привет", schedule=None)

    assert api.client.send_message.await_args.kwargs["schedule"] is None


def test_output_says_scheduled_and_when(capsys):
    """Вывод сообщает «запланировано», момент и номер в очереди, а не «sent»."""
    api, connected = _fake_api()
    api.client.send_message.return_value = SimpleNamespace(id=42)

    with patch("tg_export.cli.common.connected_api", connected):
        asyncio.run(cli_tg._tg_send("acc", [101], "привет", None, schedule=FUTURE))

    err = capsys.readouterr().err
    assert f"scheduled to 101 for {describe(FUTURE)} (scheduled id 42)" in err
    assert "sent to" not in err


def test_refusal_is_explained_and_fails_the_run(capsys):
    """Отказ Telegram печатается понятным текстом и даёт код 1."""
    api, connected = _fake_api()
    api.client.send_message.side_effect = ScheduleTooMuchError(None)

    with patch("tg_export.cli.common.connected_api", connected):
        code = asyncio.run(cli_tg._tg_send("acc", [101], "привет", None, schedule=FUTURE))

    assert code == 1
    err = capsys.readouterr().err
    assert "tg scheduled list" in err
    assert "Scheduled for 0/1" in err


def test_moment_that_came_too_close_is_not_sent_at_once():
    """Момент, приблизившийся за время отправки предыдущим, не отправляется сразу, а отвергается."""
    api, connected = _fake_api()
    soon = datetime.now().astimezone() + timedelta(seconds=3)

    code = _run_send(api, connected, [101], "привет", schedule=soon)

    assert code == 1
    api.client.send_message.assert_not_awaited()


def test_cli_refuses_a_past_time_before_connecting():
    """Прошедшее время отвергается при разборе опций, до подключения к Telegram."""
    with patch.object(cli_tg, "_tg_send") as send:
        result = CliRunner().invoke(cli_tg.tg, ["send", "101", "-t", "x", "--schedule", "2001-01-01 10:00"])

    assert result.exit_code == 2
    assert "in the past" in result.output
    send.assert_not_called()


# ---------------------------------------------------------------------------
# tg scheduled list / delete
# ---------------------------------------------------------------------------


def _queued(*ids):
    return [
        SimpleNamespace(
            id=i,
            date=datetime(2030, 6, 1, 9, 0, tzinfo=UTC_ZONE) + timedelta(hours=-i),
            message=f"текст {i}",
            entities=None,
            media=None,
            action=None,
            sender=None,
            sender_id=None,
            reply_to=None,
            out=True,
            file=None,
        )
        for i in ids
    ]


UTC_ZONE = UTC


def _patch_queue(api, messages):
    async def iter_messages(_entity, scheduled=False):
        assert scheduled, "очередь запланированных читается с scheduled=True"
        for m in messages:
            yield m

    api.client.iter_messages = iter_messages


def test_list_shows_the_queue_soonest_first(capsys):
    """Список выводит очередь по возрастанию момента отправки, с номерами для удаления."""
    api, connected = _fake_api()
    _patch_queue(api, _queued(1, 3, 2))
    with (
        patch("tg_export.cli.common.connected_api", connected),
        patch.object(cli_tg, "resolve_chat", AsyncMock(return_value=SimpleNamespace(title="Тестовый чат"))),
    ):
        code = asyncio.run(cli_tg._tg_scheduled_list([101], "acc", 0, False))

    assert code == 0
    out = capsys.readouterr().out
    assert "3 scheduled" in out
    assert out.index("[3]") < out.index("[2]") < out.index("[1]")


def test_list_json_carries_the_moment(capsys):
    """В JSON у каждого сообщения есть момент отправки с поясом."""
    import json

    api, connected = _fake_api()
    _patch_queue(api, _queued(5))
    with (
        patch("tg_export.cli.common.connected_api", connected),
        patch.object(cli_tg, "resolve_chat", AsyncMock(return_value=SimpleNamespace(title="Тестовый чат"))),
    ):
        asyncio.run(cli_tg._tg_scheduled_list([101], "acc", 0, True))

    data = json.loads(capsys.readouterr().out)
    assert data[0]["chat"] == 101
    message = data[0]["messages"][0]
    assert message["id"] == 5
    assert message["text"] == "текст 5"
    assert message["scheduled_for"] == describe(_queued(5)[0].date)


def _run_delete(api, connected, ids, delete_all=False):
    with (
        patch("tg_export.cli.common.connected_api", connected),
        patch.object(cli_tg, "resolve_chat", AsyncMock(return_value="peer")),
    ):
        return asyncio.run(cli_tg._tg_scheduled_delete(101, ids, "acc", delete_all))


def test_delete_removes_the_ids_asked_for():
    """Удаляются ровно названные сообщения очереди."""
    api, connected = _fake_api()
    _patch_queue(api, _queued(1, 2, 3))

    code = _run_delete(api, connected, [3, 1])

    assert code == 0
    request = api.client.await_args.args[0]
    assert request.id == [3, 1]


def test_delete_reports_an_id_the_queue_does_not_hold():
    """Номер, которого нет в очереди, -- ошибка: Telegram промолчал бы, и опечатка сошла бы за удаление."""
    api, connected = _fake_api()
    _patch_queue(api, _queued(1))

    code = _run_delete(api, connected, [1, 9])

    assert code == 1
    assert api.client.await_args.args[0].id == [1]


def test_delete_all_empties_the_queue():
    """`--all` удаляет всю очередь чата."""
    api, connected = _fake_api()
    _patch_queue(api, _queued(4, 2))

    code = _run_delete(api, connected, [], delete_all=True)

    assert code == 0
    assert sorted(api.client.await_args.args[0].id) == [2, 4]


def test_delete_needs_ids_or_all_but_not_both():
    """Нужно либо перечислить номера, либо `--all`, но не то и другое."""
    runner = CliRunner()
    assert runner.invoke(cli_tg.tg, ["scheduled", "delete", "101"]).exit_code == 2
    assert runner.invoke(cli_tg.tg, ["scheduled", "delete", "101", "5", "--all"]).exit_code == 2
