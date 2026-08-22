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
        "output:\n  format: both\n",
    )
    cfg = load_config(path)
    assert cfg.unmatched_action == "export_with_defaults"
    assert cfg.archived_action == "export_with_defaults"
    assert cfg.left_channels_action == "export_with_defaults"
    assert cfg.output.format == "both"


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
