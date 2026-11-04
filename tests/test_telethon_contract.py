"""Страховка на предположения о Telethon, которые не проверяет ни импорт, ни типизация.

Конвертер развязан с Telethon намеренно: сравнение `__class__.__name__` со
строкой не тянет за собой импорт TL-классов. Цена развязки в том, что
переименование класса в Telethon не даёт ни ImportError, ни ошибки pyright —
сообщение молча превращается в `unknown`. Тесты конвертера этого не заметят:
они оперируют теми же строками. Сверка со списком реальных классов —
единственное место, где расхождение видно.

То же и для `sessions`: обход дефекта Telethon читает и пишет колонки по
позиции, поэтому изменение их порядка, состава или версии схемы затрёт
значения молча.
"""

from __future__ import annotations

import ast
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import telethon.tl.types as tl_types
from telethon.sessions import SQLiteSession

import tg_export.models as models

CONVERTER = Path(__file__).resolve().parent.parent / "tg_export" / "converter.py"

# Строки конвертера, которые выглядят как имя TL-класса, но им не являются:
# фрагменты для str.replace при нормализации имени действия и подстановка для
# пользователя без имени.
NOT_TL_CLASS_NAMES = {"Action", "MessageAction", "Unknown"}

# Кнопки клавиатуры Telethon описывает по-разному до и после слоя 229
# (Telethon 1.45): прежде вид кнопки задавал её собственный класс, теперь — класс
# поля `type` у KeyboardButton или KeyboardInlineButton. Конвертер разбирает обе
# формы, пока pyproject допускает Telethon ниже 1.45, но в установленном Telethon
# есть классы только одной из них. Снимки ниже перечисляют классы каждого
# поколения, на которые опирается конвертер: имя из поколения, которого нет в
# установленном Telethon, проверяется по снимку, а сам снимок текущего поколения —
# по telethon.tl.types.
BUTTON_CLASSES_BEFORE_LAYER_229 = frozenset(
    {
        "KeyboardButtonUrl",
        "KeyboardButtonCallback",
        "KeyboardButtonGame",
        "KeyboardButtonBuy",
        "KeyboardButtonSwitchInline",
        "KeyboardButtonWebView",
        "KeyboardButtonSimpleWebView",
        "KeyboardButtonUserProfile",
        "KeyboardButtonRequestPhone",
        "KeyboardButtonRequestGeoLocation",
        "KeyboardButtonRequestPoll",
        "KeyboardButtonRequestPeer",
        "KeyboardButtonCopy",
    }
)
BUTTON_CLASSES_SINCE_LAYER_229 = frozenset(
    {
        "KeyboardInlineButton",
        "InlineButtonTypeUrl",
        "InlineButtonTypeCallback",
        "InlineButtonTypeGame",
        "InlineButtonTypeBuy",
        "InlineButtonTypeSwitchInline",
        "InlineButtonTypeWebView",
        "ButtonTypeSimpleWebView",
        "InlineButtonTypeUserProfile",
        "ButtonTypeRequestPhone",
        "ButtonTypeRequestGeoLocation",
        "ButtonTypeRequestPoll",
        "ButtonTypeRequestPeer",
        "InlineButtonTypeCopy",
    }
)


def _button_generations() -> tuple[frozenset[str], frozenset[str]]:
    """Снимки кнопок установленного Telethon и другого поколения — в этом порядке."""
    if hasattr(tl_types, "KeyboardInlineButton"):
        return BUTTON_CLASSES_SINCE_LAYER_229, BUTTON_CLASSES_BEFORE_LAYER_229
    return BUTTON_CLASSES_BEFORE_LAYER_229, BUTTON_CLASSES_SINCE_LAYER_229


# Ожидаемый порядок колонок в таблице sessions. Telethon пишет строку
# позиционно (`insert or replace into sessions values (?,?,?,?,?,?)`), поэтому
# смысл несёт позиция, а не имя; tg_export.session спасает две последние.
EXPECTED_SESSION_COLUMNS = [
    "dc_id",
    "server_address",
    "port",
    "auth_key",
    "takeout_id",
    "tmp_auth_key",
]
EXPECTED_SESSION_VERSION = 8


def _camel_case_literals(path: Path) -> set[str]:
    """Собрать из модуля строковые литералы, похожие на имя TL-класса."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and re.fullmatch(r"[A-Z][A-Za-z0-9]+", node.value)
    }


def test_converter_dispatches_on_class_names_that_telethon_still_has():
    """Каждое имя класса, по которому ветвится конвертер, есть в telethon.tl.types.

    Исключение — классы кнопок другого поколения Telethon: их в установленной
    версии нет, и они сверяются со снимком этого поколения.
    """
    _, other_generation = _button_generations()
    unknown = sorted(
        name
        for name in _camel_case_literals(CONVERTER)
        if not hasattr(tl_types, name)
        and not hasattr(models, name)
        and name not in NOT_TL_CLASS_NAMES
        and name not in other_generation
    )
    assert not unknown, (
        "converter.py ветвится по именам классов, которых нет ни в telethon.tl.types, "
        f"ни в tg_export.models: {unknown}. Класс переименован в Telethon либо строка набрана с опечаткой — "
        "и то, и другое молча уводит разбор в ветку unknown."
    )


def test_button_snapshots_split_by_the_installed_telethon():
    """Снимок текущего поколения кнопок есть в telethon.tl.types целиком, другого — отсутствует целиком.

    Иначе снимок устарел: часть классов вернулась или исчезла, и исключение для
    другого поколения в сверке выше стало бы прикрывать настоящую опечатку.
    """
    installed, other = _button_generations()
    missing = sorted(name for name in installed if not hasattr(tl_types, name))
    present = sorted(name for name in other if hasattr(tl_types, name))
    assert not missing, f"Классов кнопок текущего поколения нет в telethon.tl.types: {missing}."
    assert not present, f"Классы кнопок другого поколения есть в telethon.tl.types: {present}."


def test_converter_handles_every_button_class_of_both_generations():
    """Конвертер ветвится по каждому классу кнопок из обоих снимков."""
    literals = _camel_case_literals(CONVERTER)
    absent = sorted((BUTTON_CLASSES_BEFORE_LAYER_229 | BUTTON_CLASSES_SINCE_LAYER_229) - literals)
    assert not absent, f"converter.py не разбирает классы кнопок: {absent}."


def test_session_table_layout_matches_what_the_workaround_assumes(tmp_path):
    """Telethon по-прежнему создаёт sessions с шестью колонками в известном порядке."""
    session_path = tmp_path / "layout-probe.session"
    session = SQLiteSession(str(session_path))
    try:
        session.save()
    finally:
        session.close()

    conn = sqlite3.connect(str(session_path))
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(sessions)")]
        version = conn.execute("SELECT version FROM version").fetchone()[0]
    finally:
        conn.close()

    assert columns == EXPECTED_SESSION_COLUMNS, (
        f"Состав или порядок колонок sessions изменился: {columns}. "
        "tg_export.session читает и пишет позиции 5 и 6, поэтому такой сдвиг затирает takeout_id молча."
    )
    assert version == EXPECTED_SESSION_VERSION, (
        f"Версия схемы сессии выросла до {version}. Проверить, что _upgrade_database не переставил колонки, "
        "и обновить константы в tg_export/session.py."
    )


def test_media_subdir_pattern_does_not_depend_on_the_hash_seed():
    """Порядок альтернатив в _HREF_RE одинаков при разных PYTHONHASHSEED."""
    patterns = []
    for seed in ("0", "1", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        result = subprocess.run(
            [sys.executable, "-c", "import tg_export.importer as m; print(m._HREF_RE.pattern)"],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        patterns.append(result.stdout.strip())

    assert len(set(patterns)) == 1, (
        f"_HREF_RE зависит от рандомизации хэша строк: {patterns}. "
        "Разбор tdesktop-экспорта стал бы воспроизводимым не при каждом запуске."
    )


def test_the_flood_wait_notice_keeps_the_shape_the_filter_reads():
    """Запись telethon о сне на flood wait разбирается фильтром пакета.

    Пауза, которую Telethon выдерживает внутри себя, не доходит до кода
    пакета ни исключением, ни возвратом: единственный её след -- строка
    журнала. Фильтр читает не готовый текст, а неотформатированное сообщение
    и его аргументы, поэтому смысл несёт их порядок. Переименование метода,
    перестановка аргументов или другая формулировка молча вернули бы паузу в
    прежнюю невидимость -- отсюда сверка с живым Telethon.
    """
    import logging

    from telethon.client.users import _fmt_flood

    from tg_export.waits import FLOOD_WAIT_LOGGER, flood_wait_of

    assert _fmt_flood.__module__ == FLOOD_WAIT_LOGGER, (
        f"Сон на flood wait логируется модулем {_fmt_flood.__module__}, а фильтр "
        f"поставлен на {FLOOD_WAIT_LOGGER}: записи до доски ожиданий не дойдут."
    )

    class GetHistoryRequest:
        pass

    msg, *args = _fmt_flood(18, GetHistoryRequest())
    record = logging.LogRecord(
        name=FLOOD_WAIT_LOGGER,
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=tuple(args),
        exc_info=None,
    )
    wait = flood_wait_of(record)
    assert wait is not None, (
        f"Фильтр не узнаёт запись telethon о сне: {record.getMessage()!r}. "
        "Пауза перестанет быть видимой при штатном запуске."
    )
    assert wait.seconds == 18, f"Длительность паузы прочитана как {wait.seconds}, а названа была 18."
    assert wait.what == "GetHistoryRequest", f"Имя запроса прочитано как {wait.what!r}."
