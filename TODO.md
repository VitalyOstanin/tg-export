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
