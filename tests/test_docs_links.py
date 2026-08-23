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


def test_every_adr_is_listed_in_the_index():
    """Запись, которой нет в оглавлении ADR, не найдут.

    Оглавление ведётся вручную; каталог растёт по одной записи за решение, и
    пропуск обнаруживается только при следующем чтении каталога целиком.
    """
    adr_dir = ROOT / "docs" / "adr"
    index = (adr_dir / "README.md").read_text(encoding="utf-8")
    records = sorted(p.name for p in adr_dir.glob("[0-9][0-9][0-9][0-9]-*.md"))

    missing = [name for name in records if f"({name})" not in index]
    assert not missing, f"нет в оглавлении docs/adr/README.md: {missing}"


def test_every_adr_declares_its_status_and_sections():
    """Тело ADR неизменяемо, поэтому его состав проверяется при добавлении."""
    required = ("## Контекст", "## Рассмотренные варианты", "## Решение", "## Последствия")
    incomplete = []
    for path in sorted((ROOT / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md")):
        source = path.read_text(encoding="utf-8")
        if "- Статус:" not in source or any(section not in source for section in required):
            incomplete.append(path.name)
    assert not incomplete, f"ADR без статуса или обязательного раздела: {incomplete}"


def test_a_vulnerability_has_a_private_route() -> None:
    """Публичный issue -- раскрытие до выпуска исправления.

    Инструмент хранит ключ авторизации от аккаунта Telegram и публикуется в
    PyPI, поэтому у нашедшего проблему должен быть непубличный маршрут:
    SECURITY.md с порядком сообщения и ссылка на него из формы заведения
    issue, где выбирают, куда писать.
    """
    import yaml

    policy = ROOT / "SECURITY.md"
    assert policy.exists(), "нет SECURITY.md с порядком приватного сообщения об уязвимости"

    config = yaml.safe_load((ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml").read_text(encoding="utf-8"))
    links = config.get("contact_links") or []
    urls = " ".join(str(link.get("url", "")) for link in links)

    assert "security" in urls.lower(), f"форма заведения issue не предлагает канал для уязвимостей: {links}"


def test_the_checks_that_ci_runs_on_every_matrix_entry_are_described_as_such() -> None:
    """CONTRIBUTING обещал линтер, типы и покрытие «на 3.12», а они идут на всех версиях.

    Контрибьютор видит сообщение линтера четырежды, а падение на 3.14 не
    объясняется обещанием проверять только на одной версии. Тест сверяет
    утверждение с фактическим набором шагов, ограниченных условием по версии.
    """
    import yaml

    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    steps = [step for job in workflow["jobs"].values() for step in job.get("steps", [])]
    pinned = {step.get("name", "") for step in steps if "matrix.python-version ==" in str(step.get("if", ""))}

    # Переносы строк в markdown расставлены по ширине, а утверждение читается
    # как одна фраза, поэтому пробельные последовательности сводятся к пробелу.
    contributing = " ".join((ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8").split())

    for name in ("Lint (ruff)", "Type check (pyright)", "Check per-module coverage floors"):
        if name in pinned:
            continue
        assert "на каждой версии матрицы" in contributing, (
            f"шаг {name!r} идёт на всех версиях матрицы, а CONTRIBUTING обещает одну"
        )


def test_the_bug_report_shows_where_the_debug_flag_goes() -> None:
    """`--debug` объявлен опцией корневой группы, а не подкоманд.

    Дописанный к своей команде флаг даёт «Error: No such option '--debug'»:
    автор обращения получает отказ вместо журнала, и сопровождающему придётся
    отдельным кругом просить журнал заново.
    """
    template = (ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.md").read_text(encoding="utf-8")

    assert "`tg-export --debug " in template, "в шаблоне нет готовой формы команды с флагом до подкоманды"


def test_contributing_tells_how_to_run_a_single_test_file() -> None:
    """Порог покрытия применяется к любому запуску, а осмыслен только для полного.

    `pytest tests/test_models.py` заканчивается кодом 1 при всех зелёных
    тестах: покрытие одного файла ниже порога. Способ обойти это записан там
    же, где описан запуск тестов.
    """
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")

    assert "--no-cov" in contributing, "в разделе о запуске тестов не сказано про частичный прогон"


def _deprecated_option_spellings():
    """Old spellings kept for compatibility, by command: `list` -> {--output}."""
    import click

    from tg_export.cli import main

    def walk(cmd, prefix=""):
        if isinstance(cmd, click.Group):
            for sub in cmd.commands.values():
                yield from walk(sub, f"{prefix}{cmd.name} " if prefix or cmd.name != "main" else "")
            return
        long_opts = {
            (prefix + (cmd.name or "")).strip(): {
                opt for param in cmd.params for opt in [o for o in param.opts if o.startswith("--")][1:]
            }
        }
        yield from long_opts.items()

    return {name: opts for name, opts in walk(main) if opts}


def test_the_documentation_teaches_canonical_option_spellings():
    """Примеры в документации не должны учить написаниям, объявленным старыми.

    Справочник `docs/cli.md` -- исключение: он перечисляет принимаемые синонимы,
    а CHANGELOG описывает историю. Остальная документация -- обучение, и при
    удалении старого написания сломается именно она.
    """
    deprecated = _deprecated_option_spellings()
    skip = {"docs/cli.md", "CHANGELOG.md"}
    offenders = []
    for path in _tracked_markdown():
        if str(path.relative_to(ROOT)) in skip:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for example in re.findall(r"tg-export ((?:[a-z][a-z-]*|--[a-z-]+|\S+)(?:\s+\S+)*)", line):
                words = example.split()
                command = " ".join(w for w in words[:2] if re.fullmatch(r"[a-z][a-z-]*", w))
                while command and command not in deprecated:
                    command = command.rsplit(" ", 1)[0] if " " in command else ""
                if not command:
                    continue
                used = deprecated[command] & set(words)
                if used:
                    offenders.append(f"{path.relative_to(ROOT)}:{number} {command}: {sorted(used)}")
    assert not offenders, f"документация учит старым написаниям опций: {offenders}"


def test_the_readme_names_every_command_that_accepts_json():
    """Перечень команд с `--json` -- единственное место, где их видно вместе.

    Список отставал от интерфейса: `list` получил флаг, попал в справочник и в
    CHANGELOG, а в README остались четыре команды из пяти -- при том, что
    соседний абзац сам называл `list` среди команд с машиночитаемым выводом.
    """
    import click

    from tg_export.cli import main

    def walk(cmd, prefix=""):
        if isinstance(cmd, click.Group):
            for sub in cmd.commands.values():
                yield from walk(sub, f"{prefix}{cmd.name} " if prefix or cmd.name != "main" else "")
            return
        if any("--json" in param.opts for param in cmd.params):
            yield (prefix + (cmd.name or "")).strip()

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    line = next(ln for ln in readme.splitlines() if "`--json`" in ln and "поддерживают" in ln)
    missing = [name for name in walk(main) if f"`{name}`" not in line]
    assert not missing, f"в перечне команд с --json нет: {missing}\n{line}"


# Statuses an ADR record may carry. The vocabulary is Russian, like the records
# themselves; a second spelling of the same status means replaced records cannot
# be found by listing.
ADR_STATUSES = ("принято", "заменено ADR-", "устарело")


def test_every_adr_status_comes_from_the_documented_vocabulary():
    """Статус, записанный по-своему, нельзя найти перечислением.

    Конвенции называли один и тот же статус двумя словарями -- английским
    `superseded by` и русским `заменено ADR-NNNN`, -- а проверка требовала лишь
    наличия строки `- Статус:` и любое значение принимала.
    """
    index = (ROOT / "docs" / "adr" / "README.md").read_text(encoding="utf-8")
    assert "superseded by" not in index, "конвенции ADR называют статус вторым словарём"

    offenders = []
    for path in sorted((ROOT / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md")):
        status = next(
            (
                line.split(":", 1)[1].strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.startswith("- Статус:")
            ),
            "",
        )
        if not any(status.startswith(known) for known in ADR_STATUSES):
            offenders.append(f"{path.name}: {status!r}")
    assert not offenders, f"статус вне словаря {ADR_STATUSES}: {offenders}"


def test_the_full_config_example_is_the_example_file_itself():
    """Раздел «Полный конфиг» и `config.example.yaml` -- один пример, а не два.

    Документация утверждала тождественность, а тексты разошлись: разные правила
    `type_rules`, разный состав папок и чатов, `import_existing` в одном месте
    закомментирован. Загружается и проверяется тестами только файл, а читатель
    первым видит блок в документации.
    """
    source = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    heading = "### Полный конфиг"
    assert heading in source, "раздел «Полный конфиг» переименован -- поправь проверку"

    block = re.search(r"```yaml\n(.*?)```", source[source.index(heading) :], re.S)
    assert block, "в разделе «Полный конфиг» нет YAML-блока"

    example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    assert block.group(1) == example, "блок в документации разошёлся с config.example.yaml"
