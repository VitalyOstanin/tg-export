"""Chat catalog export and config template generation."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime

import yaml

from tg_export.models import Chat, ChatType

logger = logging.getLogger(__name__)

# Map Telegram folder flags to ChatType values
_FLAG_TO_TYPES: dict[str, set[ChatType]] = {
    "contacts": {ChatType.personal},
    "non_contacts": {ChatType.personal},
    "groups": {ChatType.private_group, ChatType.private_supergroup, ChatType.public_supergroup},
    "broadcasts": {ChatType.private_channel, ChatType.public_channel},
    "bots": {ChatType.bot},
}


def _apply_folder_flags(chats: list[Chat], folders: list[dict]) -> None:
    """Assign folder to chats matched by flag-based filters (contacts, groups, etc.)."""
    for folder in folders:
        # Collect chat types matched by this folder's flags
        matched_types: set[ChatType] = set()
        for flag, types in _FLAG_TO_TYPES.items():
            if folder.get(flag):
                matched_types.update(types)
        if not matched_types:
            continue

        exclude_ids = set(folder.get("exclude_ids", []))
        folder_name = folder["name"]

        for chat in chats:
            if chat.folder is not None:
                continue  # already assigned by explicit peer_id
            if chat.id in exclude_ids:
                continue
            if chat.type in matched_types:
                chat.folder = folder_name


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
    if chat.folder:
        d["folder"] = chat.folder
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


def generate_config_template(chats: list[Chat], account: str | None = None) -> str:
    """Generate config YAML template from catalog."""
    output_path = f"./export_output/{account}" if account else "./export_output"
    lines = [
        "# tg-export config template",
        "# Uncomment and customize sections as needed",
        "",
        "output:",
        f"  path: {output_path}",
        "  format: html",
        "",
        "defaults:",
        "  media:",
        "    types: [photo, video, voice, video_note, sticker, gif, document]",
        "    max_file_size: 100MB",
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
        "# type_rules:",
        "#   bots:",
        "#     skip: true",
        "#   public_channel:",
        "#     media:",
        "#       types: [photo]",
        "#       max_file_size: 10MB",
        "#   private:  # category: personal, private_group, private_supergroup, private_channel, self",
        "#     media:",
        "#       types: [photo, document]",
        "#   # categories: private, public, groups, channels, bots",
        "#   # exact types: personal, bot, self, private_group, private_supergroup,",
        "#   #   public_supergroup, private_channel, public_channel",
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
        lines.append(f'#     name: "{chat.name}"')
        lines.append(f"#     # type: {chat.type.value}, messages: {chat.messages_count}")

    lines.append("")
    return "\n".join(lines) + "\n"


async def fetch_catalog(api, include_left: bool = False) -> list[Chat]:
    """Fetch all chats from Telegram API and map to models.Chat."""
    import logging

    log = logging.getLogger(__name__)

    from tg_export.converter import convert_chat

    log.debug("Fetching folders...")
    folders = await api.get_folders()
    log.debug("Got %d folders: %s", len(folders), [f["name"] for f in folders])
    # Build reverse map: peer_id -> folder_name (from explicit include_peers)
    peer_to_folder: dict[int, str] = {}
    for folder in folders:
        for pid in folder["peer_ids"]:
            peer_to_folder[pid] = folder["name"]

    # Non-archived dialogs (folder=0 = main list, includes chats in named folders)
    log.debug("Fetching non-archived dialogs (folder=0)...")
    chats = []
    non_archived_ids: set[int] = set()
    async for dialog in api.iter_dialogs(archived=False):
        entity = dialog.entity
        entity_id = getattr(entity, "id", 0)
        non_archived_ids.add(entity_id)
        folder = peer_to_folder.get(entity_id)
        chat = convert_chat(dialog, folder=folder)
        chats.append(chat)
    log.debug("Got %d non-archived dialogs", len(chats))
    # Named folder peers are also non-archived
    for folder in folders:
        non_archived_ids.update(folder["peer_ids"])

    # Apply flag-based folder matching for chats without explicit folder
    _apply_folder_flags(chats, folders)

    # Archived dialogs (folder=1), skip duplicates
    log.debug("Fetching archived dialogs (folder=1)...")
    archived_count = 0
    async for dialog in api.iter_dialogs(archived=True):
        entity = dialog.entity
        entity_id = getattr(entity, "id", 0)
        if entity_id in non_archived_ids:
            continue  # already in main list
        folder = peer_to_folder.get(entity_id)
        chat = convert_chat(dialog, folder=folder)
        # Archive-only = not in main list and not in any named folder
        if entity_id not in non_archived_ids:
            chat.is_archived = True
            archived_count += 1
        chats.append(chat)
    log.debug("Got archived dialogs, %d are archive-only", archived_count)
    log.debug("Total chats: %d", len(chats))

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
        except Exception as e:
            # Left channels endpoint may not be available for all account types.
            logger.debug("get_left_channels: %s", e)

    return chats


def _classify_left_channel(entity) -> ChatType:
    """Classify left channel/group type."""

    if getattr(entity, "megagroup", False):
        if getattr(entity, "username", None):
            return ChatType.public_supergroup
        return ChatType.private_supergroup
    if getattr(entity, "username", None):
        return ChatType.public_channel
    return ChatType.private_channel
