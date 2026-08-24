"""Разбор конвейеров CI, конвейера публикации и scripts/check.sh до исполняемых команд.

Сверка по подстроке в тексте файла удовлетворяется упоминанием инструмента в
комментарии или в `echo`: три проверки релизного гейта числились выполненными,
хотя исполнял их один шаг `scripts/check.sh`, а удаление `uv run pyright` при
сохранённом `echo "==> pyright"` не роняло ничего. Здесь берутся только те
строки, которые действительно запускаются.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECK_SCRIPT = "scripts/check.sh"


def command_key(run: str) -> str | None:
    """Инструмент и подкоманда одной команды `uv ...`, без флагов и путей.

    Возвращает None для всего, что проверкой не является: подготовка
    окружения (`uv sync`, `uv python install`) и любые команды не через uv.
    """
    run = run.strip()
    if not run.startswith("uv ") or run.startswith(("uv sync", "uv python")):
        return None
    tokens = [t for t in run.split() if t != "uv"]
    if tokens[0] == "run":
        tokens = tokens[1:]
    while tokens and tokens[0].startswith("-"):
        flag = tokens.pop(0)
        if flag in ("--with", "--python", "--directory", "--project") and tokens:
            tokens.pop(0)
    if not tokens:
        return None
    if tokens[0] == "python":
        if tokens[1] == "-m":
            return tokens[2]
        # `python -c ...` -- инлайн-проверка собранного пакета, не шаг набора.
        return None if tokens[1].startswith("-") else Path(tokens[1]).name
    tail = tokens[1] if len(tokens) > 1 and not tokens[1].startswith("-") and tokens[1] != "." else ""
    return f"{tokens[0]} {tail}".strip()


def _keys(runs) -> list[str]:
    return [key for run in runs for key in [command_key(run)] if key]


def check_script_commands() -> list[str]:
    """Проверки, которые выполняет scripts/check.sh.

    Ветка `--fix` пропускается: она запускает `ruff check --fix` и
    `ruff format` -- те же инструменты, что и проверочная часть, поэтому по
    ней отсутствие `ruff format --check` было бы незаметно.
    """
    runs, skipping = [], False
    for line in (ROOT / CHECK_SCRIPT).read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("if [["):
            skipping = True
            continue
        if stripped == "fi":
            skipping = False
            continue
        if not skipping:
            runs.append(stripped)
    return _keys(runs)


def workflow(name: str) -> dict:
    """Разобранный workflow из `.github/workflows`."""
    import yaml

    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def _workflow_steps(name: str) -> list[dict]:
    return [step for job in workflow(name)["jobs"].values() for step in job["steps"]]


def job_with_step(jobs: dict, action: str) -> str:
    """Имя job'а, в котором есть шаг с действием `action`."""
    named = [
        name
        for name, job in jobs.items()
        if any(action in (step.get("uses") or "") for step in job.get("steps", []))
    ]
    assert len(named) == 1, f"шаг с {action!r} найден в job'ах: {named}"
    return named[0]


def jobs_before(jobs: dict, name: str) -> list[str]:
    """Job'ы, которые обязаны завершиться до `name`, по обходу `needs`.

    Порядок задают рёбра `needs`, а не порядок записи job'ов в YAML: после
    разделения конвейера на три job'а именно ребро отделяет проверки от
    необратимой загрузки, и его удаление оставляло разбор по порядку записи
    прежним -- то есть тест зелёным при публикации параллельно с проверками.
    """
    needs = jobs[name].get("needs") or []
    if isinstance(needs, str):
        needs = [needs]
    earlier = []
    for parent in needs:
        earlier.extend(jobs_before(jobs, parent))
        earlier.append(parent)
    return earlier


def ci_check_commands() -> list[str]:
    """Проверки, которые выполняет ci.yml."""
    return _keys(step.get("run") or "" for step in _workflow_steps("ci.yml"))


def publish_commands_before(action: str) -> list[str]:
    """Проверки конвейера публикации, выполняемые до шага с действием `action`.

    «До» -- это job'ы, от которых зависит job с этим шагом (обход `needs`), и
    шаги самого job'а выше него. Шаг `scripts/check.sh` разворачивается в
    набор проверок самого скрипта: там они и живут, и подменить его на
    `pytest` мимо этой сверки нельзя.
    """
    jobs = workflow("publish.yml")["jobs"]
    target = job_with_step(jobs, action)
    steps = [step for name in jobs_before(jobs, target) for step in jobs[name].get("steps", [])]
    for step in jobs[target].get("steps", []):
        if action in (step.get("uses") or ""):
            break
        steps.append(step)

    keys = []
    for step in steps:
        run = (step.get("run") or "").strip()
        if CHECK_SCRIPT in run:
            keys.extend(check_script_commands())
            continue
        keys.extend(_keys(run.splitlines()))
    return keys
