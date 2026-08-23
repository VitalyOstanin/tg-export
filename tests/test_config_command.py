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


def test_config_speaks_json_like_the_other_query_commands(account_env, tmp_path):
    """Сводку настроек приходилось разбирать регулярным выражением по строкам вида `  key: value`.

    Флаг `--json` есть у `list`, `account list`, `account default`, `auth check`,
    `state show` и `tg info`; здесь его не было, хотя вывод команды -- данные.
    """
    import json

    (account_env / "config.yaml").write_text("min_free_space: 20GB\n", encoding="utf-8")
    (account_env / "acc.yaml").write_text(f"output:\n  path: {tmp_path / 'exports'}\n", encoding="utf-8")

    result = CliRunner().invoke(main, ["config", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["default_account"] == "acc"
    assert payload["min_free_space"] == "20.00 GB", payload
    assert payload["proxy"] is None
    assert payload["credentials"]["api_id"] == 1
    assert [acc["name"] for acc in payload["accounts"]] == ["acc"]
    assert payload["accounts"][0]["session"] is True


def test_the_json_of_config_never_carries_the_api_hash(account_env):
    """В человекочитаемом выводе печатаются четыре первых знака -- в машинном не должно быть и их."""
    import json

    result = CliRunner().invoke(main, ["config", "--json"])

    payload = json.loads(result.stdout)

    assert "api_hash" not in payload["credentials"], payload


def test_the_json_never_carries_the_proxy_password(account_env, tmp_path):
    """Пароль прокси не должен уходить в машиночитаемый вывод.

    Текстовый вывод той же команды показывает его как `***`, а `api_hash`
    исключён из JSON намеренно и закреплён тестом. Флаг `--json` добавлен
    позже, и секция `proxy` уходила в него сырой -- с паролем в открытом виде.
    """
    import json

    (account_env / "config.yaml").write_text(
        "proxy:\n  type: socks5\n  host: 10.0.0.1\n  port: 9050\n  username: alice\n  password: s3cret\n",
        encoding="utf-8",
    )
    _write_full_config(account_env, tmp_path)

    result = CliRunner().invoke(main, ["config", "--json"])

    assert result.exit_code == 0, result.output
    assert "s3cret" not in result.output, f"пароль прокси в выводе: {result.output}"
    payload = json.loads(result.stdout)
    assert payload["proxy"]["username"] == "alice"
    assert payload["proxy"]["password_set"] is True


def test_a_proxy_the_loader_rejects_is_not_shown_as_the_setting_in_force(account_env, tmp_path):
    """Настройка, с которой экспорт откажется работать, не выдаётся за действующую.

    Для `min_free_space` это уже сделано: запись, отвергнутая загрузчиком,
    показывается как `<как записано> (invalid: причина)`. Секция `proxy`
    читалась мимо загрузчика, поэтому опечатка в имени ключа давала одну
    неверную картину в `config` и другую -- в `run`.
    """
    (account_env / "config.yaml").write_text(
        "proxy:\n  type: socks\n  host: 10.0.0.1\n  prot: 9050\n", encoding="utf-8"
    )
    _write_full_config(account_env, tmp_path)

    result = CliRunner().invoke(main, ["config"])

    assert result.exit_code == 0, result.output
    assert "invalid" in result.output, f"негодная настройка показана как действующая: {result.output}"


def test_a_proxy_written_as_a_scalar_does_not_break_the_command(account_env, tmp_path):
    """Команда, которая показывает конфигурацию, обязана пережить любую её запись."""
    (account_env / "config.yaml").write_text("proxy: socks5://10.0.0.1:9050\n", encoding="utf-8")
    _write_full_config(account_env, tmp_path)

    result = CliRunner().invoke(main, ["config"])

    assert result.exit_code == 0, result.output
    assert "AttributeError" not in result.output, result.output
    assert "invalid" in result.output, result.output


def test_the_json_settings_name_every_section_the_text_output_shows(account_env, tmp_path):
    """Состав `-v --json` совпадает с составом текстового `-v`.

    Довод тот же, по которому в текстовый вывод внесены все разделы: раздела,
    которого в выводе нет, для читателя не существует -- и для программы,
    читающей JSON, в той же мере.
    """
    import json

    _write_full_config(account_env, tmp_path)

    result = CliRunner().invoke(main, ["config", "-v", "--json"])

    assert result.exit_code == 0, result.output
    settings = json.loads(result.stdout)["accounts"][0]["settings"]
    missing = sorted(k for k in config_module._KNOWN_TOP_LEVEL_KEYS if k not in json.dumps(settings))
    assert not missing, f"в JSON нет разделов: {missing}\n{json.dumps(settings, indent=2)}"
