# Конфигурация tg-export

## Содержание

- [Обзор](#обзор)
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

tg-export использует два уровня конфигурации:

1. **Глобальный конфиг** (`~/.config/tg-export/config.yaml`) -- настройки, общие для всех аккаунтов (proxy, min_free_space)
2. **Per-account конфиг** (`~/.config/tg-export/<alias>.yaml`) -- правила экспорта для конкретного аккаунта

Аккаунт определяется через CLI-флаг `--account <alias>` или через default-аккаунт (`tg-export account default <alias>`). Конфиг загружается по конвенции имени файла; путь можно переопределить через `--config /path/to/config.yaml`.

Дополнительно хранятся:
- API credentials: `~/.config/tg-export/api_credentials.yaml` (api_id, api_hash с my.telegram.org)
- Сессии Telethon: `~/.config/tg-export/sessions/<alias>.session`
- Default аккаунт: `~/.config/tg-export/default_account`

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
uv pip install "tg-export[proxy]"
```

### min_free_space

Минимальное свободное место на диске. Экспорт приостанавливается, когда свободного места становится меньше указанного значения.

```yaml
min_free_space: 20GB
```

Поддерживаемые единицы: B, KB, MB, GB, TB (см. [Единицы измерения](#единицы-измерения)).

---

## Per-account конфиг

Файл: `~/.config/tg-export/<alias>.yaml`

Генерируется командой `tg-export init --from catalog.yaml --output <alias>.yaml`.

### output

Настройки выходного каталога.

```yaml
output:
  path: ./export_output    # базовый каталог; итоговый путь: {path}/{alias}/
  format: html             # html | json | both
```

| Поле | Тип | По умолчанию | Описание |
|------|-----|-------------|----------|
| `path` | строка | `./export_output` | Базовый каталог для экспорта |
| `format` | строка | `html` | Формат вывода |

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

Флаги экспорта данных, не привязанных к чатам. Пока не реализовано (зарезервировано).

```yaml
personal_info: true
contacts: true           # включает frequent contacts
sessions: true           # включает web sessions
userpics: true
stories: true
profile_music: true
other_data: true
```

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
  - path: ~/old_tg_export
    type: tg-export
```

| Поле | Тип | Описание |
|------|-----|----------|
| `path` | строка | Путь к каталогу предыдущего экспорта |
| `type` | строка | Тип экспорта: `tdesktop` или `tg-export` |

### unmatched

Поведение для чатов, не попавших ни под одно правило.

```yaml
unmatched:
  action: skip             # skip | export_with_defaults | ask
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

Типы без файлов (contact, geo, venue, poll, game, invoice) не скачиваются, но экспортируются как часть сообщений.

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

```yaml
output:
  path: ./export_output
  format: html

defaults:
  media:
    types: [photo, video, voice, video_note, sticker, gif, document]
    max_file_size: 100MB
    concurrent_downloads: 3
  date_from: null
  date_to: null
  export_service_messages: true

personal_info: true
contacts: true
sessions: true
userpics: true
stories: true
profile_music: true
other_data: true

type_rules:
  bots:
    skip: true
  channels:
    media:
      types: [photo]
      max_file_size: 50MB

left_channels:
  action: skip

archived:
  action: skip

import_existing:
  - path: ~/TelegramExport_2024
    type: tdesktop

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

chats:
  - name: "Saved Messages"
    media:
      types: all
      max_file_size: 2GB

  - id: 9876543210
    media:
      types: [photo]
    date_from: 2024-06-01

unmatched:
  action: skip
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
