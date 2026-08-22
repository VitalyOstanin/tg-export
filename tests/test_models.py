from tg_export.models import (
    ChatType,
)


def test_chat_type_enum():
    assert ChatType.self == "self"
    assert ChatType.private_supergroup == "private_supergroup"
