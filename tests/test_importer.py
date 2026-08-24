"""Разбор страниц чата в выгрузке Telegram Desktop.

Один чат разложен на `messages.html`, `messages2.html`, `messages3.html` и так
далее. Индекс медиа собирается по всем страницам сразу, поэтому важно, какие
файлы в него попадают и в каком порядке.
"""

from pathlib import Path

from tg_export.importer import _parse_chat_media


def _page(chat_dir: Path, name: str, msg_id: int, filename: str) -> None:
    """Записать страницу выгрузки с одним сообщением и ссылкой на файл."""
    chat_dir.mkdir(parents=True, exist_ok=True)
    (chat_dir / name).write_text(
        f'<div id="message{msg_id}">\n<a href="../../chats/chat1/photos/{filename}">photo</a>\n</div>\n',
        encoding="utf-8",
    )
    photo = chat_dir / "photos" / filename
    photo.parent.mkdir(parents=True, exist_ok=True)
    photo.write_bytes(b"jpeg")


def test_a_gap_in_the_page_numbers_does_not_cut_off_the_rest(tmp_path):
    """Пропуск страницы не должен обрывать разбор чата.

    Страницы перебирались по номерам от второй, и первый отсутствующий номер
    останавливал обход: чат, у которого страницы удалены или перенумерованы,
    терял всё, что лежит за пропуском, — молча, потому что о недостающих
    сообщениях сказать некому.
    """
    chat_dir = tmp_path / "chats" / "chat1"
    _page(chat_dir, "messages.html", 1, "a.jpg")
    _page(chat_dir, "messages3.html", 3, "c.jpg")

    index = _parse_chat_media(chat_dir)

    assert sorted(index) == [1, 3]


def test_pages_are_read_in_their_own_order_not_the_alphabetical_one(tmp_path):
    """Десятая страница идёт после девятой, а не после первой.

    Имена страниц сортируются как строки, и `messages10.html` встаёт перед
    `messages2.html`. Порядок задаёт номер страницы: у сообщения бывает
    несколько файлов, и первым в списке должен стоять тот, что раньше в чате.
    """
    chat_dir = tmp_path / "chats" / "chat1"
    _page(chat_dir, "messages.html", 1, "a.jpg")
    _page(chat_dir, "messages2.html", 7, "b.jpg")
    _page(chat_dir, "messages10.html", 7, "j.jpg")

    index = _parse_chat_media(chat_dir)

    assert [p.name for p in index[7]] == ["b.jpg", "j.jpg"]


def test_a_file_that_is_not_a_page_is_left_alone(tmp_path):
    """В каталоге чата лежат и другие файлы с таким же началом имени.

    Выгрузка кладёт рядом `messages_list.html` и подобные; принять их за
    страницу — значит разобрать чужой файл и приписать чату медиа, которых в
    нём нет.
    """
    chat_dir = tmp_path / "chats" / "chat1"
    _page(chat_dir, "messages.html", 1, "a.jpg")
    _page(chat_dir, "messages_backup.html", 42, "x.jpg")

    index = _parse_chat_media(chat_dir)

    assert sorted(index) == [1]
