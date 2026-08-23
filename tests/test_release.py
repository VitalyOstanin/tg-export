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


def _run(tag, tmp_path, *, pyproject_version="1.5.1", changelog=None, extra=(), output=True):
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
    argv = [sys.executable, str(SCRIPT), tag, "--root", str(tmp_path)]
    if output:
        argv += ["--output", str(out)]
    argv += list(extra)
    return subprocess.run(argv, capture_output=True, text=True, cwd=tmp_path), out


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


def test_a_section_dated_far_from_the_tag_stops_the_release(tmp_path):
    """Дата, скопированная из прошлого раздела, проходила проверку формы и уезжала в release notes.

    Скрипт требовал лишь вида YYYY-MM-DD, поэтому переименование «Не выпущено»
    с оставленной датой предыдущего релиза давало заголовок release notes с
    чужим днём, и отличить это от двух релизов за сутки по файлу нельзя.
    """
    result, _ = _run("v1.5.1", tmp_path, extra=["--tag-date", "2026-08-23"])

    assert result.returncode != 0
    assert "2026-07-30" in result.stderr and "2026-08-23" in result.stderr, result.stderr


def test_a_day_of_slack_is_allowed_between_the_section_and_the_tag(tmp_path):
    """Раздел пишут накануне, тег ставят утром -- расхождение в сутки штатное."""
    result, _ = _run("v1.5.1", tmp_path, extra=["--tag-date", "2026-07-31"])

    assert result.returncode == 0, result.stderr


def test_without_an_output_path_the_notes_go_to_stdout_and_leave_no_file(tmp_path):
    """Умолчание писало release_notes.md в текущий каталог, а `.gitignore` его не знал.

    Повторная локальная проверка гейта без флага оставляла файл в корне
    рабочего дерева, где его подхватывал `git add -A` релизного коммита.
    """
    result, _ = _run("v1.5.1", tmp_path, output=False)

    assert result.returncode == 0, result.stderr
    assert "Что-то починено" in result.stdout
    assert not (tmp_path / "release_notes.md").exists(), "гейт создал файл без явного --output"


def test_the_release_notes_file_is_ignored_by_git():
    """`publish.yml` пишет его в рабочее дерево, и то же имя напрашивается локально."""
    gitignore = (Path(__file__).resolve().parent.parent / ".gitignore").read_text(encoding="utf-8")

    assert "release_notes.md" in gitignore.split(), gitignore


def test_the_publishing_job_names_an_environment():
    """Без окружения удостоверение OIDC выпускается любому прогону этого workflow.

    Окружение сужает доверие на стороне PyPI (там его имя указывается у
    доверенного издателя) и даёт место, куда можно повесить ручное
    подтверждение перед необратимой загрузкой.
    """
    import yaml

    root = Path(__file__).resolve().parent.parent
    workflow = yaml.safe_load((root / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8"))

    publishing = [
        job
        for job in workflow["jobs"].values()
        if any("gh-action-pypi-publish" in (step.get("uses") or "") for step in job["steps"])
    ]
    assert len(publishing) == 1

    environment = publishing[0].get("environment")
    assert environment, "job публикации не объявляет environment"


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


def test_the_build_backend_floor_excludes_versions_with_advisories():
    """`[build-system].requires` определяет, чем соберётся пакет без сети и с закреплённым индексом.

    У setuptools ниже 83.0.0 три записи в OSV, из них две HIGH (command
    injection через URL пакета, path traversal в `PackageIndex.download`), а
    третья касается ровно сборки sdist: нормализация Unicode обходит
    исключения `MANIFEST.in`, а сборка идёт из рабочего дерева, где рядом
    лежат выгрузка и файлы покрытия. Границы jinja2 и click подняты по тому же
    поводу, а этой такого обращения не досталось.
    """
    import re
    import tomllib

    root = Path(__file__).resolve().parent.parent
    manifest = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    requires = manifest["build-system"]["requires"]

    floors = {}
    for spec in requires:
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)>=([0-9.]+)", spec)
        assert match, f"граница {spec!r} записана не как <пакет>>=<версия>"
        floors[match.group(1)] = tuple(int(p) for p in match.group(2).split("."))

    assert floors.get("setuptools", (0,)) >= (83, 0, 0), f"нижняя граница setuptools: {floors}"


def test_the_publishing_workflow_pins_the_version_of_uv():
    """SHA-пин закрепляет загрузчик, но не сам uv.

    В job'е публикации uv собирает колесо рядом с правом выпуска в PyPI, то
    есть на необратимом пути: `version: "latest"` оставляет самую
    привилегированную часть цепочки незакреплённой. В ci.yml `latest` -- это
    решение (ранняя ловля несовместимостей), и оно записано комментарием.
    """
    import re

    import yaml

    root = Path(__file__).resolve().parent.parent
    workflow = yaml.safe_load((root / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8"))

    versions = [
        step.get("with", {}).get("version")
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "astral-sh/setup-uv@" in str(step.get("uses", ""))
    ]

    assert versions, "в publish.yml не найден шаг setup-uv"
    for value in versions:
        assert value and re.fullmatch(r"\d+\.\d+\.\d+", value), f"версия uv не закреплена: {value!r}"


def test_bad_arguments_keep_the_exit_code_of_argparse(tmp_path):
    """Обёртка над SystemExit переписывала любой код отказа в 1.

    Запуск без тега -- отказ argparse с кодом 2 и строкой usage; обёртка
    печатала его как `Error: 2` и завершала скрипт кодом 1, то есть код
    «неверные аргументы» было не отличить от «гейт не пропустил релиз».
    """
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)

    assert result.returncode == 2, result.stderr
    assert "usage" in result.stderr.lower(), result.stderr


def test_the_changelog_index_lists_every_version_section():
    """Оглавление CHANGELOG ведётся руками и в шагах релиза не упоминалось.

    Забытая строка даёт файл, оглавление которого не показывает свежий релиз,
    и замечают это при следующем чтении файла целиком.
    """
    import re

    changelog = (Path(__file__).resolve().parent.parent / "CHANGELOG.md").read_text(encoding="utf-8")
    index, _, body = changelog.partition("\n## [")
    body = "## [" + body

    headings = re.findall(r"^## \[([^\]]+)\]", body, re.M)
    listed = re.findall(r"^- \[\\?\[([^\]\\]+)\\?\]", index, re.M)

    assert headings, "в CHANGELOG нет ни одного раздела версии"
    assert listed == headings, f"оглавление и разделы разошлись: {listed} != {headings}"


def test_a_number_that_is_not_a_step_up_is_named_as_such(tmp_path):
    """Отказ строился так, будто до него доходят только minor и major.

    Обе тернарные ветки выбирали «минорный» вариант и при `patch`: сообщение
    называло раздел «Добавлено», которого в заметках нет, в одной фразе
    требовало patch и «1.3.0 или позже», а настоящая причина -- нижняя секция
    с тем же номером -- не упоминалась вовсе.
    """
    changelog = (
        "# Changelog\n\n## [1.2.3] -- 2026-07-30\n\n### Исправлено\n\n- Починено.\n\n"
        "## [1.2.3] -- 2026-07-29\n\n### Исправлено\n\n- Старое.\n"
    )
    result, _ = _run("v1.2.3", tmp_path, pyproject_version="1.2.3", changelog=changelog)

    assert result.returncode != 0, result.stdout
    message = result.stdout + result.stderr
    assert "Добавлено" not in message, message
    assert "1.3.0" not in message, message
    assert "1.2.3" in message


def test_a_missing_bump_names_the_section_that_asks_for_it(tmp_path):
    """Заметки с разделом «Добавлено» требуют minor, и отказ обязан это назвать."""
    changelog = (
        "# Changelog\n\n## [1.2.4] -- 2026-07-30\n\n### Добавлено\n\n- Новое.\n\n"
        "## [1.2.3] -- 2026-07-29\n\n### Исправлено\n\n- Старое.\n"
    )
    result, _ = _run("v1.2.4", tmp_path, pyproject_version="1.2.4", changelog=changelog)

    assert result.returncode != 0, result.stdout
    message = result.stdout + result.stderr
    assert "Добавлено" in message
    assert "1.3.0" in message
