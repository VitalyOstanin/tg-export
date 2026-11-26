# T02/T08: карта RPC и граница sender

Дата: 2026-11-26. Исходный проект: `8d81bc6cf95be4f04f5daa44cb38a92ead32f61b`;
исследуемая библиотека: Telethon 1.45.0 из `uv.lock`. Это аудит исполняемых
путей, а не проверка ответов серверов Telegram. Тесты используют настоящий код
Telethon и подставные sender, без аккаунта и сети.

## Экспортные пути исходного проекта

`ordinary` означает `TgApi.client`; `proxy` — `TgApi._active_client`, который
становится Takeout-прокси при открытой сессии. Высокоуровневые методы Telethon
могут предварительно разрешать peer через дополнительные RPC. Поэтому таблица
называет основные запросы и указывает вспомогательные случаи отдельно.

| Данные / вход | Код проекта | Основной RPC Telethon 1.45.0 и путь | Сейчас | Условие для strict |
| --- | --- | --- | --- | --- |
| Каталог: обычные и архивные чаты | `catalog.fetch_catalog` → `api.iter_dialogs` | `messages.getDialogs`, два прохода folder 0/1 | ordinary | Диапазоны Takeout, обёртка каждого экспортного запроса, покрытие scope |
| Папки | `api.get_folders` | `messages.getDialogFilters` | ordinary | Классифицировать как данные архива и проверить допуск в Takeout |
| Контакты для правил папок | `catalog.fetch_contact_ids` → `api.get_contacts` | `contacts.getContacts` | ordinary | При использовании в выборе чатов разрешить маршрут или явно отделить служебный lookup |
| Покинутые каналы | `catalog.fetch_catalog(include_left)` → `api.get_left_channels` | `channels.getLeftChannels`, постранично | ordinary | Маршрут Takeout и own-only/полнота отдельно |
| История чата: новые и старые | `exporter._walk_messages` → `api.iter_messages` | `messages.getHistory` через `MessageMethods._MessagesIter`; две фазы `min_id`/`offset_id` | proxy | `InvokeWithMessagesRange` внутри Takeout, курсоры по диапазонам |
| Счётчик сообщений для прогресса | `exporter._count_chat_messages` | `get_messages(limit=0)` → `messages.getHistory` | ordinary | Перевести в проверенный маршрут либо исключить RPC-счётчик |
| Медиа сообщения | `media.MediaDownloader` → `api.download_media` | Telethon `download_media` → `download_file` → `upload.getFile` через `_client._call(sender, request)` | proxy перед загрузчиком; **сырой RPC** на sender | Локальный файловый адаптер, точный variant, один Takeout wrapper |
| Повтор повреждённого файла | `verify.fetch_broken_messages`, `redownload_broken_file` | `get_messages(ids)` → `messages.getMessages` или `channels.getMessages`; затем `upload.getFile` | lookup ordinary; загрузка как выше | Восстанавливать ровно разрешённый variant и маршрутизировать оба RPC |
| Личная информация | `exporter._export_personal_info` → `api.get_personal_info` | `users.getFullUser`; фото: `download_profile_photo` → `upload.getFile` | ordinary | Вне MVP; отдельная политика и транспорт |
| Контакты и частые собеседники | `exporter._export_contacts` → `api.get_contacts/get_top_peers` | `contacts.getContacts`, `contacts.getTopPeers` | ordinary | Вне MVP до проверки допуска и маршрута |
| Сеансы | `exporter._export_sessions` → `api.get_sessions` | `account.getAuthorizations`, `account.getWebAuthorizations` | ordinary | Вне MVP; проверить, допускает ли Takeout эти методы |
| История аватаров | `exporter._export_userpics` → `api.iter_userpics`; `_download_batch` | `photos.getUserPhotos`; затем `upload.getFile` | ordinary | Вне MVP, variant policy и транспорт |
| Stories | `exporter._export_stories` → `api.get_stories`; `_download_batch` | `stories.getPinnedStories`, `stories.getStoriesArchive`; затем `upload.getFile` | ordinary | Вне MVP, пагинация и variant policy |
| Музыка профиля | `exporter._export_other_data` → `api.get_ringtones`; `_download_batch` | `account.getSavedRingtones`; затем `upload.getFile` | ordinary | Вне MVP, variant policy и транспорт |

`_download_batch` в `exporter.py` вызывает `self.api.client.download_media`
напрямую. Вызов `api.download_media` в `media.py` не получает `thumb`, поэтому
исходный путь выбирает у Telethon его вариант по умолчанию, включая оригинал.
`verify.py` повторяет этот же выбор. Embedded `PhotoCachedSize`/
`PhotoStrippedSize` Telethon может записать без `upload.getFile`; отсутствие
файлового RPC следует проверять отдельно для каждого такого варианта.

## Смежные операции и граница scope

| Операция | Путь | Роль для strict |
| --- | --- | --- |
| Открытие/проверка/закрытие Takeout | `api.start_takeout`, `api._resume_takeout`, `api.stop_takeout` → `account.initTakeoutSession`, `updates.getState`, `account.finishTakeoutSession` | Управление сессией; проба `updates.getState` идёт через proxy, но не является данными экспорта |
| Разрешение peer, cache miss | Telethon `get_input_entity`, `get_entity`, `get_me` и внутренний `TLRequest.resolve` | Возможны `contacts.resolveUsername`, `users.getUsers`, `channels.getChannels` и др.; отделить от экспортных данных и журналировать фактические методы |
| Telethon `FILE_MIGRATE` | `client/downloads.py:_DirectDownloadIter._request` | Повтор `upload.getFile` на borrowed Media DC без Takeout wrapper |
| Устаревший `file_reference` | тот же `_request` → `client.get_messages(ids)` | Только для оригинала документа (`thumb_size == ''`) в текущем Telethon; lookup идёт через ordinary client, затем повтор `upload.getFile` |
| CDN redirect | `_DirectDownloadIter` → `upload.getCdnFile`, при необходимости `upload.reuploadCdnFile` | Отдельный транспорт, шифрование и хеши; strict не объявлять поддержанным без тестов |
| Команды `tg` (`show`, `messages`, `download`, `send`, `edit`, `delete`, `archive`, schedule) | `tg_export/cli/tg.py`, `api.client` | Самостоятельные пользовательские операции вне архивного strict scope; запись ими в strict архив потребует отдельного контракта |
| `watch` и добор пропусков | `tg_export/watch.py`, `api.client.iter_messages`, `api.iter_dialogs/peer_dialogs` | Независимый режим, не считать доказательством Takeout архива |
| Локальный import/verify без повторной загрузки, HTML | `importer.py`, `verify.py`, `html/` | Не отправляют файловый RPC; повторная загрузка в verify описана выше |

## Воспроизводимый sender-контракт

`tests/test_takeout_sender_contract.py` перехватывает фактический аргумент
`sender.send` внутри Telethon. Контрольный `takeout(GetStateRequest())`
отправляет `InvokeWithTakeoutRequest(takeout_id=123, query=GetStateRequest)`.
Вызов `takeout.download_file` отправляет **голый** `GetFileRequest` на основном
sender и на borrowed sender Media DC. При `FILE_MIGRATE` Telethon повторяет
тот же голый запрос на Media DC. Проверен и высокоуровневый
`takeout.download_media(document, thumb="m")`: он сначала отправляет запрос на
основной DC даже при `document.dc_id=2`, поскольку `_download_document` не
передаёт этот ID в `_download_file`; переход происходит по `FILE_MIGRATE`.
Все эти варианты проверены без сети.

Причина: `_TakeoutClient.__call__` создаёт обёртку, но
`_DirectDownloadIter._request` вызывает `_client._call(sender, request)`.
У proxy `_call` делегируется методу обычного клиента и не проходит через
`__call__`. Следовательно, передача Takeout-прокси в `download_media` сама по
себе не обеспечивает строгий файловый Takeout.

**Решение для T07/T08:** нужен локальный адаптер на границе файлового RPC,
который передаёт выбранный `GetFileRequest` на нужный sender ровно один раз
внутри `InvokeWithTakeoutRequest`. Не менять глобально `TelegramClient._call`:
это затронет служебные и peer lookup запросы. Адаптер должен покрыть основной
и Media DC, `FILE_MIGRATE`, повторы, reference refresh и явный отказ на
неподдержанном CDN. До такого адаптера `strict_takeout` нельзя включать.

Остались проверки с живым тестовым аккаунтом: принимает ли сервер Takeout
`upload.getFile` для выбранных variant и Media DC, поведение большого
родительского файла при `file_max_size`, обновление reference, CDN и полнота
истории по split ranges. Эти условия не выводятся из офлайн sender-теста.

## Legacy schema для T05

`tg_export/state.py:SCHEMA_SQL` имеет `SCHEMA_VERSION = 1`: таблица
`messages` с ключом `(chat_id,msg_id)` содержит нормализованные поля и JSON
медиа, `files` с ключом `(file_id,chat_id)` хранит один файл без ключа
варианта, а `export_state` с ключом `chat_id` хранит `last_msg_id`,
`oldest_msg_id` и `full_history`. Поэтому миграция не может пометить все
строки `files` как оригиналы или вывести полноценный Takeout из
`full_history`. Нужны отдельные variant identity и статус покрытия истории.
