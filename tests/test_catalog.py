from datetime import datetime
from types import SimpleNamespace

import pytest

from tg_export.catalog import format_catalog_yaml, generate_config_template
from tg_export.models import Chat, ChatType


def test_format_catalog_yaml():
    chats = [
        Chat(
            id=123,
            name="Рабочий чат",
            type=ChatType.private_supergroup,
            username=None,
            folder="Работа",
            members_count=12,
            last_message_date=datetime(2026, 3, 20),
            messages_count=45230,
            is_left=False,
            is_archived=False,
            is_forum=True,
            migrated_to_id=None,
            migrated_from_id=None,
            is_monoforum=False,
        ),
        Chat(
            id=456,
            name="Иван",
            type=ChatType.personal,
            username="ivan",
            folder=None,
            members_count=None,
            last_message_date=datetime(2026, 3, 19),
            messages_count=3200,
            is_left=False,
            is_archived=False,
            is_forum=False,
            migrated_to_id=None,
            migrated_from_id=None,
            is_monoforum=False,
        ),
    ]
    yaml_str = format_catalog_yaml(chats)
    assert "Рабочий чат" in yaml_str
    assert "private_supergroup" in yaml_str
    assert "is_forum: true" in yaml_str
    assert "Работа" in yaml_str  # folder
    assert "unfiled" in yaml_str  # Иван не в папке


def test_generate_config_template():
    chats = [
        Chat(
            id=123,
            name="Test",
            type=ChatType.personal,
            username=None,
            folder=None,
            members_count=None,
            last_message_date=None,
            messages_count=100,
            is_left=False,
            is_archived=False,
            is_forum=False,
            migrated_to_id=None,
            migrated_from_id=None,
            is_monoforum=False,
        ),
    ]
    yaml_str = generate_config_template(chats)
    assert "defaults:" in yaml_str


def test_format_catalog_includes_archived():
    chats = [
        Chat(
            id=888,
            name="Archived Chat",
            type=ChatType.personal,
            username=None,
            folder=None,
            members_count=None,
            last_message_date=None,
            messages_count=100,
            is_left=False,
            is_archived=True,
            is_forum=False,
            migrated_to_id=None,
            migrated_from_id=None,
            is_monoforum=False,
        ),
    ]
    yaml_str = format_catalog_yaml(chats)
    assert "archived:" in yaml_str
    assert "Archived Chat" in yaml_str
    assert "is_archived: true" in yaml_str


def test_format_catalog_includes_left():
    chats = [
        Chat(
            id=999,
            name="Old Channel",
            type=ChatType.public_channel,
            username="old",
            folder=None,
            members_count=None,
            last_message_date=None,
            messages_count=500,
            is_left=True,
            is_archived=False,
            is_forum=False,
            migrated_to_id=None,
            migrated_from_id=None,
            is_monoforum=False,
        ),
    ]
    yaml_str = format_catalog_yaml(chats)
    assert "left:" in yaml_str
    assert "Old Channel" in yaml_str


def _chat(**over) -> Chat:
    """Собрать Chat с уже заполненными несущественными для проверки полями."""
    fields = {
        "id": 1,
        "name": "Chat",
        "type": ChatType.personal,
        "username": None,
        "folder": None,
        "members_count": None,
        "last_message_date": None,
        "messages_count": 0,
        "is_left": False,
        "is_archived": False,
        "is_forum": False,
        "migrated_to_id": None,
        "migrated_from_id": None,
        "is_monoforum": False,
    }
    fields.update(over)
    return Chat(**fields)  # pyright: ignore[reportArgumentType]


def test_catalog_survives_a_round_trip_through_yaml():
    """`init --from` читает написанное `tg chats` обратно -- вместе с признаками и папками."""
    import yaml

    from tg_export.catalog import chats_from_catalog

    chats = [
        _chat(
            id=10,
            name="Team",
            type=ChatType.private_supergroup,
            folder="Work",
            messages_count=5,
            is_forum=True,
        ),
        _chat(id=20, name="Notes", type=ChatType.self, messages_count=1),
        _chat(id=30, name="Old", type=ChatType.public_channel, username="old", is_archived=True),
        _chat(id=40, name="Gone", type=ChatType.private_channel, is_left=True),
    ]

    restored = chats_from_catalog(yaml.safe_load(format_catalog_yaml(chats)))

    assert [(c.id, c.name, c.type) for c in restored] == [(c.id, c.name, c.type) for c in chats]
    by_id = {c.id: c for c in restored}
    assert by_id[10].folder == "Work"
    assert by_id[10].is_forum is True
    assert by_id[30].is_archived is True
    assert by_id[30].username == "old"
    assert by_id[40].is_left is True


def test_a_damaged_catalog_is_reported_instead_of_crashing():
    """Любая испорченная форма должна выходить как ConfigError, а не TypeError/KeyError."""
    from tg_export.catalog import chats_from_catalog
    from tg_export.config import ConfigError

    for data in (
        "not a catalog",
        {"unfiled": "not a list"},
        {"unfiled": ["not a mapping"]},
        {"unfiled": [{"name": "no id"}]},
        {"unfiled": [{"id": 1}]},
        {"unfiled": [{"id": 1, "name": "X", "type": "nonesuch"}]},
        {"folders": "not a mapping"},
        {"folders": {"Work": "not a list"}},
    ):
        with pytest.raises(ConfigError):
            chats_from_catalog(data)


def test_the_json_catalog_is_read_as_a_flat_list():
    """`list --format json` пишет список, а не разделы, и `init --from` принимает его тоже."""
    import json

    from tg_export.catalog import chats_from_catalog, format_catalog_json

    chats = [_chat(id=10, name="Team", type=ChatType.private_supergroup, folder="Work", is_archived=True)]

    restored = chats_from_catalog(json.loads(format_catalog_json(chats)))

    assert [(c.id, c.name, c.folder, c.is_archived) for c in restored] == [(10, "Team", "Work", True)]


class Channel:
    """Сущность супергруппы. Имя класса читает `_classify_chat`, отсюда и оно."""

    def __init__(self, chat_id: int, title: str, *, megagroup: bool = False):
        self.id = chat_id
        self.title = title
        self.megagroup = megagroup
        self.username = None


class _CatalogApi:
    """Аккаунт с одним фильтром и заданными списками диалогов."""

    def __init__(self, folders, main, archived):
        self._folders = folders
        self._main = main
        self._archived = archived

    async def get_folders(self):
        return self._folders

    async def iter_dialogs(self, archived: bool = False):
        for entity in self._archived if archived else self._main:
            yield SimpleNamespace(entity=entity, date=None, dialog=None)


@pytest.mark.asyncio
async def test_a_flag_folder_takes_in_an_archived_chat():
    """Папка по флагу `groups` должна забирать и архивную супергруппу.

    Папка чата участвует не только в раскладке каталогов, но и в подборе
    правила `folders.<имя>`, поэтому архивный чат при `archived.action:
    export_with_defaults` иначе выгружается по другим настройкам, чем такой
    же неархивный.
    """
    from tg_export.catalog import fetch_catalog

    api = _CatalogApi(
        folders=[
            {
                "name": "Группы",
                "peer_ids": [],
                "exclude_ids": [],
                "contacts": False,
                "non_contacts": False,
                "groups": True,
                "broadcasts": False,
                "bots": False,
            }
        ],
        main=[],
        archived=[Channel(10, "Команда", megagroup=True)],
    )

    chats = await fetch_catalog(api)

    assert [(c.id, c.folder, c.is_archived) for c in chats] == [(10, "Группы", True)]
