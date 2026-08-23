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


BREAKING = (
    "# Changelog\n\n## [{version}] -- 2026-08-23\n\n"
    "### Ломающие изменения\n\n- Каталог выгрузки переехал.\n\n"
    "## [1.5.1] -- 2026-07-30\n\n- Старое.\n"
)

FEATURE = (
    "# Changelog\n\n## [{version}] -- 2026-08-23\n\n"
    "### Добавлено\n\n- Новая опция.\n\n"
    "## [1.5.1] -- 2026-07-30\n\n- Старое.\n"
)


def test_a_breaking_section_without_a_major_bump_stops_the_release(tmp_path):
    """Смена контракта под номером 1.6.0 доходит до пользователя без предупреждения."""
    result, _ = _run(
        "v1.6.0", tmp_path, pyproject_version="1.6.0", changelog=BREAKING.format(version="1.6.0")
    )

    assert result.returncode != 0
    assert "major" in result.stderr, result.stderr
    assert "2.0.0" in result.stderr, result.stderr


def test_a_breaking_section_passes_with_a_major_bump(tmp_path):
    result, _ = _run(
        "v2.0.0", tmp_path, pyproject_version="2.0.0", changelog=BREAKING.format(version="2.0.0")
    )

    assert result.returncode == 0, result.stderr


def test_new_features_under_a_patch_bump_stop_the_release(tmp_path):
    """«Добавлено» -- это minor по SemVer; patch-номер скрывает новую функциональность."""
    result, _ = _run("v1.5.2", tmp_path, pyproject_version="1.5.2", changelog=FEATURE.format(version="1.5.2"))

    assert result.returncode != 0
    assert "1.6.0" in result.stderr, result.stderr


def test_fixes_alone_pass_as_a_patch(tmp_path):
    result, _ = _run(
        "v1.5.2",
        tmp_path,
        pyproject_version="1.5.2",
        changelog=(
            "# Changelog\n\n## [1.5.2] -- 2026-08-23\n\n### Исправлено\n\n- Починено.\n\n"
            "## [1.5.1] -- 2026-07-30\n\n- Старое.\n"
        ),
    )

    assert result.returncode == 0, result.stderr


def test_the_first_release_has_nothing_to_compare_with(tmp_path):
    """Единственный раздел в файле -- предыдущей версии нет, сверять размер не с чем."""
    result, _ = _run(
        "v1.0.0",
        tmp_path,
        pyproject_version="1.0.0",
        changelog="# Changelog\n\n## [1.0.0] -- 2026-08-23\n\n### Добавлено\n\n- Всё.\n",
    )

    assert result.returncode == 0, result.stderr


def test_a_tag_without_the_v_prefix_is_accepted(tmp_path):
    """Имя тега приходит из workflow как есть; префикс -- дело конвенции."""
    result, _ = _run("1.5.1", tmp_path)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "step",
    ["lock", "ruff check", "ruff format", "pyright", "pytest", "coverage_gate.py", "release_notes.py"],
)
def test_the_publish_workflow_runs_the_release_gate_before_publishing(step):
    """Проверки, заявленные в RELEASING.md, шли только в ci.yml, который на тег не срабатывает.

    Сверяются исполняемые команды, а не текст файла: по подстроке три проверки
    из четырёх удовлетворялись комментарием над шагом, и подмена
    `scripts/check.sh` на один `pytest` -- то есть снятие линтера, типов и
    сверки lock перед необратимой публикацией -- оставляла тест зелёным.
    """
    from parity import publish_commands_before

    executed = publish_commands_before("gh-action-pypi-publish")

    assert step in executed, f"проверка {step!r} не выполняется до публикации на PyPI: {executed}"


def test_declared_python_versions_match_the_ci_matrix():
    """Классификаторы обещают ровно те версии Python, на которых идут тесты.

    `requires-python = ">=3.11"` без верхней границы обещает и те версии, которых
    ещё нет; классификаторы -- единственное место, где обещание конкретно, и
    расходиться с матрицей CI оно не должно: несовместимость обнаружилась бы у
    пользователя, а не на прогоне.
    """
    import re
    import tomllib

    root = Path(__file__).resolve().parent.parent
    manifest = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    declared = {
        c.rsplit(" :: ", 1)[1]
        for c in manifest["project"]["classifiers"]
        if c.startswith("Programming Language :: Python :: 3.")
    }

    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    match = re.search(r"python-version:\s*\[([^\]]*)\]", workflow)
    assert match, "в ci.yml не найдена матрица python-version"
    tested = set(re.findall(r'"([^"]+)"', match.group(1)))

    assert declared == tested, f"классификаторы обещают {sorted(declared)}, а CI проверяет {sorted(tested)}"


def test_the_publishing_job_runs_no_project_code():
    """Право обменять OIDC-токен есть у каждого шага job'а, а не только у публикующего.

    GitHub кладёт ACTIONS_ID_TOKEN_REQUEST_URL и ACTIONS_ID_TOKEN_REQUEST_TOKEN
    в окружение всех шагов job'а с `id-token: write`, поэтому получить
    upload-token PyPI может любой из них. Пока установка зависимостей, тесты и
    сборка шли в том же job'е, компрометация зависимости из uv.lock или правка
    тестов, попавшая в тег, давали не сломанный релиз, а выпуск произвольного
    пакета от имени проекта -- необратимый, отката у PyPI нет.
    """
    import yaml

    root = Path(__file__).resolve().parent.parent
    workflow = yaml.safe_load((root / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8"))

    publishing = [
        (name, job)
        for name, job in workflow["jobs"].items()
        if any("gh-action-pypi-publish" in (step.get("uses") or "") for step in job["steps"])
    ]
    assert len(publishing) == 1, f"публикующих job'ов должно быть ровно один: {[n for n, _ in publishing]}"

    name, job = publishing[0]
    runs = [(step.get("run") or "").strip() for step in job["steps"]]
    assert not [r for r in runs if r], f"в job '{name}' выполняется код проекта: {runs}"

    for other, other_job in workflow["jobs"].items():
        if other == name:
            continue
        assert "id-token" not in (other_job.get("permissions") or {}), (
            f"job '{other}' выполняет код проекта и при этом может получить токен публикации"
        )
