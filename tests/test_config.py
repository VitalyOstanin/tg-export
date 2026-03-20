import pytest
from pathlib import Path
from tg_export.config import load_config, Config, ConfigError

FIXTURES = Path(__file__).parent / "fixtures"


def test_load_valid_config():
    cfg = load_config(FIXTURES / "valid_config.yaml")
    assert cfg.account == "my_phone"
    assert cfg.output.format == "html"
    assert cfg.output.messages_per_file == 1000
    assert cfg.output.min_free_space_bytes == 20 * 1024**3
    assert cfg.defaults.media.max_file_size_bytes == 50 * 1024**2
    assert "photo" in cfg.defaults.media.types
    assert cfg.defaults.media.concurrent_downloads == 3


def test_load_minimal_config():
    cfg = load_config(FIXTURES / "minimal_config.yaml")
    assert cfg.account is not None
    assert cfg.defaults is not None


def test_resolve_chat_config_priority():
    """Приоритет: chats > folders.*.chats > folders.* > defaults"""
    cfg = load_config(FIXTURES / "valid_config.yaml")
    # Чат из секции chats (высший приоритет)
    chat_cfg = cfg.resolve_chat_config(chat_id=9876543210, chat_name="Секретный чат", folder=None)
    assert chat_cfg.media.types == ["photo"]
    # Чат из defaults (нет правил)
    chat_cfg = cfg.resolve_chat_config(chat_id=9999999, chat_name="Unknown", folder=None)
    assert chat_cfg is None  # unmatched.action == skip


def test_parse_size_units():
    cfg = load_config(FIXTURES / "valid_config.yaml")
    assert cfg.output.min_free_space_bytes == 20 * 1024**3  # 20GB
    assert cfg.defaults.media.max_file_size_bytes == 50 * 1024**2  # 50MB


def test_invalid_config_missing_account():
    with pytest.raises(ConfigError):
        load_config(FIXTURES / "invalid_no_account.yaml")
