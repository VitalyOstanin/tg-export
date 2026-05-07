# TODO

- [x] Описание всех настроек: docs/configuration.md

- [x] Дедупликация файлов внутри одного аккаунта (hardlink/copy через get_file_any_chat)

- [x] Глобальные страницы и дополнительные шаблоны
  - [x] Шаг 1: folder_index.html.j2 -- список чатов в папке
  - [x] Шаг 2: personal_info.html.j2 -- API: GetFullUserRequest(InputUserSelf)
  - [x] Шаг 3: contacts.html.j2 -- API: GetContactsRequest + GetTopPeersRequest
  - [x] Шаг 4: sessions.html.j2 -- API: GetAuthorizationsRequest + GetWebAuthorizationsRequest
  - [x] Шаг 5: userpics.html.j2 -- API: GetUserPhotosRequest
  - [x] Шаг 6: stories.html.j2 -- API: stories.GetPinnedStoriesRequest + GetStoriesArchiveRequest
  - [x] Шаг 7: рингтоны -- API: account.GetSavedRingtonesRequest (вместо profile_music)
  - [x] Шаг 8: other_data.html.j2 -- рингтоны и прочие данные
  - [x] Шаг 9: вынести message/media/service логику из renderer.py в шаблоны (рефакторинг)
    - message.html.j2 -- макросы render_message_block, render_album_block, render_reply и др.
    - media_block.html.j2 -- макрос render_media
    - service_message.html.j2 -- макрос render_service
  - [x] Шаг 10: добавить вызов export_global_data() в exporter.run()

- [x] verify: перезагрузка проблемных файлов (exporter._verify_files + CLI verify)

- [x] convert_entities(): пропуск перекрывающихся entities (как в tdesktop)

- [x] messages_count: использует top_message ID вместо unread_count

- [x] CLI группа `tg-export tg` — прямые запросы к Telegram API
  - [x] `tg-export tg messages <chat_id>` — последние сообщения чата
  - [x] `tg-export tg info <chat_ids>` — информация о чатах
  - [x] `tg-export tg send <recipients> --text --file` — отправка текста и файлов нескольким получателям
  - [x] `tg-export tg download <chat_id> <msg_id>` — скачивание текста и всех файлов сообщения (включая альбомы)

- [ ] Telethon SQLiteSession: правильный фикс симметрично сломанных read/write `tmp_auth_key`/`takeout_id`

  Контекст бага. В `telethon/sessions/sqlite.py` (commit `5a3a94eb`, версии 1.43+) read и write используют разные порядки колонок:

  - `_update_session_table` пишет `INSERT INTO sessions VALUES (?,?,?,?,?,?)` с порядком `(dc_id, server_address, port, auth_key, takeout_id, tmp_auth_key)` — соответствует фактической схеме после `ALTER TABLE add column tmp_auth_key blob`.
  - `__init__` читает `select * from sessions` и распаковывает как `dc_id, server_address, port, key, tmp_key, takeout_id` — то есть 5-й столбец трактуется как `tmp_key`, 6-й как `takeout_id`.

  Пока обе колонки `NULL`, `AuthKey(data=None)` срабатывает по early-return (`if not value`) и асимметрия не видна. После первой успешной Takeout-сессии в БД появляется `takeout_id` (int) → при следующем старте он попадает в `tmp_key` → `AuthKey(data=int)` → `sha1(int)` → `TypeError: object supporting the buffer API required`. Перестановка колонок не помогает: write/read симметричны, любая раскладка ломает один из путей.

  Текущий workaround в `tg_export/api._sanitize_session_file` (v1.2.4): перед каждым `TgApi.__init__` обнуляем `tmp_auth_key` и `takeout_id`. `auth_key` сохраняется (нет перелогина), наш `start_takeout` всё равно создаёт свежий takeout каждый запуск. Минусы: Telegram учитывает свежие Takeout-запросы и может выставлять cooldown (TAKEOUT_INIT_DELAY) при частых запусках; пользователь в каждой сессии должен подтверждать takeout заново через клиент.

  Правильное решение -- monkey-patch `telethon.sessions.sqlite.SQLiteSession`:

  1. Подменить read-логику в `__init__` так, чтобы 5-я колонка (`takeout_id` по факту) распаковывалась в `self._takeout_id`, а 6-я (`tmp_auth_key` по факту) -- в `tmp_key`. Симметрично исправить `_update_session_table` (он сейчас пишет в "правильном" для физической схемы порядке -- его трогать не надо).
  2. Применять патч при импорте `tg_export.api` через `importlib`/класс-перехват, до создания `TelegramClient`. Вариант: подменить методы `SQLiteSession.__init__` и `SQLiteSession._update_session_table` на наши обёртки, которые читают/пишут tuple в согласованном порядке.
  3. Альтернатива monkey-patch -- собственный `Session`-класс, наследующий от `MemorySession`, с самостоятельной SQLite-сериализацией (избавляемся от багованного апстрима). Это более громоздко (придётся повторить миграции и работу с entities/sent_files), но не зависит от внутренних имён Telethon.

  Когда апстрим починят (issue + PR в LonamiWebs/Telethon), workaround можно убрать. До этого момента нужно:

  - убрать обнуление `takeout_id` (только обнулять, если оно реально невалидно из-за бага),
  - корректно сохранять `takeout_id` между запусками (избавляемся от ненужных Takeout-запросов и cooldown'ов),
  - покрыть тестами фактический read/write через настоящий `SQLiteSession` (наш текущий `test_sanitize_*` тестирует только sqlite-уровень).

  Ссылки:

  - Telethon `sqlite.py`: <https://github.com/LonamiWebs/Telethon/blob/master/telethon/sessions/sqlite.py>
  - Commit, добавивший асимметрию: <https://github.com/LonamiWebs/Telethon/commit/5a3a94eb>
