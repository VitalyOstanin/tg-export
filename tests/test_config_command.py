"""Команда `tg-export config`: что она показывает и откуда берёт значения."""

from click.testing import CliRunner

from tg_export import config as config_module
from tg_export.cli import main

_FULL_CONFIG = """\
output:
  path: {path}
  format: html
defaults:
  media:
    types: [photo]
    max_file_size: 500KB
    concurrent_downloads: 2
  export_service_messages: false
personal_info: true
contacts: false
sessions: true
userpics: false
stories: true
profile_music: false
other_data: true
left_channels:
  action: skip
archived:
  action: export_with_defaults
unmatched:
  action: skip
import_existing:
  - path: /data/desktop-export
    type: tdesktop
folders:
  Work:
    media:
      types: [photo]
      max_file_size: 10MB
type_rules:
  personal:
    media:
      types: [all]
      max_file_size: 1GB
chats:
  - id: 1234567890
    skip: true
"""


def _write_full_config(account_env, tmp_path) -> None:
    (account_env / "acc.yaml").write_text(_FULL_CONFIG.format(path=tmp_path / "exports"), encoding="utf-8")


def test_verbose_output_names_every_section_the_loader_understands(account_env, tmp_path):
    """Раздела, которого нет в выводе `config -v`, для пользователя не существует.

    Тем же доводом обоснована полнота шаблона `init`. Проверить действующее
    значение `archived` или `import_existing` было нечем: команда, назначение
    которой -- показать конфигурацию, эти разделы пропускала.
    """
    _write_full_config(account_env, tmp_path)

    result = CliRunner().invoke(main, ["config", "-v"])

    assert result.exit_code == 0, result.output
    missing = sorted(k for k in config_module._KNOWN_TOP_LEVEL_KEYS if k not in result.output)
    assert not missing, f"не показаны разделы: {missing}\n{result.output}"


def test_a_limit_below_a_megabyte_is_not_shown_as_zero(account_env, tmp_path):
    """`max_file_size: 500KB` печатался как `0MB` -- целочисленным делением."""
    _write_full_config(account_env, tmp_path)

    result = CliRunner().invoke(main, ["config", "-v"])

    assert result.exit_code == 0, result.output
    assert "500.0 KB" in result.output, result.output
    assert "0MB" not in result.output, result.output


def test_free_space_is_reported_for_the_export_directory(account_env, tmp_path, monkeypatch):
    """Каталог выгрузки читается тем же кодом, что и при экспорте.

    Разбор YAML напрямую не раскрывал `~` и не добавлял alias аккаунта, поэтому
    `output_path.exists()` был ложен и место сообщалось для текущего рабочего
    каталога -- ровно того раздела, о котором строка ничего сказать не должна.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "exports").mkdir()
    (account_env / "acc.yaml").write_text("output:\n  path: ~/exports\n", encoding="utf-8")
    (account_env / "config.yaml").write_text("min_free_space: 20GB\n", encoding="utf-8")

    result = CliRunner().invoke(main, ["config"])

    assert result.exit_code == 0, result.output
    assert f"(on {tmp_path / 'exports'})" in result.output, result.output
    assert "20.00 GB" in result.output, result.output


def test_an_unparsable_min_free_space_is_named_as_such(account_env, tmp_path):
    """`run` отказывает на записи, которую `config` показывал как действующую."""
    (account_env / "config.yaml").write_text("min_free_space: 20 GBs\n", encoding="utf-8")

    result = CliRunner().invoke(main, ["config"])

    assert result.exit_code == 0, result.output
    assert "20 GBs" in result.output, result.output
    assert "invalid" in result.output.lower(), result.output
