import contextlib
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

from tg_export.cli import common as cli_common
from tg_export.cli import export as cli_export
from tg_export.cli import main
from tg_export.cli import state as cli_state
from tg_export.cli import tg as cli_tg


def test_main_help():
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "auth" in result.output
    assert "account" in result.output
    assert "init" in result.output
    assert "run" in result.output
    assert "verify" in result.output


def test_auth_help():
    runner = CliRunner()
    result = runner.invoke(main, ["auth", "--help"])
    assert result.exit_code == 0
    assert "add" in result.output
    assert "credentials" in result.output
    assert "check" in result.output


def test_account_help():
    runner = CliRunner()
    result = runner.invoke(main, ["account", "--help"])
    assert result.exit_code == 0
    assert "list" in result.output
    assert "default" in result.output
    assert "remove" in result.output


@pytest.mark.asyncio
async def test_render_index_respects_should_stop():
    # Если во время финального render index пришёл shutdown — должен ранний
    # выход до тяжёлого jinja-рендера, чтобы не блокировать executor.
    from tg_export.cli.export import _render_index

    state_mock = AsyncMock()
    renderer_mock = MagicMock()
    cfg_mock = MagicMock()

    await _render_index(renderer_mock, [], cfg_mock, state_mock, should_stop=lambda: True)

    renderer_mock.render_index.assert_not_called()


class _RecordingDiag:
    """Collect diag calls so tests can assert on visibility, not just text."""

    def __init__(self):
        self.calls = []

    def __call__(self, message, *, essential=False, **kwargs):
        self.calls.append((message, essential))

    def texts(self, *, essential_only=False):
        return [m for m, e in self.calls if e or not essential_only]


def _takeout_cfg(max_file_size=1024):
    cfg = MagicMock()
    cfg.contacts = True
    cfg.defaults.media.max_file_size_bytes = 1024
    cfg.max_media_file_size = lambda: max_file_size
    return cfg


@pytest.mark.asyncio
async def test_start_takeout_returns_true_on_success(monkeypatch):
    diag = _RecordingDiag()
    monkeypatch.setattr(cli_common, "diag", diag)
    api = MagicMock()
    api.start_takeout = AsyncMock()

    assert await cli_export._start_takeout(api, _takeout_cfg(), require=False) is True
    assert any("Takeout session started" in m for m in diag.texts())


@pytest.mark.asyncio
async def test_start_takeout_fallback_is_essential(monkeypatch):
    # Откат на обычный API меняет способ выгрузки и должен быть виден даже
    # под --quiet: раньше сообщение печаталось без essential и пропадало.
    from telethon.errors import RPCError

    diag = _RecordingDiag()
    monkeypatch.setattr(cli_common, "diag", diag)
    api = MagicMock()
    api.start_takeout = AsyncMock(side_effect=RPCError(request=None, message="no takeout"))

    assert await cli_export._start_takeout(api, _takeout_cfg(), require=False) is False
    essential = diag.texts(essential_only=True)
    assert any("regular API" in m for m in essential)


@pytest.mark.asyncio
async def test_start_takeout_cooldown_is_essential(monkeypatch):
    from telethon.errors import TakeoutInitDelayError

    diag = _RecordingDiag()
    monkeypatch.setattr(cli_common, "diag", diag)
    err = TakeoutInitDelayError(request=None, capture=0)
    err.seconds = 7200
    api = MagicMock()
    api.start_takeout = AsyncMock(side_effect=err)

    assert await cli_export._start_takeout(api, _takeout_cfg(), require=False) is False
    essential = diag.texts(essential_only=True)
    assert any("cooldown" in m and "2h" in m for m in essential)


@pytest.mark.asyncio
async def test_start_takeout_require_turns_fallback_into_error(monkeypatch):
    from telethon.errors import RPCError

    from tg_export.errors import TakeoutUnavailableError

    monkeypatch.setattr(cli_common, "diag", _RecordingDiag())
    api = MagicMock()
    api.start_takeout = AsyncMock(side_effect=RPCError(request=None, message="no takeout"))

    with pytest.raises(TakeoutUnavailableError):
        await cli_export._start_takeout(api, _takeout_cfg(), require=True)


@pytest.mark.asyncio
async def test_start_takeout_does_not_swallow_programming_errors(monkeypatch):
    # Широкий except Exception прятал и дефекты самого кода — например TypeError
    # из повреждённого takeout_id. Такие ошибки должны доходить до вызывающего.
    monkeypatch.setattr(cli_common, "diag", _RecordingDiag())
    api = MagicMock()
    api.start_takeout = AsyncMock(side_effect=TypeError("object supporting the buffer API required"))

    with pytest.raises(TypeError):
        await cli_export._start_takeout(api, _takeout_cfg(), require=False)


# ----- Глобальные опции: уровень логирования, --quiet, работа без TTY -----


def test_resolve_log_level_priority(monkeypatch):
    from tg_export.cli.common import resolve_log_level

    # Второй элемент пары -- включать ли собственные логи библиотек.
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    assert resolve_log_level(debug=False, log_level=None) == (logging.WARNING, False)

    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    assert resolve_log_level(debug=False, log_level=None) == (logging.ERROR, False)
    # Флаг перекрывает переменную окружения...
    assert resolve_log_level(debug=False, log_level="INFO") == (logging.INFO, False)
    # ...а --debug перекрывает и флаг.
    assert resolve_log_level(debug=True, log_level="INFO") == (logging.DEBUG, False)


def test_resolve_log_level_rejects_unknown_name(monkeypatch):
    import click

    from tg_export.cli.common import resolve_log_level

    monkeypatch.setenv("LOG_LEVEL", "not-a-level")
    with pytest.raises(click.BadParameter):
        resolve_log_level(debug=False, log_level=None)


def test_diag_hides_routine_lines_under_quiet(monkeypatch):
    from tg_export import cli

    monkeypatch.setattr(cli_common, "_QUIET", True)
    printed = []
    monkeypatch.setattr(cli.click, "echo", lambda msg, **kw: printed.append(msg))

    cli_common.diag("routine status")
    cli_common.diag("something failed", essential=True)

    assert printed == ["something failed"]


def test_diag_prints_everything_without_quiet(monkeypatch):
    from tg_export import cli

    monkeypatch.setattr(cli_common, "_QUIET", False)
    printed = []
    monkeypatch.setattr(cli.click, "echo", lambda msg, **kw: printed.append(msg))

    cli_common.diag("routine status")
    cli_common.diag("something failed", essential=True)

    assert printed == ["routine status", "something failed"]


@pytest.mark.parametrize(
    "is_terminal,quiet,expected", [(False, False, False), (True, True, False), (True, False, True)]
)
def test_live_display_requires_a_terminal_and_no_quiet(monkeypatch, is_terminal, quiet, expected):
    # Без TTY (вывод в файл, конвейер) и под --quiet прогресс не должен
    # перерисовываться поверх строк: экспортёр переходит на построчный вывод.
    import asyncio

    from rich.console import Console

    from tg_export import exporter as exporter_module

    fake_console = Console(quiet=True)
    monkeypatch.setattr(type(fake_console), "is_terminal", property(lambda self: is_terminal))
    monkeypatch.setattr(exporter_module, "console", fake_console)

    downloader = AsyncMock()
    downloader.active_downloads = {}
    # snapshot_active_downloads на AsyncMock вернул бы корутину; экспортёр её
    # отбросит, но незавершённая корутина оставит RuntimeWarning.
    downloader.snapshot_active_downloads = lambda: {}
    exp = exporter_module.Exporter(
        api=MagicMock(),
        state=AsyncMock(),
        config=MagicMock(),
        renderer=MagicMock(),
        downloader=downloader,
        account="acc",
        quiet=quiet,
    )
    asyncio.run(exp.run(dry_run=True, chat_list=[]))

    assert exp._use_live is expected


def test_upload_progress_yields_nothing_under_quiet(monkeypatch):
    monkeypatch.setattr(cli_common, "_QUIET", True)
    with cli_tg._upload_progress(by_bytes=True) as progress:
        assert progress is None


@pytest.mark.asyncio
async def test_start_takeout_asks_for_the_largest_configured_limit(monkeypatch):
    """Лимит Takeout берётся не из defaults, а из максимума по применимым
    правилам: правило чата с большим max_file_size иначе было бы урезано
    самой сессией."""
    monkeypatch.setattr(cli_common, "diag", _RecordingDiag())
    api = MagicMock()
    api.start_takeout = AsyncMock()

    await cli_export._start_takeout(api, _takeout_cfg(max_file_size=2 * 1024**3), require=False)

    assert api.start_takeout.await_args.kwargs["max_file_size"] == 2 * 1024**3


def test_init_from_catalog_writes_the_config_file(tmp_path, monkeypatch):
    """`init --from` печатал «Config saved», не записав файла.

    Результат разбора каталога никуда не присваивался, шаблон не строился,
    файл не создавался -- а именно этот способ документация называет штатным.
    Пользователь получал сообщение об успехе и «Config not found» на run.
    """
    import yaml
    from click.testing import CliRunner

    from tg_export.auth import AccountManager
    from tg_export.cli import main

    cfg_dir = tmp_path / "config"
    mgr = AccountManager(config_dir=cfg_dir)
    mgr.ensure_dirs()
    monkeypatch.setattr("tg_export.cli.common.account_manager", lambda: AccountManager(config_dir=cfg_dir))

    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        yaml.dump(
            {
                "folders": {"Work": [{"id": 10, "name": "Team", "type": "private_group", "messages": 5}]},
                "unfiled": [{"id": 20, "name": "Notes", "type": "self", "messages": 1}],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    out = tmp_path / "generated.yaml"

    result = CliRunner().invoke(
        main, ["init", "--account", "acc", "--from", str(catalog), "--output", str(out)]
    )

    assert result.exit_code == 0, result.output
    assert out.exists(), result.output
    text = out.read_text(encoding="utf-8")
    assert "Team" in text
    assert "Notes" in text


def _account_with_config(tmp_path, monkeypatch, output_path: str, account: str = "acc"):
    """Завести один аккаунт, в конфиге которого указан ``output_path``; вернуть менеджер."""
    from tg_export.auth import AccountManager
    from tg_export.cli.common import account_manager  # noqa: F401  -- patched below

    cfg_dir = tmp_path / "config"
    mgr = AccountManager(config_dir=cfg_dir)
    mgr.ensure_dirs()
    mgr.config_path(account).write_text(f"output:\n  path: {output_path}\n", encoding="utf-8")
    monkeypatch.setattr("tg_export.cli.common.account_manager", lambda: AccountManager(config_dir=cfg_dir))
    return mgr


def test_each_account_exports_into_its_own_directory(tmp_path, monkeypatch):
    """`output.path` -- базовый каталог, alias приписывается, как и сказано в документации.

    Без этого два аккаунта писали в один каталог, делили одну базу состояния и
    при дедупликации принимали чужие файлы за свои.
    """
    base = tmp_path / "exports"
    _account_with_config(tmp_path, monkeypatch, str(base))

    _, _, output_base = cli_common.resolve_output("acc", None, None)

    assert output_base == base / "acc"


def test_a_path_that_already_names_the_account_is_not_doubled(tmp_path, monkeypatch):
    """Конфиги прежних версий зашивали alias прямо в путь."""
    base = tmp_path / "exports" / "acc"
    _account_with_config(tmp_path, monkeypatch, str(base))

    _, _, output_base = cli_common.resolve_output("acc", None, None)

    assert output_base == base


def test_an_existing_export_stays_where_it_was_written(tmp_path, monkeypatch):
    """Каталог, в котором уже лежит выгрузка, под alias не переносится."""
    from tg_export import cli

    base = tmp_path / "legacy"
    base.mkdir()
    (base / cli.STATE_DB_NAME).write_bytes(b"")
    _account_with_config(tmp_path, monkeypatch, str(base))

    _, _, output_base = cli_common.resolve_output("acc", None, None)

    assert output_base == base


def test_the_output_override_names_the_directory_itself(tmp_path, monkeypatch):
    """`--output` указывает на сам каталог выгрузки, к нему ничего не приписывается."""
    _account_with_config(tmp_path, monkeypatch, str(tmp_path / "exports"))
    override = tmp_path / "elsewhere"

    _, _, output_base = cli_common.resolve_output("acc", None, override)

    assert output_base == override


def test_h_is_a_synonym_of_help():
    """`-h` завершался кодом 2 с «No such option»."""
    from click.testing import CliRunner

    from tg_export.cli import main

    result = CliRunner().invoke(main, ["-h"])
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output


def test_every_option_is_described_in_help():
    """Часть опций выводилась в --help без единого слова описания.

    У `run` те же `--config`/`--output` описаны, у `state show` и `state reset`
    -- нет; расходились не только формулировки, но и сам факт описания.
    """
    import click

    from tg_export.cli import main

    undocumented = []

    def walk(command, path):
        for param in command.params:
            if isinstance(param, click.Option) and not param.help and not param.hidden:
                if param.name in {"help"}:
                    continue
                undocumented.append(f"{' '.join(path)} {param.opts[0]}")
        if isinstance(command, click.Group):
            for name, sub in command.commands.items():
                walk(sub, [*path, name])

    walk(main, ["tg-export"])
    assert not undocumented, f"опции без help=: {undocumented}"


def test_reset_of_the_whole_database_asks_before_deleting(tmp_path, monkeypatch):
    """`state reset --all --delete-messages` стирал базу без единого вопроса.

    `purge` показывает объём удаляемого и спрашивает подтверждение, а сброс
    всего состояния -- нет, хотя опечатка в команде стоит повторной выгрузки
    всего аккаунта.
    """
    import asyncio
    import contextlib
    from unittest.mock import MagicMock

    from tg_export import cli
    from tg_export.state import ExportState

    st = ExportState(tmp_path / "state.db")

    @contextlib.asynccontextmanager
    async def fake(*_a, **_k):
        await st.open()
        try:
            yield st, MagicMock(), "acc"
        finally:
            await st.close()

    monkeypatch.setattr(cli_common, "opened_state", fake)
    asked = []
    monkeypatch.setattr(cli.click, "confirm", lambda text, **kw: asked.append(text) or False)

    code = asyncio.run(cli_state._state_reset("acc", None, None, True, True, None, skip_confirm=False))

    assert asked, "подтверждение не запрошено"
    assert code == 0


def test_reset_of_the_whole_database_skips_the_question_with_yes(tmp_path, monkeypatch):
    import asyncio
    import contextlib
    from unittest.mock import MagicMock

    from tg_export import cli
    from tg_export.state import ExportState

    st = ExportState(tmp_path / "state.db")

    @contextlib.asynccontextmanager
    async def fake(*_a, **_k):
        await st.open()
        try:
            yield st, MagicMock(), "acc"
        finally:
            await st.close()

    monkeypatch.setattr(cli_common, "opened_state", fake)
    monkeypatch.setattr(cli.click, "confirm", lambda *a, **k: pytest.fail("вопрос задан вопреки --yes"))

    code = asyncio.run(cli_state._state_reset("acc", None, None, True, True, None, skip_confirm=True))
    assert code == 0


def _state_with_one_chat(tmp_path, monkeypatch, chat_id=42):
    """Открытое состояние с записью одного чата, подставленное командам `state`."""
    import contextlib
    from unittest.mock import MagicMock

    from tg_export.state import ExportState

    st = ExportState(tmp_path / "state.db")

    @contextlib.asynccontextmanager
    async def fake(*_a, **_k):
        await st.open()
        await st.set_last_msg_id(chat_id, 100)
        try:
            yield st, MagicMock(), "acc"
        finally:
            await st.close()

    monkeypatch.setattr(cli_common, "opened_state", fake)
    return st


def test_reset_of_one_chat_asks_before_deleting_its_messages(tmp_path, monkeypatch):
    """`state reset <chat_id> --delete-messages` стирал сообщения чата молча.

    Вопрос стоял только в ветке `--all`, поэтому `--yes`, описанный в справке
    как пропуск подтверждения, для одного чата пропускать было нечего. Соседние
    необратимые операции -- `purge` и `state reset --all` -- сначала показывают
    объём удаляемого строкой `essential`, чтобы вопрос не остался без предмета
    под `--quiet`.
    """
    import asyncio

    from tg_export import cli

    _state_with_one_chat(tmp_path, monkeypatch)
    lines = []
    monkeypatch.setattr(cli_common, "diag", lambda text, **kw: lines.append((text, kw)))
    asked = []
    monkeypatch.setattr(cli.click, "confirm", lambda text, **kw: asked.append(text) or False)

    code = asyncio.run(cli_state._state_reset("acc", None, None, False, True, 42, skip_confirm=False))

    assert asked, "подтверждение не запрошено"
    assert code == 0
    summary = [text for text, kw in lines if kw.get("essential") and "messages=" in text]
    assert summary, f"объём удаляемого не показан: {lines}"


def test_reset_of_one_chat_without_deleting_messages_asks_nothing(tmp_path, monkeypatch):
    """Сброс прогресса обратим -- он стоит повторной выгрузки, а не данных."""
    import asyncio

    from tg_export import cli

    _state_with_one_chat(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli.click, "confirm", lambda *a, **k: pytest.fail("вопрос задан о обратимой операции")
    )

    code = asyncio.run(cli_state._state_reset("acc", None, None, False, False, 42, skip_confirm=False))
    assert code == 0


def test_reset_of_one_chat_skips_the_question_with_yes(tmp_path, monkeypatch):
    """`--yes` пропускает вопрос и для одного чата, а не только для `--all`."""
    import asyncio

    from tg_export import cli

    st = _state_with_one_chat(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.click, "confirm", lambda *a, **k: pytest.fail("вопрос задан вопреки --yes"))

    code = asyncio.run(cli_state._state_reset("acc", None, None, False, True, 42, skip_confirm=True))
    assert code == 0
    assert st is not None


def _catalog_file(tmp_path):
    """Каталог чатов, из которого init строит шаблон."""
    import yaml

    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        yaml.dump(
            {
                "folders": {"Work": [{"id": 10, "name": "Team", "type": "private_group", "messages": 5}]},
                "unfiled": [],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return catalog


def test_init_refuses_to_overwrite_an_existing_config(tmp_path, monkeypatch):
    """Конфиг -- единственный неповторимый артефакт настройки.

    Сессии и каталог чатов восстанавливаются, а написанные руками правила по
    папкам, лимиты и даты -- нет. Повторный init для обновления списка чатов
    записывал шаблон поверх них без вопроса и завершался кодом 0.
    """
    from click.testing import CliRunner

    from tg_export.auth import AccountManager
    from tg_export.cli import main

    cfg_dir = tmp_path / "config"
    AccountManager(config_dir=cfg_dir).ensure_dirs()
    monkeypatch.setattr("tg_export.cli.common.account_manager", lambda: AccountManager(config_dir=cfg_dir))

    existing = tmp_path / "generated.yaml"
    existing.write_text("chats:\n  - id: 1\n    skip: true\n", encoding="utf-8")

    result = CliRunner().invoke(
        main, ["init", "--account", "acc", "--from", str(_catalog_file(tmp_path)), "--output", str(existing)]
    )

    assert result.exit_code == 1, result.output
    assert existing.read_text(encoding="utf-8") == "chats:\n  - id: 1\n    skip: true\n"
    assert "--force" in result.output


def test_init_force_keeps_the_previous_config_next_to_the_new_one(tmp_path, monkeypatch):
    """С --force перезапись выполняется, но прежний файл сохраняется рядом."""
    from click.testing import CliRunner

    from tg_export.auth import AccountManager
    from tg_export.cli import main

    cfg_dir = tmp_path / "config"
    AccountManager(config_dir=cfg_dir).ensure_dirs()
    monkeypatch.setattr("tg_export.cli.common.account_manager", lambda: AccountManager(config_dir=cfg_dir))

    existing = tmp_path / "generated.yaml"
    existing.write_text("chats:\n  - id: 1\n    skip: true\n", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        [
            "init",
            "--account",
            "acc",
            "--from",
            str(_catalog_file(tmp_path)),
            "--output",
            str(existing),
            "--force",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Team" in existing.read_text(encoding="utf-8")
    backup = existing.with_suffix(existing.suffix + ".bak")
    assert backup.read_text(encoding="utf-8") == "chats:\n  - id: 1\n    skip: true\n"


def test_help_never_names_a_command_that_does_not_exist():
    """Справка одиннадцати объявлений `--account` отсылала к `auth default`.

    Такой команды нет: аккаунт по умолчанию задаёт `account default`, и
    сообщение об ошибке называет её верно, а справка -- нет. Пользователь,
    следующий подсказке, получал `Error: No such command 'auth default'`.
    Проверка общая: любое имя команды, названное справкой, должно резолвиться.
    """
    import re

    import click

    from tg_export.cli import main

    def walk(command, path):
        yield path, command
        if isinstance(command, click.Group):
            for name, sub in command.commands.items():
                yield from walk(sub, (*path, name))

    tree = dict(walk(main, ()))
    assert tree, "дерево команд пусто -- проверка ничего не проверяет"
    top_level = {name for (path, _) in tree.items() for name in path[:1]}
    assert "account" in top_level, top_level

    quoted = re.compile(r"[`']([a-z][a-z0-9 _-]*)[`']")
    offenders = []
    for path, command in tree.items():
        texts = [
            command.help or "",
            command.short_help or "",
            getattr(command, "epilog", None) or "",
            *(getattr(param, "help", None) or "" for param in command.params),
        ]
        for text in texts:
            for quote in quoted.findall(text):
                words = quote.replace("tg-export", "", 1).split()
                if not words or words[0] not in top_level:
                    continue
                named = tuple(words)
                if named not in tree:
                    offenders.append(f"{' '.join(path) or 'tg-export'}: {quote!r}")

    assert not offenders, f"справка называет несуществующие команды: {offenders}"


def test_a_date_range_reads_the_same_in_defaults_and_in_a_rule():
    """Блок описания правила был скопирован, и копии разошлись оформлением.

    `defaults.date_range` печатался с пробелами вокруг тире, правила чатов и
    типов -- без них, хотя показывают одно и то же.
    """
    from types import SimpleNamespace

    from tg_export.cli.export import _date_range, _rule_summary

    rule = SimpleNamespace(skip=False, media=None, date_from="2024-01-01", date_to="2024-02-01")

    assert _rule_summary(rule) == f"dates={_date_range('2024-01-01', '2024-02-01')}"
    assert _date_range("2024-01-01", None) == "2024-01-01 — ..."
    assert _rule_summary(SimpleNamespace(skip=True)) == "skip"
    assert _rule_summary(SimpleNamespace(skip=False, media=None, date_from=None, date_to=None)) == (
        "defaults"
    )


def test_every_command_over_a_config_names_the_way_out(tmp_path, monkeypatch, account_env):
    """Отсутствие конфигурации -- одна стена для пяти команд, выход из неё один.

    Подсказку `tg-export init` передавала одна `run`, а `verify`, `state show`,
    `state reset` и `purge` отказывали голым «Config not found».
    """
    for args in (["state", "show"], ["purge", "1"], ["verify"], ["run"]):
        result = CliRunner().invoke(main, args)

        assert result.exit_code == 1, (args, result.output)
        assert "Config not found" in result.output, (args, result.output)
        assert "tg-export init --account acc" in result.output, (args, result.output)


def test_the_option_named_output_always_means_a_directory():
    """`--output` -- каталог; файл результата называется `--output-file`.

    `list` и `tg info` уже переведены на `--output-file`, а `init --output`
    принимал путь файла конфигурации: по соседним командам он читается как
    каталог, и `tg-export init --output ~/configs` создаёт файл с именем
    `configs`. Каталог экспорта описывался тоже по-разному: `Override output
    directory` у `run` против `Export output directory` у остальных.
    """
    import click

    wrong_meaning = []
    wordings = {}

    def walk(command, path):
        for param in command.params:
            if not isinstance(param, click.Option) or param.opts[0] != "--output":
                continue
            where = " ".join(path)
            if "directory" not in (param.help or "").lower():
                wrong_meaning.append(f"{where} --output: {param.help!r}")
            elif not any(len(opt) == 2 for opt in param.opts):
                wordings.setdefault(param.help, []).append(where)
        if isinstance(command, click.Group):
            for name, sub in command.commands.items():
                walk(sub, [*path, name])

    walk(main, ["tg-export"])

    assert not wrong_meaning, f"--output означает не каталог: {wrong_meaning}"
    assert len(wordings) == 1, f"каталог экспорта описан по-разному: {wordings}"


def test_account_default_prints_the_alias_alone_on_stdout(account_env):
    """Пояснение в потоке данных попадало в подстановку `NAME=$(tg-export account default)`.

    Правило проекта: stdout принадлежит машиночитаемому выводу, пояснения идут
    в stderr. Команда печатала `Default account: acc` целиком в stdout, поэтому
    значение приходилось выкусывать разбором строки.
    """
    result = CliRunner().invoke(main, ["account", "default"])

    assert result.exit_code == 0, result.output
    assert result.stdout == "acc\n", result.stdout
    assert "Default account" in result.stderr, result.stderr


def test_account_default_without_a_default_leaves_stdout_empty(tmp_path, monkeypatch):
    """«Не задан» -- это пояснение, а не значение: соседняя `account list` отвечает так же."""
    from tg_export.auth import AccountManager
    from tg_export.cli import common as cli_common

    cfg_dir = tmp_path / "config"
    mgr = AccountManager(config_dir=cfg_dir)
    mgr.ensure_dirs()
    monkeypatch.setattr(cli_common, "account_manager", lambda: AccountManager(config_dir=cfg_dir))

    result = CliRunner().invoke(main, ["account", "default"])

    assert result.exit_code == 0, result.output
    assert result.stdout == "", result.stdout
    assert "No default account set" in result.stderr, result.stderr


def test_account_default_speaks_json_like_its_neighbours(account_env):
    """`--json` есть у `account list`, `auth check`, `state show` и `tg info`."""
    import json as json_mod

    result = CliRunner().invoke(main, ["account", "default", "--json"])

    assert result.exit_code == 0, result.output
    assert json_mod.loads(result.stdout) == {"default": "acc"}


def test_auth_check_takes_the_account_as_an_option_too(tmp_path, monkeypatch):
    """Девять команд принимают `--account`, а `auth check` -- только позиционный NAME.

    Обёртка, подставляющая `--account "$ALIAS"` во все вызовы, на этой команде
    прежде отказывала: опции с таким именем у неё не было.
    """
    from unittest.mock import AsyncMock, MagicMock

    from tg_export.auth import AccountManager
    from tg_export.cli import common as cli_common

    cfg_dir = tmp_path / "config"
    mgr = AccountManager(config_dir=cfg_dir)
    mgr.ensure_dirs()
    mgr.save_credentials(1, "hash")
    for alias in ("first", "second"):
        mgr.session_path(alias).write_bytes(b"")
    monkeypatch.setattr(cli_common, "account_manager", lambda: AccountManager(config_dir=cfg_dir))

    api = MagicMock()
    api.__aenter__ = AsyncMock(side_effect=RuntimeError("cannot connect"))
    api.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("tg_export.api.TgApi", lambda *a, **k: api)

    result = CliRunner().invoke(main, ["auth", "check", "--account", "second"])

    assert "second" in result.output, result.output
    assert "first" not in result.output, result.output


def test_takeout_clear_takes_the_account_as_an_option_too(tmp_path, monkeypatch):
    import contextlib
    from unittest.mock import MagicMock

    from tg_export.cli import common as cli_common

    seen = []

    @contextlib.asynccontextmanager
    async def fake(account_name):
        seen.append(account_name)
        api = MagicMock()
        api.client.session = None
        yield api, account_name or "acc"

    monkeypatch.setattr(cli_common, "connected_api", fake)

    result = CliRunner().invoke(main, ["takeout", "clear", "--account", "second"])

    assert result.exit_code == 0, result.output
    assert seen == ["second"], seen


def test_naming_the_account_twice_in_two_ways_is_refused(account_env):
    """Позиционное имя и `--account` -- одно и то же поле; два разных значения молча
    терять нельзя."""
    result = CliRunner().invoke(main, ["auth", "check", "first", "--account", "second"])

    assert result.exit_code == 2, result.output
    assert "usage" in result.output.lower(), result.output
    assert "twice" in result.output, result.output


def test_purge_removes_the_chat_directory_and_names_the_rows_it_deleted(tmp_path, account_env):
    """Единственная необратимая команда проверяется целиком: база, диск и отчёт о сделанном.

    Прежде подтверждённое удаление не исполнял ни один тест: единственный тест,
    доходивший до вопроса, отвечал «n», а `purge_chat` подменялся заглушкой,
    возвращавшей число вместо словаря счётчиков.
    """
    import asyncio

    from tg_export.cli.common import STATE_DB_NAME
    from tg_export.state import ExportState

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (account_env / "acc.yaml").write_text(f"output:\n  path: {out_dir}\n")

    async def fill():
        async with ExportState(out_dir / STATE_DB_NAME) as state:
            for chat_id, name in ((42, "Chat"), (43, "Other")):
                await state.register_file(
                    file_id=chat_id,
                    chat_id=chat_id,
                    msg_id=1,
                    expected_size=10,
                    actual_size=10,
                    local_path=f"/tmp/{chat_id}.jpg",
                )
                await state.set_last_msg_id(chat_id, 1)
                await state.cache_catalog(
                    chat_id=chat_id,
                    name=name,
                    chat_type="user",
                    folder=None,
                    members_count=None,
                    messages_count=0,
                    last_message_date=None,
                    is_left=False,
                    is_archived=False,
                    is_forum=False,
                    is_monoforum=False,
                )

    asyncio.run(fill())

    doomed = out_dir / "unfiled" / "Chat_42"
    neighbour = out_dir / "unfiled" / "Other_43"
    for directory in (doomed, neighbour):
        directory.mkdir(parents=True)
        (directory / "photo.jpg").write_bytes(b"content")

    result = CliRunner().invoke(main, ["purge", "42"], input="y\n")

    assert result.exit_code == 0, result.output
    assert not doomed.exists(), "каталог чата остался на диске"
    assert (neighbour / "photo.jpg").exists(), "удалён каталог соседнего чата"
    assert "Deleted from DB: messages=0, files=1, export_state=1, catalog_cache=1" in result.output, (
        f"итог удаления выведен не так, как предпросмотр: {result.output!r}"
    )

    async def survivors():
        async with ExportState(out_dir / STATE_DB_NAME) as state:
            return await state.count_chat_rows(42), await state.count_chat_rows(43)

    purged, kept = asyncio.run(survivors())
    assert all(number == 0 for number in purged.values()), purged
    assert kept["files"] == 1 and kept["catalog_cache"] == 1, kept


def test_the_confirmation_question_goes_to_stderr(tmp_path, account_env):
    """Вопрос печатался в stdout, а его предмет -- в stderr, то есть ровно наоборот.

    При `purge chat > /dev/null` пользователь видел описание удаляемого и
    дальше молчание: процесс ждал ответа, а вопрос ушёл в перенаправление. Для
    скрипта stdout переставал быть пустым и получал прозу вместе с эхом ответа.
    """
    import asyncio

    from tg_export.cli.common import STATE_DB_NAME
    from tg_export.state import ExportState

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (account_env / "acc.yaml").write_text(f"output:\n  path: {out_dir}\n")

    async def fill():
        async with ExportState(out_dir / STATE_DB_NAME) as state:
            await state.set_last_msg_id(42, 1)

    asyncio.run(fill())

    result = CliRunner().invoke(main, ["purge", "42"], input="n\n")

    assert result.exit_code == 0, result.output
    assert result.stdout == "", f"stdout принадлежит машиночитаемому выводу: {result.stdout!r}"
    assert "Delete all data for this chat?" in result.stderr


def test_the_state_table_rule_matches_its_header(tmp_path, account_env):
    """Линейка под шапкой была несвязанным числом 80 при шапке в 62 знака.

    Ширины колонок выписаны в двух форматных строках, а длина разделителя -- в
    третьем месте, поэтому правка любой ширины требовала синхронной правки
    трёх мест, а совпадения не было и до неё.
    """
    import asyncio

    from tg_export.cli.common import STATE_DB_NAME
    from tg_export.state import ExportState

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (account_env / "acc.yaml").write_text(f"output:\n  path: {out_dir}\n")

    async def fill():
        async with ExportState(out_dir / STATE_DB_NAME) as state:
            await state.set_last_msg_id(42, 7)

    asyncio.run(fill())

    result = CliRunner().invoke(main, ["state", "show"])

    assert result.exit_code == 0, result.output
    header, rule, *data = result.stdout.splitlines()
    assert set(rule) == {"-"}, rule
    assert len(rule) == len(header), f"шапка {len(header)}, линейка {len(rule)}"
    assert all(len(line) <= len(rule) or line.startswith(header[:7]) for line in data)


def test_account_default_speaks_json_after_setting_it_too(account_env):
    """Единственное место, где `--json` не давал документа.

    Ветка задания аккаунта возвращалась раньше разбора флага, печатая
    пояснение в stderr: `tg-export account default acc --json | jq -r .default`
    падал разбором пустого входа, хотя справочник обещает объект без оговорок.
    """
    import json as json_mod

    result = CliRunner().invoke(main, ["account", "default", "acc", "--json"])

    assert result.exit_code == 0, result.output
    assert json_mod.loads(result.stdout) == {"default": "acc"}


def test_account_list_json_puts_a_document_on_stdout(account_env):
    """README обещает «в stdout только JSON» девяти командам; проверялись пять."""
    import json as json_mod

    result = CliRunner().invoke(main, ["account", "list", "--json"])

    assert result.exit_code == 0, result.output
    payload = json_mod.loads(result.stdout)
    assert [entry["name"] for entry in payload] == ["acc"], payload
    assert payload[0]["default"] is True, payload


def test_auth_check_json_puts_a_document_on_stdout(account_env, monkeypatch):
    """Ветки ok/not_authorized и сам json.dumps не выполнялись ни разу."""
    import json as json_mod
    from unittest.mock import AsyncMock, MagicMock

    api = MagicMock()
    api.__aenter__ = AsyncMock(side_effect=RuntimeError("cannot connect"))
    api.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("tg_export.api.TgApi", lambda *a, **k: api)

    result = CliRunner().invoke(main, ["auth", "check", "--json"])

    payload = json_mod.loads(result.stdout)
    assert [entry["account"] for entry in payload] == ["acc"], payload
    assert payload[0]["status"] == "error", payload


def test_state_show_json_puts_a_document_on_stdout(tmp_path, monkeypatch, account_env):
    import json as json_mod

    from tg_export.cli import common as cli_common
    from tg_export.state import ExportState

    state_db = tmp_path / "state.db"

    @contextlib.asynccontextmanager
    async def opened(account, config_override, output_override):
        async with ExportState(state_db) as st:
            yield st, tmp_path, "acc"

    monkeypatch.setattr(cli_common, "opened_state", opened)

    result = CliRunner().invoke(main, ["state", "show", "--json"])

    assert result.exit_code == 0, result.output
    assert json_mod.loads(result.stdout) == []
