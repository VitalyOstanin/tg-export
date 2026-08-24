from datetime import datetime

import pytest
import pytest_asyncio

from tg_export.auth import AccountManager
from tg_export.cli import common as cli_common
from tg_export.state import ExportState


@pytest.fixture(autouse=True)
def config_dir_of_its_own(tmp_path, monkeypatch):
    """Каждый тест получает свой каталог конфигурации, а не каталог разработчика.

    `AccountManager()` без явного пути читает окружение
    (`TG_EXPORT_CONFIG_DIR` > `XDG_CONFIG_HOME` > `~/.config/tg-export`), а
    `mgr()` ещё и создаёт каталоги. Изоляция держалась на том, что каждый тест
    сам подменяет менеджер: забытая подмена в тесте `init` означала бы запись
    шаблона поверх настоящей конфигурации того, кто запустил тесты.
    """
    monkeypatch.setenv("TG_EXPORT_CONFIG_DIR", str(tmp_path / "tg-export-config"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


@pytest_asyncio.fixture
async def state(tmp_path):
    s = ExportState(tmp_path / "state.db")
    await s.open()
    yield s
    await s.close()


@pytest.fixture
def account_env(tmp_path, monkeypatch):
    """Настроенный аккаунт без файла конфигурации экспорта."""
    cfg_dir = tmp_path / "config"
    mgr = AccountManager(config_dir=cfg_dir)
    mgr.ensure_dirs()
    mgr.save_credentials(1, "hash")
    mgr.set_default_account("acc")
    mgr.session_path("acc").write_bytes(b"")
    monkeypatch.setattr(cli_common, "account_manager", lambda: AccountManager(config_dir=cfg_dir))
    return cfg_dir


# ---------------------------------------------------------------------------
# Фабрики моделей
# ---------------------------------------------------------------------------
#
# Ни у `Chat`, ни у `Message` нет умолчаний -- пакет требует назвать каждое
# поле, и тесты выписывали оба списка целиком в двух десятках мест. Правка
# модели означала обход всех копий, а тест, которому важно одно поле, начинался
# с двадцати незначащих строк. Фабрики задают умолчания одним местом; тест
# называет только то, что проверяет.


def make_chat(**over):
    """`Chat` с заполненными полями; в аргументах -- только значимое для теста."""
    from tg_export.models import Chat, ChatType

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
    return Chat(**fields)


def make_message(text=None, **over):
    """`Message` с заполненными полями.

    `text` строкой разворачивается в один `TextPart`; готовый список частей
    принимается как есть -- тесты разметки строят его сами.
    """
    from tg_export.models import Message, TextPart, TextType

    parts = text if isinstance(text, list) else ([TextPart(type=TextType.text, text=text)] if text else [])

    fields = {
        "id": 1,
        "chat_id": 1,
        "date": datetime(2024, 1, 1),
        "edited": None,
        "from_id": None,
        "from_name": None,
        "text": parts,
        "media": None,
        "action": None,
        "reply_to_msg_id": None,
        "reply_to_peer_id": None,
        "forwarded_from": None,
        "reactions": [],
        "is_outgoing": False,
        "signature": None,
        "via_bot_id": None,
        "saved_from_chat_id": None,
        "inline_buttons": None,
        "topic_id": None,
        "grouped_id": None,
    }
    fields.update(over)
    return Message(**fields)
