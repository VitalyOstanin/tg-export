"""Ссылки внутри документации обязаны вести туда, где что-то есть.

Документации в репозитории около девятисот строк, и она перекрёстно ссылается
сама на себя: оглавления, разделы README, справочник команд, ADR. Переименование
заголовка или файла ломает ссылку молча -- проверки markdown в CI нет, а
читатель находит обрыв уже на опубликованной странице.

Проверяются только файлы под контролем версий: рабочее дерево может содержать
выгрузки и черновики, к репозиторию не относящиеся.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HEADING_RE = re.compile(r"^#{1,6}\s+(.*?)\s*$", re.M)
FENCE_RE = re.compile(r"^```.*?^```", re.M | re.S)


def _tracked_markdown() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.md"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    return [ROOT / name for name in out]


def _slug(title: str) -> str:
    """Якорь GitHub: разметка снята, регистр опущен, пробелы -- дефисы.

    Кириллица в якорях сохраняется как есть, поэтому `\\w` берётся в
    юникод-режиме, а не как латиница.
    """
    title = re.sub(r"`([^`]*)`", r"\1", title)
    title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", title)
    title = title.replace("\\", "").strip().lower()
    title = re.sub(r"[^\w\s-]", "", title, flags=re.UNICODE)
    return re.sub(r"\s+", "-", title)


def _anchors(source: str) -> set[str]:
    """Якоря файла, включая суффиксы -1, -2 у повторяющихся заголовков."""
    counts: dict[str, int] = {}
    for title in HEADING_RE.findall(source):
        key = _slug(title)
        counts[key] = counts.get(key, 0) + 1
    anchors = set()
    for key, times in counts.items():
        anchors.add(key)
        anchors.update(f"{key}-{i}" for i in range(1, times))
    return anchors


def test_every_documentation_link_resolves():
    broken: list[str] = []
    for path in _tracked_markdown():
        source = path.read_text(encoding="utf-8")
        # Ссылки внутри блоков кода -- примеры, а не навигация.
        source_without_code = FENCE_RE.sub("", source)
        anchors = _anchors(source)
        rel = path.relative_to(ROOT)
        for _, target in LINK_RE.findall(source_without_code):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if target.startswith("#"):
                if target[1:] not in anchors:
                    broken.append(f"{rel}: якорь {target} не соответствует ни одному заголовку")
                continue
            file_part, _, fragment = target.partition("#")
            destination = path.parent / file_part
            if not destination.exists():
                broken.append(f"{rel}: {target} -- такого файла нет")
                continue
            if (
                fragment
                and destination.suffix == ".md"
                and fragment not in _anchors(destination.read_text(encoding="utf-8"))
            ):
                broken.append(f"{rel}: {target} -- в файле нет такого заголовка")

    assert not broken, "оборванные ссылки в документации:\n" + "\n".join(broken)
