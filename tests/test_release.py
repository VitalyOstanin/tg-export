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


def test_a_version_that_is_not_semver_stops_the_release(tmp_path):
    """Номер не вида X.Y.Z обходил сверку размера бампа целиком.

    Размер бампа считался только когда номер нашёлся в списке разделов,
    собранном по образцу `X.Y.Z`; номер иной формы в список не попадал, и гейт
    -- то самое место, которое должно было это остановить, -- выпускал релиз
    без единой проверки размера. Номер `1.6` вдобавок меньше того, что требует
    раздел с ломающими изменениями.
    """
    result, _ = _run("v1.6", tmp_path, pyproject_version="1.6", changelog=BREAKING.format(version="1.6"))

    assert result.returncode != 0
    assert "1.6" in result.stderr, result.stderr


def test_a_pre_release_version_stops_the_release(tmp_path):
    """Pre-release-теги конвейером не поддержаны, и молча пропускать их нельзя.

    Номер `2.0.0rc1` не разбирался как `X.Y.Z`, обходил сверку размера бампа и
    попадал на GitHub помеченным `--latest`, то есть выдавался за очередной
    выпуск. Решение записано в RELEASING.md: выпускаются только номера вида
    `X.Y.Z`.
    """
    result, _ = _run(
        "v2.0.0rc1", tmp_path, pyproject_version="2.0.0rc1", changelog=BREAKING.format(version="2.0.0rc1")
    )

    assert result.returncode != 0
    assert "2.0.0rc1" in result.stderr, result.stderr
    assert "pre-release" in result.stderr, result.stderr


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


def test_notes_over_the_github_limit_stop_the_release(tmp_path):
    """Тело длиннее лимита GitHub роняло бы `gh release create` -- уже после PyPI.

    Публикация в индекс необратима, поэтому длина проверяется здесь, до неё, а
    не выясняется на последнем шаге workflow.
    """
    body = "\n".join(f"- Пункт {i}." for i in range(200))
    changelog = f"# Changelog\n\n## [1.5.1] -- 2026-07-30\n\n### Исправлено\n\n{body}\n\n## [1.5.0] -- 2026-07-30\n\n- Старое.\n"
    result, out = _run("v1.5.1", tmp_path, changelog=changelog, extra=["--max-chars", "500"])

    assert result.returncode != 0
    assert "over the 500" in result.stderr, result.stderr
    assert not out.exists(), "гейт записал notes, которые GitHub не примет"


def test_notes_within_the_limit_pass(tmp_path):
    """Граница включительная: ровно лимит -- всё ещё принимаемое тело."""
    result, out = _run("v1.5.1", tmp_path, extra=["--max-chars", "1000"])

    assert result.returncode == 0, result.stderr
    assert 0 < len(out.read_text(encoding="utf-8")) <= 1000


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


def test_the_coverage_gate_says_what_to_do_without_coverage_data(tmp_path):
    """На свежем клоне гейт падал трассировкой из библиотеки покрытия.

    Он предложен отдельной командой в CONTRIBUTING, то есть первый запуск без
    предшествующего `pytest` -- обычное дело; подсказка в скрипте была
    написана, но до самого частого случая не доходила.
    """
    # Скрипт ищет корень от собственного пути, поэтому копия кладётся в
    # `scripts/` временного дерева -- иначе он читает данные репозитория.
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    gate = scripts / "coverage_gate.py"
    gate.write_text((SCRIPT.parent / "coverage_gate.py").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.tg-export.coverage-floor]\n"tg_export/x.py" = 50\n', encoding="utf-8"
    )

    result = subprocess.run([sys.executable, str(gate)], capture_output=True, text=True, cwd=tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "run pytest --cov=tg_export first" in result.stderr
    assert "Traceback" not in result.stderr, result.stderr


def test_the_coverage_gate_names_where_a_floor_is_declared(tmp_path):
    """Отказ называл модуль без границы, но не файл и не секцию, где её объявить.

    Первое же добавление файла в пакет роняет весь набор проверок на последнем
    шаге -- после линтера, типов и тестов.
    """
    gate = (SCRIPT.parent / "coverage_gate.py").read_text(encoding="utf-8")

    assert "[tool.tg-export.coverage-floor] in pyproject.toml" in gate
    assert "minus 5" in gate


def test_the_repository_ignores_the_agent_and_explore_output_on_its_own():
    """Дерево было чистым только благодаря личному глобальному ignore владельца.

    У любого другого участника `.claude/settings.local.json` и вывод explore
    видны в `git status`, попадают в `git add -A` и уезжают в коммит вместе с
    локальными настройками.
    """
    root = SCRIPT.parent.parent
    ignored = (root / ".gitignore").read_text(encoding="utf-8")

    assert ".claude/" in ignored
    assert "docs/__explore/" in ignored


def test_every_action_of_every_workflow_is_pinned_by_commit():
    """Тег доезжает до нового коммита, SHA -- никогда.

    Шаги обоих workflow закреплены по полному SHA, но соглашение держалось
    практикой: шаг, добавленный как `uses: some/action@v1`, проходил весь
    набор проверок, при том что публикация выпускает пакет в PyPI с правом
    доверенного издателя. Локальные действия (`./.github/...`) ссылаются на
    сам репозиторий, и коммит для них -- тот, на котором идёт прогон.
    """
    import re

    import yaml

    workflows = sorted((Path(__file__).resolve().parent.parent / ".github" / "workflows").glob("*.yml"))
    assert workflows, "не найдено ни одного workflow"

    unpinned = []
    for path in workflows:
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in workflow["jobs"].items():
            for step in job.get("steps", []):
                uses = step.get("uses")
                if not uses or uses.startswith("./"):
                    continue
                ref = uses.partition("@")[2]
                if not re.fullmatch(r"[0-9a-f]{40}", ref):
                    unpinned.append(f"{path.name}:{job_name} {uses}")

    assert not unpinned, f"действие закреплено не по коммиту: {unpinned}"


def test_the_actions_are_kept_up_to_date_by_dependabot():
    """SHA-пин по построению не обновляется сам.

    Пины совпадали с последними выпусками только потому, что их поднимали
    руками: выход исправления в любом из действий -- включая загрузчик в PyPI
    -- не порождал ни PR, ни красного прогона, и расхождение обнаруживалось
    только новым походом в API. Экосистема `github-actions` объявлена
    Dependabot, чтобы обновление приходило само.
    """
    import yaml

    path = Path(__file__).resolve().parent.parent / ".github" / "dependabot.yml"
    assert path.exists(), "нет .github/dependabot.yml: обновлять SHA-пины нечем"

    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    ecosystems = {entry["package-ecosystem"] for entry in config["updates"]}
    assert "github-actions" in ecosystems, f"экосистема действий не объявлена: {sorted(ecosystems)}"


def test_the_tag_is_checked_against_master_before_anything_is_built():
    """Единственный шаг гейта, не выраженный командой `uv`, и потому без сверки.

    Workflow срабатывает на любой тег `v*`, а проверки о ветках не знают: тег,
    оставленный на рабочей ветке, на коммите до вливания или на откаченном
    состоянии, опубликовал бы версию, содержимое которой не попадало в
    `master`. Ветка не защищена, так что этот шаг -- единственное, что
    отделяет такой релиз от обычного, и стоять он обязан до всякой работы.
    """
    from parity import job_with_step, jobs_before, workflow

    jobs = workflow("publish.yml")["jobs"]
    building = jobs_before(jobs, job_with_step(jobs, "gh-action-pypi-publish"))
    assert building, "у job'а публикации нет предшественников"

    steps = [step for name in building for step in jobs[name].get("steps", [])]
    checked_at = [i for i, step in enumerate(steps) if "merge-base --is-ancestor" in (step.get("run") or "")]
    assert checked_at, "тег не сверяется с master до публикации"

    first_command = next(
        (i for i, step in enumerate(steps) if any(_keys_of(step))),
        len(steps),
    )
    assert checked_at[0] < first_command, "сверка с master идёт после первой проверки"


def _keys_of(step) -> list[str]:
    """Ключи команд одного шага workflow."""
    from parity import command_key

    return [key for line in (step.get("run") or "").splitlines() for key in [command_key(line)] if key]


def test_the_release_is_created_only_after_the_upload():
    """Release на GitHub создаётся из артефакта, загруженного публикацией.

    Job с правом `contents: write` идёт последним по рёбрам `needs`: собранный
    им release объявляет версию выпущенной, а выпущена она загрузкой на PyPI.
    Порядком записи в YAML это не задаётся.
    """
    from parity import job_with_step, jobs_before, workflow

    jobs = workflow("publish.yml")["jobs"]
    writers = [name for name, job in jobs.items() if job.get("permissions", {}).get("contents") == "write"]
    assert writers, "не найден job, создающий release"

    publishing = job_with_step(jobs, "gh-action-pypi-publish")
    for name in writers:
        assert publishing in jobs_before(jobs, name), f"{name} не ждёт публикации на PyPI"


def test_the_pipeline_can_be_rehearsed_without_publishing():
    """Конвейер существовал только как текст: единственный триггер -- push тега.

    Переписанный конвейер впервые исполнялся бы на настоящем релизе, причём
    непроверенными оставались как раз механизмы, отказ которых виден только в
    прогоне: передача артефактов между job'ами, имя окружения на стороне PyPI,
    поведение сверки с `master` при аннотированном теге. Репетиция запускается
    вручную и проходит сборку целиком, а необратимые шаги пропускает.
    """
    from parity import job_with_step, workflow

    published = workflow("publish.yml")
    triggers = published[True] if True in published else published["on"]
    assert "workflow_dispatch" in triggers, "конвейер нечем отрепетировать до постановки тега"

    jobs = published["jobs"]
    irreversible = {job_with_step(jobs, "gh-action-pypi-publish")}
    irreversible |= {
        name for name, job in jobs.items() if job.get("permissions", {}).get("contents") == "write"
    }
    for name in sorted(irreversible):
        condition = str(jobs[name].get("if", ""))
        assert "push" in condition, f"{name} выполняется и на репетиции: if={condition!r}"


def test_the_coverage_gate_reports_a_floor_that_fell_behind(tmp_path):
    """Граница, отставшая от факта, ловит только обвал -- и не ловит регрессию.

    Границы правятся руками, а гейт проверял одно «не ниже» и молчал, когда
    запас стал избыточным: за несколько дней работы таблица снова разошлась с
    фактом на шестнадцать пунктов, то есть по прямому назначению -- поймать
    падение покрытия -- для этих модулей не работала. Отставание видно на том
    же прогоне, со строкой нового значения.
    """
    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent
    script = (root / "scripts" / "coverage_gate.py").read_text(encoding="utf-8")
    stale = tmp_path / "scripts"
    stale.mkdir()
    (stale / "coverage_gate.py").write_text(script, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.tg-export.coverage-floor]\n"tg_export/__init__.py" = 10\n', encoding="utf-8"
    )

    measured = tmp_path / "measured.py"
    measured.write_text(
        "import json, pathlib, sys\n"
        "sys.path.insert(0, str(pathlib.Path(sys.argv[1]) / 'scripts'))\n"
        "import coverage_gate\n"
        "coverage_gate._measured = lambda: {'tg_export/__init__.py': 100.0}\n"
        "sys.exit(coverage_gate.main())\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(measured), str(tmp_path)], capture_output=True, text=True, cwd=tmp_path
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert "tg_export/__init__.py" in result.stdout + result.stderr
    assert "95" in result.stdout + result.stderr, "не назван новый номинал границы"


def test_the_publishing_pipeline_pins_the_runner_and_the_interpreter():
    """Всё на необратимом пути объявлено, а не выбрано за нас.

    Действия закреплены по коммиту, uv -- по версии, а метка раннера и
    интерпретатор оставались плавающими: `ubuntu-latest` переводится на
    следующий образ без правок в репозитории, а `uv sync` подбирал версию
    Python сам -- совпадение с только что установленной 3.12 было следствием
    порядка поиска, а не требованием. В `ci.yml` плавающая метка полезна тем
    же, чем плавающий uv: несовместимость всплывает на CI, а не на релизе.
    """
    import re

    from parity import workflow

    published = workflow("publish.yml")
    for name, job in published["jobs"].items():
        runner = job["runs-on"]
        assert re.fullmatch(r"ubuntu-\d+\.\d+", str(runner)), f"{name}: раннер не закреплён ({runner})"

    assert re.fullmatch(r"3\.\d+", str(published["env"]["UV_PYTHON"])), "интерпретатор не закреплён"


def test_the_tag_the_pipeline_publishes_is_annotated():
    """Lightweight-тег не хранит ни автора, ни даты выпуска.

    Конвенция требует аннотированных тегов, но держалась на памяти
    выпускающего: конвейер сверял принадлежность коммита `master` и не смотрел
    на тип объекта, а дата раздела CHANGELOG сверяется именно с датой тега.
    """
    from parity import workflow

    steps = workflow("publish.yml")["jobs"]["build"]["steps"]
    runs = "\n".join(step.get("run") or "" for step in steps)

    assert "cat-file -t" in runs, "тип объекта тега не проверяется"


def test_the_sdist_is_smoke_tested_like_the_wheel():
    """На PyPI уходит весь каталог `dist/`, а проверялся один файл из него.

    Состав sdist определяют умолчания setuptools: правка раскладки пакета или
    смена backend'а сломает его молча, и увидит это тот, кто ставит с
    `--no-binary`, -- уже после того, как номер версии израсходован.
    """
    from parity import workflow

    steps = workflow("publish.yml")["jobs"]["build"]["steps"]
    smoke = [step for step in steps if "smoke" in (step.get("name") or "").lower()]
    assert smoke, "шаг smoke-теста не найден"

    runs = "\n".join(step.get("run") or "" for step in smoke)
    assert ".tar.gz" in runs, "sdist не проверяется"


def test_the_declared_operating_systems_are_the_ones_ci_runs_on():
    """Классификатор -- утверждение для установщика, и подкреплён должен быть прогоном.

    Комментарий над блоком объявляет принцип «заявлено то, что прогоняет CI»,
    а строка про macOS ему не отвечала: оба workflow идут только на Linux,
    записи `os` в матрице нет. README при этом формулирует состояние честно --
    «на macOS работа ожидается, но не проверяется».
    """
    import tomllib

    from parity import workflow

    root = Path(__file__).resolve().parent.parent
    with (root / "pyproject.toml").open("rb") as fh:
        classifiers = tomllib.load(fh)["project"]["classifiers"]

    declared = {c for c in classifiers if c.startswith("Operating System ::")}
    runners = {
        str(job["runs-on"]).split("-")[0]
        for name in ("ci.yml", "publish.yml")
        for job in workflow(name)["jobs"].values()
    }
    for entry in ("macos", "windows"):
        assert entry not in runners, f"матрица покрывает {entry}: классификатор можно заявить"
    unbacked = {c for c in declared if "Linux" not in c}
    assert not unbacked, f"заявлены системы, на которых ничего не прогоняется: {sorted(unbacked)}"
