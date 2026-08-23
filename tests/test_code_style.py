"""Статические проверки стиля по исходникам.

Здесь собраны требования, для которых поведенческий тест пришлось бы писать на
каждый путь выполнения отдельно, а нарушение появляется с каждой новой командой:
вывод только через `click.echo`/`console.print`, отказ команды виден под
`--quiet`, ресурсы открываются контекстменеджером, SQL живёт в слое состояния,
стандартная библиотека импортируется в шапке модуля, у каждой опции есть
описание. Каждая проверка объясняет в своём docstring, какой именно отказ она
предотвращает.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent / "tg_export"


def _read(name: str) -> str:
    return (PROJECT / name).read_text(encoding="utf-8")


def _tree(name: str) -> ast.Module:
    """Разобрать модуль пакета.

    Проверки по дереву, а не по тексту: регулярное выражение по исходнику
    находит совпадение и в комментарии, и в строке, а при ином форматировании
    перестаёт находить вообще -- и тест становится зелёным, ничего не проверяя.
    """
    return ast.parse(_read(name))


def _called_name(node: ast.AST) -> str | None:
    """Имя вызываемого: `foo(...)` -> "foo", `bar.foo(...)` -> "foo"."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, "id", None)


def _calls(tree: ast.AST) -> Iterator[ast.Call]:
    """Только узлы вызова: номер строки объявлен у ast.Call, у ast.AST его нет."""
    return (node for node in ast.walk(tree) if isinstance(node, ast.Call))


def _qualified_call(node: ast.AST) -> str | None:
    """Полное имя вызова через точку: `console.log(...)` -> "console.log"."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    parts = [node.func.attr]
    owner = node.func.value
    while isinstance(owner, ast.Attribute):
        parts.append(owner.attr)
        owner = owner.value
    if isinstance(owner, ast.Name):
        parts.append(owner.id)
    return ".".join(reversed(parts))


def _bare_prints(tree: ast.AST) -> list[int]:
    """Строки вызовов `print(...)` без владельца (не `console.print`)."""
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print"
    ]


def _function(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """Найти функцию по имени; её отсутствие -- отказ, а не повод пропустить проверку."""
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
    ]
    assert found, f"функция {name} не найдена: проверка ниже не имела бы смысла"
    return found[0]


# CLI разложен на пакет: модуль на группу команд плюс common. Проверки, которые
# раньше читали cli.py, обходят все модули пакета.
CLI_MODULES = sorted((PROJECT / "cli").glob("*.py"))


def _read_cli() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in CLI_MODULES)


def test_auth_uses_click_echo_not_bare_print():
    """auth.py не должен вызывать bare print(); используй click.echo.

    print пишет в stdout, зарезервированный за машиночитаемым выводом команд
    запроса, и не подчиняется --quiet.
    """
    lines = _bare_prints(_tree("auth.py"))
    assert not lines, f"auth.py содержит bare print() на строках: {lines}"


def test_log_function_uses_rich_console_not_bare_print():
    """exporter._log не должен использовать bare print, чтобы не зависеть от состояния Live.

    Прежняя редакция искала тело функции регулярным выражением и проверяла его
    только при совпадении: переименование, декоратор или иное форматирование --
    и проверка молча переставала что-либо проверять.
    """
    lines = _bare_prints(_function(_tree("exporter.py"), "_log"))
    assert not lines, (
        f"_log использует bare print() на строках {lines}; нужен console.print(..., markup=False)"
    )


def test_no_console_log_calls_in_exporter():
    """Унификация: console.log смешан с console.print. Используем только console.print."""
    lines = [node.lineno for node in _calls(_tree("exporter.py")) if _qualified_call(node) == "console.log"]
    assert not lines, f"console.log() не должен использоваться, строки: {lines}"


def test_no_manual_live_enter_exit_in_exporter():
    """Live должен использоваться через with-блок, а не ручным __enter__/__exit__."""
    manual = [
        (node.lineno, _called_name(node))
        for node in _calls(_tree("exporter.py"))
        if _called_name(node) in {"__enter__", "__exit__"}
    ]
    assert not manual, f"Live должен использоваться через with-блок: {manual}"


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
    """True for a call to `_fail(...)` with a non-zero code.

    `_fail` is the single way a command stops with a failure (см.
    tg_export/cli/common.py); до этого то же место выглядело как
    `raise click.exceptions.Exit(1)`, и обе формы здесь распознаются.
    """
    import ast

    if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
        call = node.exc
    elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        call = node.value
    else:
        return False
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name not in ("Exit", "_fail"):
        return False
    args = list(call.args) + [kw.value for kw in call.keywords if kw.arg == "code"]
    codes = [a for a in args if isinstance(a, ast.Constant) and isinstance(a.value, int)]
    return not any(c.value == 0 for c in codes)


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

    offenders = []
    for node in ast.walk(ast.parse(_read_cli())):
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
    manual = [
        (node.lineno, _called_name(node))
        for node in _calls(_tree("api.py"))
        if _called_name(node) in {"__aenter__", "__aexit__"}
    ]
    assert not manual, f"используй AsyncExitStack вместо ручного вызова контекста: {manual}"


def test_strip_markup_function_removed():
    """_strip_markup дублирует Text.from_markup(s).plain и не нужен после рефактора _log."""
    defined = [
        node.name
        for node in ast.walk(_tree("exporter.py"))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "_strip_markup"
    ]
    assert not defined, (
        "_strip_markup должен быть удалён -- используй console.print(markup=False) или "
        "rich.text.Text.from_markup(s).plain"
    )


def test_logger_declared_after_all_module_imports():
    """logger = logging.getLogger должен идти после блока импортов (PEP 8)."""
    body = _tree("exporter.py").body
    logger_idx = next(
        (
            i
            for i, node in enumerate(body)
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "logger" for t in node.targets)
        ),
        None,
    )
    assert logger_idx is not None, "logger не объявлен"

    late = [
        (node.lineno, ast.unparse(node))
        for node in body[logger_idx + 1 :]
        if isinstance(node, ast.Import | ast.ImportFrom)
    ]
    assert not late, f"после logger=... идут module-level импорты (PEP 8): {late}"


def test_timedelta_imported_at_module_level_in_exporter():
    """from datetime import timedelta должен быть на уровне модуля, не внутри функции."""
    tree = _tree("exporter.py")
    at_module_level = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "datetime"
        and any(alias.name == "timedelta" for alias in node.names)
        for node in tree.body
    )
    assert at_module_level, "from datetime import timedelta должен стоять в шапке exporter.py"


def test_standard_library_is_imported_at_module_level():
    """Отложенный импорт оправдан только для тяжёлых модулей.

    Старт консольной команды экономится за счёт того, что telethon, jinja2 и
    модули пакета грузятся по требованию. К стандартной библиотеке это не
    относится: `json`, `os`, `shutil`, `logging` и `collections` уже загружены
    интерпретатором, а импорт внутри функции вдобавок перекрывал модульный
    `logger` локальной переменной.
    """
    import sys

    offenders = []
    for path in sorted(PROJECT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Import):
                    modules = [a.name for a in sub.names]
                elif isinstance(sub, ast.ImportFrom):
                    modules = [sub.module or ""]
                else:
                    continue
                for module in modules:
                    if module.split(".")[0] in sys.stdlib_module_names:
                        offenders.append((path.name, sub.lineno, module))
    assert not offenders, f"стандартную библиотеку импортировать в шапке модуля: {offenders}"


def test_exporter_imports_rich_escape():
    """exporter должен импортировать rich.markup.escape, чтобы экранировать пользовательский ввод."""
    imported = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "rich.markup"
        and any(alias.name == "escape" for alias in node.names)
        for node in ast.walk(_tree("exporter.py"))
    )
    assert imported, "exporter.py должен импортировать escape из rich.markup для эскейпа имён чатов/файлов"


def test_development_tools_are_declared_as_a_group_not_as_an_extra():
    """pytest, ruff и pyright объявляются группой, а не extra проекта.

    Extra попадает в метаданные колеса и предлагается тому, кто ставит пакет из
    PyPI, хотя инструменты проверки нужны только в исходном дереве. Практическое
    следствие важнее: `uv sync` ставит группы по умолчанию, а любой явный
    `--extra` заменяет набор extras целиком, и объявленный через extra dev
    вымывался из окружения при `uv sync --extra proxy`.
    """
    import tomllib

    root = PROJECT.parent
    with (root / "pyproject.toml").open("rb") as fh:
        pyproject = tomllib.load(fh)

    tools = {"pytest", "ruff", "pyright", "pytest-asyncio", "pytest-cov", "pytest-timeout"}
    groups = pyproject.get("dependency-groups", {})
    declared_in_groups = {
        req.split(">")[0].split("=")[0].split("[")[0].strip()
        for reqs in groups.values()
        for req in reqs
        if isinstance(req, str)
    }
    assert tools <= declared_in_groups, (
        f"инструменты разработки не объявлены в [dependency-groups]: {sorted(tools - declared_in_groups)}"
    )

    extras = pyproject["project"].get("optional-dependencies", {})
    misplaced = {
        name: req
        for name, reqs in extras.items()
        for req in reqs
        if req.split(">")[0].split("=")[0].split("[")[0].strip() in tools
    }
    assert not misplaced, f"инструменты разработки объявлены как extra проекта: {misplaced}"


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

    return ast.parse(_read_cli())


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

    src = _read("cli/common.py")
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
    hits = []
    for path in CLI_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in _calls(tree):
            call = _qualified_call(node)
            if call and re.match(r"^(?:state|st)\.db\.", call):
                hits.append((path.name, node.lineno, call))
    assert not hits, f"обращайся к БД через методы ExportState: {hits}"


def test_cli_has_a_module_docstring():
    """У каждого модуля пакета cli есть строка назначения, у пакета -- карта групп."""
    import ast

    missing = [p.name for p in CLI_MODULES if not ast.get_docstring(ast.parse(p.read_text(encoding="utf-8")))]
    assert not missing, f"модулям пакета cli нужен docstring: {missing}"
    doc = ast.get_docstring(ast.parse(_read("cli/__init__.py")))
    assert doc and "import" in doc.lower(), "объясни в нём приём с отложенными импортами"


def test_cli_takes_formatting_helpers_from_their_own_module():
    """`_format_size` в exporter.py -- лишь псевдоним для format.format_size."""
    src = _read_cli()
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


def test_one_command_runs_everything_ci_runs():
    """scripts/check.sh должен покрывать все проверки CI.

    Пока такой команды не было, шаг `ruff format --check` ломался коммитом и
    выяснялось это только на CI: локально его никто не запускал.

    Сверяются исполняемые команды обоих файлов, а не подстроки в тексте: по
    подстроке шаг числился на месте и тогда, когда от него осталась одна
    строка `echo "==> pyright"`, а ветка `--fix` подменяла собой отсутствующий
    `ruff format --check`.
    """
    from parity import check_script_commands, ci_check_commands

    script = check_script_commands()
    missing = [key for key in ci_check_commands() if key not in script]
    assert not missing, f"проверки CI отсутствуют в scripts/check.sh: {missing}"


def test_cli_does_not_use_click_private_exit_exception():
    """Завершение команды -- через ctx.exit(), а не через внутренний класс click.

    `click.exceptions.Exit` не вынесен в публичное пространство имён (в 8.3.3
    `hasattr(click, "Exit")` даёт False), то есть обращение к нему опирается на
    деталь реализации зависимости. Документированный способ -- `ctx.exit(code)`.
    """
    offenders = []
    for path in PROJECT.rglob("*.py"):
        for ln_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "click.exceptions." in line:
                offenders.append((path.name, ln_no, line.strip()))
    assert not offenders, f"использован внутренний API click: {offenders!r}"


def test_no_action_repeats_its_class_name_as_a_string():
    """Имя класса, выписанное литералом рядом с самим классом, устаревает молча.

    `_detailed_action` собирал шестнадцать действий как
    `ActionChatCreate(type="ActionChatCreate", ...)`: переименование dataclass
    оставило бы литерал прежним, а значение `type` уходит в базу и в шаблоны,
    где сверяется по строке. Имя берётся у класса, а не пишется рядом.
    """
    offenders = [
        (node.lineno, keyword.value.value)
        for node in _calls(_tree("converter.py"))
        for keyword in node.keywords
        if keyword.arg == "type" and isinstance(keyword.value, ast.Constant)
        if isinstance(keyword.value.value, str)
    ]

    assert not offenders, f"type= задан строковым литералом: {offenders}"
