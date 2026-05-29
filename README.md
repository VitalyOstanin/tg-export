# tg-export

[![CI](https://github.com/VitalyOstanin/tg-export/actions/workflows/ci.yml/badge.svg)](https://github.com/VitalyOstanin/tg-export/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/VitalyOstanin/tg-export/graph/badge.svg?branch=master)](https://codecov.io/gh/VitalyOstanin/tg-export)
[![PyPI version](https://img.shields.io/pypi/v/tg-export.svg)](https://pypi.org/project/tg-export/)

Экспорт данных из Telegram на локальный диск с гибкой настройкой.

## Содержание

- [Возможности](#возможности)
- [Сценарии использования](#сценарии-использования)
- [Установка](#установка)
- [Быстрый старт](#быстрый-старт)
- [Структура конфигов](#структура-конфигов)
- [Глобальные опции](#глобальные-опции)
- [Документация](#документация)
- [Автодополнение командной строки](#автодополнение-командной-строки)
- [Тесты](#тесты)
- [Разработка](#разработка)

## Возможности

- **Инкрементальный экспорт** -- при повторном запуске скачиваются только новые сообщения и файлы, состояние хранится в SQLite
- **Sibling-дедупликация** -- при экспорте нескольких аккаунтов (семья, рабочий/личный) общие файлы не скачиваются повторно, а линкуются через hardlink
- **Импорт из tdesktop** -- файлы из стандартного экспорта Telegram Desktop копируются вместо повторного скачивания
- **Takeout API** -- использует официальный Takeout для обхода rate-limiting
- **Гибкие правила экспорта** -- настройка по чатам, папкам, типам (personal, groups, channels, bots), с фильтрами по датам, типам медиа и размеру файлов
- **Логика архивных чатов** -- чат считается архивным (`is_archived`) только если он присутствует исключительно в архиве Telegram. Если чат есть в основном списке диалогов или в именованной папке, он не помечается как архивный, даже если одновременно находится в архиве
- **Проверка размера в runtime** -- если реальный размер файла превышает лимит, скачивание прерывается и частичный файл удаляется
- **HTML-рендеринг по месяцам** -- каждый месяц в отдельном файле, с оглавлением (TOC) и навигацией prev/next
- **Progress bars** -- основной прогресс по сообщениям + sub-progress bars для каждого скачиваемого файла
- **Контроль свободного места** -- экспорт останавливается, если свободное место на диске падает ниже порога (`min_free_space` в `config.yaml`, по умолчанию 20 GB)
- **Корректный shutdown** -- Ctrl+C сохраняет состояние, двойной Ctrl+C -- force shutdown с cleanup
- **Поддержка прокси** -- SOCKS5, SOCKS4, HTTP прокси для подключения к Telegram API
- **Данные в SQLite** -- все сообщения и метаданные хранятся в SQLite, что позволяет пересоздать HTML без обращения к Telegram API, строить поисковые индексы, подключить веб-интерфейс или использовать данные в любых других целях
- **CLI для анализа** -- команда `tg info` для batch-запросов информации о чатах через API
- **Очистка данных чата** -- команда `purge` для удаления данных конкретного чата из БД и с диска

## Сценарии использования

**Личный архив** -- экспорт всех личных переписок и групп с медиафайлами на локальный диск для долгосрочного хранения.

**Семейный экспорт** -- экспорт аккаунтов нескольких членов семьи. Общие группы и каналы содержат одинаковые файлы -- sibling-дедупликация экономит место на диске через hardlink.

**Выборочный экспорт** -- экспорт только нужных чатов/папок с правилами: рабочие чаты экспортируются, боты и публичные каналы пропускаются, для флудилок скачиваются только фото.

**Миграция с tdesktop** -- если уже есть экспорт из Telegram Desktop, файлы из него импортируются без повторного скачивания.

## Установка

Из PyPI:

```bash
pip install tg-export
```

Для работы через прокси:

```bash
pip install tg-export[proxy]
```

Из исходников (для конечного использования):

```bash
uv pip install -e .
```

Для разработки используйте `uv sync --extra dev` (синхронизация по `uv.lock` с dev-зависимостями) -- см. раздел [Разработка](#разработка).

## Быстрый старт

### 1. Получить API credentials

Зайти на [my.telegram.org](https://my.telegram.org), создать приложение, получить `api_id` и `api_hash`.

```bash
uv run tg-export auth credentials
```

Ввести `api_id` и `api_hash`. Они сохраняются в `~/.config/tg-export/api_credentials.yaml` и используются для всех аккаунтов.

### 2. Настроить прокси (если нужен)

Создать файл `~/.config/tg-export/config.yaml`:

```yaml
proxy:
  type: socks5      # socks5, socks4, http
  host: 127.0.0.1
  port: 1080
  # username: user   # опционально
  # password: pass   # опционально
```

### 3. Залогиниться в Telegram

```bash
uv run tg-export auth add --name myaccount
```

Ввести номер телефона и код подтверждения. Сессия сохранится в `~/.config/tg-export/sessions/myaccount.session`.

Для нескольких аккаунтов повторить с разными именами. Установить аккаунт по умолчанию:

```bash
uv run tg-export account default myaccount
```

### 4. Анализ чатов и каталог

Получить каталог всех чатов аккаунта:

```bash
uv run tg-export list --output catalog.yaml
uv run tg-export list --format json --output catalog.json
```

Посмотреть информацию о конкретных чатах (количество сообщений, последние сообщения):

```bash
uv run tg-export tg info 123456789 987654321
uv run tg-export tg info --from-catalog catalog.json --type personal --last 3
```

### 5. Настройка правил экспорта

Сгенерировать шаблон конфига:

```bash
uv run tg-export init
```

Конфиг создается в `~/.config/tg-export/myaccount.yaml`. Настроить правила: какие чаты экспортировать, какие пропускать, какие типы медиа скачивать.

AI-агенты (Claude Code, Cursor и т.д.) могут помочь с настройкой: показать список чатов по категориям, задать вопросы о каждом и сформировать конфиг интерактивно.

Пример конфига:

```yaml
output:
  path: ./export_output/myaccount

defaults:
  media:
    types: [all]
    max_file_size: 100MB
    concurrent_downloads: 3

# Импорт файлов из существующего экспорта tdesktop
import_existing:
  - path: ~/Downloads/Telegram Desktop/DataExport
    type: tdesktop

# Правила по типам чатов
type_rules:
  bots:
    skip: true
  public:
    skip: true
  personal: {}    # экспортировать все личные чаты
  self: {}        # экспортировать Saved Messages

# Правила по папкам Telegram
folders:
  Work: {}
  Family:
    media:
      types: [photo, video]

# Правила по конкретным чатам
chats:
  - id: 777000
    name: Telegram
    action: skip
  - id: 123456789
    name: "Important Chat"
    media:
      types: []    # без медиа

# Что делать с чатами, не попавшими ни в одно правило
unmatched:
  action: skip    # skip | export_with_defaults | ask
```

### 6. Запустить экспорт

```bash
uv run tg-export run
```

Для пробного запуска без скачивания:

```bash
uv run tg-export run --dry-run
```

При повторном запуске скачиваются только новые сообщения (инкрементальный экспорт).

### 7. Sibling-дедупликация

При экспорте нескольких аккаунтов в один каталог (`./export_output/`), tg-export автоматически находит соседние базы данных и использует уже скачанные файлы через hardlink:

```
export_output/
  account1/         # первый аккаунт
  account2/         # файлы из общих чатов линкуются из account1
```

## Структура конфигов

```
~/.config/tg-export/
  api_credentials.yaml      # API ID и Hash (общие для всех аккаунтов)
  config.yaml               # Глобальные настройки (proxy, min_free_space)
  default_account            # Имя аккаунта по умолчанию
  sessions/
    myaccount.session        # Telethon-сессия
  myaccount.yaml             # Конфиг экспорта для аккаунта

export_output/
  myaccount/
    .tg-export-state.db      # SQLite: состояние экспорта, сообщения, файлы
    unfiled/
      Chat_Name_123456/
        messages.html         # Redirect на первый месяц
        messages_2024-01.html # Сообщения за январь 2024
        messages_2024-02.html # Сообщения за февраль 2024
        photos/               # Скачанные фото
        videos/               # Скачанные видео
        files/                # Скачанные документы
    folders/
      Work/
        ...
```

## Глобальные опции

Опции указываются до подкоманды (`uv run tg-export <опция> <команда>`):

| Опция               | Назначение                                                                                  |
| ------------------- | ------------------------------------------------------------------------------------------- |
| `--debug`           | Включить отладочное логирование (принудительно уровень `DEBUG`).                             |
| `--log-level LEVEL` | Уровень логирования: `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`. Переопределяет `LOG_LEVEL`. |
| `--quiet`, `-q`     | Подавить прогресс и статусные строки; ошибки и итоговая сводка остаются.                     |

Приоритет уровня логирования (по убыванию): `--debug` > `--log-level` > переменная окружения `LOG_LEVEL` > значение по умолчанию `WARNING`.

Потоки вывода: машиночитаемый вывод команд `list`, `state show`, `tg info`, `tg messages` идёт в stdout; прогресс, статусы, диагностика и ошибки — в stderr. Это позволяет безопасно использовать пайпинг, например `uv run tg-export list --format json | jq ...`.

Флаг `--json` для машиночитаемого вывода поддерживают команды `account list`, `auth check` и `state show`. В этом режиме в stdout печатается только JSON.

Коды выхода: `0` — успех; `2` — ошибка разбора аргументов; прерывание команды `run` сигналом завершает процесс с кодом `130` (SIGINT, Ctrl+C) или `143` (SIGTERM).

## Документация

- [docs/configuration.md](docs/configuration.md) -- подробное описание YAML-конфигурации экспорта (правила, фильтры, типы медиа, настройки выгрузки).
- [CHANGELOG.md](CHANGELOG.md) -- список изменений между версиями.
- [CONTRIBUTING.md](CONTRIBUTING.md) -- руководство для контрибьюторов: установка dev-окружения, запуск тестов, формат коммитов.
- [docs/adr/](docs/adr/) -- архитектурные решения (ADR) в формате MADR.
- [RELEASING.md](RELEASING.md) -- процесс выпуска версий и конвенции тегов.

## Автодополнение командной строки

tg-export использует Click, который поддерживает автодополнение для bash/zsh/fish. Включить его можно через переменную окружения (добавьте строку в `~/.bashrc`, `~/.zshrc` или конфиг fish):

```bash
# bash
eval "$(_TG_EXPORT_COMPLETE=bash_source tg-export)"
# zsh
eval "$(_TG_EXPORT_COMPLETE=zsh_source tg-export)"
# fish
_TG_EXPORT_COMPLETE=fish_source tg-export | source
```

## Тесты

```bash
uv run python -m pytest tests/ -v
```

## Разработка

Установка dev-зависимостей:

```bash
uv sync --extra dev
```

Запуск тестов:

```bash
uv run python -m pytest
```

Подробнее -- в [CONTRIBUTING.md](CONTRIBUTING.md).

## Лицензия

[MIT License](LICENSE)
