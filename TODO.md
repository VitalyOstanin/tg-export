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

- [x] Telethon SQLiteSession: правильный фикс симметрично сломанных read/write `tmp_auth_key`/`takeout_id`

  Реализовано в [tg_export/session.py](tg_export/session.py) -- subclass `FixedSQLiteSession`. Перед `super().__init__()` читаем `takeout_id` и `tmp_auth_key` явно по именам колонок, обнуляем их в БД (чтобы баггованный `__init__` не крашился на `AuthKey(data=int)`), вызываем `super().__init__()`, восстанавливаем значения через сеттеры (write-путь корректен).

  Регрессионный тест (`tests/test_api.py::test_fixed_sqlite_session_restores_takeout_id_and_survives_open`) явно сравнивает оба варианта: ванильный `SQLiteSession` крашится, наш -- открывает БД и сохраняет `takeout_id`.

- [ ] Открыть issue/PR в апстриме Telethon (codeberg.org/Lonami/Telethon)

  Минимальный repro подготовлен. Когда апстрим починят, можно удалить `tg_export/session.py` и пересмотреть `start_takeout` (там сейчас pre-clear стейл `takeout_id`, который нужен из-за `TakeoutClient.__aenter__` ValueError; это отдельный bug).

  Ссылки:

  - Telethon `sqlite.py` (codeberg, активный fork): <https://codeberg.org/Lonami/Telethon/src/branch/v1/telethon/sessions/sqlite.py>
  - Коммит, добавивший асимметрию (PR #4618 PFS): <https://github.com/LonamiWebs/Telethon/commit/5a3a94eb>

- [x] `tg` CLI: принимать не только числовой chat_id, но и @username, ссылку t.me и invite-ссылку

  Реализовано в [tg_export/cli/chatref.py](tg_export/cli/chatref.py): общий тип
  аргумента Click `CHAT_REF` принимает `123456789`, `-1001234567890`, `@name`,
  `name`, `https://t.me/name`, `https://t.me/name/1234`, `t.me/name` и
  `https://t.me/c/<internal>/<msg>`; ссылка-приглашение отвергается с подсказкой
  вступить в чат. Тип подключён к `messages`, `info`, `download` и `send`.
  Разрешение сущности идёт через `resolve_chat`, который различает «такого имени
  нет» и «чат закрыт для этого аккаунта». Тесты -- `tests/test_chatref.py`.

- [ ] `tg` CLI: команда поиска каналов и групп по строке

  `contacts.SearchRequest(q=..., limit=...)` возвращает каналы и группы вместе с
  `participants_count` и username. Обёртка вида `tg-export tg search <строка>`
  закрывала бы сценарий «найти тематические чаты и посмотреть, что в них», для
  которого сейчас приходится писать скрипт.
