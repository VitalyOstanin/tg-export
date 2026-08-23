"""Примеры конфигурации из документации обязаны загружаться загрузчиком.

Читатель копирует блок `yaml` из README или справочника к себе в конфиг, и
единственное, что стоит между примером и рабочим файлом, -- совпадение имён
ключей. Ключ, которого загрузчик не знает, отвергается целиком, поэтому
устаревший пример не «частично работает», а не даёт запустить экспорт вовсе.

Пример относится к одному из двух файлов -- глобальному `config.yaml` или
конфигу аккаунта `<alias>.yaml`, -- и принадлежность определяется по именам
ключей верхнего уровня: наборы этих имён у файлов не пересекаются. Блок, ни
один ключ которого ни одному файлу не принадлежит, конфигом не считается.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

from tg_export.auth import _KNOWN_GLOBAL_KEYS, AccountManager
from tg_export.config import _KNOWN_TOP_LEVEL_KEYS, load_config

ROOT = Path(__file__).resolve().parent.parent

YAML_BLOCK_RE = re.compile(r"```yaml\n(.*?)```", re.S)


def _config_examples() -> list[tuple[str, str]]:
    """Собрать примеры конфигурации из отслеживаемых .md с их местом в файле."""
    tracked = subprocess.run(
        ["git", "ls-files", "*.md"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()

    examples: list[tuple[str, str]] = []
    for name in tracked:
        text = (ROOT / name).read_text(encoding="utf-8")
        for match in YAML_BLOCK_RE.finditer(text):
            block = match.group(1)
            try:
                data = yaml.safe_load(block)
            except yaml.YAMLError:
                # Разбор YAML проверяет сам загрузчик -- пример дойдёт до него.
                examples.append((f"{name}:{text[: match.start()].count(chr(10)) + 1}", block))
                continue
            if not isinstance(data, dict):
                continue
            keys = set(data)
            if not keys & (_KNOWN_TOP_LEVEL_KEYS | _KNOWN_GLOBAL_KEYS):
                continue
            examples.append((f"{name}:{text[: match.start()].count(chr(10)) + 1}", block))
    return examples


EXAMPLES = _config_examples()


def test_the_documentation_shows_at_least_one_config_example():
    """Пустой список примеров сделал бы проверку ниже бессодержательной."""
    assert EXAMPLES, "в документации не найдено ни одного примера конфигурации"


@pytest.mark.parametrize(("where", "block"), EXAMPLES, ids=[where for where, _ in EXAMPLES])
def test_a_config_example_from_the_documentation_loads(where: str, block: str, tmp_path: Path):
    """Пример из документации проходит тот же загрузчик, что и файл пользователя."""
    data = yaml.safe_load(block)
    keys = set(data) if isinstance(data, dict) else set()

    if keys and keys <= _KNOWN_GLOBAL_KEYS:
        config_dir = tmp_path / "tg-export"
        config_dir.mkdir(parents=True)
        (config_dir / "config.yaml").write_text(block, encoding="utf-8")
        mgr = AccountManager(config_dir=config_dir)
        mgr.load_global_config()
        mgr.load_proxy()
        mgr.load_min_free_space()
        return

    path = tmp_path / "account.yaml"
    path.write_text(block, encoding="utf-8")
    load_config(path)
