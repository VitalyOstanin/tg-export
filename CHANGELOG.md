# Changelog

Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
проект следует [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Содержание

- [\[Не выпущено\]](#не-выпущено)
- [\[1.5.1\] -- 2026-07-30](#151----2026-07-30)
- [\[1.5.0\] -- 2026-07-30](#150----2026-07-30)
- [\[1.4.0\] -- 2026-05-29](#140----2026-05-29)
- [\[1.3.0\] -- 2026-05-27](#130----2026-05-27)
- [\[1.2.7\] -- 2026-05-18](#127----2026-05-18)
- [\[1.2.6\] -- 2026-05-18](#126----2026-05-18)
- [\[1.2.5\] -- 2026-05-08](#125----2026-05-08)
- [\[1.2.4\] -- 2026-05-07](#124----2026-05-07)
- [\[1.2.3\] -- 2026-05-07](#123----2026-05-07)
- [\[1.2.2\] -- 2026-05-07](#122----2026-05-07)
- [\[1.2.1\] -- 2026-05-07](#121----2026-05-07)
- [\[1.2.0\] -- 2026-05-07](#120----2026-05-07)
- [\[1.1.0\] -- 2026-05-02](#110----2026-05-02)
- [\[1.0.0\] -- 2026-03-28](#100----2026-03-28)

## [Не выпущено]

### Добавлено

- Опция `run --require-takeout`: отказ Takeout завершает команду ошибкой вместо перехода на обычный API. Без флага поведение прежнее -- переход выполняется, но теперь он объявляется как существенное сообщение (видно и под `--quiet`), а использованный режим печатается в итоговой сводке строкой `API:`.
- Команда `tg send` принимает `--as-document`: файл отправляется без сжатия, как документ.
- Прогресс отправки файлов в `tg send`.

### Изменено

- Коды возврата приведены к единому правилу: `0` — успех, `1` — команда сообщила об отказе, `2` — ошибка разбора аргументов, `128 + номер сигнала` — прерывание. Раньше кодом `0` завершались `account remove` с неизвестным именем, `auth check` с непригодным аккаунтом, `tg send` при недоставке, `tg download` для отсутствующего сообщения, `state reset` для чата без состояния, `tg info` с ошибками запроса, а также `run` с ошибками экспорта. Прерывание по Ctrl+C вне цикла экспорта теперь тоже даёт `130`, а не `0` или `1`. Таблица кодов добавлена в README.
- Покрытие тестами считается при каждом запуске pytest, а не только в CI, и проверяется не одним общим порогом, а нижними границами по модулям (`scripts/coverage_gate.py`, секция `[tool.tg-export.coverage-floor]`). Общий порог поднят с 45% до 50%.
- Ошибки команды `tg info` печатаются в stderr, а не в stdout: машиночитаемый вывод остаётся пригодным для конвейера.

### Исправлено

- Команда `verify` больше не удаляет битый файл до того, как получит замену. Файл скачивается во временный каталог рядом с целевым и переносится атомарным переименованием только после успеха; обрыв связи или Ctrl+C оставляет прежний файл на месте. Каталоги, оставшиеся от прерванного запуска, убираются при следующем. Команда возвращает `1`, если удалось перекачать не все файлы.
- Потеря `takeout_id` на файлах сессии с физическим порядком колонок `(..., auth_key, tmp_auth_key, takeout_id)`. Telethon пишет строку `sessions` позиционно, поэтому смысл несёт позиция, а не имя колонки; чтение по имени забирало чужие значения, признавало настоящий `takeout_id` мусором и стирало его на каждом запуске, из-за чего режим Takeout не переиспользовался. Чтение переведено на позиции, значение резервируется до обнуления и восстанавливается после, а вся операция идёт в одной транзакции `BEGIN IMMEDIATE`.
- Падение старта на файле сессии схемы `version = 7` с активным Takeout (`TypeError: object supporting the buffer API required`): такая схема больше не пропускается обходом.
- Jinja-шаблоны и статика (CSS, JS, иконки) теперь попадают в дистрибутив. Без `[tool.setuptools.package-data]` в колесо уходили только `*.py`, поэтому любой экспорт после `pip install tg-export` падал с `TemplateNotFound: 'index.html.j2'`; дефект действовал с версии 1.0.0. Smoke-тест перед публикацией больше не ограничивается `--version`: он компилирует все шаблоны установленного пакета и рендерит индексную страницу.
- Прерывание фазы 1 (Ctrl+C) больше не оставляет безвозвратно пропущенный интервал сообщений. Фаза 1 идёт от новых сообщений к старым, поэтому указатель `last_msg_id` продвигается только после её полного прохода; фаза 2 спускается от `oldest_msg_id` и в пропущенный интервал не заходила, так что потерянные сообщения не забирал никто.
- Указатель `last_msg_id` больше не откатывается назад: запись из копии, прочитанной в начале экспорта чата, не может уменьшить уже достигнутое значение. Сброс через `state reset` работает как прежде.
- Отказ Takeout больше не поглощается широким `except Exception` -- обрабатываются только ошибки, которые действительно означают недоступность Takeout, а дефекты кода доходят до вызывающего.

## [1.5.1] -- 2026-07-30

### Исправлено

- Классификация чатов, из которых аккаунт удалён или заблокирован (`ChatForbidden`, `ChannelForbidden`). Ранее такой чат попадал в тип `personal` с предупреждением `Unknown entity class`; теперь он определяется как `private_group`, `private_supergroup` или `private_channel` и помечается признаком `is_left`, поэтому попадает в раздел `left` каталога и пропускается при экспорте по умолчанию (`left_channels.action = skip`) -- вместо гарантированной ошибки запроса недоступной истории.

## [1.5.0] -- 2026-07-30

### Добавлено

- Опции `--truncate N` и `--no-truncate` для команды `tg messages`: длина обрезки текста сообщения задаётся явно, `--truncate 0` и `--no-truncate` печатают текст целиком. По умолчанию сохранено прежнее поведение -- обрезка на 200 символов. Одновременное указание `--no-truncate` с ненулевым `--truncate` -- ошибка разбора аргументов (код `2`).

## [1.4.0] -- 2026-05-29

Выпуск устраняет 42 находки автоматического ревью кода.

### Добавлено

- Глобальные флаги `--quiet`/`-q` (подавление прогресса и статусов; ошибки и итоговая сводка сохраняются) и `--log-level` (уровень логирования с поддержкой переменной окружения `LOG_LEVEL`; приоритет `--debug` > `--log-level` > `LOG_LEVEL` > `WARNING`).
- Флаг `--json` для машиночитаемого вывода команд `account list`, `auth check`, `state show`.
- Базовый класс доменных ошибок `TgExportError` ([tg_export/errors.py](tg_export/errors.py)); все доменные исключения наследуются от него.
- Общий модуль форматирования [tg_export/format.py](tg_export/format.py) (`format_size`).
- Файлы `.editorconfig`, `RELEASING.md`, каталог `docs/adr/`; разделы README про глобальные опции и автодополнение.

### Изменено

- Диагностика, прогресс и логи направлены в stderr; stdout оставлен только для машиночитаемого вывода команд `list`, `state show`, `tg info`, `tg messages` — безопасный пайпинг (`tg-export list --format json | jq ...`).
- Прерывание команды `run` сигналом завершает процесс с кодом `130` (SIGINT) или `143` (SIGTERM) вместо `0`.
- Доменные ошибки на верхнем уровне выводятся кратким сообщением и кодом выхода `1`; полный traceback — только под `--debug` (обёртка `run_cli`).
- Точка входа консольного скрипта изменена на `tg_export.cli:run_cli`.
- Порог покрытия тестами `cov-fail-under=45`; `codecov-action` обновлён до v6.0.1; перед публикацией на PyPI прогоняются тесты и smoke-тест собранного wheel.
- Все комментарии в исходном коде переведены на английский.

### Исправлено

- `_export_userpics`: имя файла больше не переиспользуется при неуспешной загрузке (отдельный счётчик на каждой итерации).
- Конфликт `offset_id`/`offset_date` в фазе 2 при заданном `date_to`; атомарный commit прогресса фазы 2.
- Проглатывание ошибок (скачивание фото профиля) заменено логированием; в warning'ах экспорта добавлен stack trace (`exc_info`).
- Ссылка на чат в индексе учитывает `sanitize_name(folder)`.
- Потокобезопасный доступ к `active_downloads` между event loop и потоком обновления Rich Live; ограничен рост словаря блокировок `file_id`; jitter в retry скачивания.
- Валидация значений `action`/`format` и неизвестных ключей YAML-конфига при загрузке.
- `purge_chat` выполняет удаление в одной транзакции; запросы по диапазону дат используют индекс.

## [1.3.0] -- 2026-05-27

### Добавлено

- Флаг `--version` (`tg-export --version`): выводит версию пакета из метаданных установленного дистрибутива (`importlib.metadata`), номер версии в коде не дублируется.

### Безопасность

- Fail-fast при заданном в конфиге `proxy`, но отсутствующем пакете `python-socks`: ранее Telethon молча игнорировал прокси и подключался напрямую, раскрывая реальный IP. Теперь `TgApi` прерывает запуск с понятной ошибкой и подсказкой по установке extra `[proxy]` ([tg_export/api.py](tg_export/api.py)).

## [1.2.7] -- 2026-05-18

### Инфраструктура релиза

- GitHub Actions: автоматическое создание GitHub Release при пуше тега `v*` (`.github/workflows/publish.yml`): вырезает секцию из `CHANGELOG.md` и создаёт release через `gh release create`.
- CI: шаг `uv lock --check` в `.github/workflows/ci.yml` проверяет синхронизацию `uv.lock` с `pyproject.toml`. Расхождение версий теперь валит CI и не попадёт в следующий релиз.
- CLAUDE.md: раздел «Релиз и версионирование» -- при бампе версии / изменении зависимостей обязателен `uv lock`.

## [1.2.6] -- 2026-05-18

### Исправлено

- `_extract_and_clear` в `FixedSQLiteSession`: type-валидация `takeout_id` и `tmp_auth_key`, `has_data` через `is not None` (раньше `bool(b'')==False` пропускал пустые BLOB как «нет данных»). Telethon при `store_tmp_auth_key_on_disk=False` пишет `b''` в physical позицию `tmp_auth_key`, а swap-баг при следующем чтении ставит `session._takeout_id = b''`, что валит сериализатор `InvokeWithTakeoutRequest(takeout_id=b'')` с `struct.error: required argument is not an integer`. Дополнительно post-init валидация: если после `super().__init__()` `_takeout_id` оказался non-int -- clear.
- Атомарный `commit_phase_progress` в `ExportState` ([tg_export/state.py](tg_export/state.py)). Ранее фаза 2 делала 4 раздельных commit-а (last/oldest/full_history/messages_count); прерывание сети между ними оставляло несогласованное состояние, и `set_oldest_msg_id` мог упасть с `NOT NULL constraint failed: export_state.last_msg_id`. Все четыре сеттера переведены на единый `_upsert_chat_state` с whitelist колонок и явным `last_msg_id=0` в INSERT-ветке. Схема: `last_msg_id INTEGER NOT NULL DEFAULT 0`.
- Кооперативное прерывание HTML-рендера в `render_chat_streaming` и `_render_index` через параметр `should_stop`. Force shutdown зависал после «state saved», потому что `asyncio.to_thread` ставит задачу в default `ThreadPoolExecutor`, `task.cancel()` прерывает только `await`, а сам thread продолжает Jinja2-рендер; `asyncio.run().__exit__` ждёт thread join() с таймаутом. Гранулярность прерывания -- month bucket; внутри одного `jinja2.render()` прерывания нет (CPython не позволяет прервать thread без yield).
- `chat_error_line` принимает опциональный `chat_id`, и сообщение об ошибке экспорта чата включает id (`Error exporting Alice (id=12345): ...`) для возможности ручной правки записи в `export_state`.

### Качество кода

- `cast(Any, ...)` для возвращаемых значений Telethon `get_personal_info()` / `get_top_peers()`: Pyright не разрешал доступ к `.full_user` / `.users` / `.categories` для `Union` без stubs.

## [1.2.5] -- 2026-05-08

### Исправлено

- Замена workaround'а 1.2.4 на корректный фикс через subclass `FixedSQLiteSession` ([tg_export/session.py](tg_export/session.py)). Перед `super().__init__()` читаем `takeout_id`/`tmp_auth_key` явно по именам колонок, обнуляем их на диске (чтобы баггованный позиционный unpack в Telethon не крашился на `AuthKey(data=int)`), затем восстанавливаем значения через сеттеры -- write-путь у Telethon корректный. Главное преимущество: `takeout_id` теперь **сохраняется между запусками**, прежний sanitize терял его и заставлял каждый запуск создавать свежий takeout (cooldown TAKEOUT_INIT_DELAY и повторное подтверждение в клиенте Telegram). Регрессионный тест в `tests/test_api.py` явно проверяет, что ванильный `SQLiteSession` крашится на той же фикстуре, на которой наш subclass работает корректно.

## [1.2.4] -- 2026-05-07

### Исправлено

- Workaround для асимметричного бага Telethon `SQLiteSession` 1.43+ ([commit 5a3a94eb](https://github.com/LonamiWebs/Telethon/commit/5a3a94eb)): `_update_session_table` пишет в порядке `(..., auth_key, takeout_id, tmp_auth_key)`, а `__init__` читает `select *` и распаковывает как `(..., key, tmp_key, takeout_id)` -- 5-й и 6-й столбцы переставлены местами. Пока обе колонки `NULL`, `AuthKey(data=None)` срабатывает по early-return и баг не виден; как только Takeout-экспорт сохраняет `takeout_id`, при следующем старте этот int попадает в `tmp_key`, и Telethon крашится с `TypeError: object supporting the buffer API required` из `sha1(int)`. Перестановка колонок не помогает -- read/write симметрично сломаны. `TgApi.__init__` теперь обнуляет `tmp_auth_key` и `takeout_id` через `_sanitize_session_file` перед открытием `TelegramClient`. `auth_key` (256 байт) сохраняется, перелогин не нужен; наш `start_takeout` всё равно начинает свежий takeout. Правильный фикс через monkey-patch `SQLiteSession` записан в [TODO.md](TODO.md).

## [1.2.3] -- 2026-05-07

### Исправлено

- `start_takeout` автоматически завершает (или локально обнуляет) стейл `session.takeout_id` перед началом новой Takeout-сессии. Раньше Telethon бросал `ValueError("Can't send a takeout request while another takeout for the current session still not been finished yet.")` из `TakeoutClient.__aenter__` без обращения к серверу, и пользователю приходилось вручную запускать `tg-export takeout clear`.

## [1.2.2] -- 2026-05-07

### Исправлено

- `[proxy]` extras: вернули `python-socks[asyncio]>=2.0`. Telethon 1.43+ проверяет `python_socks` и при его отсутствии **молча игнорирует** аргумент `proxy=` с warning'ом "proxy argument will be ignored because python-socks is not installed". PySocks-путь в `connection.py` гейтится той же проверкой и не запускается; в 1.2.1 прокси из конфига фактически не применялся.

## [1.2.1] -- 2026-05-07

### Исправлено

- `[proxy]` extras: ошибочный переход на `PySocks>=1.7`. Откачено в 1.2.2.

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
