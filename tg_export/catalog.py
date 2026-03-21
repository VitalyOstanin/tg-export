"""Chat catalog export and config template generation."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

import yaml

from tg_export.models import Chat


def _chat_to_dict(chat: Chat) -> dict:
    """Convert Chat to catalog YAML dict."""
    d = {
        "id": chat.id,
        "name": chat.name,
        "type": chat.type.value,
        "messages": chat.messages_count,
    }
    if chat.last_message_date:
        d["last_message"] = chat.last_message_date.strftime("%Y-%m-%d")
    if chat.members_count is not None:
        d["members"] = chat.members_count
    if chat.username:
        d["username"] = chat.username
    if chat.is_left:
        d["is_left"] = True
    if chat.is_archived:
        d["is_archived"] = True
    if chat.is_forum:
        d["is_forum"] = True
    if chat.is_monoforum:
        d["is_monoforum"] = True
    if chat.migrated_to_id:
        d["migrated_to_id"] = chat.migrated_to_id
    if chat.migrated_from_id:
        d["migrated_from_id"] = chat.migrated_from_id
    return d


def format_catalog_yaml(chats: list[Chat]) -> str:
    """Format chat catalog as YAML, grouped by folders/unfiled/left."""
    folders: dict[str, list[dict]] = defaultdict(list)
    unfiled: list[dict] = []
    left: list[dict] = []
    archived: list[dict] = []

    for chat in chats:
        d = _chat_to_dict(chat)
        if chat.is_left:
            left.append(d)
        elif chat.is_archived:
            archived.append(d)
        elif chat.folder:
            folders[chat.folder].append(d)
        else:
            unfiled.append(d)

    result: dict = {
        "generated": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }

    if folders:
        result["folders"] = dict(folders)
    if unfiled:
        result["unfiled"] = unfiled
    if archived:
        result["archived"] = archived
    if left:
        result["left"] = left

    return yaml.dump(result, default_flow_style=False, allow_unicode=True, sort_keys=False)


def format_catalog_json(chats: list[Chat]) -> str:
    """Format chat catalog as JSON."""
    import json
    data = [_chat_to_dict(c) for c in chats]
    return json.dumps(data, ensure_ascii=False, indent=2)


def generate_config_template(chats: list[Chat]) -> str:
    """Generate config YAML template from catalog."""
    lines = [
        "# tg-export config template",
        "# Uncomment and customize sections as needed",
        "",
        "output:",
        "  path: ./export_output",
        "  format: html",
        "  messages_per_file: 1000",
        "  min_free_space: 20GB",
        "",
        "defaults:",
        "  media:",
        "    types: [photo, video, voice, video_note, sticker, gif, document]",
        "    max_file_size: 50MB",
        "    concurrent_downloads: 3",
        "  export_service_messages: true",
        "",
        "personal_info: true",
        "contacts: true",
        "sessions: true",
        "userpics: true",
        "stories: true",
        "profile_music: true",
        "other_data: true",
        "",
        "left_channels:",
        "  action: skip  # skip | export_with_defaults",
        "",
        "unmatched:",
        "  action: skip  # skip | export_with_defaults | ask",
        "",
        "# folders:",
        '#   "Folder Name":',
        "#     media:",
        "#       types: [photo, document]",
        "",
        "# chats:",
    ]

    # Add commented-out chat entries
    for chat in chats:
        lines.append(f"#   - id: {chat.id}")
        lines.append(f"#     name: \"{chat.name}\"")
        lines.append(f"#     # type: {chat.type.value}, messages: {chat.messages_count}")

    lines.append("")
    return "\n".join(lines) + "\n"


async def fetch_catalog(api, include_left: bool = False) -> list[Chat]:
    """Fetch all chats from Telegram API and map to models.Chat."""
    from tg_export.converter import convert_chat

    folders = await api.get_folders()
    # Build reverse map: peer_id -> folder_name
    peer_to_folder: dict[int, str] = {}
    for folder_name, peer_ids in folders.items():
        for pid in peer_ids:
            peer_to_folder[pid] = folder_name

    # All dialogs (folders + unfiled + archived)
    chats = []
    all_ids: set[int] = set()
    async for dialog in api.iter_dialogs():  # archived=None -> all
        entity = dialog.entity
        entity_id = getattr(entity, "id", 0)
        all_ids.add(entity_id)
        folder = peer_to_folder.get(entity_id)
        chat = convert_chat(dialog, folder=folder)
        chats.append(chat)

    # Determine which are archive-only:
    # archived=True gives folder=1 (archive), we check which of those
    # are NOT also in folder=0 (non-archive non-folder dialogs) or in a named folder
    archived_ids: set[int] = set()
    async for dialog in api.iter_dialogs(archived=True):
        entity = dialog.entity
        archived_ids.add(getattr(entity, "id", 0))

    # Non-archived = those in folder=0 or in named folders
    non_archived_ids: set[int] = set()
    async for dialog in api.iter_dialogs(archived=False):  # folder=0
        entity = dialog.entity
        non_archived_ids.add(getattr(entity, "id", 0))
    # Add those in named folders
    for peer_ids in folders.values():
        non_archived_ids.update(peer_ids)

    # Mark archive-only chats
    archive_only_ids = archived_ids - non_archived_ids
    for chat in chats:
        if chat.id in archive_only_ids:
            chat.is_archived = True

    if include_left:
        try:
            left_result = await api.get_left_channels()
            for ch in getattr(left_result, "chats", []):
                chat = Chat(
                    id=ch.id,
                    name=getattr(ch, "title", ""),
                    type=_classify_left_channel(ch),
                    username=getattr(ch, "username", None),
                    folder=None,
                    members_count=getattr(ch, "participants_count", None),
                    last_message_date=None,
                    messages_count=0,
                    is_left=True,
                    is_archived=False,
                    is_forum=False,
                    migrated_to_id=None,
                    migrated_from_id=None,
                    is_monoforum=False,
                )
                chats.append(chat)
        except Exception:
            pass  # Left channels may not be available

    return chats


def _classify_left_channel(entity) -> str:
    """Classify left channel/group type."""
    from tg_export.models import ChatType
    if getattr(entity, "megagroup", False):
        if getattr(entity, "username", None):
            return ChatType.public_supergroup
        return ChatType.private_supergroup
    if getattr(entity, "username", None):
        return ChatType.public_channel
    return ChatType.private_channel
