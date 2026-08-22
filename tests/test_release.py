"""Гейт релиза: согласованность тега, версии манифеста и CHANGELOG.

Единственная проверка CHANGELOG стояла ниже публикации на PyPI: при отсутствующем
разделе версия уходила в индекс необратимо, GitHub Release не создавался, а
workflow краснел уже после. Сверки «тег = версия манифеста» не было вовсе.
"""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "release_notes.py"


def _run(tag, tmp_path, *, pyproject_version="1.5.1", changelog=None):
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "tg-export"\nversion = "{pyproject_version}"\n', encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        changelog
        if changelog is not None
        else "# Changelog\n\n## [1.5.1] -- 2026-07-30\n\n### Исправлено\n\n- Что-то починено.\n\n## [1.5.0] -- 2026-07-30\n\n- Старое.\n",
        encoding="utf-8",
    )
    out = tmp_path / "release_notes.md"
    return subprocess.run(
        [sys.executable, str(SCRIPT), tag, "--root", str(tmp_path), "--output", str(out)],
        capture_output=True,
        text=True,
    ), out


def test_release_notes_are_extracted_for_a_matching_tag(tmp_path):
    result, out = _run("v1.5.1", tmp_path)

    assert result.returncode == 0, result.stderr
    notes = out.read_text(encoding="utf-8")
    assert "tg-export v1.5.1 -- 2026-07-30" in notes
    assert "Что-то починено" in notes
    assert "Старое" not in notes, "в notes попал раздел предыдущей версии"


def test_a_tag_that_does_not_match_the_manifest_stops_the_release(tmp_path):
    """Тег без бампа версии давал сборку прежней версии и отказ PyPI по дублю."""
    result, _ = _run("v1.6.0", tmp_path, pyproject_version="1.5.1")

    assert result.returncode != 0
    assert "1.6.0" in result.stderr and "1.5.1" in result.stderr


def test_a_missing_changelog_section_stops_the_release(tmp_path):
    """Раньше это выяснялось после публикации на PyPI, откатить которую нельзя."""
    result, _ = _run("v1.5.1", tmp_path, changelog="# Changelog\n\n## [1.5.0] -- 2026-07-30\n\n- Старое.\n")

    assert result.returncode != 0
    assert "CHANGELOG" in result.stderr


def test_an_empty_changelog_section_stops_the_release(tmp_path):
    """Пустой раздел дал бы релиз без описания -- это тоже отказ, а не notes."""
    result, _ = _run(
        "v1.5.1",
        tmp_path,
        changelog="# Changelog\n\n## [1.5.1] -- 2026-07-30\n\n## [1.5.0] -- 2026-07-30\n\n- Старое.\n",
    )

    assert result.returncode != 0


def test_a_tag_without_the_v_prefix_is_accepted(tmp_path):
    """Имя тега приходит из workflow как есть; префикс -- дело конвенции."""
    result, _ = _run("1.5.1", tmp_path)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("step", ["uv lock --check", "ruff check", "pyright", "release_notes.py"])
def test_the_publish_workflow_runs_the_release_gate_before_publishing(step):
    """Проверки, заявленные в RELEASING.md, шли только в ci.yml, который на тег не срабатывает."""
    workflow = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "publish.yml").read_text(
        encoding="utf-8"
    )
    assert step in workflow, f"шаг {step!r} отсутствует в publish.yml"
    assert workflow.index(step) < workflow.index("gh-action-pypi-publish"), (
        f"шаг {step!r} стоит после необратимой публикации на PyPI"
    )
