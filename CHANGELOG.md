# Changelog

Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
проект следует [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Содержание

- [\[1.2.1\] -- 2026-05-07](#121----2026-05-07)
- [\[1.2.0\] -- 2026-05-07](#120----2026-05-07)
- [\[1.1.0\] -- 2026-05-02](#110----2026-05-02)
- [\[1.0.0\] -- 2026-03-28](#100----2026-03-28)

## [1.2.1] -- 2026-05-07

### Исправлено

- `[proxy]` extras: `PySocks>=1.7` вместо `python-socks[asyncio]`. Telethon импортирует `from socks import ...` (модуль из пакета PySocks); `python-socks` -- это другой пакет с модулем `python_socks` и не удовлетворяет импорт. При попытке использовать прокси падало `ModuleNotFoundError: No module named 'socks'`.

## [1.2.0] -- 2026-05-07

### Безопасность

- Включено `autoescape` в Jinja2-шаблонах HTML-рендера: имена чатов, авторов, контактов, sessions и stories больше не позволяют XSS через `<script>` или `<img onerror>`.
- Добавлен whitelist URL-схем (`http`, `https`, `mailto`, `tel`, `tg`) и `rel="noopener noreferrer"` для всех `target="_blank"`-ссылок: `javascript:`/`data:`-инъекции через Telegram URL-entities и inline-кнопки больше не приводят к исполнению JS.
- `sanitize_name` отбрасывает `..`, управляющие символы, RTL-override; нормализует Unicode (NFKC); ограничивает длину 200 байт.
- `purge` больше не использует `rglob` -- сканирует только известные префиксы `unfiled/`, `archived/`, `left/`, `folders/*` и проверяет, что путь действительно внутри `output_base.resolve()`. Симлинки пропускаются.
- `tdesktop` import: пути из внешнего HTML валидируются через `is_relative_to(chat_dir.resolve())`.
- Sibling-БД: путь к файлу проверяется на принадлежность tree соседа; размер сравнивается с заявленным Telegram.
- Конфигурация: `~/.config/tg-export` принудительно получает 0o700; `api_credentials.yaml` валидируется на типы и слабые права.

### Исправлено

- Логика `last_msg_id` в фазе 2: накапливаем максимум через `phase2_max_id`, а не теряем после первого сообщения.
- `register_file` теперь явно делает `commit`: при kill -9 файл не остаётся незарегистрированным в БД.
- `_verify_files` использует тот же путь `register_file -> commit`, поэтому verify-результаты сохраняются и не теряются при `close()`.
- `_handle_shutdown` через `asyncio.shield` защищает идущий `commit` от отмены: повторный SIGINT не теряет batch до 500 сообщений.
- `_cleanup_orphaned_files` сравнивает абсолютные resolved-пути; запуск из другой cwd больше не приводит к удалению легитимных файлов как orphaned.
- `_download_if_new` сравнивает SHA-256 первых 64KB, а не только размер: два разных файла одной длины больше не схлопываются в один.
- `start_takeout` отлавливает `TakeoutInvalidError`/`TakeoutRequiredError` напрямую, а не через подстроку в `str(e)`.
- `cache_catalog` явно коммитит запись о чате: статистика не теряется, если экспорт прервался до первого batch-commit.
- Сообщение об отсутствии default-аккаунта указывает правильную команду `tg-export account default`, а не несуществующую `auth default`.

### Зависимости

- `rich>=15.0`: обновлено с 14.x. API Live/Progress/Console сохранён.
- `click>=8`, `pytest>=8`, `telethon>=1.36`, `pygments` обновлены до актуальных версий.
- В dev добавлены `pytest-cov`, `pytest-timeout`, `ruff`, `pyright`.

### Качество кода

- `ruff` (lint + format) и `pyright` (basic) включены в CI как блокирующие шаги. На текущий момент 0 ошибок и 0 предупреждений.
- pytest: `--strict-markers --strict-config -ra`, `timeout=60`, `asyncio_mode=auto`, фильтр `error::DeprecationWarning`.
- Тесты на XSS, URL-схемы, lock-файл, sanitize_name, credentials.

### Добавлено

- SQLite PRAGMA: `journal_mode=WAL`, `synchronous=NORMAL`, `cache_size=-65536`, `mmap_size=268435456`, `temp_store=MEMORY`, `foreign_keys=ON`.
- Lock-файл `<state>.db.lock` через `fcntl.flock`: защита от случайного второго процесса экспорта над одной БД.
- Стриминговый рендер по месяцам через `render_chat_streaming`: пиковая память пропорциональна одному месяцу, а не всему чату.
- HTML-рендер вынесен в `asyncio.to_thread`: больше не блокирует event loop на крупных чатах.
- Per-`file_id` `asyncio.Lock` в `MediaDownloader`: параллельные сообщения с одинаковым `file_id` сериализуются и получают cross-chat dedup.
- Индексы SQLite: `idx_files_chat`, `idx_files_status`, `idx_files_local_path`, `idx_messages_grouped`.
- `state.list_message_months`, `state.load_messages_for_month`, `state.get_catalog_entry`.
- `tg send` теперь предупреждает о best-effort семантике и выводит `N/M` после неуспешных получателей.
- `_register_skip` -- skipped_by_size/skipped_by_type записываются в БД, чтобы verify/count корректно их различали.

### CI/CD

- GitHub Actions запинены на полный commit SHA (`actions/checkout`, `astral-sh/setup-uv`, `pypa/gh-action-pypi-publish`); версии обновлены до актуальных.
- `permissions: contents: read` на уровне workflow в `ci.yml`; `publish.yml` дополнительно ограничен необходимым.
- `timeout-minutes` для всех jobs (15 для test, 10 для publish).
- `enable-cache: true` для setup-uv с `cache-dependency-glob: uv.lock`.

## [1.1.0] -- 2026-05-02

- Эскейп rich-markup в именах чатов и файлов; устранена гонка в `Live`-выводе.

## [1.0.0] -- 2026-03-28

- Первый стабильный релиз: инкрементальный экспорт, sibling-дедупликация, импорт из tdesktop, HTML-рендер по месяцам.
