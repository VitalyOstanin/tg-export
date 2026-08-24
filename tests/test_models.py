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


def test_media_of_an_unknown_class_degrades_instead_of_failing():
    """Запасная ветка давала ровно тот отказ, который должна была предотвращать.

    База, записанная другой версией (класс удалён, переименован или ещё не
    существует здесь), читается при рендере месяца, поэтому `TypeError` из
    `cls(**d)` терял не одно сообщение, а страницу чата.
    """
    from tg_export.models import MediaType, UnsupportedMedia, media_from_dict

    media = media_from_dict(
        {
            "__media_class__": "FutureMedia",
            "type": "photo",
            "file": None,
            "width": 1,
            "height": 2,
            "spoilered": False,
        }
    )

    assert isinstance(media, UnsupportedMedia)
    assert media.type is MediaType.photo


def test_a_service_action_of_an_unknown_class_degrades_instead_of_failing():
    """То же для системных сообщений: базовый класс не принимает полей производных."""
    from tg_export.models import ServiceAction, action_from_dict

    action = action_from_dict(
        {"__action_class__": "ActionUnknownFuture", "type": "ActionUnknownFuture", "title": "x"}
    )

    assert isinstance(action, ServiceAction)
    assert action.type == "ActionUnknownFuture"


def test_rebuilding_a_record_leaves_it_as_it_was():
    """Сборка объекта из записи не должна её портить.

    Одна из копий этого разбора забирала поле `type` из переданного словаря
    методом `pop`: запись, прочитанная во второй раз, приходила уже без типа,
    и собрать её было нельзя. Разбор читает запись, а не потребляет её.
    """
    from tg_export.models import TextPart, TextType, with_enum_type

    record = {"type": "bold", "text": "x"}

    part = with_enum_type(TextPart, record, TextType)

    assert part == TextPart(type=TextType.bold, text="x")
    assert record == {"type": "bold", "text": "x"}
