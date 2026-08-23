"""Общее форматирование: размеры, скорость, момент времени."""

from datetime import datetime

from tg_export.format import MOMENT_WITH_SECONDS_FORMAT, format_moment


def test_a_moment_reads_the_same_whatever_shape_it_arrives_in():
    """Telegram отдаёт одно и то же поле то datetime, то unix-меткой.

    Ветка, различавшая эти формы, была скопирована по местам вызова: сессии
    приложений и веб-сессии разбирали `date_active` двумя одинаковыми блоками.
    """
    moment = datetime(2024, 5, 17, 8, 30, 15)

    assert format_moment(moment) == "2024-05-17 08:30"
    assert format_moment(moment.timestamp()) == "2024-05-17 08:30"
    assert format_moment(moment, fmt=MOMENT_WITH_SECONDS_FORMAT) == "2024-05-17 08:30:15"


def test_a_missing_moment_gives_the_caller_its_own_placeholder():
    """Список сообщений печатает `?`, таблица сессий -- пустую ячейку."""
    assert format_moment(None) == ""
    assert format_moment(0) == ""
    assert format_moment(None, missing="?") == "?"
