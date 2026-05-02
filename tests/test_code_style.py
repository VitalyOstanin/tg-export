"""Статические проверки стиля по исходникам.

Покрывают замечания из code review, для которых поведенческого теста недостаточно
(см. tmp/code-review-2026-05-02.md).
"""
from __future__ import annotations

import re
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent / "tg_export"


def _read(name: str) -> str:
    return (PROJECT / name).read_text(encoding="utf-8")


def test_auth_uses_click_echo_not_bare_print():
    """auth.py не должен вызывать bare print(); используй click.echo."""
    src = _read("auth.py")
    matches = []
    for ln_no, line in enumerate(src.splitlines(), start=1):
        # bare print(, не console.print, не click.print и т.п.
        if re.search(r"(?<![\w.])print\(", line):
            matches.append((ln_no, line.strip()))
    assert not matches, f"auth.py содержит bare print(): {matches!r}"


def test_log_function_uses_rich_console_not_bare_print():
    """exporter._log не должен использовать bare print, чтобы не зависеть от состояния Live."""
    src = _read("exporter.py")
    m = re.search(
        r"^def _log\([^)]*\)[^\n]*:\n(?:\s+\".*?\"\"\"\n)?(?P<body>(?:    [^\n]*\n)+)",
        src, flags=re.MULTILINE | re.DOTALL,
    )
    if m:
        body = m.group("body")
        # bare print( без префикса (например, console.print)
        assert not re.search(r"(?<![\w.])print\(", body), (
            "_log не должен использовать bare print(); используй console.print(..., markup=False)"
        )


def test_no_console_log_calls_in_exporter():
    """Унификация: console.log смешан с console.print. Используем только console.print."""
    src = _read("exporter.py")
    matches = re.findall(r"console\.log\(", src)
    assert not matches, f"console.log() не должен использоваться: {len(matches)} вхождений"


def test_no_manual_live_enter_exit_in_exporter():
    """Live должен использоваться через with-блок, а не ручным __enter__/__exit__."""
    src = _read("exporter.py")
    assert "live_ctx.__enter__" not in src, "Live должен использоваться через with-блок"
    assert "live_ctx.__exit__" not in src, "Live должен использоваться через with-блок"


def test_strip_markup_function_removed():
    """_strip_markup дублирует Text.from_markup(s).plain и не нужен после рефактора _log."""
    src = _read("exporter.py")
    assert "def _strip_markup" not in src, (
        "_strip_markup должен быть удалён -- используй console.print(markup=False) или "
        "rich.text.Text.from_markup(s).plain"
    )


def test_logger_declared_after_all_module_imports():
    """logger = logging.getLogger должен идти после блока импортов (PEP 8)."""
    src = _read("exporter.py")
    lines = src.splitlines()

    logger_idx = next(
        (i for i, ln in enumerate(lines)
         if re.match(r"^logger\s*=\s*logging\.getLogger", ln)),
        None,
    )
    assert logger_idx is not None, "logger не объявлен"

    after = lines[logger_idx + 1:]
    bad_imports = [
        (logger_idx + 1 + i, ln)
        for i, ln in enumerate(after)
        if re.match(r"^(import|from)\s+\w", ln)
    ]
    assert not bad_imports, (
        f"после logger=... идут module-level импорты (PEP 8): {bad_imports!r}"
    )


def test_timedelta_imported_at_module_level_in_exporter():
    """from datetime import timedelta должен быть на уровне модуля, не внутри функции."""
    src = _read("exporter.py")
    inside_func = re.findall(
        r"^[ \t]+from datetime import timedelta",
        src, flags=re.MULTILINE,
    )
    assert not inside_func, (
        "from datetime import timedelta должен быть на уровне модуля, не внутри функции"
    )


def test_exporter_imports_rich_escape():
    """exporter должен импортировать rich.markup.escape, чтобы экранировать пользовательский ввод."""
    src = _read("exporter.py")
    assert re.search(r"from rich\.markup import\b.*\bescape\b", src), (
        "exporter.py должен импортировать escape из rich.markup для эскейпа имён чатов/файлов"
    )
