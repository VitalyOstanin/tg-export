"""Chat catalog export and config template generation."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime
from typing import Any

import yaml

from tg_export.format import DATE_FORMAT, ISO_MOMENT_FORMAT, format_moment
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


def _chat_to_dict(chat: Chat) -> dict[str, Any]:
    """Convert Chat to catalog YAML dict."""
    d = {
        "id": chat.id,
        "name": chat.name,
        "type": chat.type.value,
        "messages": chat.messages_count,
    }
    if chat.last_message_date:
        d["last_message"] = format_moment(chat.last_message_date, fmt=DATE_FORMAT)
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


def _chat_from_dict(d, *, folder=None, is_archived=False, is_left=False) -> Chat:
    """Restore a Chat from one catalog entry."""
    from tg_export.config import ConfigError

    if not isinstance(d, dict):
        raise ConfigError(f"catalog entry must be a mapping, got {type(d).__name__}")
    for key in ("id", "name"):
        if key not in d:
            raise ConfigError(f"catalog entry is missing '{key}': {d!r}")
    raw_type = d.get("type", ChatType.personal.value)
    try:
        chat_type = ChatType(raw_type)
    except ValueError:
        allowed = ", ".join(t.value for t in ChatType)
        raise ConfigError(
            f"unknown chat type {raw_type!r} in catalog entry id={d['id']}; allowed: {allowed}"
        ) from None
    last_message = d.get("last_message")
    if isinstance(last_message, str):
        try:
            last_message = datetime.strptime(last_message, DATE_FORMAT)
        except ValueError:
            raise ConfigError(f"bad last_message {last_message!r} in catalog entry id={d['id']}") from None
    elif last_message is not None and not isinstance(last_message, datetime):
        last_message = None
    return Chat(
        id=d["id"],
        name=d["name"],
        type=chat_type,
        username=d.get("username"),
        folder=d.get("folder", folder),
        members_count=d.get("members"),
        last_message_date=last_message,
        messages_count=d.get("messages", 0),
        is_left=bool(d.get("is_left", is_left)),
        is_archived=bool(d.get("is_archived", is_archived)),
        is_forum=bool(d.get("is_forum", False)),
        migrated_to_id=d.get("migrated_to_id"),
        migrated_from_id=d.get("migrated_from_id"),
        is_monoforum=bool(d.get("is_monoforum", False)),
    )


def chats_from_catalog(data) -> list[Chat]:
    """Read back a catalog written by :func:`format_catalog_yaml`.

    The section a chat sits in carries information the entry itself may omit:
    ``folders`` names the folder, ``archived`` and ``left`` set the flags. An
    entry that states them explicitly wins, so a hand-edited catalog behaves
    the way it reads. The JSON catalog has no sections and is read as one list.
    """
    from tg_export.config import ConfigError

    # The JSON form of the same catalog is a flat list: no sections, every flag
    # spelled out in the entry itself.
    if isinstance(data, list):
        return [_chat_from_dict(d) for d in data]
    if not isinstance(data, dict):
        raise ConfigError(f"catalog must be a mapping, got {type(data).__name__}")

    chats: list[Chat] = []

    folders = data.get("folders") or {}
    if not isinstance(folders, dict):
        raise ConfigError(f"catalog section 'folders' must be a mapping, got {type(folders).__name__}")
    for name, entries in folders.items():
        if not isinstance(entries, list):
            raise ConfigError(f"catalog folder {name!r} must hold a list, got {type(entries).__name__}")
        chats.extend(_chat_from_dict(d, folder=name) for d in entries)

    for section, flags in (("unfiled", {}), ("archived", {"is_archived": True}), ("left", {"is_left": True})):
        entries = data.get(section) or []
        if not isinstance(entries, list):
            raise ConfigError(f"catalog section {section!r} must be a list, got {type(entries).__name__}")
        chats.extend(_chat_from_dict(d, **flags) for d in entries)

    return chats


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
        "generated": format_moment(datetime.now(), fmt=ISO_MOMENT_FORMAT),
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
    data = [_chat_to_dict(c) for c in chats]
    return json.dumps(data, ensure_ascii=False, indent=2)


# The starting config offers more media types than the loader's default of
# `[photo]`: a first export that saves photographs alone surprises more people
# than it saves disk space. The same list is written in `config.example.yaml`,
# and the two are kept together by a test rather than by attention.
TEMPLATE_MEDIA_TYPES = ["photo", "video", "voice", "video_note", "sticker", "gif", "document"]


def generate_config_template(chats: list[Chat], account: str | None = None) -> str:
    """Build the starting config of one account, with its chats listed as comments.

    This is what `tg-export init` writes. It differs from `config.example.yaml`
    in the root of the repository by exactly that: the example is a static
    starting point, the template is generated for the chat list of a particular
    account. The values that are also defaults of the loader are taken from
    `Config()` rather than written here again -- otherwise a changed default
    would leave the template quietly claiming the old one. The one value that
    is deliberately not the loader's default is the media type list: see
    `TEMPLATE_MEDIA_TYPES`.
    """
    from tg_export.config import GLOBAL_DATA_SECTIONS, Config

    defaults = Config()
    media = defaults.defaults.media
    # The alias is appended by the exporter, so the template names the base
    # directory only -- writing it here as well gave ``export_output/acc/acc``.
    output_path = defaults.output.path
    lines = [
        "# tg-export config template",
        "# Uncomment and customize sections as needed",
        "",
        "output:",
        f"  path: {output_path}",
        f"  format: {defaults.output.format}",
        "",
        "defaults:",
        "  media:",
        f"    types: [{', '.join(TEMPLATE_MEDIA_TYPES)}]",
        f"    max_file_size: {media.max_file_size_bytes // 1024**2}MB",
        f"    concurrent_downloads: {media.concurrent_downloads}",
        f"  export_service_messages: {str(defaults.defaults.export_service_messages).lower()}",
        "",
        *(f"{name}: {str(getattr(defaults, name)).lower()}" for name in GLOBAL_DATA_SECTIONS),
        "",
        "left_channels:",
        f"  action: {defaults.left_channels_action}  # skip | export_with_defaults",
        "",
        "archived:",
        f"  action: {defaults.archived_action}  # skip | export_with_defaults",
        "",
        "unmatched:",
        f"  action: {defaults.unmatched_action}  # skip | export_with_defaults",
        "",
        "# Reuse files already downloaded by Telegram Desktop instead of",
        "# fetching them again. Exports made by tg-export itself are found",
        "# automatically when they lie next to this one.",
        "# import_existing:",
        "#   - path: ~/TelegramExport_2024",
        "#     type: tdesktop  # the only supported value",
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
    from tg_export.converter import convert_chat

    logger.debug("Fetching folders...")
    folders = await api.get_folders()
    logger.debug("Got %d folders: %s", len(folders), [f["name"] for f in folders])
    # Build reverse map: peer_id -> folder_name (from explicit include_peers)
    peer_to_folder: dict[int, str] = {}
    for folder in folders:
        for pid in folder["peer_ids"]:
            peer_to_folder[pid] = folder["name"]

    # Non-archived dialogs (folder=0 = main list, includes chats in named folders)
    logger.debug("Fetching non-archived dialogs (folder=0)...")
    chats = []
    non_archived_ids: set[int] = set()
    async for dialog in api.iter_dialogs(archived=False):
        entity = dialog.entity
        entity_id = getattr(entity, "id", 0)
        non_archived_ids.add(entity_id)
        folder = peer_to_folder.get(entity_id)
        chat = convert_chat(dialog, folder=folder)
        chats.append(chat)
    logger.debug("Got %d non-archived dialogs", len(chats))
    # Named folder peers are also non-archived
    for folder in folders:
        non_archived_ids.update(folder["peer_ids"])

    # Archived dialogs (folder=1), skip duplicates
    logger.debug("Fetching archived dialogs (folder=1)...")
    archived_count = 0
    async for dialog in api.iter_dialogs(archived=True):
        entity = dialog.entity
        entity_id = getattr(entity, "id", 0)
        if entity_id in non_archived_ids:
            continue  # already in main list
        folder = peer_to_folder.get(entity_id)
        chat = convert_chat(dialog, folder=folder)
        # Everything left here is archive-only: the main list and the named
        # folders were skipped by the `continue` above.
        chat.is_archived = True
        archived_count += 1
        chats.append(chat)
    logger.debug("Got archived dialogs, %d are archive-only", archived_count)
    logger.debug("Total chats: %d", len(chats))

    # Flag-based folders are applied to chats without an explicit folder, and
    # the archived ones are part of that: the folder decides which
    # `folders.<name>` rule an export follows, so assigning it before the
    # archive was fetched made an archived chat follow different settings
    # than the same chat outside the archive.
    _apply_folder_flags(chats, folders)

    if include_left:
        try:
            for ch in await api.get_left_channels():
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
            # The endpoint is not available for every account type, but the
            # caller asked for left channels: silently returning a catalog
            # without them looks like the account has none.
            logger.warning("Left channels could not be fetched (%s); they are missing from the catalog", e)

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
