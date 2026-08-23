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
    """Collect _diag calls so tests can assert on visibility, not just text."""

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
    monkeypatch.setattr(cli_common, "_diag", diag)
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
    monkeypatch.setattr(cli_common, "_diag", diag)
    api = MagicMock()
    api.start_takeout = AsyncMock(side_effect=RPCError(request=None, message="no takeout"))

    assert await cli_export._start_takeout(api, _takeout_cfg(), require=False) is False
    essential = diag.texts(essential_only=True)
    assert any("regular API" in m for m in essential)


@pytest.mark.asyncio
async def test_start_takeout_cooldown_is_essential(monkeypatch):
    from telethon.errors import TakeoutInitDelayError

    diag = _RecordingDiag()
    monkeypatch.setattr(cli_common, "_diag", diag)
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

    monkeypatch.setattr(cli_common, "_diag", _RecordingDiag())
    api = MagicMock()
    api.start_takeout = AsyncMock(side_effect=RPCError(request=None, message="no takeout"))

    with pytest.raises(TakeoutUnavailableError):
        await cli_export._start_takeout(api, _takeout_cfg(), require=True)


@pytest.mark.asyncio
async def test_start_takeout_does_not_swallow_programming_errors(monkeypatch):
    # Широкий except Exception прятал и дефекты самого кода — например TypeError
    # из повреждённого takeout_id. Такие ошибки должны доходить до вызывающего.

    monkeypatch.setattr(cli_common, "_diag", _RecordingDiag())
    api = MagicMock()
    api.start_takeout = AsyncMock(side_effect=TypeError("object supporting the buffer API required"))

    with pytest.raises(TypeError):
        await cli_export._start_takeout(api, _takeout_cfg(), require=False)


# ----- Глобальные опции: уровень логирования, --quiet, работа без TTY -----


def test_resolve_log_level_priority(monkeypatch):
    from tg_export.cli.common import _resolve_log_level

    # Второй элемент пары -- включать ли собственные логи библиотек.
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    assert _resolve_log_level(debug=False, log_level=None) == (logging.WARNING, False)

    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    assert _resolve_log_level(debug=False, log_level=None) == (logging.ERROR, False)
    # Флаг перекрывает переменную окружения...
    assert _resolve_log_level(debug=False, log_level="INFO") == (logging.INFO, False)
    # ...а --debug перекрывает и флаг.
    assert _resolve_log_level(debug=True, log_level="INFO") == (logging.DEBUG, False)


def test_resolve_log_level_rejects_unknown_name(monkeypatch):
    import click

    from tg_export.cli.common import _resolve_log_level

    monkeypatch.setenv("LOG_LEVEL", "not-a-level")
    with pytest.raises(click.BadParameter):
        _resolve_log_level(debug=False, log_level=None)


def test_diag_hides_routine_lines_under_quiet(monkeypatch):
    from tg_export import cli

    monkeypatch.setattr(cli_common, "_QUIET", True)
    printed = []
    monkeypatch.setattr(cli.click, "echo", lambda msg, **kw: printed.append(msg))

    cli_common._diag("routine status")
    cli_common._diag("something failed", essential=True)

    assert printed == ["something failed"]


def test_diag_prints_everything_without_quiet(monkeypatch):
    from tg_export import cli

    monkeypatch.setattr(cli_common, "_QUIET", False)
    printed = []
    monkeypatch.setattr(cli.click, "echo", lambda msg, **kw: printed.append(msg))

    cli_common._diag("routine status")
    cli_common._diag("something failed", essential=True)

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

    monkeypatch.setattr(cli_common, "_diag", _RecordingDiag())
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
    monkeypatch.setattr("tg_export.cli.common._mgr", lambda: AccountManager(config_dir=cfg_dir))

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
    """Set up one account whose config names ``output_path``; return the manager."""
    from tg_export.auth import AccountManager
    from tg_export.cli.common import _mgr  # noqa: F401  -- patched below

    cfg_dir = tmp_path / "config"
    mgr = AccountManager(config_dir=cfg_dir)
    mgr.ensure_dirs()
    mgr.config_path(account).write_text(f"output:\n  path: {output_path}\n", encoding="utf-8")
    monkeypatch.setattr("tg_export.cli.common._mgr", lambda: AccountManager(config_dir=cfg_dir))
    return mgr


def test_each_account_exports_into_its_own_directory(tmp_path, monkeypatch):
    """`output.path` is the base; the alias is appended, as the documentation says.

    Without it two accounts wrote into one directory, shared one state database
    and took each other's files for their own during dedup.
    """

    base = tmp_path / "exports"
    _account_with_config(tmp_path, monkeypatch, str(base))

    _, _, output_base = cli_common._resolve_output("acc", None, None)

    assert output_base == base / "acc"


def test_a_path_that_already_names_the_account_is_not_doubled(tmp_path, monkeypatch):
    """Configs generated by earlier versions baked the alias into the path."""

    base = tmp_path / "exports" / "acc"
    _account_with_config(tmp_path, monkeypatch, str(base))

    _, _, output_base = cli_common._resolve_output("acc", None, None)

    assert output_base == base


def test_an_existing_export_stays_where_it_was_written(tmp_path, monkeypatch):
    """A directory that already holds an export is never moved under the alias."""
    from tg_export import cli

    base = tmp_path / "legacy"
    base.mkdir()
    (base / cli.STATE_DB_NAME).write_bytes(b"")
    _account_with_config(tmp_path, monkeypatch, str(base))

    _, _, output_base = cli_common._resolve_output("acc", None, None)

    assert output_base == base


def test_the_output_override_names_the_directory_itself(tmp_path, monkeypatch):
    """`--output` points at the export directory; nothing is appended to it."""

    _account_with_config(tmp_path, monkeypatch, str(tmp_path / "exports"))
    override = tmp_path / "elsewhere"

    _, _, output_base = cli_common._resolve_output("acc", None, override)

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

    monkeypatch.setattr(cli_common, "_opened_state", fake)
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

    monkeypatch.setattr(cli_common, "_opened_state", fake)
    monkeypatch.setattr(cli.click, "confirm", lambda *a, **k: pytest.fail("вопрос задан вопреки --yes"))

    code = asyncio.run(cli_state._state_reset("acc", None, None, True, True, None, skip_confirm=True))
    assert code == 0


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
    monkeypatch.setattr("tg_export.cli.common._mgr", lambda: AccountManager(config_dir=cfg_dir))

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
    monkeypatch.setattr("tg_export.cli.common._mgr", lambda: AccountManager(config_dir=cfg_dir))

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
