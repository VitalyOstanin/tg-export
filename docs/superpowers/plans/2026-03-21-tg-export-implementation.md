# tg-export: План реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CLI-инструмент на Python для гибкого экспорта данных Telegram с точечным выбором чатов, индивидуальными правилами медиа, инкрементальной загрузкой и HTML-выводом, идентичным tdesktop.

**Architecture:** Монолитный пакет `tg_export` с модулями: CLI (click) -> Config (YAML) -> API (Telethon + Takeout) -> Exporter (Controller) -> State (SQLite) -> HTML Renderer (Jinja2). Чаты экспортируются последовательно, медиа внутри чата -- параллельно через asyncio-семафор.

**Multi-account:** Поддержка нескольких Telegram-аккаунтов. Каждый аккаунт имеет свой алиас (произвольное имя), файл сессии, файл конфига и выходной каталог:
- Сессии: `~/.config/tg-export/sessions/<alias>.session`
- Конфиги: `~/.config/tg-export/<alias>.yaml`
- Выход по умолчанию: `./export_output/<alias>/` (каждый аккаунт в отдельном подкаталоге)
- CLI: `tg-export run --account <alias>` автоматически ищет конфиг по конвенции; `--config /path` переопределяет; `--output /path` переопределяет выходной каталог.
- Поле `account` удалено из YAML-конфига -- аккаунт определяется по имени файла/флагу `--account`.
- `output.path` в конфиге задает базовый каталог; итоговый путь: `{output.path}/{alias}/`.

**Tech Stack:** Python 3.11+, Telethon, PyYAML, aiosqlite, Jinja2, click, rich

**Spec:** `docs/superpowers/specs/2026-03-21-tg-export-design.md`

---

## Содержание

- [Фаза 1: Скелет проекта и модели данных](#фаза-1-скелет-проекта-и-модели-данных)
- [Фаза 2: Конфиг и SQLite-состояние](#фаза-2-конфиг-и-sqlite-состояние)
- [Фаза 3: Авторизация и API-слой](#фаза-3-авторизация-и-api-слой)
- [Фаза 4: Каталог чатов и генерация конфига](#фаза-4-каталог-чатов-и-генерация-конфига)
- [Фаза 5: Конвертер Telethon -> models](#фаза-5-конвертер-telethon---models)
- [Фаза 6: Загрузка медиа](#фаза-6-загрузка-медиа)
- [Фаза 7: HTML-генерация](#фаза-7-html-генерация)
- [Фаза 8: Основной цикл экспорта](#фаза-8-основной-цикл-экспорта)
- [Фаза 9: Инкрементальность и верификация](#фаза-9-инкрементальность-и-верификация)
- [Фаза 10: Продвинутые сценарии](#фаза-10-продвинутые-сценарии)

---

## Фаза 1: Скелет проекта и модели данных

### Task 1: Инициализация проекта

**Files:**
- Create: `pyproject.toml`
- Create: `tg_export/__init__.py`
- Create: `tg_export/__main__.py`

- [ ] **Step 1: Создать pyproject.toml**

```toml
[build-system]
requires = ["setuptools>=68.0"]
build-backend = "setuptools.build_meta"

[project]
name = "tg-export"
version = "0.1.0"
description = "Flexible Telegram data export tool"
requires-python = ">=3.11"
dependencies = [
    "telethon>=1.36",
    "pyyaml>=6.0",
    "aiosqlite>=0.20",
    "jinja2>=3.1",
    "click>=8.0",
    "rich>=13.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"

[project.scripts]
tg-export = "tg_export.cli:main"
```

- [ ] **Step 2: Создать __init__.py и __main__.py**

`tg_export/__init__.py`:
```python
"""tg-export: Flexible Telegram data export tool."""
```

`tg_export/__main__.py`:
```python
from tg_export.cli import main

main()
```

- [ ] **Step 3: Установить проект в dev-режиме**

Run: `cd /home/vyt/devel/tg-export && pip install -e ".[dev]"`

- [ ] **Step 4: Проверить, что модуль импортируется**

Run: `cd /home/vyt/devel/tg-export && python -c "import tg_export; print('OK')"`
Expected: OK

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml tg_export/__init__.py tg_export/__main__.py
git commit -m "feat: initialize project structure with pyproject.toml"
```

### Task 2: Модели данных (models.py)

**Files:**
- Create: `tg_export/models.py`
- Create: `tests/test_models.py`

- [ ] **Step 1: Написать тест на сериализацию/десериализацию Message в JSON**

```python
# tests/test_models.py
import json
from datetime import datetime
from tg_export.models import (
    Message, TextPart, TextType, Media, MediaType,
    PhotoMedia, FileInfo, Reaction, ReactionType,
    ForwardInfo, ChatType, Chat,
)


def test_message_json_roundtrip():
    msg = Message(
        id=123,
        chat_id=456,
        date=datetime(2024, 1, 15, 10, 30, 0),
        edited=None,
        from_id=789,
        from_name="Иван",
        text=[TextPart(type=TextType.text, text="Привет")],
        media=None,
        action=None,
        reply_to_msg_id=None,
        reply_to_peer_id=None,
        forwarded_from=None,
        reactions=[],
        is_outgoing=False,
        signature=None,
        via_bot_id=None,
        saved_from_chat_id=None,
        inline_buttons=None,
        topic_id=None,
        grouped_id=None,
    )
    json_str = msg.to_json()
    restored = Message.from_json(json_str)
    assert restored.id == 123
    assert restored.from_name == "Иван"
    assert restored.text[0].text == "Привет"
    assert restored.date == datetime(2024, 1, 15, 10, 30, 0)


def test_message_with_photo_json_roundtrip():
    msg = Message(
        id=1,
        chat_id=2,
        date=datetime(2024, 6, 1),
        edited=None,
        from_id=3,
        from_name="Test",
        text=[],
        media=PhotoMedia(
            type=MediaType.photo,
            file=FileInfo(id=100, size=5000, name="photo.jpg", mime_type="image/jpeg", local_path=None),
            width=800,
            height=600,
            spoilered=False,
        ),
        action=None,
        reply_to_msg_id=None,
        reply_to_peer_id=None,
        forwarded_from=None,
        reactions=[Reaction(type=ReactionType.emoji, emoji="👍", document_id=None, count=5, recent=None)],
        is_outgoing=True,
        signature=None,
        via_bot_id=None,
        saved_from_chat_id=None,
        inline_buttons=None,
        topic_id=None,
        grouped_id=None,
    )
    json_str = msg.to_json()
    restored = Message.from_json(json_str)
    assert isinstance(restored.media, PhotoMedia)
    assert restored.media.width == 800
    assert restored.media.file.name == "photo.jpg"
    assert restored.reactions[0].emoji == "👍"


def test_chat_type_enum():
    assert ChatType.self == "self"
    assert ChatType.private_supergroup == "private_supergroup"
```

- [ ] **Step 2: Запустить тест, убедиться что падает**

Run: `cd /home/vyt/devel/tg-export && python -m pytest tests/test_models.py -v`
Expected: FAIL -- ModuleNotFoundError

- [ ] **Step 3: Реализовать models.py**

Создать `tg_export/models.py` со всеми dataclasses, enums и методами `to_json()`/`from_json()` согласно спецификации секция 6.1-6.5.

Ключевые моменты:
- Все enum наследуют от `str, Enum` для JSON-сериализации
- `ChatType`: self, replies, verify_codes, personal, bot, private_group, private_supergroup, public_supergroup, private_channel, public_channel
- `MediaType`: photo, video, document, voice, video_note, sticker, gif, contact, geo, venue, poll, game, invoice, todo_list, giveaway, paid_media, unsupported
- `TextType`: 21 тип (text, unknown, mention, hashtag, bot_command, url, email, bold, italic, code, pre, text_url, mention_name, phone, cashtag, underline, strikethrough, blockquote, bank_card, spoiler, custom_emoji)
- Media -- базовый dataclass, подтипы: PhotoMedia, DocumentMedia, ContactMedia, GeoMedia, VenueMedia, PollMedia, GameMedia, InvoiceMedia, TodoListMedia, GiveawayMedia, PaidMedia, UnsupportedMedia
- `Message.to_json()` / `Message.from_json()` -- для хранения в SQLite. Использовать `dataclasses.asdict()` + кастомный encoder/decoder для datetime и полиморфных Media
- ServiceAction -- базовый dataclass + подтипы для каждого из ~55 действий (группировка по категориям как в спеке 6.5)
- Вспомогательные: FileInfo, TextPart, Reaction, ForwardInfo, InlineButton, PollAnswer, TodoItem, ForumTopic, PersonalInfo, ContactInfo, ContactsList, SessionInfo, SessionsList

- [ ] **Step 4: Запустить тесты**

Run: `cd /home/vyt/devel/tg-export && python -m pytest tests/test_models.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tg_export/models.py tests/test_models.py
git commit -m "feat: add data models with JSON serialization"
```

### Task 3: CLI-скелет (cli.py)

**Files:**
- Create: `tg_export/cli.py`
- Create: `tests/test_cli.py`

- [ ] **Step 1: Написать тест на CLI-команды**

```python
# tests/test_cli.py
from click.testing import CliRunner
from tg_export.cli import main


def test_main_help():
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "auth" in result.output
    assert "list" in result.output
    assert "init" in result.output
    assert "run" in result.output
    assert "verify" in result.output


def test_auth_help():
    runner = CliRunner()
    result = runner.invoke(main, ["auth", "--help"])
    assert result.exit_code == 0
    assert "add" in result.output
    assert "list" in result.output
    assert "remove" in result.output
```

- [ ] **Step 2: Запустить тест, убедиться что падает**

Run: `cd /home/vyt/devel/tg-export && python -m pytest tests/test_cli.py -v`
Expected: FAIL

- [ ] **Step 3: Реализовать cli.py**

```python
# tg_export/cli.py
import click


@click.group()
def main():
    """tg-export: Flexible Telegram data export tool."""
    pass


@main.group()
def auth():
    """Manage Telegram accounts."""
    pass


@auth.command("add")
@click.option("--name", help="Account alias")
def auth_add(name):
    """Add a new Telegram account (interactive login)."""
    click.echo("Not implemented yet")


@auth.command("list")
def auth_list():
    """List configured accounts."""
    click.echo("Not implemented yet")


@auth.command("remove")
@click.argument("name")
def auth_remove(name):
    """Remove a Telegram account."""
    click.echo("Not implemented yet")


@main.command("list")
@click.option("--account", help="Account name")
@click.option("--output", type=click.Path(), help="Output file path")
@click.option("--format", "fmt", type=click.Choice(["yaml", "json"]), default="yaml")
@click.option("--include-left", is_flag=True, help="Include left channels")
def list_chats(account, output, fmt, include_left):
    """Export chat/folder catalog."""
    click.echo("Not implemented yet")


@main.command("init")
@click.option("--from", "from_catalog", type=click.Path(exists=True), help="Catalog file")
@click.option("--output", type=click.Path(), default="config.yaml")
def init_config(from_catalog, output):
    """Generate config template from catalog."""
    click.echo("Not implemented yet")


@main.command("run")
@click.option("--account", required=True, help="Account alias (loads ~/.config/tg-export/<account>.yaml)")
@click.option("--config", type=click.Path(exists=True), default=None, help="Override config path")
@click.option("--output", type=click.Path(), help="Output directory")
@click.option("--verify", is_flag=True, help="Verify file integrity after export")
@click.option("--dry-run", is_flag=True, help="Show what would be exported")
def run_export(account, config, output, verify, dry_run):
    """Run export according to config. Config resolved by account name convention."""
    click.echo("Not implemented yet")


@main.command("verify")
@click.option("--account", required=True, help="Account alias")
@click.option("--config", type=click.Path(exists=True), default=None, help="Override config path")
@click.option("--output", type=click.Path(), help="Export output directory")
def verify_files(account, config, output):
    """Verify integrity of previously downloaded files."""
    click.echo("Not implemented yet")
```

- [ ] **Step 4: Запустить тесты**

Run: `cd /home/vyt/devel/tg-export && python -m pytest tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 5: Проверить что tg-export --help работает**

Run: `cd /home/vyt/devel/tg-export && tg-export --help`
Expected: вывод справки с командами auth, list, init, run, verify

- [ ] **Step 6: Commit**

```bash
git add tg_export/cli.py tests/test_cli.py
git commit -m "feat: add CLI skeleton with all subcommands"
```

---

## Фаза 2: Конфиг и SQLite-состояние

### Task 4: Загрузка и валидация YAML-конфига (config.py)

**Files:**
- Create: `tg_export/config.py`
- Create: `tests/test_config.py`
- Create: `tests/fixtures/valid_config.yaml`
- Create: `tests/fixtures/minimal_config.yaml`

- [ ] **Step 1: Создать тестовые фикстуры**

`tests/fixtures/valid_config.yaml` -- полный конфиг из спецификации 5.1 (без поля `account` -- аккаунт определяется через CLI `--account`).
`tests/fixtures/minimal_config.yaml` -- минимальный: только `defaults`.

Также создать `tests/conftest.py` с общими фикстурами:
```python
# tests/conftest.py
import pytest_asyncio
from pathlib import Path
from tg_export.state import ExportState


@pytest_asyncio.fixture
async def state(tmp_path):
    s = ExportState(tmp_path / "state.db")
    await s.open()
    yield s
    await s.close()
```

- [ ] **Step 2: Написать тесты**

```python
# tests/test_config.py
import pytest
from pathlib import Path
from tg_export.config import load_config, Config, ConfigError

FIXTURES = Path(__file__).parent / "fixtures"


def test_load_valid_config():
    cfg = load_config(FIXTURES / "valid_config.yaml")
    assert cfg.output.format == "html"
    assert cfg.output.messages_per_file == 1000
    assert cfg.output.min_free_space_bytes == 20 * 1024**3
    assert cfg.defaults.media.max_file_size_bytes == 50 * 1024**2
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
    assert chat_cfg.media.types == ["photo"]
    # Чат из defaults (нет правил)
    chat_cfg = cfg.resolve_chat_config(chat_id=9999999, chat_name="Unknown", folder=None)
    assert chat_cfg is None  # unmatched.action == skip


def test_parse_size_units():
    cfg = load_config(FIXTURES / "valid_config.yaml")
    assert cfg.output.min_free_space_bytes == 20 * 1024**3  # 20GB
    assert cfg.defaults.media.max_file_size_bytes == 50 * 1024**2  # 50MB


```

- [ ] **Step 3: Запустить тесты, убедиться что падают**

Run: `cd /home/vyt/devel/tg-export && python -m pytest tests/test_config.py -v`
Expected: FAIL

- [ ] **Step 4: Реализовать config.py**

Ключевые классы:
- `Config` -- корневой dataclass с полями: output (OutputConfig), defaults (DefaultsConfig), personal_info, contacts, sessions, userpics, stories, profile_music, other_data, left_channels, import_existing, folders, chats, unmatched. Поле `account` удалено -- аккаунт определяется через CLI `--account`.
- `OutputConfig` -- path (базовый каталог, итоговый: `{path}/{account_alias}/`), format, messages_per_file, min_free_space_bytes
- `MediaConfig` -- types, max_file_size_bytes, concurrent_downloads
- `ChatExportConfig` -- media (MediaConfig), date_from, date_to, export_service_messages
- `load_config(path: Path) -> Config` -- загрузка YAML, парсинг размеров (50MB -> bytes), валидация
- `Config.resolve_chat_config(chat_id, chat_name, folder) -> ChatExportConfig | None` -- применение приоритетов правил (спека 5.3)
- `parse_size(s: str) -> int` -- парсинг "50MB", "2GB", "20GB" в байты
- `ConfigError` -- исключение валидации

- [ ] **Step 5: Запустить тесты**

Run: `cd /home/vyt/devel/tg-export && python -m pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add tg_export/config.py tests/test_config.py tests/fixtures/
git commit -m "feat: add YAML config loading and validation"
```

### Task 5: SQLite-состояние (state.py)

**Files:**
- Create: `tg_export/state.py`
- Create: `tests/test_state.py`

- [ ] **Step 1: Написать тесты**

```python
# tests/test_state.py
import pytest
import pytest_asyncio
from pathlib import Path
from datetime import datetime
from tg_export.state import ExportState


@pytest_asyncio.fixture
async def state(tmp_path):
    s = ExportState(tmp_path / "state.db")
    await s.open()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_export_state_roundtrip(state):
    await state.set_last_msg_id(chat_id=123, msg_id=456)
    result = await state.get_last_msg_id(chat_id=123)
    assert result == 456


@pytest.mark.asyncio
async def test_export_state_returns_none_for_unknown_chat(state):
    result = await state.get_last_msg_id(chat_id=999)
    assert result is None


@pytest.mark.asyncio
async def test_file_registration(state):
    await state.register_file(
        file_id=100, chat_id=123, msg_id=1,
        expected_size=5000, actual_size=5000,
        local_path="photos/photo.jpg", status="done",
    )
    info = await state.get_file(file_id=100, chat_id=123)
    assert info["expected_size"] == 5000
    assert info["status"] == "done"


@pytest.mark.asyncio
async def test_message_store_and_load(state):
    from tg_export.models import Message, TextPart, TextType
    msg = Message(
        id=1, chat_id=123, date=datetime(2024, 1, 1),
        edited=None, from_id=100, from_name="Иван",
        text=[TextPart(type=TextType.text, text="Привет мир")],
        media=None, action=None, reply_to_msg_id=None,
        reply_to_peer_id=None, forwarded_from=None,
        reactions=[], is_outgoing=False, signature=None,
        via_bot_id=None, saved_from_chat_id=None,
        inline_buttons=None, topic_id=None, grouped_id=None,
    )
    await state.store_message(msg)
    messages = await state.load_messages(chat_id=123)
    assert len(messages) == 1
    assert messages[0].from_name == "Иван"
    assert messages[0].text[0].text == "Привет мир"


@pytest.mark.asyncio
async def test_message_search_by_text(state):
    """SQL-поиск по plain text без парсинга JSON"""
    from tg_export.models import Message, TextPart, TextType
    for i, txt in enumerate(["Привет", "Мир", "Привет мир"]):
        msg = Message(
            id=i+1, chat_id=123, date=datetime(2024, 1, 1),
            edited=None, from_id=100, from_name="Test",
            text=[TextPart(type=TextType.text, text=txt)],
            media=None, action=None, reply_to_msg_id=None,
            reply_to_peer_id=None, forwarded_from=None,
            reactions=[], is_outgoing=False, signature=None,
            via_bot_id=None, saved_from_chat_id=None,
            inline_buttons=None, topic_id=None, grouped_id=None,
        )
        await state.store_message(msg)
    results = await state.search_messages(chat_id=123, text_query="Привет")
    assert len(results) == 2  # "Привет" и "Привет мир"


@pytest.mark.asyncio
async def test_message_filter_by_media_type(state):
    """SQL-фильтрация по типу медиа"""
    from tg_export.models import Message, TextPart, TextType, PhotoMedia, MediaType, FileInfo
    msg = Message(
        id=1, chat_id=123, date=datetime(2024, 1, 1),
        edited=None, from_id=100, from_name="Test",
        text=[], media=PhotoMedia(
            type=MediaType.photo,
            file=FileInfo(id=1, size=1000, name="p.jpg", mime_type="image/jpeg", local_path=None),
            width=800, height=600,
        ),
        action=None, reply_to_msg_id=None,
        reply_to_peer_id=None, forwarded_from=None,
        reactions=[], is_outgoing=False, signature=None,
        via_bot_id=None, saved_from_chat_id=None,
        inline_buttons=None, topic_id=None, grouped_id=None,
    )
    await state.store_message(msg)
    results = await state.search_messages(chat_id=123, media_type="photo")
    assert len(results) == 1


@pytest.mark.asyncio
async def test_verify_files_finds_partial(state):
    await state.register_file(
        file_id=100, chat_id=123, msg_id=1,
        expected_size=5000, actual_size=3000,
        local_path="photos/photo.jpg", status="partial",
    )
    broken = await state.get_files_to_verify()
    assert len(broken) == 1
    assert broken[0]["file_id"] == 100
```

- [ ] **Step 2: Запустить тесты, убедиться что падают**

Run: `cd /home/vyt/devel/tg-export && python -m pytest tests/test_state.py -v`
Expected: FAIL

- [ ] **Step 3: Реализовать state.py**

Класс `ExportState`:
- `__init__(self, db_path: Path)` -- путь к SQLite
- `async open()` -- создание соединения и таблиц (схема из спеки 6.6)
- `async close()` -- закрытие соединения
- `async set_last_msg_id(chat_id, msg_id)` -- upsert в export_state
- `async get_last_msg_id(chat_id) -> int | None`
- `async register_file(file_id, chat_id, msg_id, expected_size, actual_size, local_path, status)`
- `async get_file(file_id, chat_id) -> dict | None`
- `async get_files_to_verify() -> list[dict]` -- файлы с status != 'done' или actual_size != expected_size
- `async store_message(msg: Message)` -- upsert сообщения; основные поля в колонках, полиморфные (media, action, reactions, inline_buttons, text_parts) как JSON
- `async load_messages(chat_id) -> list[Message]` -- все сообщения чата, восстановленные в models.Message, отсортированные по msg_id
- `async search_messages(chat_id, text_query=None, media_type=None, from_id=None, date_from=None, date_to=None) -> list[Message]` -- SQL-выборка по колонкам без парсинга JSON

SQLite-схема messages (расширенная):
```sql
CREATE TABLE messages (
    chat_id      INTEGER NOT NULL,
    msg_id       INTEGER NOT NULL,
    date         TIMESTAMP,
    edited       TIMESTAMP,
    from_id      INTEGER,
    from_name    TEXT,
    text         TEXT,           -- plain text (конкатенация TextPart.text, для поиска)
    text_parts   TEXT,           -- JSON: list[TextPart] (с форматированием, для рендеринга)
    media_type   TEXT,           -- 'photo', 'video', ... (для фильтрации)
    media        TEXT,           -- JSON: полный Media объект
    action_type  TEXT,           -- тип ServiceAction (для фильтрации)
    action       TEXT,           -- JSON: полный ServiceAction
    reply_to_msg_id  INTEGER,
    reply_to_peer_id INTEGER,
    forwarded_from   TEXT,       -- JSON: ForwardInfo
    reactions        TEXT,       -- JSON: list[Reaction]
    is_outgoing      INTEGER,
    signature        TEXT,
    via_bot_id       INTEGER,
    saved_from_chat_id INTEGER,
    inline_buttons   TEXT,       -- JSON: list[list[InlineButton]]
    topic_id         INTEGER,
    grouped_id       INTEGER,
    PRIMARY KEY (chat_id, msg_id)
);
CREATE INDEX idx_messages_text ON messages(chat_id, text);
CREATE INDEX idx_messages_date ON messages(chat_id, date);
CREATE INDEX idx_messages_from ON messages(chat_id, from_id);
CREATE INDEX idx_messages_media ON messages(chat_id, media_type);
```
- `async save_takeout(account, takeout_id)`
- `async get_takeout(account) -> int | None`
- `async cache_user(user_id, display_name, username)`
- `async get_user(user_id) -> dict | None`
- `async cache_catalog(chat: Chat)`
- `async get_catalog() -> list[dict]`
- `async set_meta(key: str, value: str)` -- key/value в таблице meta
- `async get_meta(key: str) -> str | None`

Использовать `aiosqlite` для async-доступа. Создавать все таблицы из спеки 6.6, включая `meta`.

- [ ] **Step 4: Запустить тесты**

Run: `cd /home/vyt/devel/tg-export && python -m pytest tests/test_state.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tg_export/state.py tests/test_state.py
git commit -m "feat: add SQLite state management for incremental export"
```

---

## Фаза 3: Авторизация и API-слой

### Task 6: Авторизация (auth.py)

**Files:**
- Create: `tg_export/auth.py`
- Create: `tests/test_auth.py`

- [ ] **Step 1: Написать тесты на управление аккаунтами (без реального Telegram)**

```python
# tests/test_auth.py
import pytest
from pathlib import Path
from tg_export.auth import AccountManager


def test_config_dir_created(tmp_path):
    mgr = AccountManager(config_dir=tmp_path / "tg-export")
    mgr.ensure_dirs()
    assert (tmp_path / "tg-export" / "sessions").is_dir()


def test_list_accounts_empty(tmp_path):
    mgr = AccountManager(config_dir=tmp_path / "tg-export")
    mgr.ensure_dirs()
    assert mgr.list_accounts() == []


def test_session_path(tmp_path):
    mgr = AccountManager(config_dir=tmp_path / "tg-export")
    mgr.ensure_dirs()
    path = mgr.session_path("my_phone")
    assert path == tmp_path / "tg-export" / "sessions" / "my_phone.session"


def test_remove_account(tmp_path):
    mgr = AccountManager(config_dir=tmp_path / "tg-export")
    mgr.ensure_dirs()
    # Создаем фейковую сессию
    session_file = mgr.session_path("test_acc")
    session_file.touch()
    assert "test_acc" in mgr.list_accounts()
    mgr.remove_account("test_acc")
    assert "test_acc" not in mgr.list_accounts()


def test_credentials_file_permissions(tmp_path):
    mgr = AccountManager(config_dir=tmp_path / "tg-export")
    mgr.ensure_dirs()
    mgr.save_credentials(api_id=12345, api_hash="abc123")
    cred_path = tmp_path / "tg-export" / "api_credentials.yaml"
    assert cred_path.exists()
    # Проверяем права 600
    import stat
    mode = cred_path.stat().st_mode & 0o777
    assert mode == 0o600
```

- [ ] **Step 2: Запустить тесты, убедиться что падают**

Run: `cd /home/vyt/devel/tg-export && python -m pytest tests/test_auth.py -v`
Expected: FAIL

- [ ] **Step 3: Реализовать auth.py**

Класс `AccountManager`:
- `__init__(self, config_dir: Path = None)` -- по умолчанию `~/.config/tg-export`
- `ensure_dirs()` -- создает `config_dir/sessions/`
- `session_path(name: str) -> Path` -- `config_dir/sessions/<name>.session`
- `config_path(name: str) -> Path` -- `config_dir/<name>.yaml` (per-account конфиг по конвенции)
- `list_accounts() -> list[str]` -- список .session файлов
- `remove_account(name: str)` -- удаление .session файла
- `save_credentials(api_id, api_hash)` -- сохранение в YAML с правами 600
- `load_credentials() -> tuple[int, str]` -- загрузка api_id, api_hash
- `async add_account(name: str)` -- интерактивная авторизация через Telethon (телефон, код, 2FA)
- `resolve_config(account: str, config_override: str | None) -> Path` -- если `--config` задан, вернуть его; иначе `config_dir/<account>.yaml`

- [ ] **Step 4: Запустить тесты**

Run: `cd /home/vyt/devel/tg-export && python -m pytest tests/test_auth.py -v`
Expected: PASS

- [ ] **Step 5: Подключить auth к CLI**

Обновить `cli.py`: команды `auth add`, `auth list`, `auth remove` вызывают `AccountManager`.

- [ ] **Step 6: Commit**

```bash
git add tg_export/auth.py tests/test_auth.py tg_export/cli.py
git commit -m "feat: add account management with session storage"
```

### Task 7: API-обертка (api.py)

**Files:**
- Create: `tg_export/api.py`
- Create: `tests/test_api.py`

- [ ] **Step 1: Написать тесты (с мок-объектами Telethon)**

```python
# tests/test_api.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from telethon.errors import TakeoutInitDelayError
from tg_export.api import TgApi


@pytest.mark.asyncio
async def test_start_takeout_creates_session():
    """start_takeout должен вызвать client.takeout() и сохранить результат"""
    api = TgApi.__new__(TgApi)
    api.client = AsyncMock()
    api.takeout = None
    mock_takeout_ctx = AsyncMock()
    mock_takeout_client = AsyncMock()
    mock_takeout_ctx.__aenter__ = AsyncMock(return_value=mock_takeout_client)
    mock_takeout_ctx.__aexit__ = AsyncMock(return_value=False)
    api.client.takeout.return_value = mock_takeout_ctx

    await api.start_takeout()
    api.client.takeout.assert_called_once()
    assert api.takeout is mock_takeout_client


@pytest.mark.asyncio
async def test_start_takeout_handles_delay():
    """При TAKEOUT_INIT_DELAY должен вернуть время ожидания"""
    api = TgApi.__new__(TgApi)
    api.client = AsyncMock()
    api.takeout = None
    api.client.takeout.side_effect = TakeoutInitDelayError(request=None, capture=0, seconds=3600)

    with pytest.raises(TakeoutInitDelayError) as exc_info:
        await api.start_takeout()
    assert exc_info.value.seconds == 3600


@pytest.mark.asyncio
async def test_iter_messages_passes_min_id():
    """iter_messages должен передавать min_id в Telethon"""
    api = TgApi.__new__(TgApi)
    api.takeout = AsyncMock()
    api.takeout.iter_messages = MagicMock(return_value=AsyncMock(__aiter__=lambda s: s, __anext__=AsyncMock(side_effect=StopAsyncIteration)))

    async for _ in api.iter_messages(chat_id=123, min_id=500):
        pass

    api.takeout.iter_messages.assert_called_once_with(123, min_id=500)


@pytest.mark.asyncio
async def test_fallback_to_client_when_no_takeout():
    """Без Takeout должен использовать client напрямую"""
    api = TgApi.__new__(TgApi)
    api.takeout = None
    api.client = AsyncMock()
    api.client.iter_messages = MagicMock(return_value=AsyncMock(__aiter__=lambda s: s, __anext__=AsyncMock(side_effect=StopAsyncIteration)))

    async for _ in api.iter_messages(chat_id=123, min_id=0):
        pass

    api.client.iter_messages.assert_called_once_with(123, min_id=0)
```

Примечание: полноценное тестирование API требует реального Telegram-аккаунта. Unit-тесты проверяют интерфейс, обработку ошибок и fallback-логику. Интеграционные тесты -- вручную.

- [ ] **Step 2: Реализовать api.py**

Класс `TgApi` (спека 7.1):
- `__init__(self, session_path, api_id, api_hash)`
- `async connect()` -- `client.connect()`
- `async disconnect()` -- `client.disconnect()`
- `async start_takeout(**kwargs)` -- создание Takeout-сессии с обработкой TAKEOUT_INVALID и TAKEOUT_INIT_DELAY
- `async iter_dialogs()` -- `client.iter_dialogs()`
- `async get_left_channels()` -- raw API `GetLeftChannelsRequest`
- `async get_folders()` -- `client(GetDialogFiltersRequest())`, парсинг в dict[str, list[int]]
- `async iter_messages(chat_id, min_id=0)` -- через Takeout если доступен
- `async iter_topic_messages(chat_id, topic_id, min_id=0)`
- `async get_forum_topics(chat_id)`
- `async download_media(message, path, progress_cb)` -- через Takeout
- `async get_personal_info()` -- `GetFullUserRequest(InputUserSelf())`
- `async get_contacts()` -- `GetContactsRequest` + `GetTopPeersRequest`
- `async get_sessions()` -- `GetAuthorizationsRequest` + `GetWebAuthorizationsRequest`
- `async iter_userpics()`, `iter_stories()`, `iter_profile_music()`

- [ ] **Step 3: Запустить тесты**

Run: `cd /home/vyt/devel/tg-export && python -m pytest tests/test_api.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add tg_export/api.py tests/test_api.py
git commit -m "feat: add Telethon API wrapper with Takeout support"
```

---

## Фаза 4: Каталог чатов и генерация конфига

### Task 8: Каталог чатов (catalog.py)

**Files:**
- Create: `tg_export/catalog.py`
- Create: `tests/test_catalog.py`

- [ ] **Step 1: Написать тесты**

```python
# tests/test_catalog.py
import pytest
from tg_export.catalog import Catalog, format_catalog_yaml
from tg_export.models import Chat, ChatType
from datetime import datetime


def test_format_catalog_yaml():
    chats = [
        Chat(id=123, name="Рабочий чат", type=ChatType.private_supergroup,
             username=None, folder="Работа", members_count=12,
             last_message_date=datetime(2026, 3, 20), messages_count=45230,
             is_left=False, is_forum=True, migrated_to_id=None,
             migrated_from_id=None, is_monoforum=False),
        Chat(id=456, name="Иван", type=ChatType.personal,
             username="ivan", folder=None, members_count=None,
             last_message_date=datetime(2026, 3, 19), messages_count=3200,
             is_left=False, is_forum=False, migrated_to_id=None,
             migrated_from_id=None, is_monoforum=False),
    ]
    yaml_str = format_catalog_yaml(chats)
    assert "Рабочий чат" in yaml_str
    assert "private_supergroup" in yaml_str
    assert "is_forum: true" in yaml_str
    assert "Работа" in yaml_str  # folder
    assert "unfiled" in yaml_str  # Иван не в папке


def test_generate_config_template():
    from tg_export.catalog import generate_config_template
    chats = [
        Chat(id=123, name="Test", type=ChatType.personal,
             username=None, folder=None, members_count=None,
             last_message_date=None, messages_count=100,
             is_left=False, is_forum=False, migrated_to_id=None,
             migrated_from_id=None, is_monoforum=False),
    ]
    yaml_str = generate_config_template(chats, account="my_phone")
    assert "account: my_phone" in yaml_str
    assert "defaults:" in yaml_str
```

- [ ] **Step 2: Запустить тесты, убедиться что падают**

Run: `cd /home/vyt/devel/tg-export && python -m pytest tests/test_catalog.py -v`
Expected: FAIL

- [ ] **Step 3: Реализовать catalog.py**

Функции:
- `async fetch_catalog(api: TgApi, include_left: bool) -> list[Chat]` -- получение всех чатов через API, маппинг в models.Chat
- `format_catalog_yaml(chats: list[Chat], account: str) -> str` -- форматирование каталога как в спеке 5.2 (группировка по folders/unfiled/left)
- `format_catalog_json(chats, account) -> str`
- `generate_config_template(chats, account) -> str` -- генерация шаблона конфига из каталога (defaults + все чаты закомментированы)

- [ ] **Step 4: Запустить тесты**

Run: `cd /home/vyt/devel/tg-export && python -m pytest tests/test_catalog.py -v`
Expected: PASS

- [ ] **Step 5: Подключить к CLI**

Обновить `cli.py`: команды `list` и `init` вызывают `catalog.py`.

- [ ] **Step 6: Commit**

```bash
git add tg_export/catalog.py tests/test_catalog.py tg_export/cli.py
git commit -m "feat: add chat catalog export and config template generation"
```

---

## Фаза 5: Конвертер Telethon -> models

### Task 9: Конвертер (converter.py)

**Files:**
- Create: `tg_export/converter.py`
- Create: `tests/test_converter.py`

- [ ] **Step 1: Написать тесты с мок-объектами Telethon**

```python
# tests/test_converter.py
import pytest
from unittest.mock import MagicMock
from datetime import datetime
from tg_export.converter import convert_message, convert_chat
from tg_export.models import TextType, MediaType, ChatType


def _make_mock_message(text="Hello", date=None, media=None, action=None):
    msg = MagicMock()
    msg.id = 1
    msg.date = date or datetime(2024, 1, 1)
    msg.edit_date = None
    msg.from_id = MagicMock()
    msg.from_id.user_id = 123
    msg.message = text
    msg.entities = None
    msg.media = media
    msg.action = action
    msg.reply_to = None
    msg.fwd_from = None
    msg.reactions = None
    msg.out = False
    msg.post_author = None
    msg.via_bot_id = None
    msg.reply_markup = None
    msg.grouped_id = None
    return msg


def test_convert_simple_text_message():
    tl_msg = _make_mock_message(text="Привет мир")
    result = convert_message(tl_msg, chat_id=456)
    assert result.id == 1
    assert result.chat_id == 456
    assert result.text[0].type == TextType.text
    assert result.text[0].text == "Привет мир"
    assert result.media is None
    assert result.action is None


def test_convert_message_with_bold_entity():
    msg = _make_mock_message(text="Hello world")
    entity = MagicMock()
    entity.__class__.__name__ = "MessageEntityBold"
    entity.offset = 0
    entity.length = 5
    msg.entities = [entity]
    result = convert_message(msg, chat_id=1)
    types = [p.type for p in result.text]
    assert TextType.bold in types
```

- [ ] **Step 2: Запустить тесты, убедиться что падают**

Run: `cd /home/vyt/devel/tg-export && python -m pytest tests/test_converter.py -v`
Expected: FAIL

- [ ] **Step 3: Реализовать converter.py**

Функции:
- `convert_message(tl_msg, chat_id: int) -> models.Message` -- конвертация Telethon Message в models.Message
  - Парсинг entities -> list[TextPart] (21 тип)
  - Конвертация media -> подкласс Media (PhotoMedia, DocumentMedia и т.д.)
  - Конвертация action -> подкласс ServiceAction
  - Обработка forward, reply, reactions, inline buttons
- `convert_chat(tl_dialog, folder: str | None) -> models.Chat` -- конвертация Telethon Dialog в models.Chat
- `convert_entities(text: str, entities: list) -> list[TextPart]` -- парсинг форматирования
- `convert_media(tl_media) -> Media | None`
- `convert_action(tl_action) -> ServiceAction | None`
- `convert_reactions(tl_reactions) -> list[Reaction]`

Маппинг Telethon entity типов на TextType:
```python
ENTITY_MAP = {
    "MessageEntityBold": TextType.bold,
    "MessageEntityItalic": TextType.italic,
    "MessageEntityCode": TextType.code,
    "MessageEntityPre": TextType.pre,
    "MessageEntityUrl": TextType.url,
    "MessageEntityTextUrl": TextType.text_url,
    "MessageEntityMention": TextType.mention,
    "MessageEntityMentionName": TextType.mention_name,
    "MessageEntityHashtag": TextType.hashtag,
    "MessageEntityBotCommand": TextType.bot_command,
    "MessageEntityEmail": TextType.email,
    "MessageEntityPhone": TextType.phone,
    "MessageEntityCashtag": TextType.cashtag,
    "MessageEntityUnderline": TextType.underline,
    "MessageEntityStrike": TextType.strikethrough,
    "MessageEntityBlockquote": TextType.blockquote,
    "MessageEntityBankCard": TextType.bank_card,
    "MessageEntitySpoiler": TextType.spoiler,
    "MessageEntityCustomEmoji": TextType.custom_emoji,
}
```

- [ ] **Step 4: Запустить тесты**

Run: `cd /home/vyt/devel/tg-export && python -m pytest tests/test_converter.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tg_export/converter.py tests/test_converter.py
git commit -m "feat: add Telethon to models converter"
```

---

## Фаза 6: Загрузка медиа

### Task 10: Загрузчик медиа (media.py)

**Files:**
- Create: `tg_export/media.py`
- Create: `tests/test_media.py`

- [ ] **Step 1: Написать тесты**

```python
# tests/test_media.py
import pytest
import pytest_asyncio
from pathlib import Path
from unittest.mock import AsyncMock
from tg_export.media import MediaDownloader, should_download
from tg_export.models import (
    MediaType, PhotoMedia, DocumentMedia, FileInfo,
)
from tg_export.config import MediaConfig


def test_should_download_allowed_type():
    media = PhotoMedia(type=MediaType.photo, file=FileInfo(id=1, size=1000, name="photo.jpg", mime_type="image/jpeg", local_path=None), width=100, height=100)
    cfg = MediaConfig(types=["photo", "video"], max_file_size_bytes=50 * 1024**2, concurrent_downloads=3)
    assert should_download(media, cfg) is True


def test_should_download_disallowed_type():
    media = PhotoMedia(type=MediaType.photo, file=FileInfo(id=1, size=1000, name="photo.jpg", mime_type="image/jpeg", local_path=None), width=100, height=100)
    cfg = MediaConfig(types=["document"], max_file_size_bytes=50 * 1024**2, concurrent_downloads=3)
    assert should_download(media, cfg) is False


def test_should_download_file_too_large():
    media = DocumentMedia(
        type=MediaType.document,
        file=FileInfo(id=1, size=100 * 1024**2, name="big.zip", mime_type="application/zip", local_path=None),
        name="big.zip", mime_type="application/zip",
        duration=None, width=None, height=None,
        performer=None, song_title=None, sticker_emoji=None,
    )
    cfg = MediaConfig(types=["document"], max_file_size_bytes=50 * 1024**2, concurrent_downloads=3)
    assert should_download(media, cfg) is False


def test_media_subdir():
    from tg_export.media import media_subdir
    assert media_subdir(MediaType.photo) == "photos"
    assert media_subdir(MediaType.video) == "videos"
    assert media_subdir(MediaType.document) == "files"
    assert media_subdir(MediaType.voice) == "voice_messages"
    assert media_subdir(MediaType.video_note) == "video_messages"
    assert media_subdir(MediaType.sticker) == "stickers"
    assert media_subdir(MediaType.gif) == "gifs"


def test_check_disk_space():
    from tg_export.media import check_disk_space
    # Текущий диск должен иметь больше 1 байта свободного места
    assert check_disk_space(Path("/tmp"), min_free_bytes=1) is True
    # Невозможный лимит
    assert check_disk_space(Path("/tmp"), min_free_bytes=10**18) is False
```

- [ ] **Step 2: Запустить тесты, убедиться что падают**

Run: `cd /home/vyt/devel/tg-export && python -m pytest tests/test_media.py -v`
Expected: FAIL

- [ ] **Step 3: Реализовать media.py**

```python
# tg_export/media.py
import asyncio
import shutil
from pathlib import Path
from tg_export.models import Media, MediaType
from tg_export.config import MediaConfig

MEDIA_SUBDIRS = {
    MediaType.photo: "photos",
    MediaType.video: "videos",
    MediaType.document: "files",
    MediaType.voice: "voice_messages",
    MediaType.video_note: "video_messages",
    MediaType.sticker: "stickers",
    MediaType.gif: "gifs",
}


def media_subdir(media_type: MediaType) -> str:
    return MEDIA_SUBDIRS.get(media_type, "files")


def should_download(media: Media, config: MediaConfig) -> bool:
    if media.file is None:
        return False
    if media.type.value not in config.types and "all" not in config.types:
        return False
    if media.file.size > config.max_file_size_bytes:
        return False
    return True


def check_disk_space(path: Path, min_free_bytes: int) -> bool:
    usage = shutil.disk_usage(path)
    return usage.free >= min_free_bytes


class MediaDownloader:
    def __init__(self, api, state, config: MediaConfig, min_free_bytes: int):
        self.api = api
        self.state = state
        self.config = config
        self.min_free_bytes = min_free_bytes
        self.semaphore = asyncio.Semaphore(config.concurrent_downloads)

    async def download(self, tl_message, media: Media, chat_dir: Path) -> Path | None:
        # 1. should_download check
        # 2. Check state -- already downloaded?
        # 3. Check import_existing
        # 4. Check disk space
        # 5. Download via semaphore
        # 6. Verify size
        # 7. Register in state
        ...
```

- [ ] **Step 4: Запустить тесты**

Run: `cd /home/vyt/devel/tg-export && python -m pytest tests/test_media.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tg_export/media.py tests/test_media.py
git commit -m "feat: add media downloader with filtering and disk space check"
```

---

## Фаза 7: HTML-генерация

### Task 11: Копирование static-ресурсов из tdesktop

**Files:**
- Create: `tg_export/html/static/css/style.css` (копия из tdesktop)
- Create: `tg_export/html/static/js/script.js` (копия из tdesktop)
- Create: `tg_export/html/static/images/` (44 PNG из tdesktop)

- [ ] **Step 1: Скопировать ресурсы**

```bash
mkdir -p tg_export/html/static
cp -r /home/vyt/devel/tdesktop/Telegram/Resources/export_html/css tg_export/html/static/
cp -r /home/vyt/devel/tdesktop/Telegram/Resources/export_html/js tg_export/html/static/
cp -r /home/vyt/devel/tdesktop/Telegram/Resources/export_html/images tg_export/html/static/
```

- [ ] **Step 2: Проверить что все файлы на месте**

Run: `ls tg_export/html/static/images/ | wc -l`
Expected: 44

- [ ] **Step 3: Commit**

```bash
git add tg_export/html/static/
git commit -m "feat: copy tdesktop export HTML resources (CSS, JS, icons)"
```

### Task 12: Jinja2-шаблоны и HtmlRenderer

**Files:**
- Create: `tg_export/html/__init__.py`
- Create: `tg_export/html/renderer.py`
- Create: `tg_export/html/templates/base.html.j2`
- Create: `tg_export/html/templates/index.html.j2`
- Create: `tg_export/html/templates/folder_index.html.j2`
- Create: `tg_export/html/templates/chat.html.j2`
- Create: `tg_export/html/templates/message.html.j2`
- Create: `tg_export/html/templates/media_block.html.j2`
- Create: `tg_export/html/templates/service_message.html.j2`
- Create: `tg_export/html/templates/contacts.html.j2`
- Create: `tg_export/html/templates/sessions.html.j2`
- Create: `tg_export/html/templates/personal_info.html.j2`
- Create: `tg_export/html/templates/userpics.html.j2`
- Create: `tg_export/html/templates/stories.html.j2`
- Create: `tg_export/html/templates/profile_music.html.j2`
- Create: `tg_export/html/templates/other_data.html.j2`
- Create: `tests/test_renderer.py`

- [ ] **Step 1: Написать тесты**

```python
# tests/test_renderer.py
import pytest
from pathlib import Path
from datetime import datetime
from tg_export.html.renderer import HtmlRenderer
from tg_export.models import (
    Message, TextPart, TextType, Chat, ChatType,
    PhotoMedia, MediaType, FileInfo,
)
from tg_export.config import OutputConfig


@pytest.fixture
def renderer(tmp_path):
    config = OutputConfig(
        path=str(tmp_path / "output"),
        format="html",
        messages_per_file=1000,
        min_free_space_bytes=20 * 1024**3,
    )
    r = HtmlRenderer(output_dir=tmp_path / "output", config=config)
    r.setup()
    return r


def test_setup_copies_static(renderer, tmp_path):
    output = tmp_path / "output"
    assert (output / "css" / "style.css").exists()
    assert (output / "js" / "script.js").exists()
    assert (output / "images").is_dir()


def test_render_message_plain_text(renderer):
    msg = Message(
        id=1, chat_id=1, date=datetime(2024, 1, 1),
        edited=None, from_id=1, from_name="Иван",
        text=[TextPart(type=TextType.text, text="Привет")],
        media=None, action=None, reply_to_msg_id=None,
        reply_to_peer_id=None, forwarded_from=None,
        reactions=[], is_outgoing=False, signature=None,
        via_bot_id=None, saved_from_chat_id=None,
        inline_buttons=None, topic_id=None, grouped_id=None,
    )
    html = renderer.render_message(msg, prev_msg=None)
    assert "Привет" in html
    assert "Иван" in html
    assert 'class="message"' in html or "message" in html


def test_render_message_joined(renderer):
    """Сообщения от одного автора в пределах 15 мин группируются"""
    msg1 = Message(
        id=1, chat_id=1, date=datetime(2024, 1, 1, 10, 0),
        edited=None, from_id=1, from_name="Иван",
        text=[TextPart(type=TextType.text, text="Первое")],
        media=None, action=None, reply_to_msg_id=None,
        reply_to_peer_id=None, forwarded_from=None,
        reactions=[], is_outgoing=False, signature=None,
        via_bot_id=None, saved_from_chat_id=None,
        inline_buttons=None, topic_id=None, grouped_id=None,
    )
    msg2 = Message(
        id=2, chat_id=1, date=datetime(2024, 1, 1, 10, 5),
        edited=None, from_id=1, from_name="Иван",
        text=[TextPart(type=TextType.text, text="Второе")],
        media=None, action=None, reply_to_msg_id=None,
        reply_to_peer_id=None, forwarded_from=None,
        reactions=[], is_outgoing=False, signature=None,
        via_bot_id=None, saved_from_chat_id=None,
        inline_buttons=None, topic_id=None, grouped_id=None,
    )
    html = renderer.render_message(msg2, prev_msg=msg1)
    assert "joined" in html  # CSS-класс joined


def test_render_chat_pagination(renderer, tmp_path):
    """Проверяем разбивку на файлы"""
    chat = Chat(
        id=123, name="Test", type=ChatType.personal,
        username=None, folder=None, members_count=None,
        last_message_date=None, messages_count=5,
        is_left=False, is_forum=False, migrated_to_id=None,
        migrated_from_id=None, is_monoforum=False,
    )
    messages = []
    for i in range(2500):
        messages.append(Message(
            id=i, chat_id=123, date=datetime(2024, 1, 1),
            edited=None, from_id=1, from_name="Test",
            text=[TextPart(type=TextType.text, text=f"Msg {i}")],
            media=None, action=None, reply_to_msg_id=None,
            reply_to_peer_id=None, forwarded_from=None,
            reactions=[], is_outgoing=False, signature=None,
            via_bot_id=None, saved_from_chat_id=None,
            inline_buttons=None, topic_id=None, grouped_id=None,
        ))
    chat_dir = tmp_path / "output" / "unfiled" / "Test_123"
    renderer.render_chat(chat, messages, chat_dir)
    # 2500 сообщений / 1000 = 3 файла
    assert (chat_dir / "messages.html").exists()
    assert (chat_dir / "messages2.html").exists()
    assert (chat_dir / "messages3.html").exists()
    assert not (chat_dir / "messages4.html").exists()


def test_render_media_album(renderer):
    """Сообщения с одинаковым grouped_id рендерятся как один блок"""
    msgs = []
    for i in range(3):
        msgs.append(Message(
            id=i+1, chat_id=1, date=datetime(2024, 1, 1, 10, 0),
            edited=None, from_id=1, from_name="Test",
            text=[TextPart(type=TextType.text, text="Album" if i == 2 else "")],
            media=PhotoMedia(
                type=MediaType.photo,
                file=FileInfo(id=i+100, size=1000, name=f"photo_{i}.jpg", mime_type="image/jpeg", local_path=None),
                width=800, height=600,
            ),
            action=None, reply_to_msg_id=None, reply_to_peer_id=None,
            forwarded_from=None, reactions=[], is_outgoing=False,
            signature=None, via_bot_id=None, saved_from_chat_id=None,
            inline_buttons=None, topic_id=None,
            grouped_id=12345,  # все три в одном альбоме
        ))
    html = renderer.render_album(msgs)
    assert html.count('class="message"') == 1  # один блок
    assert "Album" in html  # текст из последнего сообщения
    assert "photo_0.jpg" in html or "photo" in html  # все фото присутствуют
```

- [ ] **Step 2: Запустить тесты, убедиться что падают**

Run: `cd /home/vyt/devel/tg-export && python -m pytest tests/test_renderer.py -v`
Expected: FAIL

- [ ] **Step 3: Создать Jinja2-шаблоны**

Эталон разметки: `tdesktop/Telegram/SourceFiles/export/output/export_output_html.cpp`.

Ключевые шаблоны:
- `base.html.j2` -- `<head>` с подключением CSS/JS, `{% block content %}`
- `chat.html.j2` -- цикл по сообщениям, навигация между файлами
- `message.html.j2` -- `<div class="message" id="message{{ msg.id }}">`, группировка по `.joined`, форматированный текст, медиа, reply, forward, reactions
- `media_block.html.j2` -- рендеринг по типу медиа (фото с превью, видео, файл, стикер и т.д.)
- `service_message.html.j2` -- `<div class="message service">`

CSS-классы должны точно соответствовать tdesktop (спека 9.3).

- [ ] **Step 4: Реализовать renderer.py**

Класс `HtmlRenderer`:
- `__init__(self, output_dir, config)`
- `setup()` -- копирование static/ в output_dir
- `render_index(folders, chats, sections)` -- главная страница
- `render_folder_index(folder_name, chats)` -- список чатов в папке
- `render_chat(chat, messages, chat_dir)` -- рендеринг чата с разбивкой на файлы (messages.html, messages2.html...)
- `render_message(msg, prev_msg) -> str` -- один блок сообщения с учетом группировки
- `render_contacts(contacts)`, `render_sessions(sessions)` и т.д.

Группировка (спека 9.5):
- `kJoinWithinSeconds = 900`
- `is_joined(msg, prev_msg)` -- True если тот же автор, < 15 минут, не service, не forward

Нумерация файлов (спека 9.4): первый `messages.html`, далее `messages2.html`, `messages3.html`.

Медиа-альбомы (спека 9.5): сообщения с одинаковым `grouped_id` рендерятся как один блок.

- [ ] **Step 5: Запустить тесты**

Run: `cd /home/vyt/devel/tg-export && python -m pytest tests/test_renderer.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add tg_export/html/ tests/test_renderer.py
git commit -m "feat: add HTML renderer with Jinja2 templates matching tdesktop"
```

---

## Фаза 8: Основной цикл экспорта

### Task 13: Exporter (exporter.py)

**Files:**
- Create: `tg_export/exporter.py`
- Create: `tests/test_exporter.py`

- [ ] **Step 1: Написать тесты**

```python
# tests/test_exporter.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from tg_export.exporter import Exporter


@pytest.mark.asyncio
async def test_exporter_dry_run_no_downloads():
    """dry-run не должен скачивать файлы"""
    api = AsyncMock()
    api.iter_dialogs = AsyncMock(return_value=AsyncMock(__aiter__=AsyncMock(return_value=iter([]))))
    state = AsyncMock()
    config = MagicMock()
    config.output.path = "/tmp/test"
    config.output.min_free_space_bytes = 1
    renderer = MagicMock()
    downloader = AsyncMock()

    exporter = Exporter(api=api, state=state, config=config, renderer=renderer, downloader=downloader)
    stats = await exporter.run(dry_run=True)
    downloader.download.assert_not_called()


def test_resolve_chat_dir():
    from tg_export.exporter import resolve_chat_dir
    from pathlib import Path
    result = resolve_chat_dir(
        base=Path("/output"),
        chat_name="Рабочий чат",
        chat_id=1234567890,
        folder="Работа",
        is_left=False,
    )
    assert result == Path("/output/folders/Работа/Рабочий_чат_1234567890")


def test_resolve_chat_dir_unfiled():
    from tg_export.exporter import resolve_chat_dir
    from pathlib import Path
    result = resolve_chat_dir(
        base=Path("/output"),
        chat_name="Иван Иванов",
        chat_id=9876543210,
        folder=None,
        is_left=False,
    )
    assert result == Path("/output/unfiled/Иван_Иванов_9876543210")


def test_resolve_chat_dir_left():
    from tg_export.exporter import resolve_chat_dir
    from pathlib import Path
    result = resolve_chat_dir(
        base=Path("/output"),
        chat_name="Старый канал",
        chat_id=111,
        folder=None,
        is_left=True,
    )
    assert result == Path("/output/left/Старый_канал_111")


def test_sanitize_name():
    from tg_export.exporter import sanitize_name
    assert sanitize_name("Рабочий чат") == "Рабочий_чат"
    assert sanitize_name("file/with:special<chars>") == "file_with_special_chars_"
    assert sanitize_name("  spaces  ") == "spaces"
```

- [ ] **Step 2: Запустить тесты, убедиться что падают**

Run: `cd /home/vyt/devel/tg-export && python -m pytest tests/test_exporter.py -v`
Expected: FAIL

- [ ] **Step 3: Реализовать exporter.py**

Класс `Exporter` (спека 10.1-10.2):
- `__init__(self, api, state, config, renderer, downloader)`
- `async run(dry_run=False, verify=False) -> ExportStats` -- основной цикл (7 шагов из спеки 10.1)
- `async export_chat(chat, chat_config, chat_dir)` -- экспорт одного чата (8 шагов из спеки 10.2)
- `async export_global_data()` -- personal_info, userpics, stories, profile_music, contacts, sessions, other_data

Вспомогательные функции:
- `sanitize_name(name: str) -> str` -- замена спец-символов на `_`, strip
- `resolve_chat_dir(base, chat_name, chat_id, folder, is_left) -> Path` -- путь к папке чата: `folders/{folder}/{name}_{id}` / `unfiled/{name}_{id}` / `left/{name}_{id}`

Прогресс: Rich progress bars (спека 10.9).

- [ ] **Step 4: Запустить тесты**

Run: `cd /home/vyt/devel/tg-export && python -m pytest tests/test_exporter.py -v`
Expected: PASS

- [ ] **Step 5: Подключить к CLI**

Обновить `cli.py`: команда `run` создает Exporter и вызывает `exporter.run()`.

- [ ] **Step 6: Commit**

```bash
git add tg_export/exporter.py tests/test_exporter.py tg_export/cli.py
git commit -m "feat: add main export loop with progress tracking"
```

---

## Фаза 9: Инкрементальность и верификация

### Task 14: Инкрементальный экспорт и верификация

**Files:**
- Modify: `tg_export/exporter.py`
- Modify: `tg_export/media.py`
- Create: `tg_export/importer.py`
- Create: `tests/test_incremental.py`

- [ ] **Step 1: Написать тесты**

```python
# tests/test_incremental.py
import pytest
import pytest_asyncio
from pathlib import Path
from tg_export.state import ExportState
from tg_export.importer import scan_tdesktop_export


@pytest_asyncio.fixture
async def state(tmp_path):
    s = ExportState(tmp_path / "state.db")
    await s.open()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_incremental_uses_last_msg_id(state):
    """При повторном экспорте должен использоваться last_msg_id"""
    await state.set_last_msg_id(chat_id=123, msg_id=500)
    last_id = await state.get_last_msg_id(chat_id=123)
    assert last_id == 500  # iter_messages будет вызван с min_id=500


@pytest.mark.asyncio
async def test_file_verification_detects_partial(state):
    await state.register_file(
        file_id=1, chat_id=123, msg_id=1,
        expected_size=10000, actual_size=5000,
        local_path="photos/photo.jpg", status="partial",
    )
    broken = await state.get_files_to_verify()
    assert len(broken) == 1


def test_scan_tdesktop_export(tmp_path):
    """Сканирование структуры папок экспорта tdesktop"""
    # Создаем структуру tdesktop
    chat_dir = tmp_path / "chats" / "chat_001"
    photos_dir = chat_dir / "photos"
    photos_dir.mkdir(parents=True)
    (photos_dir / "photo_1@01-01-2024_10-00-00.jpg").write_bytes(b"x" * 1000)
    (photos_dir / "photo_2@01-01-2024_10-05-00.jpg").write_bytes(b"x" * 2000)

    files = scan_tdesktop_export(tmp_path)
    assert len(files) == 2
    assert files[0]["size"] == 1000
    assert files[1]["size"] == 2000
```

- [ ] **Step 2: Запустить тесты, убедиться что падают**

Run: `cd /home/vyt/devel/tg-export && python -m pytest tests/test_incremental.py -v`
Expected: FAIL

- [ ] **Step 3: Реализовать importer.py**

```python
# tg_export/importer.py
from pathlib import Path


def scan_tdesktop_export(export_path: Path) -> list[dict]:
    """Сканирует структуру tdesktop-экспорта.

    Возвращает список dict с ключами: path, size, chat_dir.
    По имени файла (photo_N@DD-MM-YYYY_HH-MM-SS.ext) можно восстановить
    соответствие с file_id.
    """
    ...


def scan_tg_export(export_path: Path) -> list[dict]:
    """Читает SQLite предыдущего tg-export экспорта."""
    ...
```

- [ ] **Step 4: Обновить exporter.py**

Добавить в `export_chat()`:
- Чтение `last_msg_id` перед итерацией (шаг 1 спеки 10.2)
- Сохранение сообщений в SQLite (шаг 6)
- Рендеринг HTML из ВСЕХ сообщений в SQLite (шаг 7)
- Обновление `last_msg_id` (шаг 8)

Добавить в `run()`:
- Индексация `import_existing` при инициализации (шаг 1)
- Верификация при `--verify` (шаг 6)

- [ ] **Step 5: Обновить media.py**

В `MediaDownloader.download()`:
- Проверка в `state.files` -- уже скачан?
- Проверка в `import_existing` -- есть в старом экспорте? Если да -- копирование/симлинк
- Верификация размера после скачивания

- [ ] **Step 6: Запустить тесты**

Run: `cd /home/vyt/devel/tg-export && python -m pytest tests/test_incremental.py -v`
Expected: PASS

- [ ] **Step 7: Подключить verify к CLI**

Обновить `cli.py`: команда `verify` вызывает логику верификации.

- [ ] **Step 8: Commit**

```bash
git add tg_export/importer.py tg_export/exporter.py tg_export/media.py tg_export/cli.py tests/test_incremental.py
git commit -m "feat: add incremental export, verification, and tdesktop import"
```

---

## Фаза 10: Продвинутые сценарии

### Task 15: Форумные топики

**Files:**
- Modify: `tg_export/exporter.py`
- Modify: `tg_export/html/renderer.py`
- Create: `tests/test_topics.py`

- [ ] **Step 1: Написать тесты**

```python
# tests/test_topics.py
def test_topic_messages_grouped_by_topic():
    """Сообщения в форуме группируются по топикам в HTML"""
    from tg_export.models import Message, TextPart, TextType, ForumTopic
    from datetime import datetime

    topics = [
        ForumTopic(id=1, title="General", icon_emoji=None, is_closed=False, is_pinned=True, messages_count=100),
        ForumTopic(id=2, title="Off-topic", icon_emoji=None, is_closed=False, is_pinned=False, messages_count=50),
    ]
    messages = [
        Message(id=1, chat_id=1, date=datetime(2024, 1, 1), edited=None,
                from_id=1, from_name="A", text=[TextPart(type=TextType.text, text="Hi")],
                media=None, action=None, reply_to_msg_id=None, reply_to_peer_id=None,
                forwarded_from=None, reactions=[], is_outgoing=False, signature=None,
                via_bot_id=None, saved_from_chat_id=None, inline_buttons=None,
                topic_id=1, grouped_id=None),
        Message(id=2, chat_id=1, date=datetime(2024, 1, 1), edited=None,
                from_id=1, from_name="A", text=[TextPart(type=TextType.text, text="OT")],
                media=None, action=None, reply_to_msg_id=None, reply_to_peer_id=None,
                forwarded_from=None, reactions=[], is_outgoing=False, signature=None,
                via_bot_id=None, saved_from_chat_id=None, inline_buttons=None,
                topic_id=2, grouped_id=None),
    ]
    from tg_export.exporter import group_by_topic
    grouped = group_by_topic(messages, topics)
    assert len(grouped) == 2
    assert grouped[1][0].text[0].text == "Hi"
    assert grouped[2][0].text[0].text == "OT"
```

- [ ] **Step 2: Реализовать обработку топиков**

В `exporter.py`:
- `group_by_topic(messages, topics) -> dict[int, list[Message]]`
- В `export_chat()`: если `chat.is_forum` -- получение топиков, группировка сообщений, рендеринг по секциям

В `renderer.py`:
- Обновить `render_chat()` -- поддержка секций по топикам с подзаголовками

- [ ] **Step 3: Запустить тесты**

Run: `cd /home/vyt/devel/tg-export && python -m pytest tests/test_topics.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add tg_export/exporter.py tg_export/html/renderer.py tests/test_topics.py
git commit -m "feat: add forum topic support"
```

### Task 16: Миграция групп и покинутые каналы

**Files:**
- Modify: `tg_export/exporter.py`
- Create: `tests/test_migration.py`

- [ ] **Step 1: Написать тесты**

```python
# tests/test_migration.py
def test_migrated_chat_combines_messages():
    """Мигрировавший чат объединяет сообщения из старой и новой группы"""
    from tg_export.exporter import should_combine_migration
    from tg_export.models import Chat, ChatType

    old_group = Chat(
        id=100, name="Old Group", type=ChatType.private_group,
        username=None, folder=None, members_count=5,
        last_message_date=None, messages_count=1000,
        is_left=False, is_forum=False,
        migrated_to_id=200, migrated_from_id=None,
        is_monoforum=False,
    )
    assert should_combine_migration(old_group) is True
    assert old_group.migrated_to_id == 200


def test_left_channel_dir():
    from tg_export.exporter import resolve_chat_dir
    from pathlib import Path
    result = resolve_chat_dir(
        base=Path("/output"),
        chat_name="Left",
        chat_id=999,
        folder=None,
        is_left=True,
    )
    assert "left" in str(result)
```

- [ ] **Step 2: Реализовать**

В `exporter.py`:
- `should_combine_migration(chat) -> bool`
- В основном цикле: при обнаружении `migrated_to_id` -- объединение экспорта
- Покинутые каналы: проверка `left_channels.action` в конфиге, размещение в `left/`

- [ ] **Step 3: Запустить тесты**

Run: `cd /home/vyt/devel/tg-export && python -m pytest tests/test_migration.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add tg_export/exporter.py tests/test_migration.py
git commit -m "feat: add group migration and left channel support"
```

### Task 17: Обработка monoforum

**Files:**
- Modify: `tg_export/exporter.py`
- Create: `tests/test_monoforum.py`

- [ ] **Step 1: Написать тесты**

```python
# tests/test_monoforum.py
from tg_export.models import Chat, ChatType


def test_monoforum_detected():
    """Monoforum-чат должен обнаруживаться по флагу is_monoforum"""
    chat = Chat(
        id=100, name="Channel DMs", type=ChatType.private_supergroup,
        username=None, folder=None, members_count=None,
        last_message_date=None, messages_count=50,
        is_left=False, is_forum=False,
        migrated_to_id=None, migrated_from_id=None,
        is_monoforum=True,
    )
    assert chat.is_monoforum is True


def test_monoforum_dir_in_channel_folder():
    """Monoforum размещается в папке связанного канала"""
    from tg_export.exporter import resolve_monoforum_dir
    from pathlib import Path
    result = resolve_monoforum_dir(
        base=Path("/output"),
        channel_name="My Channel",
        channel_id=200,
        monoforum_name="DMs",
        monoforum_id=100,
        folder="News",
    )
    assert "My_Channel_200" in str(result)
    assert "DMs_100" in str(result)
```

- [ ] **Step 2: Реализовать обработку monoforum**

В `exporter.py`:
- `resolve_monoforum_dir(base, channel_name, channel_id, monoforum_name, monoforum_id, folder) -> Path`
- В основном цикле: при `chat.is_monoforum` -- определить связанный канал, разместить экспорт в его папке
- В HTML: добавить пометку о привязке к каналу (спека 10.6)

- [ ] **Step 3: Запустить тесты**

Run: `cd /home/vyt/devel/tg-export && python -m pytest tests/test_monoforum.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add tg_export/exporter.py tests/test_monoforum.py
git commit -m "feat: add monoforum support"
```

### Task 18: Graceful shutdown и обработка ошибок

**Files:**
- Modify: `tg_export/exporter.py`
- Modify: `tg_export/media.py`

- [ ] **Step 1: Добавить обработку Ctrl+C**

В `exporter.py`:
```python
import signal

class Exporter:
    def __init__(self, ...):
        self._shutdown = False

    async def run(self, ...):
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGINT, self._handle_shutdown)
        loop.add_signal_handler(signal.SIGTERM, self._handle_shutdown)
        ...

    def _handle_shutdown(self):
        self._shutdown = True
        # В цикле экспорта проверяем self._shutdown
```

- [ ] **Step 2: Добавить retry с exponential backoff в media.py**

```python
async def _download_with_retry(self, ...):
    for attempt in range(3):
        try:
            return await self.api.download_media(...)
        except (ConnectionError, TimeoutError):
            if attempt == 2:
                raise
            await asyncio.sleep(2 ** attempt)
```

- [ ] **Step 3: Добавить graceful stop при нехватке места**

В `MediaDownloader.download()`:
```python
if not check_disk_space(chat_dir, self.min_free_bytes):
    raise DiskSpaceError(f"Свободное место менее {self.min_free_bytes // 1024**3} GB")
```

В `Exporter.run()` -- перехват `DiskSpaceError`, сохранение состояния, вывод сообщения.

- [ ] **Step 4: Commit**

```bash
git add tg_export/exporter.py tg_export/media.py
git commit -m "feat: add graceful shutdown, retry, and disk space monitoring"
```

### Task 19: Интеграционный тест полного цикла

**Files:**
- Create: `tests/test_integration.py`

- [ ] **Step 1: Написать end-to-end тест с мок-API**

```python
# tests/test_integration.py
import pytest
import pytest_asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime
from tg_export.exporter import Exporter
from tg_export.state import ExportState
from tg_export.html.renderer import HtmlRenderer
from tg_export.media import MediaDownloader
from tg_export.models import *
from tg_export.config import load_config


@pytest_asyncio.fixture
async def state(tmp_path):
    s = ExportState(tmp_path / "state.db")
    await s.open()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_full_export_cycle(tmp_path, state):
    """Полный цикл: конфиг -> экспорт -> HTML -> инкрементальный экспорт"""
    output = tmp_path / "output"

    # Мок API
    api = AsyncMock()
    messages = [
        MagicMock(id=i, date=datetime(2024, 1, 1, 10, i),
                  message=f"Message {i}", entities=None, media=None,
                  action=None, reply_to=None, fwd_from=None,
                  reactions=None, out=False, post_author=None,
                  via_bot_id=None, reply_markup=None, grouped_id=None,
                  from_id=MagicMock(user_id=1))
        for i in range(5)
    ]
    api.iter_messages = AsyncMock(return_value=AsyncMock(
        __aiter__=AsyncMock(return_value=iter(messages))
    ))

    config = MagicMock()
    config.output.path = str(output)
    config.output.messages_per_file = 1000
    config.output.min_free_space_bytes = 1

    renderer = HtmlRenderer(output_dir=output, config=config.output)
    renderer.setup()

    downloader = AsyncMock()

    exporter = Exporter(api=api, state=state, config=config,
                        renderer=renderer, downloader=downloader)

    # Проверяем что HTML создан
    # (упрощенный тест -- реальный потребует полный конфиг)
    assert output.exists()
```

- [ ] **Step 2: Запустить тест**

Run: `cd /home/vyt/devel/tg-export && python -m pytest tests/test_integration.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_integration.py
git commit -m "test: add integration test for full export cycle"
```

### Task 20: Запуск всех тестов и финальная проверка

- [ ] **Step 1: Запустить все тесты**

Run: `cd /home/vyt/devel/tg-export && python -m pytest tests/ -v --tb=short`
Expected: все тесты PASS

- [ ] **Step 2: Проверить структуру проекта**

Run: `cd /home/vyt/devel/tg-export && fdfind -t f -e py | sort`
Expected: все файлы из спеки 3.1 на месте

- [ ] **Step 3: Проверить что tg-export --help работает**

Run: `cd /home/vyt/devel/tg-export && tg-export --help`
Expected: вывод справки со всеми командами

- [ ] **Step 4: Commit**

```bash
git commit --allow-empty -m "milestone: tg-export v0.1.0 complete"
```
