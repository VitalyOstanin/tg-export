import tempfile
from pathlib import Path

import pytest

from tg_export.config import Config, ConfigError, load_config

FIXTURES = Path(__file__).parent / "fixtures"


def test_load_valid_config():
    cfg = load_config(FIXTURES / "valid_config.yaml")
    assert cfg.output.format == "html"
    assert cfg.defaults.media.max_file_size_bytes == 100 * 1024**2
    assert "photo" in cfg.defaults.media.types
    assert cfg.defaults.media.concurrent_downloads == 3


def test_load_minimal_config():
    cfg = load_config(FIXTURES / "minimal_config.yaml")
    assert cfg.defaults is not None


def test_resolve_chat_config_priority():
    """Приоритет: chats > folders.*.chats > folders.* > defaults"""
    cfg = load_config(FIXTURES / "valid_config.yaml")
    # Чат из секции chats (высший приоритет)
    chat_cfg = cfg.resolve_chat_config(chat_id=9876543210, chat_name="Секретный чат", folder=None)
    assert chat_cfg is not None
    assert chat_cfg.media.types == ["photo"]
    # Чат из defaults (нет правил)
    chat_cfg = cfg.resolve_chat_config(chat_id=9999999, chat_name="Unknown", folder=None)
    assert chat_cfg is None  # unmatched.action == skip


def test_parse_size_units():
    cfg = load_config(FIXTURES / "valid_config.yaml")
    assert cfg.defaults.media.max_file_size_bytes == 100 * 1024**2  # 100MB
    assert cfg.defaults.media.max_file_size_bytes == 100 * 1024**2  # 100MB


def test_type_rules_exact_match():
    """type_rules по точному типу."""
    from tg_export.config import TypeRule

    cfg = Config(
        type_rules={"bot": TypeRule(skip=True)},
        unmatched_action="export_with_defaults",
    )
    # bot -> skip
    result = cfg.resolve_chat_config(1, "SomeBot", None, chat_type="bot")
    assert result is None
    # personal -> defaults (no type_rule match)
    result = cfg.resolve_chat_config(2, "Person", None, chat_type="personal")
    assert result is not None


def test_type_rules_category_match():
    """type_rules по категории-шорткату."""
    from tg_export.config import MediaConfig, TypeRule

    media = MediaConfig(types=["photo"], max_file_size_bytes=10 * 1024**2)
    cfg = Config(
        type_rules={"public": TypeRule(media=media)},
        unmatched_action="export_with_defaults",
    )
    # public_channel -> matches "public" category
    result = cfg.resolve_chat_config(1, "News", None, chat_type="public_channel")
    assert result is not None
    assert result.media.types == ["photo"]
    assert result.media.max_file_size_bytes == 10 * 1024**2
    # private_group -> no match, falls to defaults
    result = cfg.resolve_chat_config(2, "Group", None, chat_type="private_group")
    assert result is not None
    assert result.media == cfg.defaults.media


def test_type_rules_exact_beats_category():
    """Точный тип приоритетнее категории."""
    from tg_export.config import MediaConfig, TypeRule

    media_exact = MediaConfig(types=["document"], max_file_size_bytes=100 * 1024**2)
    cfg = Config(
        type_rules={
            "private": TypeRule(skip=True),
            "bot": TypeRule(media=media_exact),
        },
        unmatched_action="export_with_defaults",
    )
    # bot is in "private" category, but exact "bot" rule takes priority
    result = cfg.resolve_chat_config(1, "Bot", None, chat_type="bot")
    assert result is not None
    assert result.media.types == ["document"]
    # personal -> matches "private" category -> skip
    result = cfg.resolve_chat_config(2, "Person", None, chat_type="personal")
    assert result is None


def test_type_rules_applies_inside_folder():
    """type_rules применяется внутри папки (бот в папке -> skip по type_rules)."""
    from tg_export.config import FolderRule, MediaConfig, TypeRule

    folder_media = MediaConfig(types=["photo", "video"], max_file_size_bytes=50 * 1024**2)
    cfg = Config(
        folders={"work": FolderRule(media=folder_media)},
        type_rules={"bots": TypeRule(skip=True)},
        unmatched_action="export_with_defaults",
    )
    # bot in folder "work" -> type_rules.bots.skip applies
    result = cfg.resolve_chat_config(1, "WorkBot", "work", chat_type="bot")
    assert result is None
    # non-bot in folder "work" -> folder media applies
    result = cfg.resolve_chat_config(2, "WorkChat", "work", chat_type="private_group")
    assert result is not None
    assert result.media.types == ["photo", "video"]


def test_folder_chats_beats_type_rules():
    """Явное правило в folders.chats побеждает type_rules."""
    from tg_export.config import ChatRule, FolderRule, MediaConfig, TypeRule

    bot_media = MediaConfig(types=["document"], max_file_size_bytes=10 * 1024**2)
    cfg = Config(
        folders={"work": FolderRule(chats=[ChatRule(id=1, media=bot_media)])},
        type_rules={"bots": TypeRule(skip=True)},
    )
    # bot explicitly listed in folder chats -> folder chat rule wins
    result = cfg.resolve_chat_config(1, "ImportantBot", "work", chat_type="bot")
    assert result is not None
    assert result.media.types == ["document"]


def _write_config(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(content)
    return path


def test_invalid_unmatched_action_raises(tmp_path):
    path = _write_config(tmp_path, "unmatched:\n  action: skipp\n")
    with pytest.raises(ConfigError, match="unmatched.action"):
        load_config(path)


def test_invalid_output_format_raises(tmp_path):
    path = _write_config(tmp_path, "output:\n  format: xml\n")
    with pytest.raises(ConfigError, match="output.format"):
        load_config(path)


def test_invalid_archived_action_raises(tmp_path):
    path = _write_config(tmp_path, "archived:\n  action: nope\n")
    with pytest.raises(ConfigError, match="archived.action"):
        load_config(path)


def test_unknown_top_level_key_raises(tmp_path):
    # `default` is a typo for `defaults` and must fail fast, not be ignored.
    path = _write_config(tmp_path, "default:\n  media:\n    types: [photo]\n")
    with pytest.raises(ConfigError, match="Unknown config key"):
        load_config(path)


def test_valid_actions_accepted(tmp_path):
    path = _write_config(
        tmp_path,
        "unmatched:\n  action: export_with_defaults\n"
        "archived:\n  action: export_with_defaults\n"
        "left_channels:\n  action: export_with_defaults\n"
        "output:\n  format: html\n",
    )
    cfg = load_config(path)
    assert cfg.unmatched_action == "export_with_defaults"
    assert cfg.archived_action == "export_with_defaults"
    assert cfg.left_channels_action == "export_with_defaults"
    assert cfg.output.format == "html"


def test_max_media_file_size_covers_rules_above_defaults(tmp_path):
    """Takeout открывается с одним лимитом на всю сессию, поэтому он должен
    покрывать самое большое ограничение из применимых правил.

    Правила chats / type_rules / folders задают собственный max_file_size, и
    значение больше, чем в defaults.media, -- обычный случай: мелкий лимит по
    умолчанию и один чат, из которого забирают всё.
    """
    path = tmp_path / "cfg.yaml"
    path.write_text(
        "defaults:\n"
        "  media:\n"
        "    types: [photo]\n"
        "    max_file_size: 50MB\n"
        "type_rules:\n"
        "  channels:\n"
        "    media:\n"
        "      types: [document]\n"
        "      max_file_size: 200MB\n"
        "chats:\n"
        "  - id: 111\n"
        "    media:\n"
        "      types: [document]\n"
        "      max_file_size: 2GB\n"
    )
    cfg = load_config(path)

    assert cfg.max_media_file_size() == 2 * 1024**3


def test_max_media_file_size_ignores_skipped_rules(tmp_path):
    """Правило со skip не приводит к скачиванию, поэтому его лимит не должен
    расширять область Takeout."""
    path = tmp_path / "cfg.yaml"
    path.write_text(
        "defaults:\n"
        "  media:\n"
        "    types: [photo]\n"
        "    max_file_size: 50MB\n"
        "chats:\n"
        "  - id: 111\n"
        "    skip: true\n"
        "    media:\n"
        "      types: [document]\n"
        "      max_file_size: 2GB\n"
    )
    cfg = load_config(path)

    assert cfg.max_media_file_size() == 50 * 1024**2


def test_max_media_file_size_ignores_chats_of_a_skipped_folder(tmp_path):
    """Пропуск папки отсекает и вложенные в неё чаты: resolve_chat_config
    возвращает None до того, как дойдёт до вложенных правил."""
    path = tmp_path / "cfg.yaml"
    path.write_text(
        "defaults:\n"
        "  media:\n"
        "    types: [photo]\n"
        "    max_file_size: 50MB\n"
        "folders:\n"
        "  Work:\n"
        "    skip: true\n"
        "    chats:\n"
        "      - id: 222\n"
        "        media:\n"
        "          types: [document]\n"
        "          max_file_size: 2GB\n"
    )
    cfg = load_config(path)

    assert cfg.max_media_file_size() == 50 * 1024**2


@pytest.mark.parametrize(
    "value",
    ["0", "-1", "6", "'3'", "true"],
    ids=["zero-hangs", "negative-raises", "above-range", "string", "bool"],
)
def test_concurrent_downloads_outside_the_documented_range_is_rejected(tmp_path, value):
    """`concurrent_downloads: 0` подвешивал экспорт молча и без таймаута.

    Значение уходило прямо в `asyncio.Semaphore`: ноль давал захват, который
    никогда не будет удовлетворён, отрицательное -- ValueError уже в середине
    настройки экспорта, строка -- TypeError там же. Документация при этом
    обещает диапазон 1-5.
    """
    path = _write_config(tmp_path, f"defaults:\n  media:\n    concurrent_downloads: {value}\n")
    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert "concurrent_downloads" in str(e.value)


def test_media_types_as_a_scalar_is_rejected(tmp_path):
    """`types: photo` делал проверку вхождения поиском по подстроке.

    При `types: video_note` условие `"video" not in "video_note"` ложно, и
    обычные видео скачивались, хотя настроены были только кружки.
    """
    path = _write_config(tmp_path, "defaults:\n  media:\n    types: photo\n")
    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert "types" in str(e.value)


def test_unknown_media_type_is_rejected(tmp_path):
    """Опечатка `photos` не давала ошибки: файлы просто никогда не скачивались."""
    path = _write_config(tmp_path, "defaults:\n  media:\n    types: [photos]\n")
    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert "photos" in str(e.value)
    assert "photo" in str(e.value)


def test_media_types_all_stays_valid(tmp_path):
    """`types: all` -- документированная форма записи скаляром."""
    path = _write_config(tmp_path, "defaults:\n  media:\n    types: all\n")
    assert load_config(path).defaults.media.types == ["all"]


def test_broken_yaml_reports_the_file_instead_of_a_traceback(tmp_path):
    """Синтаксическая ошибка YAML выходила трассировкой мимо обработчика."""
    path = _write_config(tmp_path, "output:\n  format: [html\n")
    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert str(path) in str(e.value)


def test_invalid_date_names_the_field(tmp_path):
    """`ValueError: Invalid isoformat string` не говорил, где именно ошибка."""
    path = _write_config(tmp_path, "defaults:\n  date_from: 01.05.2024\n")
    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert "date_from" in str(e.value)
    assert "01.05.2024" in str(e.value)


def test_date_of_a_wrong_type_is_not_silently_dropped(tmp_path):
    """`date_from: 2024` -- YAML-число; настройка молча терялась."""
    path = _write_config(tmp_path, "defaults:\n  date_from: 2024\n")
    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert "date_from" in str(e.value)


def test_import_existing_without_type_names_the_section(tmp_path):
    """Пропущенный ключ давал KeyError с трассировкой."""
    path = _write_config(tmp_path, "import_existing:\n  - path: /tmp/x\n")
    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert "import_existing" in str(e.value)
    assert "type" in str(e.value)


def test_unknown_import_existing_type_is_rejected(tmp_path):
    """Опечатка `tdektop` молча пропускалась потребителем."""
    path = _write_config(tmp_path, "import_existing:\n  - path: /tmp/x\n    type: tdektop\n")
    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert "tdektop" in str(e.value)


def test_unknown_type_rule_key_is_rejected(tmp_path):
    """Опечатка в имени типа чата отключала правило без единого сообщения."""
    path = _write_config(tmp_path, "type_rules:\n  privat:\n    skip: true\n")
    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert "privat" in str(e.value)


def test_output_path_expands_the_tilde(tmp_path):
    """`output.path: ~/exports` создавал каталог с именем `~` в текущем каталоге."""
    path = _write_config(tmp_path, "output:\n  path: ~/exports\n")
    cfg = load_config(path)
    assert not cfg.output.path.startswith("~"), cfg.output.path


def test_unimplemented_output_format_is_rejected(tmp_path):
    """`format: json` проходил валидацию и показывался как действующая настройка.

    Ни один потребитель значение не читал: рендерер создавался безусловно, и
    пользователь узнавал про HTML только по содержимому каталога.
    """
    path = _write_config(tmp_path, "output:\n  format: json\n")
    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert "json" in str(e.value)


def test_unmatched_action_ask_is_rejected(tmp_path):
    """`action: ask` работал как export_with_defaults.

    Интерактивной ветки в коде нет, а цена ошибки несимметрична: пользователь
    ждёт подтверждения по каждому неохваченному чату, а получает полную
    выгрузку всех несопоставленных.
    """
    path = _write_config(tmp_path, "unmatched:\n  action: ask\n")
    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert "ask" in str(e.value)


def test_rule_sections_must_be_mappings(tmp_path):
    """`skip: true` без отступа даёт скаляр вместо правила.

    Проверка `isinstance(d, dict)` в начале разбора правила была бесполезна:
    при False разбор всё равно шёл дальше и падал на `d["media"]` трассировкой
    `TypeError`, вместо сообщения о том, какой раздел конфигурации не тот.
    """
    for section, text in (
        ("type_rules.personal", "type_rules:\n  personal: true\n"),
        ("folders.Работа", "folders:\n  Работа: true\n"),
        ("chats[0]", "chats:\n  - true\n"),
    ):
        path = _write_config(tmp_path, text)
        with pytest.raises(ConfigError) as e:
            load_config(path)
        assert section in str(e.value), (section, str(e.value))


def test_the_example_config_loads_and_names_every_section():
    """`config.example.yaml` -- рабочая отправная точка, а не иллюстрация.

    Пример, который не загружается, хуже отсутствующего: он выглядит проверенным.
    Заодно сверяется состав разделов -- пример обязан называть каждый, который
    разбирает загрузчик, иначе о настройке узнают только из документации.
    """
    import re

    from tg_export.config import load_config

    root = Path(__file__).resolve().parent.parent
    example = root / "config.example.yaml"
    # Через копию: `load_config` защищает права файла с перечнем чатов, и на
    # отслеживаемом образце эта защита меняла режим с 644 на 600 -- незаметно,
    # потому что git хранит только бит исполнения.
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "config.yaml"
        copy.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        cfg = load_config(copy)
    assert cfg.output.path.endswith("export_output")

    source = example.read_text(encoding="utf-8")
    named = set(re.findall(r"^#?\s*([a-z_]+):", source, re.M))
    loader = (root / "tg_export" / "config.py").read_text(encoding="utf-8")
    # Именно `raw.get`, а не `def_raw.get`/`out_raw.get`: нужны разделы
    # верхнего уровня, а не поля внутри них.
    supported = set(re.findall(r'(?<![\w])raw\.get\("([a-z_]+)"', loader))
    assert supported <= named, f"в примере не названы разделы: {sorted(supported - named)}"


def test_the_generated_template_names_every_section():
    """Шаблон `tg-export init` -- основной способ узнать состав настроек.

    Раздела, которого в нём нет, для пользователя не существует: `archived` и
    `import_existing` разбирались кодом и были описаны в документации, но в
    шаблоне не упоминались.
    """
    import re

    from tg_export.catalog import generate_config_template

    template = generate_config_template([], account="acc")
    named = set(re.findall(r"^#?\s*([a-z_]+):", template, re.M))
    loader = (Path(__file__).resolve().parent.parent / "tg_export" / "config.py").read_text(encoding="utf-8")
    # Именно `raw.get`, а не `def_raw.get`/`out_raw.get`: нужны разделы
    # верхнего уровня, а не поля внутри них.
    supported = set(re.findall(r'(?<![\w])raw\.get\("([a-z_]+)"', loader))
    assert supported <= named, f"в шаблоне не названы разделы: {sorted(supported - named)}"


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_typo_inside_a_section_is_reported_not_replaced_by_a_default(tmp_path):
    """Опечатка в имени ключа секции меняла поведение молча.

    Проверка состава ключей действовала только на верхнем уровне, а внутри
    секций всё читалось через .get с умолчанием: `max_size` вместо
    `max_file_size` давал предел 100 MB вместо заданных 10 MB, `date_form`
    вместо `date_from` -- выгрузку всей истории вместо среза, `paht` вместо
    `path` -- выгрузку не в тот каталог.
    """
    cases = {
        "output": "output:\n  paht: ./somewhere\n",
        "defaults.media": "defaults:\n  media:\n    max_size: 10MB\n",
        "defaults": "defaults:\n  date_form: 2024-01-01\n",
        "left_channels": "left_channels:\n  actoin: export_with_defaults\n",
        "chats": 'chats:\n  - nmae: "X"\n',
    }
    for section, text in cases.items():
        with pytest.raises(ConfigError) as excinfo:
            load_config(_write(tmp_path, text))
        assert section.split(".")[0] in str(excinfo.value), (
            f"сообщение не называет секцию {section}: {excinfo.value}"
        )


def test_an_action_section_written_as_a_scalar_is_an_error(tmp_path):
    """`unmatched: export_with_defaults` означало ровно противоположное.

    Не-отображение сводилось к "skip" тернарником, поэтому просьба выгружать
    оборачивалась пропуском всех неохваченных чатов и кодом возврата 0.
    """
    for section in ("unmatched", "left_channels", "archived"):
        with pytest.raises(ConfigError) as excinfo:
            load_config(_write(tmp_path, f"{section}: export_with_defaults\n"))
        assert section in str(excinfo.value)


def test_a_chat_rule_that_names_no_chat_is_an_error(tmp_path):
    """Правило без id и без name не совпадёт ни с одним чатом и молча мертво."""
    with pytest.raises(ConfigError) as excinfo:
        load_config(_write(tmp_path, "chats:\n  - media:\n      types: [document]\n"))
    assert "chats[0]" in str(excinfo.value)


def test_a_section_written_as_a_scalar_names_the_section(tmp_path):
    """`output: ./dir` падал AttributeError без имени файла и секции."""
    for text, section in (
        ("output: ./dir\n", "output"),
        ("defaults: true\n", "defaults"),
        ("defaults:\n  media: photo\n", "defaults.media"),
    ):
        with pytest.raises(ConfigError) as excinfo:
            load_config(_write(tmp_path, text))
        assert section in str(excinfo.value), f"сообщение не называет {section}: {excinfo.value}"


def test_the_template_and_the_example_carry_the_defaults_of_the_loader(tmp_path):
    """Стартовый конфиг существует в двух видах, а умолчание -- одно.

    Значения вроде `concurrent_downloads: 3` были выписаны литералами в шаблоне
    `init`, в `config.example.yaml` и в разборе конфигурации: смена умолчания в
    коде оставляла оба артефакта тихо утверждающими прежнее значение.
    """
    from tg_export.catalog import generate_config_template
    from tg_export.config import GLOBAL_DATA_SECTIONS, Config, load_config

    defaults = Config()
    template = tmp_path / "template.yaml"
    template.write_text(generate_config_template([], account="acc"), encoding="utf-8")
    # Копия, а не сам образец: загрузчик ужесточает права поданного файла, и на
    # отслеживаемом файле репозитория это меняло режим с 644 на 600.
    example = tmp_path / "example.yaml"
    example.write_text(
        (Path(__file__).resolve().parent.parent / "config.example.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    for path in (template, example):
        cfg = load_config(path)
        assert cfg.output.path.endswith("export_output"), path
        assert cfg.output.format == defaults.output.format, path
        assert cfg.defaults.media.max_file_size_bytes == defaults.defaults.media.max_file_size_bytes, path
        assert cfg.defaults.media.concurrent_downloads == defaults.defaults.media.concurrent_downloads, path
        assert cfg.defaults.export_service_messages == defaults.defaults.export_service_messages, path
        assert cfg.left_channels_action == defaults.left_channels_action, path
        assert cfg.archived_action == defaults.archived_action, path
        assert cfg.unmatched_action == defaults.unmatched_action, path
        for flag in GLOBAL_DATA_SECTIONS:
            assert getattr(cfg, flag) == getattr(defaults, flag), (path, flag)


def test_every_global_data_section_is_known_everywhere_it_is_named():
    """Разделы общих данных перечислены в нескольких местах, и все — из одного.

    Раздел («личные данные», «контакты», «истории» и остальные) назывался
    заново в полях `Config`, в наборе известных ключей верхнего уровня, в
    разборе конфигурации, в шаблоне и в перечне для `config -v`. Пропуск в
    любом из этих мест проявлялся по-своему: отказом «Unknown config key(s)»,
    настройкой, которой не видно в `config -v`, или строкой, которой нет в
    шаблоне. Перечень объявлен один раз, и проверка сверяет с ним остальные.
    """
    from typing import get_type_hints

    from tg_export.catalog import generate_config_template
    from tg_export.cli.export import _global_data_summary
    from tg_export.config import _KNOWN_TOP_LEVEL_KEYS, GLOBAL_DATA_SECTIONS, Config

    flags = {name for name, hint in get_type_hints(Config).items() if hint is bool}
    assert set(GLOBAL_DATA_SECTIONS) == flags

    template = generate_config_template([], account="acc")
    summary = _global_data_summary(Config())
    for name in GLOBAL_DATA_SECTIONS:
        assert name in _KNOWN_TOP_LEVEL_KEYS, name
        assert f"{name}=" in summary, name
        assert f"{name}: " in template, name


def test_folder_media_is_not_overridden_by_a_type_rule():
    """Правило папки старше правила типа -- как объявлено в докстроке и документации.

    Внутри ветки папки `type_rules` спрашивались раньше медиа-настроек самой
    папки, и настройка папки не применялась вовсе, если для типа чата нашлось
    правило. Порядок «chats > folders.chats > folders > type_rules > defaults»
    записан и в методе, и в docs/configuration.md.
    """
    from tg_export.config import FolderRule, MediaConfig, TypeRule

    cfg = Config(
        folders={"Work": FolderRule(media=MediaConfig(types=["all"], max_file_size_bytes=1024**3))},
        type_rules={"channels": TypeRule(media=MediaConfig(types=["photo"], max_file_size_bytes=1024**2))},
    )

    result = cfg.resolve_chat_config(1, "c", "Work", chat_type="public_channel")

    assert result is not None
    assert result.media.types == ["all"], "правило типа перекрыло медиа-настройки папки"
    assert result.media.max_file_size_bytes == 1024**3


def test_a_chat_allowed_by_the_left_flag_is_exported_with_defaults():
    """`left_channels: export_with_defaults` обязан выгружать, а не молча пропускать.

    Значение снимало запрет, но чат затем шёл через общий подбор правил и
    отбрасывался веткой `unmatched: skip` -- значением по умолчанию и тем, что
    пишет в конфиг `init`. Покинутые каналы при этом запрашивались у сервера.
    """
    cfg = Config(left_channels_action="export_with_defaults")

    result = cfg.resolve_chat_config(
        1, "Left channel", None, chat_type="public_channel", allow_unmatched=True
    )

    assert result is not None, "чат, разрешённый флагом, отброшен как не подошедший ни под одно правило"
    assert result.media.types == cfg.defaults.media.types


def test_a_boolean_setting_written_as_a_string_is_refused(tmp_path):
    """`personal_info: "false"` включало то, что пользователь выключал.

    Строка в YAML истинна, поэтому личные данные выгружались вопреки записи.
    Остальные разделы свой тип проверяют -- булевы флаги были единственным
    исключением.
    """
    path = _write_config(tmp_path, 'personal_info: "false"\n')

    with pytest.raises(ConfigError, match="personal_info"):
        load_config(path)


def test_a_size_written_as_a_boolean_or_a_negative_number_is_refused():
    """`max_file_size: true` и отрицательный размер не могут быть размерами.

    `parse_size` пропускает любое число, а `bool` в Python -- число: `true`
    превращался в один байт, то есть в запрет скачивать что-либо, а
    отрицательное значение -- в тот же запрет с другой стороны.
    """
    from tg_export.config import parse_size

    with pytest.raises(ConfigError, match="[Ss]ize"):
        parse_size(True)
    with pytest.raises(ConfigError, match="[Ss]ize"):
        parse_size(-1)
    assert parse_size(100) == 100


def test_a_changed_default_reaches_a_file_that_omits_the_key(tmp_path, monkeypatch):
    """Умолчания были выписаны дважды: в поле dataclass и вторым литералом в загрузчике.

    Пятнадцать пар держались на внимательности: правка поля не долетала до
    файла, где секция есть, а нужный ключ опущен, и наоборот. Здесь умолчания
    подменяются на уровне классов, и загрузчик обязан отдать именно их.
    """
    from dataclasses import dataclass, field

    from tg_export import config as config_module
    from tg_export.config import DefaultsConfig, MediaConfig, OutputConfig

    @dataclass
    class WiderDefaults(DefaultsConfig):
        media: MediaConfig = field(
            default_factory=lambda: MediaConfig(
                types=["photo", "video"], max_file_size_bytes=7 * 1024**2, concurrent_downloads=2
            )
        )

    @dataclass
    class OtherOutput(OutputConfig):
        path: str = "./elsewhere"

    @dataclass
    class OtherConfig(Config):
        stories: bool = False
        archived_action: str = "export_with_defaults"

    monkeypatch.setattr(config_module, "DefaultsConfig", WiderDefaults)
    monkeypatch.setattr(config_module, "OutputConfig", OtherOutput)
    monkeypatch.setattr(config_module, "Config", OtherConfig)

    path = tmp_path / "config.yaml"
    path.write_text("output: {}\ndefaults:\n  media: {}\narchived: {}\n", encoding="utf-8")

    cfg = load_config(path)

    assert cfg.defaults.media.types == ["photo", "video"]
    assert cfg.defaults.media.max_file_size_bytes == 7 * 1024**2
    assert cfg.defaults.media.concurrent_downloads == 2
    assert cfg.output.path == "elsewhere"  # Path нормализует ведущее ./
    assert cfg.archived_action == "export_with_defaults"
    assert cfg.stories is False


def test_the_template_and_the_example_offer_the_same_media_types():
    """Шаблон `init` и `config.example.yaml` предлагают список типов шире умолчания.

    Умолчание загрузчика -- `[photo]`, и это записано в справочнике; шире --
    осознанно, чтобы стартовый конфиг не скачивал одни фотографии. Списки
    выписаны в двух файлах, и разойтись им мешает только эта проверка.
    """
    import yaml

    from tg_export.catalog import TEMPLATE_MEDIA_TYPES

    root = Path(__file__).resolve().parent.parent
    example = yaml.safe_load((root / "config.example.yaml").read_text(encoding="utf-8"))

    assert example["defaults"]["media"]["types"] == TEMPLATE_MEDIA_TYPES


def test_the_example_config_keeps_the_permissions_of_a_published_file():
    """Прогон тестов менял права отслеживаемого файла с 644 на 600.

    `load_config` защищает права файла с перечнем чатов аккаунта, и защита
    срабатывала на образце из репозитория: в свежем клоне 644, после первого
    прогона тестов -- 600. Правка незаметна, потому что git хранит только бит
    исполнения. Образец секретов не содержит, поэтому подавать его в загрузчик
    напрямую тесты не должны -- только копию.
    """
    import stat

    root = Path(__file__).resolve().parent.parent
    example = root / "config.example.yaml"

    assert stat.S_IMODE(example.stat().st_mode) & 0o044, oct(stat.S_IMODE(example.stat().st_mode))


def test_the_log_level_is_read_from_the_namespaced_variable_first(monkeypatch):
    """`LOG_LEVEL` занята многими инструментами и часто стоит в профиле оболочки.

    Значение из окружения проходит ту же проверку, что и флаг, поэтому одного
    `LOG_LEVEL=verbose` хватало, чтобы ни одна команда не запускалась, а
    сообщение называло `--log-level`, которого пользователь не передавал.
    """
    import logging

    from tg_export.cli.common import resolve_log_level

    monkeypatch.setenv("LOG_LEVEL", "verbose")
    monkeypatch.setenv("TG_EXPORT_LOG_LEVEL", "DEBUG")

    assert resolve_log_level(False, None) == (logging.DEBUG, False)


def test_an_unusable_log_level_from_the_environment_names_the_environment(monkeypatch):
    """Ошибка должна называть источник значения, иначе поиск причины уходит в argv."""
    import click
    import pytest as pytest_module

    from tg_export.cli.common import resolve_log_level

    monkeypatch.delenv("TG_EXPORT_LOG_LEVEL", raising=False)
    monkeypatch.setenv("LOG_LEVEL", "verbose")

    with pytest_module.raises(click.BadParameter) as excinfo:
        resolve_log_level(False, None)

    assert "LOG_LEVEL" in str(excinfo.value)
