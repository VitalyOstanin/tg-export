# TODO

- [ ] README.md: подробное описание всех настроек
  - глобальный конфиг (`~/.config/tg-export/config.yaml`): proxy, min_free_space
  - per-account конфиг (`~/.config/tg-export/<alias>.yaml`): output, defaults, type_rules, folders, chats
  - единицы измерения: min_free_space поддерживает B, KB, MB, GB, TB (не только GB)
  - примеры конфигов

- [ ] CLI группа `tg` — прямые запросы к Telegram API
  - все команды поддерживают `--account` или используют default аккаунт
  - `tg messages <chat_id>` — последние сообщения чата
  - `tg send <chat_id> <text>` — отправка сообщения
  - `tg upload <chat_id> <file>` — отправка файла
  - `tg download <chat_id> <msg_id>` — скачивание файла из сообщения
