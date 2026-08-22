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
    """Каждое имя класса, по которому ветвится конвертер, есть в telethon.tl.types."""
    unknown = sorted(
        name
        for name in _camel_case_literals(CONVERTER)
        if not hasattr(tl_types, name) and not hasattr(models, name) and name not in NOT_TL_CLASS_NAMES
    )
    assert not unknown, (
        "converter.py ветвится по именам классов, которых нет ни в telethon.tl.types, "
        f"ни в tg_export.models: {unknown}. Класс переименован в Telethon либо строка набрана с опечаткой — "
        "и то, и другое молча уводит разбор в ветку unknown."
    )


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
