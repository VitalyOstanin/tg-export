# TODO

- [x] Описание всех настроек: docs/configuration.md

- [x] Дедупликация файлов внутри одного аккаунта (hardlink/copy через get_file_any_chat)

- [ ] Глобальные страницы и дополнительные шаблоны
  - Шаг 1: folder_index.html.j2 -- список чатов в папке (аналог index.html, но для одной папки)
    - Данные: список Chat из catalog_cache, отфильтрованный по folder
    - Ссылки на messages_YYYY-MM.html каждого чата
  - Шаг 2: personal_info.html.j2 -- API: GetFullUserRequest(InputUserSelf)
    - Поля: имя, фамилия, username, телефон, био, аватарка
  - Шаг 3: contacts.html.j2 -- API: GetContactsRequest + GetTopPeersRequest
    - Две секции: contacts и frequent contacts
  - Шаг 4: sessions.html.j2 -- API: GetAuthorizationsRequest + GetWebAuthorizationsRequest
    - Два списка: app sessions и web sessions
  - Шаг 5: userpics.html.j2 -- API: GetUserPhotosRequest
    - Скачать аватарки, показать галерею
  - Шаг 6: stories.html.j2 -- API: stories.GetPinnedStoriesRequest + GetStoriesArchiveRequest
  - Шаг 7: profile_music.html.j2 -- API: через Takeout (GetSavedRingtones)
  - Шаг 8: other_data.html.j2 -- заглушка или данные из Takeout
  - Шаг 9: вынести message/media/service логику из renderer.py в шаблоны (рефакторинг)
    - message.html.j2 -- один блок сообщения (include)
    - media_block.html.j2 -- рендеринг медиа вложения (include)
    - service_message.html.j2 -- системное сообщение (include)
  - Шаг 10: добавить вызов export_global_data() в exporter.run()

- [x] verify: перезагрузка проблемных файлов (exporter._verify_files + CLI verify)

- [x] convert_entities(): пропуск перекрывающихся entities (как в tdesktop)

- [x] messages_count: использует top_message ID вместо unread_count

- [ ] CLI группа `tg` — прямые запросы к Telegram API
  - все команды поддерживают `--account` или используют default аккаунт
  - `tg messages <chat_id>` — последние сообщения чата
  - `tg send <chat_id> <text>` — отправка сообщения
  - `tg upload <chat_id> <file>` — отправка файла
  - `tg download <chat_id> <msg_id>` — скачивание файла из сообщения
