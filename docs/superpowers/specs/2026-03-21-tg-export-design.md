# tg-export: Инструмент гибкого экспорта данных Telegram

---

## Содержание

- [1. Назначение](#1-назначение)
- [2. Требования](#2-требования)
- [3. Архитектура](#3-архитектура)
  - [3.1. Структура проекта](#31-структура-проекта)
  - [3.2. Зависимости](#32-зависимости)
- [4. CLI-интерфейс](#4-cli-интерфейс)
- [5. YAML-конфиг](#5-yaml-конфиг)
  - [5.1. Структура конфига](#51-структура-конфига)
  - [5.2. Каталог чатов](#52-каталог-чатов)
  - [5.3. Приоритет правил](#53-приоритет-правил)
  - [5.4. Чат в нескольких папках](#54-чат-в-нескольких-папках)
- [6. Модель данных](#6-модель-данных)
  - [6.1. Основные dataclasses](#61-основные-dataclasses)
  - [6.2. Типы чатов](#62-типы-чатов)
  - [6.3. Типы медиа](#63-типы-медиа)
  - [6.4. Типы форматирования текста](#64-типы-форматирования-текста)
  - [6.5. Типы service-действий](#65-типы-service-действий)
  - [6.6. SQLite-схема состояния](#66-sqlite-схема-состояния)
- [7. API-слой (Telethon + Takeout)](#7-api-слой-telethon--takeout)
  - [7.1. Класс TgApi](#71-класс-tgapi)
  - [7.2. Кеширование](#72-кеширование)
  - [7.3. Takeout-сессия](#73-takeout-сессия)
- [8. Загрузка медиа](#8-загрузка-медиа)
  - [8.1. Дедупликация файлов](#81-дедупликация-файлов)
  - [8.2. Типы медиа и подпапки](#82-типы-медиа-и-подпапки)
- [9. HTML-генерация](#9-html-генерация)
  - [9.1. Структура выходных файлов](#91-структура-выходных-файлов)
  - [9.2. Шаблоны Jinja2](#92-шаблоны-jinja2)
  - [9.3. Совместимость с tdesktop](#93-совместимость-с-tdesktop)
  - [9.4. Нумерация файлов сообщений](#94-нумерация-файлов-сообщений)
  - [9.5. Группировка сообщений и медиа-альбомы](#95-группировка-сообщений-и-медиа-альбомы)
- [10. Основной цикл экспорта](#10-основной-цикл-экспорта)
  - [10.1. Шаги экспорта](#101-шаги-экспорта)
  - [10.2. Экспорт одного чата](#102-экспорт-одного-чата)
  - [10.3. Обработка покинутых каналов](#103-обработка-покинутых-каналов)
  - [10.4. Обработка топиков (форумных тем)](#104-обработка-топиков-форумных-тем)
  - [10.5. Обработка миграции групп в супергруппы](#105-обработка-миграции-групп-в-супергруппы)
  - [10.6. Обработка monoforum](#106-обработка-monoforum)
  - [10.7. Контроль свободного места](#107-контроль-свободного-места)
  - [10.8. Обработка ошибок](#108-обработка-ошибок)
  - [10.9. Прогресс-бар](#109-прогресс-бар)
- [11. Инкрементальность](#11-инкрементальность)
  - [11.1. Дозагрузка новых сообщений](#111-дозагрузка-новых-сообщений)
  - [11.2. Хранение сообщений для перегенерации HTML](#112-хранение-сообщений-для-перегенерации-html)
  - [11.3. Верификация целостности файлов](#113-верификация-целостности-файлов)
  - [11.4. Импорт из предыдущих экспортов](#114-импорт-из-предыдущих-экспортов)
  - [11.5. Уникальная идентификация файлов в Telegram](#115-уникальная-идентификация-файлов-в-telegram)
- [12. Авторизация и мульти-аккаунт](#12-авторизация-и-мульти-аккаунт)
- [13. Безопасность](#13-безопасность)

---

## 1. Назначение

tg-export -- CLI-инструмент на Python для гибкого экспорта данных Telegram с точечным выбором чатов и индивидуальными правилами для каждого чата. Генерирует HTML, визуально идентичный экспорту официального клиента Telegram Desktop.

Основные отличия от встроенного экспорта tdesktop:
- Точечная настройка правил экспорта медиа для каждого чата/группы отдельно
- Инкрементальный экспорт: дозагрузка только новых сообщений
- Импорт файлов из предыдущих экспортов (tdesktop или tg-export)
- Поддержка нескольких аккаунтов
- Группировка по папкам Telegram
- Контроль свободного места на диске

## 2. Требования

- Python 3.11+
- Linux
- Telegram API credentials (api_id, api_hash) с my.telegram.org
- Telethon как основная библиотека для Telegram API
- Takeout Session API как основной режим (fallback на обычные запросы)
- HTML-вывод, максимально близкий к tdesktop (CSS, JS, иконки из tdesktop)
- YAML-конфиг для описания правил экспорта
- SQLite для хранения состояния экспорта и сообщений (для перегенерации HTML)
- Инкрементальная загрузка: только новые сообщения + верификация последних файлов
- Поддержка нескольких аккаунтов Telegram

## 3. Архитектура

Подход: монолитный пакет с четким разделением на модули. Чаты экспортируются последовательно, медиа-файлы внутри одного чата -- параллельно через asyncio-семафор (до 3 одновременных загрузок).

### 3.1. Структура проекта

```
tg-export/
  pyproject.toml
  tg_export/
    __init__.py
    __main__.py                # python -m tg_export
    cli.py                     # click-группа с подкомандами
    config.py                  # загрузка/валидация YAML-конфига
    auth.py                    # управление аккаунтами и Telethon-сессиями
    catalog.py                 # выгрузка каталога чатов/папок
    api.py                     # обертка Telethon + Takeout
    exporter.py                # основной цикл экспорта (аналог Controller)
    state.py                   # SQLite: состояние, инкрементальность, верификация
    models.py                  # dataclasses: Message, Chat, Media, ServiceAction
    media.py                   # загрузка медиа, семафор, проверка целостности
    converter.py               # конвертация Telethon объектов в models.*
    html/
      renderer.py              # Jinja2 рендеринг всех страниц
      templates/
        base.html.j2           # общий layout
        index.html.j2          # главная страница
        folder_index.html.j2   # список чатов в папке
        chat.html.j2           # страница сообщений чата
        message.html.j2        # один блок сообщения (include)
        media_block.html.j2    # рендеринг медиа вложения (include)
        service_message.html.j2 # системное сообщение (include)
        contacts.html.j2
        sessions.html.j2
        personal_info.html.j2
        userpics.html.j2
        stories.html.j2
        profile_music.html.j2
        other_data.html.j2
      static/
        css/style.css           # из tdesktop
        js/script.js            # из tdesktop
        images/                 # иконки из tdesktop (44 PNG)
  docs/
  tests/
```

### 3.2. Зависимости

```
telethon >= 1.36
pyyaml >= 6.0
aiosqlite >= 0.20
jinja2 >= 3.1
click >= 8.0
rich >= 13.0
```

## 4. CLI-интерфейс

```
tg-export auth add [--name ALIAS]         # интерактивная авторизация, сохранение сессии
tg-export auth list                       # список аккаунтов
tg-export auth remove <name>              # удалить аккаунт

tg-export list [--account NAME]           # выгрузить каталог чатов/папок
  --output catalog.yaml                   # куда записать
  --format yaml|json                      # формат каталога
  --include-left                          # включить покинутые каналы/группы

tg-export init [--from catalog.yaml]      # сгенерировать шаблон конфига из каталога
  --output config.yaml

tg-export run [--config config.yaml]      # запустить экспорт
  --account NAME                          # какой аккаунт
  --output ./export_output                # куда сохранять
  --verify                                # проверить целостность после экспорта
  --dry-run                               # показать что будет скачано, без скачивания

tg-export verify [--config config.yaml]   # проверить целостность скачанных файлов
  --output ./export_output
```

## 5. YAML-конфиг

### 5.1. Структура конфига

```yaml
account: my_phone

output:
  path: ./export_output
  format: html                # html | json | both
  messages_per_file: 1000     # разбивка HTML на файлы
  min_free_space: 20GB        # остановить экспорт при нехватке места

defaults:
  media:
    types: [photo, video, voice, video_note, sticker, gif, document]
    max_file_size: 50MB
    concurrent_downloads: 3   # допустимый диапазон: 1-5
  date_from: null             # формат: YYYY-MM-DD (PyYAML парсит как date)
  date_to: null               # формат: YYYY-MM-DD
  export_service_messages: true

# Глобальные данные (не привязаны к чатам)
personal_info: true
contacts: true                # включает frequent contacts
sessions: true                # включает web sessions
userpics: true
stories: true
profile_music: true
other_data: true

# Покинутые каналы/группы
left_channels:
  action: export_with_defaults  # skip | export_with_defaults
  # при export_with_defaults используются правила из defaults

# Импорт ранее скачанных файлов
import_existing:
  - path: ~/TelegramExport_2024
    type: tdesktop
  - path: ~/old_tg_export
    type: tg-export

# Правила по папкам Telegram
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

# Отдельные чаты вне папок (или переопределение)
chats:
  - name: "Saved Messages"
    media:
      types: all
      max_file_size: 2GB

  - id: 9876543210
    name: "Секретный чат"       # name опционален при указании id
    media:
      types: [photo]
    date_from: 2024-06-01

# Чаты, которые не попали ни в одно правило
unmatched:
  action: skip                  # skip | export_with_defaults | ask
```

Все списки чатов (`folders.*.chats`, `chats`) используют единый формат -- список объектов с обязательным `name` или `id`.

### 5.2. Каталог чатов

Генерируется командой `tg-export list`:

```yaml
account: my_phone
generated: 2026-03-21T12:00:00

folders:
  "Работа":
    - id: 1234567890
      name: "Рабочий чат"
      type: private_supergroup
      messages: 45230
      last_message: 2026-03-20
      members: 12
      is_forum: true              # группа с топиками
      topics_count: 15
    - id: 1234567891
      name: "Канал новостей"
      type: public_channel
      messages: 8900
      last_message: 2026-03-21

unfiled:
  - id: 9876543210
    name: "Иван Иванов"
    type: personal
    messages: 3200
    last_message: 2026-03-19

left:
  - id: 1111111111
    name: "Старый канал"
    type: public_channel
    messages: 500
    last_message: 2025-01-15
```

### 5.3. Приоритет правил

От высшего к низшему:
1. Конкретный чат в секции `chats` (по id или имени)
2. Конкретный чат внутри папки в `folders.*.chats`
3. Правила папки в `folders.*`
4. `defaults`

### 5.4. Чат в нескольких папках

В Telegram один чат может входить в несколько папок одновременно. Правило: чат экспортируется **один раз**, в папку с наивысшим приоритетом совпадения правил:
1. Если чат явно указан в `folders.*.chats` одной из папок -- экспортируется в эту папку
2. Если чат попадает под правила нескольких папок -- используется первая папка по порядку в конфиге
3. В каталоге (`tg-export list`) чат отображается во всех папках, но при экспорте размещается только в одной

## 6. Модель данных

### 6.1. Основные dataclasses

```python
@dataclass
class Chat:
    id: int
    name: str
    type: ChatType
    username: str | None                # для публичных чатов/каналов (ссылки в HTML)
    folder: str | None
    members_count: int | None
    last_message_date: datetime | None
    messages_count: int
    is_left: bool                       # покинутый канал/группа
    is_forum: bool                      # группа с топиками
    migrated_to_id: int | None          # ID супергруппы при миграции
    migrated_from_id: int | None        # ID старой группы
    is_monoforum: bool                  # monoforum (связан с каналом)

@dataclass
class Message:
    id: int
    chat_id: int
    date: datetime
    edited: datetime | None
    from_id: int | None
    from_name: str
    text: list[TextPart]
    media: Media | None
    action: ServiceAction | None
    reply_to_msg_id: int | None
    reply_to_peer_id: int | None
    forwarded_from: ForwardInfo | None
    reactions: list[Reaction]
    is_outgoing: bool
    signature: str | None               # подпись автора в каналах
    via_bot_id: int | None              # сообщение через inline-бота
    saved_from_chat_id: int | None      # для Saved Messages
    inline_buttons: list[list[InlineButton]] | None
    topic_id: int | None                # ID топика в форум-группе
    grouped_id: int | None              # ID медиа-альбома (несколько фото/видео в одном блоке)

@dataclass
class Media:
    """Базовый класс. Конкретные типы наследуют от него."""
    type: MediaType
    file: FileInfo | None

@dataclass
class PhotoMedia(Media):
    width: int
    height: int
    spoilered: bool = False

@dataclass
class DocumentMedia(Media):
    name: str | None
    mime_type: str | None
    duration: int | None                # для видео/аудио
    width: int | None                   # для видео
    height: int | None                  # для видео
    performer: str | None               # для аудио
    song_title: str | None              # для аудио
    sticker_emoji: str | None           # для стикеров
    spoilered: bool = False
    ttl: int | None = None              # самоуничтожающееся медиа

@dataclass
class ContactMedia(Media):
    phone: str
    first_name: str
    last_name: str
    vcard: str | None

@dataclass
class GeoMedia(Media):
    latitude: float
    longitude: float

@dataclass
class VenueMedia(Media):
    latitude: float
    longitude: float
    title: str
    address: str

@dataclass
class PollMedia(Media):
    question: list[TextPart]            # форматированный текст
    answers: list[PollAnswer]
    total_votes: int
    closed: bool

@dataclass
class GameMedia(Media):
    title: str
    description: str
    short_name: str

@dataclass
class InvoiceMedia(Media):
    title: str
    description: str
    currency: str
    amount: int
    receipt_msg_id: int | None

@dataclass
class TodoListMedia(Media):
    title: str
    items: list[TodoItem]
    others_can_append: bool = False
    others_can_complete: bool = False

@dataclass
class GiveawayMedia(Media):
    """Для GiveawayStart и GiveawayResults."""
    is_results: bool

@dataclass
class PaidMedia(Media):
    stars_amount: int

@dataclass
class UnsupportedMedia(Media):
    """Fallback для неизвестных типов медиа."""
    pass

@dataclass
class FileInfo:
    id: int
    size: int
    name: str | None
    mime_type: str | None
    local_path: str | None

@dataclass
class TextPart:
    type: TextType
    text: str
    href: str | None = None             # для TextUrl
    user_id: int | None = None          # для MentionName

@dataclass
class Reaction:
    type: ReactionType                  # emoji, custom_emoji, paid
    emoji: str | None
    document_id: int | None             # для custom_emoji
    count: int
    recent: list[int] | None           # user_id недавних реакторов

@dataclass
class ForwardInfo:
    from_id: int | None
    from_name: str | None
    date: datetime | None
    saved_from_chat_id: int | None
    show_as_original: bool = False

@dataclass
class InlineButton:
    type: InlineButtonType              # см. перечисление ниже
    text: str
    data: str | None                    # URL или callback data

class InlineButtonType(str, Enum):
    default = "default"                         # обычная кнопка клавиатуры
    url = "url"
    callback = "callback"
    callback_with_password = "callback_with_password"
    request_phone = "request_phone"
    request_location = "request_location"
    request_poll = "request_poll"
    request_peer = "request_peer"
    switch_inline = "switch_inline"
    switch_inline_same = "switch_inline_same"
    game = "game"
    buy = "buy"
    auth = "auth"
    web_view = "web_view"
    simple_web_view = "simple_web_view"
    user_profile = "user_profile"
    copy_text = "copy_text"

class ReactionType(str, Enum):
    emoji = "emoji"
    custom_emoji = "custom_emoji"
    paid = "paid"

@dataclass
class PollAnswer:
    text: list[TextPart]                # форматированный текст (как в tdesktop)
    voters: int
    chosen: bool = False

@dataclass
class TodoItem:
    id: int                             # нужен для ActionTodoCompletions
    text: str
    completed: bool = False

@dataclass
class ForumTopic:
    id: int
    title: str
    icon_emoji: str | None
    is_closed: bool
    is_pinned: bool
    messages_count: int

@dataclass
class PersonalInfo:
    first_name: str
    last_name: str | None
    username: str | None
    phone: str
    bio: str | None
    userpic: FileInfo | None

@dataclass
class ContactInfo:
    user_id: int
    first_name: str
    last_name: str | None
    phone: str | None
    username: str | None

@dataclass
class ContactsList:
    contacts: list[ContactInfo]
    frequent: list[ContactInfo]         # frequent contacts (top peers)

@dataclass
class SessionInfo:
    device: str
    platform: str
    system_version: str
    app_name: str
    app_version: str
    date_created: datetime
    date_active: datetime
    ip: str
    country: str

@dataclass
class SessionsList:
    sessions: list[SessionInfo]
    web_sessions: list[SessionInfo]
```

### 6.2. Типы чатов

Соответствие типов с tdesktop `DialogInfo::Type`:

| ChatType | tdesktop | Описание |
|----------|----------|----------|
| `self` | Self | Saved Messages |
| `replies` | Replies | Ответы |
| `verify_codes` | VerifyCodes | Коды верификации |
| `personal` | Personal | Личный чат |
| `bot` | Bot | Чат с ботом |
| `private_group` | PrivateGroup | Приватная группа (старая) |
| `private_supergroup` | PrivateSupergroup | Приватная супергруппа |
| `public_supergroup` | PublicSupergroup | Публичная супергруппа |
| `private_channel` | PrivateChannel | Приватный канал |
| `public_channel` | PublicChannel | Публичный канал |

### 6.3. Типы медиа

| MediaType | Подпапка | Описание |
|-----------|----------|----------|
| `photo` | photos/ | Фотографии |
| `video` | videos/ | Видео |
| `document` | files/ | Документы/файлы |
| `voice` | voice_messages/ | Голосовые сообщения |
| `video_note` | video_messages/ | Видео-кружки |
| `sticker` | stickers/ | Стикеры |
| `gif` | gifs/ | GIF-анимации |
| `contact` | -- | Контакт (без файла) |
| `geo` | -- | Геолокация (без файла) |
| `venue` | -- | Место (без файла) |
| `poll` | -- | Опрос (без файла) |
| `game` | -- | Игра |
| `invoice` | -- | Счет/платеж |
| `todo_list` | -- | Список задач |
| `giveaway` | -- | Розыгрыш |
| `paid_media` | -- | Платный контент |
| `unsupported` | -- | Неизвестный тип (fallback) |

### 6.4. Типы форматирования текста

Полный список TextType, соответствующий tdesktop `TextPart::Type`:

| TextType | tdesktop | Описание |
|----------|----------|----------|
| `text` | Text | Обычный текст |
| `unknown` | Unknown | Неизвестный тип |
| `mention` | Mention | @username |
| `hashtag` | Hashtag | #тег |
| `bot_command` | BotCommand | /command |
| `url` | Url | Ссылка |
| `email` | Email | Email-адрес |
| `bold` | Bold | Жирный |
| `italic` | Italic | Курсив |
| `code` | Code | Инлайн-код |
| `pre` | Pre | Блок кода |
| `text_url` | TextUrl | Текст со ссылкой |
| `mention_name` | MentionName | Упоминание по ID |
| `phone` | Phone | Номер телефона |
| `cashtag` | Cashtag | $TAG |
| `underline` | Underline | Подчеркивание |
| `strikethrough` | Strike | Зачеркивание |
| `blockquote` | Blockquote | Цитата |
| `bank_card` | BankCard | Номер карты |
| `spoiler` | Spoiler | Спойлер |
| `custom_emoji` | CustomEmoji | Кастомный эмодзи |

### 6.5. Типы service-действий

Полный перечень ServiceAction, соответствующий tdesktop. Группировка по категориям:

**Группы/каналы:**
ActionChatCreate, ActionChatEditTitle, ActionChatEditPhoto, ActionChatDeletePhoto,
ActionChatAddUser, ActionChatDeleteUser, ActionChatJoinedByLink, ActionChatJoinedByRequest,
ActionChannelCreate, ActionChatMigrateTo, ActionChannelMigrateFrom

**Сообщения:**
ActionPinMessage, ActionHistoryClear

**Звонки:**
ActionPhoneCall (состояния: Missed, Disconnect, Hangup, Busy)

**Групповые звонки:**
ActionGroupCall, ActionInviteToGroupCall, ActionGroupCallScheduled

**Платежи:**
ActionGameScore, ActionPaymentSent, ActionPaymentRefunded,
ActionPaidMessagesRefunded, ActionPaidMessagesPrice

**Безопасность:**
ActionScreenshotTaken, ActionBotAllowed, ActionSecureValuesSent

**Контакты:**
ActionContactSignUp, ActionPhoneNumberRequest, ActionGeoProximityReached

**Темы/оформление:**
ActionTopicCreate, ActionTopicEdit, ActionSetChatTheme, ActionSetMessagesTTL,
ActionSetChatWallPaper

**Платформа:**
ActionWebViewDataSent, ActionRequestedPeer

**Подарки/премиум:**
ActionGiftPremium, ActionGiftCredits, ActionStarGift, ActionGiftCode

**Розыгрыши:**
ActionGiveawayLaunch, ActionGiveawayResults, ActionPrizeStars

**Предложенные посты:**
ActionSuggestedPostApproval, ActionSuggestedPostSuccess, ActionSuggestedPostRefund

**Прочее:**
ActionCustomAction, ActionSuggestProfilePhoto, ActionBoostApply,
ActionNoForwardsToggle, ActionNoForwardsRequest,
ActionNewCreatorPending, ActionChangeCreator, ActionSuggestBirthday,
ActionTodoCompletions, ActionTodoAppendTasks

Для каждого ServiceAction создается отдельный dataclass с соответствующими полями.

### 6.6. SQLite-схема состояния

SQLite-база состояния хранится в `{output.path}/.tg-export-state.db` — рядом с выходными файлами экспорта, **вне** директории проекта.

```sql
-- Состояние экспорта по чатам
CREATE TABLE export_state (
    chat_id        INTEGER PRIMARY KEY,
    last_msg_id    INTEGER NOT NULL,
    messages_count INTEGER DEFAULT 0,
    updated_at     TIMESTAMP
);

-- Сообщения (хранятся для перегенерации HTML)
CREATE TABLE messages (
    chat_id        INTEGER NOT NULL,
    msg_id         INTEGER NOT NULL,
    data           TEXT NOT NULL,          -- JSON-сериализованный models.Message
    PRIMARY KEY (chat_id, msg_id)
);

-- Реестр скачанных файлов (дедупликация по file_id)
CREATE TABLE files (
    file_id        INTEGER PRIMARY KEY,    -- глобально уникальный Telegram document.id / photo.id
    expected_size  INTEGER NOT NULL,
    actual_size    INTEGER,
    local_path     TEXT NOT NULL,           -- путь в media_store/
    sha256_head    TEXT,                    -- sha256 первых 64 KB (для сопоставления с tdesktop)
    status         TEXT DEFAULT 'done',     -- done | partial | missing
    downloaded_at  TIMESTAMP
);

-- Takeout-сессия
CREATE TABLE takeout (
    account    TEXT PRIMARY KEY,
    takeout_id INTEGER,
    created_at TIMESTAMP
);

-- Кеш пользователей (для HTML-рендеринга)
CREATE TABLE users_cache (
    user_id      INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    username     TEXT,
    updated_at   TIMESTAMP
);

-- Кеш каталога чатов
CREATE TABLE catalog_cache (
    chat_id           INTEGER PRIMARY KEY,
    name              TEXT,
    type              TEXT,
    folder            TEXT,
    members_count     INTEGER,
    messages_count    INTEGER,
    last_message_date TIMESTAMP,
    is_left           INTEGER DEFAULT 0,
    is_forum          INTEGER DEFAULT 0,
    is_monoforum      INTEGER DEFAULT 0,
    updated_at        TIMESTAMP
);

-- Метаданные
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
```

## 7. API-слой (Telethon + Takeout)

### 7.1. Класс TgApi

```python
class TgApi:
    def __init__(self, session_path: Path, api_id: int, api_hash: str):
        self.client = TelegramClient(str(session_path), api_id, api_hash)
        self.takeout: TakeoutClient | None = None

    async def start_takeout(self, **kwargs) -> None
    async def iter_dialogs(self) -> AsyncIterator[Dialog]
    async def get_left_channels(self) -> list[Dialog]  # через raw API: GetLeftChannelsRequest
    async def get_folders(self) -> dict[str, list[int]]
    async def iter_messages(self, chat_id, min_id=0) -> AsyncIterator[Message]
    async def iter_topic_messages(self, chat_id, topic_id, min_id=0) -> AsyncIterator[Message]
    async def get_forum_topics(self, chat_id) -> list[ForumTopic]
    async def download_media(self, message, path, progress_cb=None) -> Path | None
    async def get_personal_info(self) -> PersonalInfo
    async def get_contacts(self) -> ContactsList          # включает frequent contacts
    async def get_sessions(self) -> SessionsList          # включает web sessions
    async def iter_userpics(self) -> AsyncIterator
    async def iter_stories(self) -> AsyncIterator
    async def iter_profile_music(self) -> AsyncIterator
```

### 7.2. Кеширование

Что кеширует Telethon из коробки (не дублируем):

| Что | Где | Описание |
|-----|-----|----------|
| Entity (User, Chat, Channel) | SQLite-сессия | `get_entity()` без запросов к API после первого раза |
| input_peer | SQLite-сессия | Все запросы используют кешированный peer |
| file_reference | Память + auto-refresh | Автоматическое обновление истекших ссылок |
| Flood wait | Встроенный handler | Автоматическое ожидание при FloodWaitError |
| DC-миграции | SQLite-сессия | Прозрачное переключение между дата-центрами |

Что кешируем дополнительно в state.py:

| Что | Зачем |
|-----|-------|
| `last_msg_id` per chat | Инкрементальность |
| Сообщения (JSON в SQLite) | Перегенерация HTML без запросов к API |
| Скачанные файлы + размеры | Не скачивать повторно, верификация |
| Каталог чатов/папок | Быстрый `tg-export list --cached` |
| user_id -> display_name | HTML-рендеринг без обращения к entity cache |

### 7.3. Takeout-сессия

Telegram Takeout Session API (`account.initTakeoutSession`) -- специальный режим для массового экспорта данных. Преимущества: мягкие rate-limit'ы, минимальный риск бана.

Логика работы:
1. При первом запуске -- создание Takeout-сессии, сохранение `takeout_id` в SQLite
2. При повторных запусках -- переиспользование сохраненного `takeout_id` (Telethon хранит его в `session.takeout_id`)
3. При ошибке `TAKEOUT_INVALID` -- создание новой сессии
4. При `TAKEOUT_INIT_DELAY_{seconds}` -- вывод времени ожидания пользователю
5. Сессия не имеет фиксированного лимита по времени. Живет пока не будет явно закрыта через `account.finishTakeoutSession` или инвалидирована сервером
6. Fallback на обычные запросы, если Takeout недоступен

## 8. Загрузка медиа

```python
class MediaDownloader:
    def __init__(self, api: TgApi, state: ExportState, config: ChatConfig):
        self.semaphore = asyncio.Semaphore(config.concurrent_downloads)

    async def download(self, message: Message, chat_dir: Path) -> Path | None:
        # 1. Проверка типа медиа по конфигу чата
        # 2. Проверка размера файла vs max_file_size
        # 3. Проверка в state.files по file_id -- уже скачан и целый?
        #    Если да -- создать симлинк в папке чата и вернуть путь
        # 4. Проверка в import_existing -- есть в старом экспорте?
        # 5. Проверка свободного места на диске vs min_free_space
        # 6. Скачивание через семафор в media_store/
        # 7. Верификация размера (actual == expected)
        # 8. Регистрация в state.files
        # 9. Создание симлинка в папке чата -> media_store/
```

### 8.1. Дедупликация файлов

Одно и то же видео/фото, пересланное в несколько чатов, имеет одинаковый `file_id` на стороне Telegram. Файл скачивается **один раз** в общее хранилище `media_store/`, а в папках чатов создаются симлинки.

**Структура media_store:**

```
media_store/{type}/{hex(file_id)[:2]}/{hex(file_id)}{ext}
```

Шардинг по первым 2 символам hex от `file_id` даёт 256 подкаталогов на тип медиа. При 100 000 файлов одного типа -- ~390 файлов на каталог.

**Связь с папками чатов:**

```
folders/Работа/Рабочий_чат_123/photos/photo_001.jpg
  -> симлинк на ../../../../../../media_store/photos/a3/a3f291e8b0c1d2.jpg
```

Относительные симлинки позволяют перемещать весь каталог экспорта.

**SQLite-таблица `files`:** ключ `PRIMARY KEY (file_id)` -- глобально уникальный, без привязки к чату. Это обеспечивает дедупликацию: перед скачиванием проверяется, есть ли `file_id` в таблице.

### 8.2. Типы медиа и подпапки

| Тип | Подпапка в media_store и в чате | Расширения |
|-----|--------------------------------|------------|
| photo | photos/ | .jpg, .png |
| video | videos/ | .mp4, .mov |
| document | files/ | любые |
| voice | voice_messages/ | .ogg |
| video_note | video_messages/ | .mp4 |
| sticker | stickers/ | .webp, .tgs, .webm |
| gif | gifs/ | .mp4, .gif |

## 9. HTML-генерация

### 9.1. Структура выходных файлов

```
export_output/
  .tg-export-state.db                   # SQLite состояния
  media_store/                           # общее хранилище медиа (дедупликация)
    photos/                              # шардинг: {hex(file_id)[:2]}/{hex(file_id)}{ext}
      a3/
        a3f291e8b0c1d2.jpg
      b7/
        b7e012fa93c4e5.jpg
    videos/
      1c/
        1c8a45de67f890.mp4
    files/
      ...
    voice_messages/
      ...
    video_messages/
      ...
    stickers/
      ...
    gifs/
      ...
  index.html
  css/style.css
  js/script.js
  images/                              # иконки из tdesktop
  personal_information/
    result.html
  userpics/
    userpics.html
    photos/
  stories/
    stories.html
  profile_music/
    profile_music.html
  contacts/
    contacts.html                      # включает frequent contacts
  sessions/
    sessions.html                      # включает web sessions
  other_data/
    other_data.html
  folders/
    Работа/
      index.html                       # список чатов в папке
      Рабочий_чат_1234567890/
        messages.html                  # первая порция
        messages2.html                 # вторая и далее
        photos/                        # симлинки -> media_store/photos/xx/...
        videos/                        # симлинки -> media_store/videos/xx/...
        files/
        voice_messages/
        video_messages/
        stickers/
        gifs/
    Семья/
      ...
  unfiled/
    Иван_Иванов_9876543210/
      ...
  left/                                # покинутые каналы/группы
    Старый_канал_1111111111/
      ...
```

Именование папок чатов: `{sanitized_name}_{chat_id}` -- имя для читаемости, id для уникальности.

### 9.2. Шаблоны Jinja2

```
templates/
  base.html.j2              # общий layout: <head>, CSS/JS, навигация
  index.html.j2              # главная страница
  folder_index.html.j2       # список чатов в папке
  chat.html.j2               # страница сообщений
  message.html.j2            # один блок сообщения (include)
  media_block.html.j2        # медиа вложение (include)
  service_message.html.j2    # системное сообщение (include)
  contacts.html.j2
  sessions.html.j2
  personal_info.html.j2
  userpics.html.j2
  stories.html.j2
  profile_music.html.j2
  other_data.html.j2
```

Класс HtmlRenderer:
- `render_index()` -- главная страница со ссылками на разделы и папки
- `render_folder_index()` -- список чатов в папке
- `render_chat()` -- страница сообщений, разбивка по `messages_per_file`
- `render_message()` -- один блок сообщения: форматированный текст (21 тип), медиа (17 типов), ответы, пересылки, реакции, inline-кнопки, группировка по автору

### 9.3. Совместимость с tdesktop

Эталон разметки -- `tdesktop/Telegram/SourceFiles/export/output/export_output_html.cpp`.

Ключевые CSS-классы, которые нужно воспроизвести:
- `.message` -- контейнер сообщения
- `.body` -- тело сообщения (внутри .message)
- `.from_name` -- имя отправителя
- `.text` -- текст сообщения
- `.media_wrap` -- контейнер медиа
- `.reply_to` -- цитата ответа
- `.forwarded` -- пересланное сообщение
- `.service` -- системное сообщение
- `.joined` -- группированное сообщение (без повторения имени/аватара)

`script.js` из tdesktop работает без изменений -- навигация по `#go_to_messageN`, тосты, переходы между файлами.

Иконки -- те же PNG из `export_html/images/`.

### 9.4. Нумерация файлов сообщений

Схема именования как в tdesktop:
- Первый файл: `messages.html`
- Второй: `messages2.html`
- Третий: `messages3.html`
- и т.д.

Навигация между файлами: "Ранние сообщения" / "Поздние сообщения" внизу каждого файла.

### 9.5. Группировка сообщений и медиа-альбомы

**По автору (визуальная):** как в tdesktop (константа `kJoinWithinSeconds = 900`):
- Последовательные сообщения от одного автора в пределах 15 минут объединяются визуально
- У группированного сообщения не повторяется имя и аватар (CSS-класс `.joined`)
- Группировка сбрасывается при: смене автора, разрыве > 15 минут, service-сообщении, forwarded-сообщении

**Медиа-альбомы:** сообщения с одинаковым `grouped_id` отображаются как один блок:
- Несколько фото/видео рендерятся в сетке внутри одного `.media_wrap`
- Текст берется из последнего сообщения альбома (как в tdesktop)
- В HTML альбом -- один `<div class="message">` с несколькими медиа внутри

## 10. Основной цикл экспорта

### 10.1. Шаги экспорта

1. **Инициализация** -- подключение, Takeout-сессия, загрузка состояния из SQLite, индексация import_existing
2. **Глобальные данные** (если включены в конфиге):
   - personal_info
   - userpics (с загрузкой фото)
   - stories (с загрузкой медиа)
   - profile_music (с загрузкой файлов)
   - contacts (включая frequent contacts)
   - sessions (включая web sessions)
   - other_data
3. **Список чатов** -- получение актуального списка, включая покинутые каналы, сопоставление с конфигом, применение правил
4. **Последовательный экспорт чатов** -- по одному чату за раз
5. **Генерация индексов** -- index.html, folder_index.html
6. **Верификация** (если `--verify`) -- проверка последних файлов, дозагрузка
7. **Финализация** -- сохранение состояния, вывод статистики

### 10.2. Экспорт одного чата

1. Определение `min_id` из SQLite (инкрементальность)
2. Если чат -- форум: получение списка топиков, итерация по каждому топику
3. Итерация сообщений через `api.iter_messages(min_id=min_id)`
4. Параллельная загрузка медиа через asyncio-семафор
5. Конвертация Telethon Message в models.Message (`converter.py`)
6. Сохранение сообщения в SQLite (`messages` таблица)
7. После завершения загрузки -- рендеринг HTML из всех сообщений в SQLite (не только новых)
8. Обновление состояния в SQLite: `last_msg_id`, `files`

### 10.3. Обработка покинутых каналов

Покинутые каналы/группы (`left channels`) -- отдельный поток данных:
- Получаются через `api.get_left_channels()` (в Telethon: `GetLeftChannelsRequest`)
- Включаются в каталог (`tg-export list --include-left`)
- Размещаются в папке `left/` в структуре выходных файлов
- Управляются секцией `left_channels` в конфиге

### 10.4. Обработка топиков (форумных тем)

Для групп с `is_forum = true`:
- Получаем список топиков через `api.get_forum_topics(chat_id)`
- Сообщения привязаны к топикам через `reply_to.reply_to_top_id`
- В HTML: каждый топик -- отдельная секция внутри чата, с подзаголовком
- Service-сообщения: `ActionTopicCreate`, `ActionTopicEdit` отображаются как системные

### 10.5. Обработка миграции групп в супергруппы

Старые группы мигрировали в супергруппы -- история разделена между двумя ID:
- При обнаружении `migrated_to_id`/`migrated_from_id` -- объединяем сообщения в один экспорт
- Сначала экспортируем сообщения из старой группы, затем из новой супергруппы
- В HTML визуально это один непрерывный чат с пометкой о миграции (service-сообщение)

### 10.6. Обработка monoforum

Monoforum -- приватные переписки канала с подписчиками (не путать с discussion-группой для комментариев). Канал имеет привязанный monoforum-чат, где каждый подписчик может вести приватный диалог с администрацией канала.

Связанные поля в tdesktop: `isMonoforum`, `isMonoforumAdmin`, `monoforumLinkId`, `isMonoforumOfPublicBroadcast`.

Обработка при экспорте:
- Monoforum-чаты обнаруживаются через `is_monoforum` флаг в Chat
- Экспортируются как отдельные чаты в папке связанного канала
- В HTML отображаются со специальной пометкой о привязке к каналу

### 10.7. Контроль свободного места

- Параметр `output.min_free_space` в конфиге (по умолчанию 20 GB)
- Проверка перед началом экспорта
- Проверка перед скачиванием каждого медиа-файла
- При срабатывании -- graceful stop: сохранение состояния в SQLite, сообщение пользователю: "Экспорт приостановлен: свободное место на диске менее 20 GB. Освободите место и запустите повторно -- экспорт продолжится с того же места."
- При повторном запуске экспорт продолжится с того же места

### 10.8. Обработка ошибок

| Ошибка | Действие |
|--------|----------|
| `FloodWaitError` | Telethon обрабатывает автоматически (ждет и ретраит) |
| `TAKEOUT_INVALID` | Пересоздание сессии, продолжение с того же места |
| `TAKEOUT_INIT_DELAY_{N}` | Вывод времени ожидания пользователю |
| Сеть/таймаут | Retry с exponential backoff (3 попытки) |
| Ошибка записи файла | Логирование, `status=partial` в state, продолжение |
| Нехватка места на диске | Graceful stop, сохранение состояния |
| Ctrl+C | Graceful shutdown: сохранение state, закрытие сессий |

### 10.9. Прогресс-бар

Rich-вывод в терминал:

```
Экспорт: Работа / Рабочий чат
  Сообщения: ████████████░░░░ 12,450/18,000  69%
  Медиа:     ██████░░░░░░░░░░   234/580     40%  [photo_2024.jpg 2.3MB]

Общий прогресс: 5/23 чатов  ██████░░░░░░░░░░ 22%
Свободное место: 145 GB
```

dry-run режим: проходит все шаги кроме скачивания и рендеринга, выводит ожидаемое количество чатов, сообщений, файлов и объем.

## 11. Инкрементальность

### 11.1. Дозагрузка новых сообщений

При повторном запуске для каждого чата:
1. Из SQLite читаем `last_msg_id`
2. Запрашиваем только сообщения с `id > last_msg_id`
3. Новые сообщения сохраняются в SQLite таблицу `messages`
4. HTML перегенерируется из всех сообщений в SQLite (старых + новых)
5. Обновляем `last_msg_id` в SQLite

### 11.2. Хранение сообщений для перегенерации HTML

Проблема: при инкрементальном экспорте нужно перегенерировать HTML, но старые сообщения не перезагружаются из Telegram.

Решение: все экспортированные сообщения хранятся в таблице `messages` в SQLite как JSON. При перегенерации HTML:
1. Загружаем все сообщения чата из SQLite
2. Десериализуем из JSON в models.Message
3. Рендерим HTML из полного набора сообщений
4. Это позволяет также применять обновленные шаблоны к старым данным

Размер SQLite: ~1 KB на сообщение (без медиа), для чата на 100 000 сообщений -- ~100 MB.

### 11.3. Верификация целостности файлов

При запуске с `--verify` или через `tg-export verify`:
1. Для каждой записи в `files` проверяем: файл существует, `actual_size == expected_size`
2. Файлы с `status = partial` или несовпадением размера -- перезагружаем
3. Файлы с `status = missing` (файл удален с диска) -- перезагружаем
4. При повторном запуске экспорта автоматически проверяются последние N файлов (последняя сессия загрузки)

### 11.4. Импорт из предыдущих экспортов

Секция `import_existing` в конфиге. Два типа источников:

**type: tg-export** -- импорт из предыдущего экспорта tg-export:
- Читает SQLite-состояние предыдущего экспорта
- Сопоставление по `file_id` (сохранён в таблице `files`) -- точное совпадение
- Файл переиспользуется через симлинк в `media_store/`

**type: tdesktop** -- импорт из экспорта официального клиента:
- tdesktop не сохраняет Telegram `file_id` в экспорте
- Имена файлов: `photo_123@2025-03-21_14-30-45.jpg` (123 -- порядковый счётчик, не file_id)
- Сопоставление двухэтапное:
  1. **Быстрый фильтр:** размер файла + дата сообщения (из имени файла `@YYYY-MM-DD_HH-MM-SS`) -> список кандидатов
  2. **Подтверждение:** sha256 первых 64 KB файла-кандидата vs sha256 первых 64 KB файла из Telegram API
- При совпадении -- файл регистрируется в `files` с полученным `file_id`, создаётся симлинк или копия в `media_store/`

### 11.5. Уникальная идентификация файлов в Telegram

| Идентификатор | Описание | Стабильность |
|---|---|---|
| `document.id` / `photo.id` | Глобально уникальный int64 | Постоянный. Одно видео, пересланное в N чатов, имеет одинаковый id |
| `dc_id` + `id` | Полный LocationKey (как в tdesktop) | Постоянный |
| `size` | Размер файла в байтах | Постоянный |

`document.id` / `photo.id` -- главный ключ дедупликации. Используется как `file_id` в таблице `files` и в именах файлов в `media_store/`.

## 12. Авторизация и мульти-аккаунт

Сессии Telethon хранятся в `~/.config/tg-export/sessions/`:

```
~/.config/tg-export/
  sessions/
    my_phone.session        # SQLite-сессия Telethon
    work_account.session
  api_credentials.yaml      # api_id, api_hash
```

Команды:
- `tg-export auth add --name my_phone` -- интерактивная авторизация (телефон, код, 2FA), сохранение сессии
- `tg-export auth list` -- список аккаунтов с статусом (активен/истек)
- `tg-export auth remove <name>` -- удаление сессии

В конфиге и CLI указывается `--account NAME` для выбора аккаунта.

## 13. Безопасность

- `api_credentials.yaml` содержит `api_hash` -- файл создается с правами `600` (чтение/запись только владельцу)
- `.session` файлы Telethon содержат токен авторизации -- также `600`
- При `tg-export auth add` выводится предупреждение о необходимости защиты файлов сессий
- `api_id` и `api_hash` не логируются и не выводятся в терминал
