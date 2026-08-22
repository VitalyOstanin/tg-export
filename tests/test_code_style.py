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
        src,
        flags=re.MULTILINE | re.DOTALL,
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


def test_every_telegram_client_is_built_on_the_fixed_session():
    """Ни один путь создания клиента не должен обходить FixedSQLiteSession.

    Telethon принимает строку вместо объекта сессии и молча строит обычный
    SQLiteSession -- тот самый, что читает колонки по имени, тогда как пишутся
    они позиционно. Проверка статическая: подсчитывать поведением каждый новый
    путь создания клиента пришлось бы отдельным тестом.
    """
    import ast

    offenders = []
    for path in sorted(PROJECT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name != "TelegramClient" or not node.args:
                continue
            first = node.args[0]
            built_here = (
                isinstance(first, ast.Call)
                and getattr(first.func, "id", getattr(first.func, "attr", None)) == "FixedSQLiteSession"
            )
            if not built_here and not (isinstance(first, ast.Name) and first.id == "session"):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"TelegramClient должен получать FixedSQLiteSession: {offenders}"


def _exit_is_a_failure(node) -> bool:
    """True for `raise click.exceptions.Exit(<non-zero>)`."""
    import ast

    if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
        return False
    func = node.exc.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name != "Exit":
        return False
    if not node.exc.args:
        # `Exit()` defaults to code 1 -- a failure like any other.
        return True
    arg = node.exc.args[0]
    return not (isinstance(arg, ast.Constant) and arg.value == 0)


def _is_suppressible_diag(node) -> bool:
    """True for a `_diag(...)` call that --quiet would swallow."""
    import ast

    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return False
    func = node.value.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name != "_diag":
        return False
    return not any(kw.arg == "essential" for kw in node.value.keywords)


def test_messages_before_a_failure_exit_survive_quiet():
    """Сообщение, за которым команда завершается ошибкой, обязано пережить --quiet.

    Признак essential проставлялся вручную у отдельных вызовов, и из 94 вызовов
    _diag его получили 20, из которых 17 -- строки итоговой сводки. Поэтому
    `--quiet tg send 123` печатал пустоту и возвращал 1: причина отказа
    оставалась невидимой. Проверка статическая -- поведенческий тест на каждый
    путь отказа пришлось бы писать отдельно, а новый путь появляется с каждой
    командой.
    """
    import ast

    tree = ast.parse(_read("cli.py"))
    offenders = []
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for i, stmt in enumerate(block):
                if not _exit_is_a_failure(stmt):
                    continue
                j = i - 1
                while j >= 0 and _is_suppressible_diag(block[j]):
                    offenders.append(block[j].lineno)
                    j -= 1
    assert not offenders, f"под --quiet потеряются сообщения об отказе на строках: {sorted(offenders)}"


def test_no_manual_async_context_calls_in_api():
    """Takeout-контекст должен вестись через AsyncExitStack, а не ручными
    __aenter__/__aexit__: при ручном вызове выход из контекста не привязан к
    выходу из функции, и отпустить сессию на всех путях выполнения нечем."""
    src = _read("api.py")
    calls = re.findall(r"\.__a(?:enter|exit)__\(", src)
    assert not calls, f"используй AsyncExitStack вместо ручного вызова контекста: {calls}"


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
        (i for i, ln in enumerate(lines) if re.match(r"^logger\s*=\s*logging\.getLogger", ln)),
        None,
    )
    assert logger_idx is not None, "logger не объявлен"

    after = lines[logger_idx + 1 :]
    bad_imports = [
        (logger_idx + 1 + i, ln) for i, ln in enumerate(after) if re.match(r"^(import|from)\s+\w", ln)
    ]
    assert not bad_imports, f"после logger=... идут module-level импорты (PEP 8): {bad_imports!r}"


def test_timedelta_imported_at_module_level_in_exporter():
    """from datetime import timedelta должен быть на уровне модуля, не внутри функции."""
    src = _read("exporter.py")
    inside_func = re.findall(
        r"^[ \t]+from datetime import timedelta",
        src,
        flags=re.MULTILINE,
    )
    assert not inside_func, "from datetime import timedelta должен быть на уровне модуля, не внутри функции"


def test_exporter_imports_rich_escape():
    """exporter должен импортировать rich.markup.escape, чтобы экранировать пользовательский ввод."""
    src = _read("exporter.py")
    assert re.search(r"from rich\.markup import\b.*\bescape\b", src), (
        "exporter.py должен импортировать escape из rich.markup для эскейпа имён чатов/файлов"
    )


def test_every_package_data_file_is_declared_for_packaging():
    """Каждый не-.py файл пакета должен попадать в дистрибутив.

    Без [tool.setuptools.package-data] в колесо уходят только *.py, и любой
    экспорт после pip install падает с TemplateNotFound. Тест сверяет фактические
    файлы данных с шаблонами, объявленными в pyproject.toml, чтобы новый тип
    ресурса (шрифт, favicon) не выпал из сборки молча.
    """
    import fnmatch
    import tomllib

    root = PROJECT.parent
    with (root / "pyproject.toml").open("rb") as fh:
        pyproject = tomllib.load(fh)

    package_data = pyproject["tool"]["setuptools"]["package-data"]
    data_files = [
        p for p in PROJECT.rglob("*") if p.is_file() and p.suffix != ".py" and "__pycache__" not in p.parts
    ]
    assert data_files, "в пакете не найдено файлов данных — проверьте раскладку"

    undeclared = []
    for path in data_files:
        rel_to_package = path.relative_to(PROJECT)
        covered = False
        for package, patterns in package_data.items():
            package_dir = PROJECT.joinpath(*package.split(".")[1:])
            if package_dir not in path.parents:
                continue
            rel = path.relative_to(package_dir).as_posix()
            if any(fnmatch.fnmatch(rel, pattern) for pattern in patterns):
                covered = True
                break
        if not covered:
            undeclared.append(rel_to_package.as_posix())

    assert not undeclared, (
        f"файлы данных не объявлены в [tool.setuptools.package-data] и не попадут в дистрибутив: {undeclared}"
    )


def _cli_ast():
    import ast

    return ast.parse(_read("cli.py"))


def test_cli_never_manages_connection_lifetime_by_hand():
    """Соединение с Telegram и БД состояния открываются только через `async with`.

    Ручная пара `connect()` + `finally: disconnect()` была повторена в cli.py
    десять раз, и в двух командах исключение между `connect()` и входом в `try`
    оставляло соединение открытым. Контекстменеджер убирает и повтор, и разрыв
    между захватом ресурса и его защитой.
    """
    import ast

    manual = {"connect", "disconnect", "open", "close"}
    hits = []
    for node in ast.walk(_cli_ast()):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in manual:
            continue
        target = node.func.value
        if isinstance(target, ast.Name) and target.id in {"api", "state", "st"}:
            hits.append((node.lineno, f"{target.id}.{node.func.attr}()"))
    assert not hits, f"открывай ресурс через `async with`: {hits}"


def test_cli_helpers_are_context_managers():
    """Помощники подключения и открытия состояния не отдают ресурс наружу.

    `_connect_tg` возвращал подключённый TgApi и переносил уборку на
    вызывающего строкой docstring «Caller must call api.disconnect() when
    done»; ровно так же поступал `_open_state`. Обязательство, записанное в
    docstring, а не в коде, соблюдается ровно до первой невнимательности.
    """
    import ast

    src = _read("cli.py")
    tree = ast.parse(src)
    for name in ("_connected_api", "_opened_state"):
        fn = next(
            (n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == name),
            None,
        )
        assert fn is not None, f"ожидается помощник-контекстменеджер {name}"
        decorators = {ast.unparse(d) for d in fn.decorator_list}
        assert any("asynccontextmanager" in d for d in decorators), (
            f"{name} должен быть @contextlib.asynccontextmanager, а не возвращать ресурс: {decorators}"
        )
    assert "Caller must call api.disconnect" not in src


def test_no_function_is_longer_than_a_screenful():
    """Ни одна функция пакета не длиннее ста строк.

    Восемь функций были длиннее, и четыре крупнейшие складывались в один
    сценарий «запустить экспорт»: `Exporter.run` (259 строк) держала в одном
    теле установку обработчиков сигналов, отбор чатов, три замыкания
    форматирования статуса, настройку Live и цикл по чатам. На таком объёме
    побочные эффекты собственной правки перестают быть видны.
    """
    import ast

    limit = 100
    too_long = []
    for path in sorted(PROJECT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            length = (node.end_lineno or node.lineno) - node.lineno + 1
            if length > limit:
                too_long.append((length, f"{path.name}:{node.lineno}", node.name))
    too_long.sort(reverse=True)
    assert not too_long, f"функции длиннее {limit} строк: {too_long}"


def test_cli_does_not_run_sql_of_its_own():
    """Схема БД известна только слою состояния.

    CLI выполнял девять запросов через `state.db.execute` мимо `ExportState`,
    и кортеж таблиц чата был продублирован в двух местах: правка схемы в одном
    из них давала purge, который удаляет не то, о чём предупредил.
    """
    src = _read("cli.py")
    hits = [
        (n, line.strip())
        for n, line in enumerate(src.splitlines(), start=1)
        if re.search(r"\b(?:state|st)\.db\.", line)
    ]
    assert not hits, f"обращайся к БД через методы ExportState: {hits}"


def test_cli_has_a_module_docstring():
    """Самый крупный модуль проекта -- единственный без строки назначения."""
    import ast

    tree = ast.parse(_read("cli.py"))
    doc = ast.get_docstring(tree)
    assert doc, "cli.py нужен модульный docstring с картой групп команд"
    assert "import" in doc.lower(), "объясни в нём приём с отложенными импортами"


def test_cli_takes_formatting_helpers_from_their_own_module():
    """`_format_size` в exporter.py -- лишь псевдоним для format.format_size."""
    src = _read("cli.py")
    assert "_format_size" not in src, "импортируй format_size из tg_export.format"


def test_every_text_file_is_read_as_utf8():
    """Чтение файлов идёт в UTF-8, а не в кодировке локали.

    Запись почти везде выполнялась явно в UTF-8, а чтение -- в
    `locale.getpreferredencoding`. Конфиг содержит имена папок и чатов на
    кириллице, поэтому на системе с не-UTF-8 локалью это либо падение, либо
    искажённые имена, из-за которых правила молча не срабатывают.
    """
    import ast

    def _mode(node: ast.Call) -> str:
        """Режим открытия: у open(path, mode) он второй, у Path.open(mode) -- первый."""
        args = node.args[1:] if isinstance(node.func, ast.Name) else node.args
        return getattr(args[0], "value", "") if args else ""

    offenders = []
    for path in sorted(PROJECT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in {"open", "read_text", "write_text"}:
                continue
            if name == "open":
                target = getattr(node.func, "value", None)
                # os.open отдаёт дескриптор, ExportState.open открывает БД --
                # ни у того, ни у другого кодировки нет.
                if isinstance(target, ast.Name) and target.id in {"os", "self"}:
                    continue
                if "b" in _mode(node):
                    continue
            if any(kw.arg == "encoding" for kw in node.keywords):
                continue
            offenders.append((path.name, node.lineno, name))
    assert not offenders, f"добавь encoding='utf-8': {offenders}"


def test_every_command_is_described_in_the_cli_reference():
    """Новая команда должна попасть в docs/cli.md вместе с кодом.

    Справочник разошёлся с интерфейсом однажды -- восемь команд из двадцати не
    были описаны нигде, и найти их можно было только через --help.
    """
    import click

    from tg_export.cli import main

    reference = (PROJECT.parent / "docs" / "cli.md").read_text(encoding="utf-8")

    def names(cmd, prefix=""):
        if isinstance(cmd, click.Group):
            for sub in cmd.commands.values():
                yield from names(sub, f"{prefix}{cmd.name} " if prefix or cmd.name != "main" else "")
        else:
            yield (prefix + cmd.name).strip()

    missing = [name for name in names(main) if f"`{name}`" not in reference]
    assert not missing, f"команды не описаны в docs/cli.md: {missing}"


def _ci_check_commands() -> list[str]:
    """Ключи проверок из ci.yml: имя инструмента и подкоманда, без флагов и путей."""
    import yaml

    workflow = yaml.safe_load(
        (PROJECT.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    keys = []
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            run = (step.get("run") or "").strip()
            if not run.startswith("uv ") or run.startswith("uv sync") or run.startswith("uv python"):
                continue
            tokens = [t for t in run.split() if t != "uv"]
            if tokens[0] == "run":
                tokens = tokens[1:]
            if tokens[0] == "python":
                keys.append(tokens[2] if tokens[1] == "-m" else Path(tokens[1]).name)
                continue
            tail = tokens[1] if len(tokens) > 1 and not tokens[1].startswith("-") and tokens[1] != "." else ""
            keys.append(f"{tokens[0]} {tail}".strip())
    return keys


def test_one_command_runs_everything_ci_runs():
    """scripts/check.sh должен покрывать все проверки CI.

    Пока такой команды не было, шаг `ruff format --check` ломался коммитом и
    выяснялось это только на CI: локально его никто не запускал.
    """
    script = (PROJECT.parent / "scripts" / "check.sh").read_text(encoding="utf-8")
    missing = [key for key in _ci_check_commands() if key not in script]
    assert not missing, f"проверки CI отсутствуют в scripts/check.sh: {missing}"
