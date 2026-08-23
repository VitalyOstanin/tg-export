from tg_export.models import (
    ChatType,
)


def test_chat_type_enum():
    assert ChatType.self == "self"
    assert ChatType.private_supergroup == "private_supergroup"


def test_media_without_a_class_marker_loads_by_its_type():
    """Ветка без `__media_class__` -- страховка на запись, правленную руками.

    Каждая запись этого проекта несёт маркер класса, поэтому без теста ветка
    неотличима от мёртвого кода: удалившему её ничто не возразит.
    """
    from tg_export.models import PhotoMedia, media_from_dict

    media = media_from_dict({"type": "photo", "file": None, "width": 640, "height": 480})

    assert isinstance(media, PhotoMedia)
