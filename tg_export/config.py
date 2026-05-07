"""YAML config loading and validation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------------------
# Size parsing
# ---------------------------------------------------------------------------

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)\s*$", re.IGNORECASE)
_SIZE_MULTIPLIERS = {
    "B": 1,
    "KB": 1024,
    "MB": 1024**2,
    "GB": 1024**3,
    "TB": 1024**4,
}


def parse_size(s: str | int) -> int:
    """Parse '50MB', '2GB' etc. into bytes. Plain int passes through."""
    if isinstance(s, (int, float)):
        return int(s)
    m = _SIZE_RE.match(str(s))
    if not m:
        raise ConfigError(f"Invalid size format: {s!r}")
    return int(float(m.group(1)) * _SIZE_MULTIPLIERS[m.group(2).upper()])


# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------


@dataclass
class MediaConfig:
    types: list[str]
    max_file_size_bytes: int
    concurrent_downloads: int = 3


@dataclass
class ChatExportConfig:
    media: MediaConfig
    date_from: date | None = None
    date_to: date | None = None
    export_service_messages: bool = True


@dataclass
class OutputConfig:
    path: str = "./export_output"
    format: str = "html"


@dataclass
class ImportExistingEntry:
    path: str
    type: str  # tdesktop | tg-export


@dataclass
class ChatRule:
    id: int | None = None
    name: str | None = None
    media: MediaConfig | None = None
    date_from: date | None = None
    date_to: date | None = None
    skip: bool = False


@dataclass
class TypeRule:
    media: MediaConfig | None = None
    date_from: date | None = None
    date_to: date | None = None
    skip: bool = False


@dataclass
class FolderRule:
    media: MediaConfig | None = None
    skip: bool = False
    chats: list[ChatRule] = field(default_factory=list)


# Shortcut categories -> exact ChatType values
TYPE_CATEGORIES: dict[str, list[str]] = {
    "private": ["personal", "private_group", "private_supergroup", "private_channel", "self"],
    "public": ["public_supergroup", "public_channel"],
    "groups": ["private_group", "private_supergroup", "public_supergroup"],
    "channels": ["private_channel", "public_channel"],
    "bots": ["bot"],
}


@dataclass
class DefaultsConfig:
    media: MediaConfig = field(
        default_factory=lambda: MediaConfig(
            types=["photo"], max_file_size_bytes=100 * 1024**2, concurrent_downloads=3
        )
    )
    date_from: date | None = None
    date_to: date | None = None
    export_service_messages: bool = True


@dataclass
class Config:
    output: OutputConfig = field(default_factory=OutputConfig)
    defaults: DefaultsConfig = field(default_factory=DefaultsConfig)
    personal_info: bool = True
    contacts: bool = True
    sessions: bool = True
    userpics: bool = True
    stories: bool = True
    profile_music: bool = True
    other_data: bool = True
    left_channels_action: str = "skip"
    archived_action: str = "skip"  # skip | export_with_defaults
    import_existing: list[ImportExistingEntry] = field(default_factory=list)
    folders: dict[str, FolderRule] = field(default_factory=dict)
    type_rules: dict[str, TypeRule] = field(default_factory=dict)
    chats: list[ChatRule] = field(default_factory=list)
    unmatched_action: str = "skip"

    def resolve_chat_config(
        self,
        chat_id: int,
        chat_name: str,
        folder: str | None,
        chat_type: str | None = None,
    ) -> ChatExportConfig | None:
        """Resolve config for a chat using priority rules.

        Priority: chats > folders.chats > folders > type_rules > defaults.
        Returns None if the chat should be skipped.
        """
        # Priority 1: explicit chats section
        for rule in self.chats:
            if rule.id is not None and rule.id == chat_id:
                return self._rule_to_export_config(rule)
            if rule.name is not None and rule.name == chat_name:
                return self._rule_to_export_config(rule)

        # Priority 2 & 3: folder rules
        if folder and folder in self.folders:
            folder_rule = self.folders[folder]
            if folder_rule.skip:
                return None

            # Priority 2: chat within folder
            for chat_rule in folder_rule.chats:
                if chat_rule.id is not None and chat_rule.id == chat_id:
                    return self._rule_to_export_config(chat_rule)
                if chat_rule.name is not None and chat_rule.name == chat_name:
                    return self._rule_to_export_config(chat_rule)

            # Priority 3: folder-level rule
            # If folder is defined (not skipped), check type_rules for this chat,
            # then fall back to folder media or defaults
            if chat_type and self.type_rules:
                type_rule = self._match_type_rule(chat_type)
                if type_rule is not None:
                    return self._type_rule_to_export_config(type_rule)

            media = folder_rule.media if folder_rule.media is not None else self.defaults.media
            return ChatExportConfig(
                media=media,
                date_from=self.defaults.date_from,
                date_to=self.defaults.date_to,
                export_service_messages=self.defaults.export_service_messages,
            )

        # Priority 4: type_rules
        if chat_type and self.type_rules:
            type_rule = self._match_type_rule(chat_type)
            if type_rule is not None:
                return self._type_rule_to_export_config(type_rule)

        # Priority 5: defaults (if unmatched allows it)
        if self.unmatched_action == "skip":
            return None

        return ChatExportConfig(
            media=self.defaults.media,
            date_from=self.defaults.date_from,
            date_to=self.defaults.date_to,
            export_service_messages=self.defaults.export_service_messages,
        )

    def _match_type_rule(self, chat_type: str) -> TypeRule | None:
        """Find matching type rule. Exact type > category, first match wins."""
        # Exact type match first
        if chat_type in self.type_rules:
            return self.type_rules[chat_type]
        # Category match (order of type_rules dict)
        for key, rule in self.type_rules.items():
            if key in TYPE_CATEGORIES and chat_type in TYPE_CATEGORIES[key]:
                return rule
        return None

    def _rule_to_export_config(self, rule: ChatRule) -> ChatExportConfig | None:
        if rule.skip:
            return None
        media = rule.media if rule.media is not None else self.defaults.media
        return ChatExportConfig(
            media=media,
            date_from=rule.date_from or self.defaults.date_from,
            date_to=rule.date_to or self.defaults.date_to,
            export_service_messages=self.defaults.export_service_messages,
        )

    def _type_rule_to_export_config(self, rule: TypeRule) -> ChatExportConfig | None:
        if rule.skip:
            return None
        media = rule.media if rule.media is not None else self.defaults.media
        return ChatExportConfig(
            media=media,
            date_from=rule.date_from or self.defaults.date_from,
            date_to=rule.date_to or self.defaults.date_to,
            export_service_messages=self.defaults.export_service_messages,
        )


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_media_config(d: dict) -> MediaConfig:
    types = d.get("types", ["photo"])
    if types == "all":
        types = ["all"]
    max_size = parse_size(d.get("max_file_size", "100MB"))
    concurrent = d.get("concurrent_downloads", 3)
    return MediaConfig(types=types, max_file_size_bytes=max_size, concurrent_downloads=concurrent)


def _parse_date(val: Any) -> date | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        return date.fromisoformat(val)
    return None


def _parse_chat_rule(d: dict) -> ChatRule:
    media = _parse_media_config(d["media"]) if "media" in d else None
    return ChatRule(
        id=d.get("id"),
        name=d.get("name"),
        media=media,
        date_from=_parse_date(d.get("date_from")),
        date_to=_parse_date(d.get("date_to")),
        skip=d.get("skip", False),
    )


def _parse_type_rule(d: dict) -> TypeRule:
    if isinstance(d, dict) and d.get("skip"):
        return TypeRule(skip=True)
    media = _parse_media_config(d["media"]) if "media" in d else None
    return TypeRule(
        media=media,
        date_from=_parse_date(d.get("date_from")),
        date_to=_parse_date(d.get("date_to")),
        skip=False,
    )


def _parse_folder_rule(d: dict) -> FolderRule:
    if isinstance(d, dict) and d.get("skip"):
        return FolderRule(skip=True)
    media = _parse_media_config(d["media"]) if "media" in d else None
    chats = [_parse_chat_rule(c) for c in d.get("chats", [])]
    return FolderRule(media=media, skip=False, chats=chats)


# ---------------------------------------------------------------------------
# Main loader
# ---------------------------------------------------------------------------


def load_config(path: Path) -> Config:
    """Load and validate YAML config file."""
    with open(path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ConfigError(f"Config must be a YAML mapping, got {type(raw).__name__}")

    # Output
    out_raw = raw.get("output", {})
    output = OutputConfig(
        path=out_raw.get("path", "./export_output"),
        format=out_raw.get("format", "html"),
    )

    # Defaults
    def_raw = raw.get("defaults", {})
    media_raw = def_raw.get("media", {})
    defaults = DefaultsConfig(
        media=_parse_media_config(media_raw),
        date_from=_parse_date(def_raw.get("date_from")),
        date_to=_parse_date(def_raw.get("date_to")),
        export_service_messages=def_raw.get("export_service_messages", True),
    )

    # Import existing
    import_existing = []
    for entry in raw.get("import_existing", []):
        import_existing.append(
            ImportExistingEntry(
                path=entry["path"],
                type=entry["type"],
            )
        )

    # Folders
    folders = {}
    for name, folder_data in raw.get("folders", {}).items():
        folders[name] = _parse_folder_rule(folder_data)

    # Type rules
    type_rules = {}
    for type_key, type_data in raw.get("type_rules", {}).items():
        type_rules[type_key] = _parse_type_rule(type_data)

    # Chats
    chats = [_parse_chat_rule(c) for c in raw.get("chats", [])]

    # Left channels
    lc_raw = raw.get("left_channels", {})
    left_channels_action = lc_raw.get("action", "skip") if isinstance(lc_raw, dict) else "skip"

    # Archived
    ar_raw = raw.get("archived", {})
    archived_action = ar_raw.get("action", "skip") if isinstance(ar_raw, dict) else "skip"

    # Unmatched
    um_raw = raw.get("unmatched", {})
    unmatched_action = um_raw.get("action", "skip") if isinstance(um_raw, dict) else "skip"

    return Config(
        output=output,
        defaults=defaults,
        personal_info=raw.get("personal_info", True),
        contacts=raw.get("contacts", True),
        sessions=raw.get("sessions", True),
        userpics=raw.get("userpics", True),
        stories=raw.get("stories", True),
        profile_music=raw.get("profile_music", True),
        other_data=raw.get("other_data", True),
        left_channels_action=left_channels_action,
        archived_action=archived_action,
        import_existing=import_existing,
        folders=folders,
        type_rules=type_rules,
        chats=chats,
        unmatched_action=unmatched_action,
    )
