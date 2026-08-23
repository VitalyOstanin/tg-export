# Конфигурация tg-export

## Содержание

- [Обзор](#обзор)
  - [Расположение каталога конфигурации](#расположение-каталога-конфигурации)
  - [Переменные окружения и приоритет источников](#переменные-окружения-и-приоритет-источников)
- [Глобальный конфиг](#глобальный-конфиг)
  - [proxy](#proxy)
  - [min_free_space](#min_free_space)
- [Per-account конфиг](#per-account-конфиг)
  - [output](#output)
  - [defaults](#defaults)
  - [type_rules](#type_rules)
  - [folders](#folders)
  - [chats](#chats)
  - [Глобальные данные](#глобальные-данные)
  - [left_channels](#left_channels)
  - [archived](#archived)
  - [import_existing](#import_existing)
  - [unmatched](#unmatched)
- [Приоритет правил](#приоритет-правил)
- [Единицы измерения](#единицы-измерения)
- [Типы медиа](#типы-медиа)
- [Типы чатов и категории](#типы-чатов-и-категории)
- [Примеры конфигов](#примеры-конфигов)

---

## Обзор

tg-export использует два уровня конфигурации в файлах, поверх которых действуют переменные окружения и флаги командной строки:

1. **Глобальный конфиг** (`~/.config/tg-export/config.yaml`) -- настройки, общие для всех аккаунтов (proxy, min_free_space)
2. **Per-account конфиг** (`~/.config/tg-export/<alias>.yaml`) -- правила экспорта для конкретного аккаунта

Аккаунт определяется через CLI-флаг `--account <alias>` или через default-аккаунт (`tg-export account default <alias>`). Конфиг загружается по конвенции имени файла; путь можно переопределить через `--config /path/to/config.yaml`.

Дополнительно хранятся:
- API credentials: `~/.config/tg-export/api_credentials.yaml` (api_id, api_hash с my.telegram.org)
- Сессии Telethon: `~/.config/tg-export/sessions/<alias>.session`
- Default аккаунт: `~/.config/tg-export/default_account`

### Расположение каталога конфигурации

По умолчанию каталог -- `~/.config/tg-export`. Он определяется по стандарту XDG Base Directory:
если задана переменная `XDG_CONFIG_HOME`, каталог берётся как `$XDG_CONFIG_HOME/tg-export`.

Переменная `TG_EXPORT_CONFIG_DIR` задаёт каталог напрямую и имеет приоритет над `XDG_CONFIG_HOME`.
Так держат несколько независимых наборов аккаунтов -- например, рабочий и личный:

```bash
TG_EXPORT_CONFIG_DIR=~/.config/tg-export-work tg-export run
```

Флаг `--config` переопределяет только per-account YAML, но не каталог с сессиями и учётными данными.

### Переменные окружения и приоритет источников

Значение берётся из первого источника, где оно задано; ниже перечислены источники от
наиболее приоритетного к наименее.

| № | Что настраивается     | Порядок источников                                                                    |
|---|-----------------------|----------------------------------------------------------------------------------------|
| 1 | Каталог конфигурации  | `TG_EXPORT_CONFIG_DIR` > `XDG_CONFIG_HOME`/tg-export > `~/.config/tg-export`             |
| 2 | Файл конфигурации     | `--config` > `<каталог конфигурации>/<alias>.yaml`                                        |
| 3 | Аккаунт               | `--account` > аккаунт по умолчанию (`tg-export account default`)                          |
| 4 | Каталог экспорта      | `--output` (путь целиком) > `output.path` с добавлением alias аккаунта                    |
| 5 | Уровень логирования   | `--debug` > `--log-level` > `LOG_LEVEL` > `WARNING`                                       |
| 6 | Логи библиотек        | `WARNING` всегда, кроме суффикса `:all` (`LOG_LEVEL=DEBUG:all`)                           |

Переменные окружения проекта -- `TG_EXPORT_CONFIG_DIR` и `LOG_LEVEL`; `XDG_CONFIG_HOME`
используется по стандарту XDG. Флаги `--config` и `--output` принимают команды `run`,
`state show`, `state reset`, `purge` и `verify`.

Настройки экспорта (правила чатов, фильтры, типы медиа) задаются только в YAML: флагов
командной строки для них нет.

---

## Глобальный конфиг

Файл: `~/.config/tg-export/config.yaml`

### proxy

Настройка SOCKS5/SOCKS4/HTTP прокси для подключения к Telegram API.

```yaml
proxy:
  type: socks5       # socks5 | socks4 | http
  host: 127.0.0.1
  port: 1080
  rdns: true          # reverse DNS через прокси (по умолчанию true)
  username: null      # опционально
  password: null      # опционально
```

Для работы прокси необходимо установить дополнительную зависимость:

```bash
pip install "tg-export[proxy]"
```

При работе из исходников тот же набор ставится командой `uv sync --extra proxy`.

### min_free_space

Минимальное свободное место на диске. Экспорт приостанавливается, когда свободного места становится меньше указанного значения.

```yaml
min_free_space: 20GB
```

Поддерживаемые единицы: B, KB, MB, GB, TB (см. [Единицы измерения](#единицы-измерения)).

Значение `0` отключает проверку свободного места.

---

## Per-account конфиг

Файл: `~/.config/tg-export/<alias>.yaml`

Генерируется командой `tg-export init --account <alias>`: она запрашивает список чатов
у Telegram и пишет шаблон. Если каталог уже выгружен командой
`tg-export list --account <alias> --output-file catalog.yaml`, шаблон строится из файла без
обращения к сети: `tg-export init --from-catalog catalog.yaml --output-file <alias>.yaml`. Принимается
и YAML-каталог с разделами `folders`/`unfiled`/`archived`/`left`, и плоский JSON-каталог
(`list --format json`).

### output

Настройки выходного каталога.

```yaml
output:
  path: ./export_output    # базовый каталог; итоговый путь: {path}/{alias}/
  format: html             # единственный поддерживаемый формат
```

| Поле | Тип | По умолчанию | Описание |
|------|-----|-------------|----------|
| `path` | строка | `./export_output` | Базовый каталог для экспорта |
| `format` | строка | `html` | Формат вывода; поддерживается только `html` |

Экспорт аккаунта идёт в подкаталог с его alias: при `path: ./export_output` и аккаунте
`myaccount` файлы и база состояния окажутся в `./export_output/myaccount/`. Так два
аккаунта не делят один каталог и одну базу состояния, а файлы общих чатов переиспользуются
между ними через жёсткие ссылки (см. sibling-дедупликацию в README).

Alias не добавляется в двух случаях, чтобы не переносить уже сделанные выгрузки:
последний элемент `path` совпадает с alias (`path: ./export_output/myaccount`) либо каталог
`path` уже содержит базу состояния `.tg-export-state.db`. Опция `--output` задаёт каталог
экспорта целиком: к ней alias не добавляется никогда.

### defaults

Правила экспорта по умолчанию, применяемые ко всем чатам, если не переопределены более специфичными правилами.

```yaml
defaults:
  media:
    types: [photo, video, voice, video_note, sticker, gif, document]
    max_file_size: 100MB
    concurrent_downloads: 3    # допустимый диапазон: 1-5
  date_from: null              # формат: YYYY-MM-DD
  date_to: null                # формат: YYYY-MM-DD
  export_service_messages: true
```

| Поле | Тип | По умолчанию | Описание |
|------|-----|-------------|----------|
| `media.types` | список / `all` | `[photo]` | Типы медиа для скачивания (см. [Типы медиа](#типы-медиа)) |
| `media.max_file_size` | размер | `100MB` | Максимальный размер файла |
| `media.concurrent_downloads` | число (1-5) | `3` | Параллельные загрузки внутри чата |
| `date_from` | дата | `null` | Экспорт сообщений начиная с этой даты |
| `date_to` | дата | `null` | Экспорт сообщений до этой даты |
| `export_service_messages` | bool | `true` | Экспортировать системные сообщения |

### type_rules

Правила экспорта по типам чатов. Позволяют задать поведение для всех чатов определенного типа или категории.

```yaml
type_rules:
  bots:
    skip: true
  channels:
    media:
      types: [photo]
      max_file_size: 50MB
  personal:
    media:
      types: all
      max_file_size: 500MB
```

Ключ -- точный тип чата (`bot`, `personal`, `private_supergroup`, ...) или категория (`bots`, `channels`, `groups`, `private`, `public`). См. [Типы чатов и категории](#типы-чатов-и-категории).

Каждое правило поддерживает:

| Поле | Тип | Описание |
|------|-----|----------|
| `skip` | bool | Пропустить все чаты этого типа |
| `media` | объект | Переопределение настроек медиа |
| `date_from` | дата | Ограничение по дате начала |
| `date_to` | дата | Ограничение по дате конца |

### folders

Правила экспорта по папкам Telegram. Имена папок должны совпадать с папками в Telegram.

```yaml
folders:
  "Работа":
    media:
      types: [photo, document]
      max_file_size: 100MB
    chats:
      - name: "Рабочий чат"
        media:
          types: [document]
          max_file_size: 500MB
      - id: 1234567890
        media:
          types: all

  "Семья":
    media:
      types: all
      max_file_size: 500MB

  "Новости":
    skip: true
```

| Поле | Тип | Описание |
|------|-----|----------|
| `skip` | bool | Пропустить все чаты в папке |
| `media` | объект | Настройки медиа для всех чатов папки (если не переопределены в `chats`) |
| `chats` | список | Индивидуальные правила для конкретных чатов в папке |

Чаты в `chats` идентифицируются по `id` или `name`. Поддерживают те же поля, что и правила в секции [chats](#chats).

Если чат входит в несколько папок Telegram, он экспортируется один раз -- в папку с наивысшим приоритетом совпадения правил (первая папка по порядку в конфиге).

### chats

Индивидуальные правила для конкретных чатов. Имеют наивысший приоритет.

```yaml
chats:
  - name: "Saved Messages"
    media:
      types: all
      max_file_size: 2GB

  - id: 9876543210
    name: "Секретный чат"     # name опционален при указании id
    media:
      types: [photo]
    date_from: 2024-06-01
```

| Поле | Тип | Описание |
|------|-----|----------|
| `id` | число | ID чата в Telegram |
| `name` | строка | Имя чата (для поиска, если id не указан) |
| `media` | объект | Настройки медиа |
| `date_from` | дата | Экспорт начиная с даты |
| `date_to` | дата | Экспорт до даты |
| `skip` | bool | Пропустить этот чат |

Должен быть указан хотя бы `id` или `name`.

### Глобальные данные

Флаги экспорта данных, не привязанных к чатам. Каждый флаг управляет и выгрузкой,
и соответствующим разделом на главной странице экспорта.

```yaml
personal_info: true
contacts: true           # включает frequent contacts
sessions: true           # включает web sessions
userpics: true
stories: true
profile_music: true      # сохранённые мелодии; ими и наполнена страница «Прочее»
other_data: true         # зарезервирован: своих данных пока не добавляет
```

Страница «Прочее» (`other_data.html`) сейчас содержит только мелодии профиля, поэтому
её создание управляется флагом `profile_music`. Флаг `other_data` зарезервирован под
данные, которые появятся позже.

### left_channels

Поведение для покинутых каналов и групп.

```yaml
left_channels:
  action: skip             # skip | export_with_defaults
```

При `export_with_defaults` используются правила из `defaults`.

### archived

Поведение для архивных чатов.

```yaml
archived:
  action: skip             # skip | export_with_defaults
```

### import_existing

Импорт ранее скачанных файлов из предыдущих экспортов. Позволяет избежать повторного скачивания файлов, которые уже есть на диске.

```yaml
import_existing:
  - path: ~/TelegramExport_2024
    type: tdesktop
```

| Поле   | Тип    | Описание                                          |
|--------|--------|---------------------------------------------------|
| `path` | строка | Путь к каталогу предыдущего экспорта               |
| `type` | строка | Тип экспорта; поддерживается только `tdesktop`     |

Оба поля обязательны. Неизвестное значение `type` -- ошибка загрузки конфига: раньше такая запись
молча пропускалась, и файлы продолжали скачиваться заново.

Файлы из другого экспорта tg-export переиспользуются без этой настройки: экспорты соседних
аккаунтов, лежащие рядом в одном родительском каталоге, находятся автоматически.

### unmatched

Поведение для чатов, не попавших ни под одно правило.

```yaml
unmatched:
  action: skip             # skip | export_with_defaults
```

---

## Приоритет правил

От высшего к низшему:

1. Конкретный чат в секции `chats` (по id или имени)
2. Конкретный чат внутри папки в `folders.*.chats`
3. Правила папки в `folders.*`
4. `type_rules` (точный тип > категория)
5. `defaults` (если `unmatched.action` != `skip`)

---

## Единицы измерения

Поля `max_file_size` и `min_free_space` поддерживают следующие единицы:

| Единица | Множитель |
|---------|-----------|
| `B` | 1 |
| `KB` | 1024 |
| `MB` | 1024^2 (1 048 576) |
| `GB` | 1024^3 (1 073 741 824) |
| `TB` | 1024^4 |

Примеры: `50MB`, `2GB`, `20GB`, `500KB`, `1TB`.

Число может быть дробным: `1.5GB`.

---

## Типы медиа

Допустимые значения для `media.types`:

| Тип | Подпапка | Описание |
|-----|----------|----------|
| `photo` | photos/ | Фотографии |
| `video` | videos/ | Видео |
| `document` | files/ | Документы/файлы |
| `voice` | voice_messages/ | Голосовые сообщения |
| `video_note` | video_messages/ | Видео-кружки |
| `sticker` | stickers/ | Стикеры |
| `gif` | gifs/ | GIF-анимации |

Специальное значение `all` -- включает все типы медиа.

Остальные типы медиа файла не имеют и потому не скачиваются, но экспортируются как часть
сообщения: `contact`, `geo`, `venue`, `poll`, `game`, `invoice`, `todo_list`, `giveaway`,
`paid_media`, а также `unsupported` -- вложение, которое tg-export не распознал. Указывать
их в `media.types` бессмысленно: список задаёт, что скачивать.

---

## Типы чатов и категории

### Точные типы

| Тип | Описание |
|-----|----------|
| `self` | Saved Messages |
| `replies` | Ответы |
| `verify_codes` | Коды верификации |
| `personal` | Личный чат |
| `bot` | Чат с ботом |
| `private_group` | Приватная группа (старая) |
| `private_supergroup` | Приватная супергруппа |
| `public_supergroup` | Публичная супергруппа |
| `private_channel` | Приватный канал |
| `public_channel` | Публичный канал |

### Категории (шорткаты для type_rules)

| Категория | Включает типы |
|-----------|---------------|
| `private` | personal, private_group, private_supergroup, private_channel, self |
| `public` | public_supergroup, public_channel |
| `groups` | private_group, private_supergroup, public_supergroup |
| `channels` | private_channel, public_channel |
| `bots` | bot |

---

## Примеры конфигов

### Минимальный конфиг

```yaml
defaults:
  media:
    types: [photo]
    max_file_size: 100MB

unmatched:
  action: export_with_defaults
```

### Полный конфиг

Этот же текст лежит в корне репозитория файлом
[config.example.yaml](../config.example.yaml) и загружается как есть; совпадение
двух текстов проверяется тестом.

```yaml
# Пример конфигурации tg-export.
#
# Рабочая отправная точка: скопируйте файл в ~/.config/tg-export/<алиас>.yaml
# (или укажите путь флагом --config) и правьте под себя. Полное описание всех
# разделов -- docs/configuration.md; шаблон под конкретный список чатов
# генерирует команда `tg-export init`.
#
# Имена чатов и идентификаторы здесь вымышленные.

output:
  # Каталог выгрузки. Алиас аккаунта добавляется сам: ./export_output/<алиас>.
  path: ./export_output
  format: html

# Правила, которые действуют, когда чат не подошёл ни под одно другое правило.
defaults:
  media:
    types: [photo, video, voice, video_note, sticker, gif, document]
    max_file_size: 100MB
    concurrent_downloads: 3
  # Границы периода: null -- без ограничения.
  date_from: null
  date_to: null
  export_service_messages: true

# Общие данные аккаунта: профиль, контакты, сессии, аватары, истории, музыка.
personal_info: true
contacts: true
sessions: true
userpics: true
stories: true
profile_music: true
other_data: true

# Правила по типу чата. Точный тип важнее категории.
# Категории: private, public, groups, channels, bots.
# Точные типы: personal, bot, self, private_group, private_supergroup,
#   public_supergroup, private_channel, public_channel.
type_rules:
  bots:
    skip: true
  public_channel:
    media:
      types: [photo]
      max_file_size: 10MB

# Правила по папкам Telegram. Внутри папки можно задать правило для чата.
folders:
  "Работа":
    media:
      types: [photo, document]
      max_file_size: 100MB
    chats:
      - name: "Проект «Ромашка»"
        media:
          types: [document]
          max_file_size: 500MB

# Правила для отдельных чатов. Приоритет выше папок и типов.
chats:
  - id: 1234567890
    media:
      types: all
  - name: "Иван Иванов"
    date_from: 2024-01-01

# Покинутые каналы: skip | export_with_defaults.
left_channels:
  action: skip

# Архивные чаты: skip | export_with_defaults.
archived:
  action: skip

# Чат, не подошедший ни под одно правило: skip | export_with_defaults.
unmatched:
  action: skip

# Переиспользование файлов из экспорта Telegram Desktop вместо повторного
# скачивания. Экспорты самого tg-export, лежащие рядом, находятся сами.
# import_existing:
#   - path: ~/TelegramExport_2024
#     type: tdesktop
```

### Глобальный конфиг с прокси

```yaml
# ~/.config/tg-export/config.yaml

proxy:
  type: socks5
  host: 127.0.0.1
  port: 9050

min_free_space: 20GB
```
