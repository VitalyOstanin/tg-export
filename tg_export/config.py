"""YAML config loading and validation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from tg_export.errors import TgExportError
from tg_export.models import ChatType, MediaType


class ConfigError(TgExportError):
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

    def max_media_file_size(self) -> int:
        """Largest media limit any exportable chat can ask for, in bytes.

        A Takeout session carries a single ``max_file_size`` for its whole
        lifetime, while chats / type_rules / folders each may raise the limit
        above ``defaults.media``. Taking the maximum is the only value that
        does not silently cap a chat the configuration says to download in
        full. Rules marked ``skip`` never download anything, so their limit
        must not widen the session.
        """
        limits = [self.defaults.media.max_file_size_bytes]

        def add(media: MediaConfig | None, skip: bool) -> None:
            if media is not None and not skip:
                limits.append(media.max_file_size_bytes)

        for chat_rule in self.chats:
            add(chat_rule.media, chat_rule.skip)
        for type_rule in self.type_rules.values():
            add(type_rule.media, type_rule.skip)
        for folder_rule in self.folders.values():
            add(folder_rule.media, folder_rule.skip)
            for chat_rule in folder_rule.chats:
                # resolve_chat_config returns None for every chat of a skipped
                # folder before it ever looks at the nested rules, so a nested
                # limit only counts while the folder itself is exported.
                add(chat_rule.media, folder_rule.skip or chat_rule.skip)

        return max(limits)

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
                    return self._rule_to_export_config(type_rule)

            return self._defaults_export_config(folder_rule.media)

        # Priority 4: type_rules
        if chat_type and self.type_rules:
            type_rule = self._match_type_rule(chat_type)
            if type_rule is not None:
                return self._rule_to_export_config(type_rule)

        # Priority 5: defaults (if unmatched allows it)
        if self.unmatched_action == "skip":
            return None

        return self._defaults_export_config()

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

    def _defaults_export_config(self, media: MediaConfig | None = None) -> ChatExportConfig:
        """Build a ChatExportConfig from defaults, optionally overriding media."""
        return ChatExportConfig(
            media=media if media is not None else self.defaults.media,
            date_from=self.defaults.date_from,
            date_to=self.defaults.date_to,
            export_service_messages=self.defaults.export_service_messages,
        )

    def _rule_to_export_config(self, rule: ChatRule | TypeRule) -> ChatExportConfig | None:
        """Build export config for a ChatRule or TypeRule (shared fields)."""
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


# Media kinds accepted in `media.types`, plus the catch-all.
_MEDIA_TYPE_NAMES = {t.value for t in MediaType} | {"all"}

# Range documented for `concurrent_downloads`. Beyond it the value would reach
# asyncio.Semaphore unchecked: zero produces an acquire nobody ever satisfies.
MIN_CONCURRENT_DOWNLOADS = 1
MAX_CONCURRENT_DOWNLOADS = 5


def _parse_media_types(value: Any) -> list[str]:
    """Validate `media.types`, which must be a list of known media kinds.

    A scalar (`types: photo`) used to pass through as a string, turning the
    membership test in the downloader into a substring search: with
    `types: video_note` the check `"video" not in "video_note"` is false, so
    ordinary videos were downloaded although only round ones were configured.
    """
    if value == "all":
        return ["all"]
    if isinstance(value, str) or not isinstance(value, list):
        raise ConfigError(
            f"media.types must be a list, got {type(value).__name__} ({value!r}); "
            f"write it as [photo, video] or as the single word all"
        )
    allowed = ", ".join(sorted(_MEDIA_TYPE_NAMES))
    for item in value:
        if item not in _MEDIA_TYPE_NAMES:
            raise ConfigError(f"Unknown media type {item!r} in media.types; allowed values: {allowed}")
    return list(value)


def _parse_concurrent_downloads(value: Any) -> int:
    """Validate `concurrent_downloads` against the documented range."""
    # bool is an int in Python, and `concurrent_downloads: true` means nothing.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"media.concurrent_downloads must be an integer "
            f"between {MIN_CONCURRENT_DOWNLOADS} and {MAX_CONCURRENT_DOWNLOADS}, got {value!r}"
        )
    if not MIN_CONCURRENT_DOWNLOADS <= value <= MAX_CONCURRENT_DOWNLOADS:
        raise ConfigError(
            f"media.concurrent_downloads must be between {MIN_CONCURRENT_DOWNLOADS} "
            f"and {MAX_CONCURRENT_DOWNLOADS}, got {value}"
        )
    return value


def _parse_media_config(d: dict) -> MediaConfig:
    return MediaConfig(
        types=_parse_media_types(d.get("types", ["photo"])),
        max_file_size_bytes=parse_size(d.get("max_file_size", "100MB")),
        concurrent_downloads=_parse_concurrent_downloads(d.get("concurrent_downloads", 3)),
    )


def _parse_date(val: Any, field_name: str) -> date | None:
    """Parse a date field, naming the field when the value is unusable.

    A bare ValueError from date.fromisoformat named neither the field nor the
    chat whose rule carried the typo, and any other type (`date_from: 2024`
    reads as a YAML integer) used to be dropped without a word.
    """
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        try:
            return date.fromisoformat(val)
        except ValueError as e:
            raise ConfigError(f"Invalid date in {field_name}: {val!r}; expected YYYY-MM-DD") from e
    raise ConfigError(f"Invalid date in {field_name}: {val!r}; expected YYYY-MM-DD")


def _parse_chat_rule(d: dict) -> ChatRule:
    media = _parse_media_config(d["media"]) if "media" in d else None
    return ChatRule(
        id=d.get("id"),
        name=d.get("name"),
        media=media,
        date_from=_parse_date(d.get("date_from"), "chats[].date_from"),
        date_to=_parse_date(d.get("date_to"), "chats[].date_to"),
        skip=d.get("skip", False),
    )


def _parse_type_rule(d: dict) -> TypeRule:
    if isinstance(d, dict) and d.get("skip"):
        return TypeRule(skip=True)
    media = _parse_media_config(d["media"]) if "media" in d else None
    return TypeRule(
        media=media,
        date_from=_parse_date(d.get("date_from"), "type_rules.*.date_from"),
        date_to=_parse_date(d.get("date_to"), "type_rules.*.date_to"),
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

# Allowed values for enumerated config fields. Validated at load time so a
# typo in YAML (e.g. `action: skipp`) fails fast instead of silently changing
# behaviour at runtime (where comparisons are against exact literals).
_LEFT_CHANNELS_ACTIONS = {"skip", "export_with_defaults"}
_ARCHIVED_ACTIONS = {"skip", "export_with_defaults"}
# "ask" was accepted here but behaved as export_with_defaults: there is no
# interactive branch in the code, and the price of the mistake is asymmetric --
# the user expects a prompt per unmatched chat and gets all of them exported.
_UNMATCHED_ACTIONS = {"skip", "export_with_defaults"}
# Only HTML is rendered. "json"/"both" used to pass validation and show up in
# `tg-export config -v` as an active setting while changing nothing.
_OUTPUT_FORMATS = {"html"}

# Known top-level config keys. An unknown key is most likely a typo in a
# section name (e.g. `default` instead of `defaults`); without this check such
# data is silently ignored and defaults are applied.
_KNOWN_TOP_LEVEL_KEYS = {
    "output",
    "defaults",
    "personal_info",
    "contacts",
    "sessions",
    "userpics",
    "stories",
    "profile_music",
    "other_data",
    "left_channels",
    "archived",
    "unmatched",
    "import_existing",
    "folders",
    "type_rules",
    "chats",
}


# Chat types and categories that may head a `type_rules` block. A typo here
# used to disable the rule silently: matching is by exact key, so an unknown
# one simply never matches.
_TYPE_RULE_KEYS = {t.value for t in ChatType} | set(TYPE_CATEGORIES)

# Sources `import_existing` can point at. Only tdesktop is read today; an entry
# of any other type would be skipped by the consumer without a word.
_IMPORT_TYPES = {"tdesktop"}


def _parse_import_entry(entry: Any) -> ImportExistingEntry:
    """Validate one `import_existing` record.

    Both keys used to be read by direct indexing, so a missing one produced a
    KeyError with a traceback instead of naming the section at fault.
    """
    if not isinstance(entry, dict):
        raise ConfigError(f"import_existing entries must be mappings, got {type(entry).__name__}")
    for key in ("path", "type"):
        if key not in entry:
            raise ConfigError(f"import_existing entry is missing the {key!r} key: {entry!r}")
    allowed = ", ".join(sorted(_IMPORT_TYPES))
    if entry["type"] not in _IMPORT_TYPES:
        raise ConfigError(f"Unknown import_existing type {entry['type']!r}; supported values: {allowed}")
    return ImportExistingEntry(path=entry["path"], type=entry["type"])


def _validate_choice(value: str, allowed: set[str], field_name: str) -> str:
    if value not in allowed:
        allowed_str = ", ".join(sorted(allowed))
        raise ConfigError(f"Invalid value {value!r} for {field_name}; allowed values: {allowed_str}")
    return value


def load_config(path: Path) -> Config:
    """Load and validate YAML config file."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"Cannot parse {path}: {e}") from e

    if not isinstance(raw, dict):
        raise ConfigError(f"Config must be a YAML mapping, got {type(raw).__name__}")

    unknown_keys = set(raw) - _KNOWN_TOP_LEVEL_KEYS
    if unknown_keys:
        unknown_str = ", ".join(sorted(unknown_keys))
        known_str = ", ".join(sorted(_KNOWN_TOP_LEVEL_KEYS))
        raise ConfigError(f"Unknown config key(s): {unknown_str}. Known keys: {known_str}")

    # Output
    out_raw = raw.get("output", {})
    output = OutputConfig(
        # The shell expands ~ only for unquoted arguments, so a path written in
        # YAML reaches us verbatim: without this a directory literally named ~
        # would be created in the current working directory.
        path=str(Path(out_raw.get("path", "./export_output")).expanduser()),
        format=_validate_choice(out_raw.get("format", "html"), _OUTPUT_FORMATS, "output.format"),
    )

    # Defaults
    def_raw = raw.get("defaults", {})
    media_raw = def_raw.get("media", {})
    defaults = DefaultsConfig(
        media=_parse_media_config(media_raw),
        date_from=_parse_date(def_raw.get("date_from"), "defaults.date_from"),
        date_to=_parse_date(def_raw.get("date_to"), "defaults.date_to"),
        export_service_messages=def_raw.get("export_service_messages", True),
    )

    # Import existing
    import_existing = [_parse_import_entry(entry) for entry in raw.get("import_existing", [])]

    # Folders
    folders = {}
    for name, folder_data in raw.get("folders", {}).items():
        folders[name] = _parse_folder_rule(folder_data)

    # Type rules
    type_rules = {}
    for type_key, type_data in raw.get("type_rules", {}).items():
        if type_key not in _TYPE_RULE_KEYS:
            allowed = ", ".join(sorted(_TYPE_RULE_KEYS))
            raise ConfigError(f"Unknown key {type_key!r} in type_rules; allowed values: {allowed}")
        type_rules[type_key] = _parse_type_rule(type_data)

    # Chats
    chats = [_parse_chat_rule(c) for c in raw.get("chats", [])]

    # Left channels
    lc_raw = raw.get("left_channels", {})
    left_channels_action = lc_raw.get("action", "skip") if isinstance(lc_raw, dict) else "skip"
    left_channels_action = _validate_choice(
        left_channels_action, _LEFT_CHANNELS_ACTIONS, "left_channels.action"
    )

    # Archived
    ar_raw = raw.get("archived", {})
    archived_action = ar_raw.get("action", "skip") if isinstance(ar_raw, dict) else "skip"
    archived_action = _validate_choice(archived_action, _ARCHIVED_ACTIONS, "archived.action")

    # Unmatched
    um_raw = raw.get("unmatched", {})
    unmatched_action = um_raw.get("action", "skip") if isinstance(um_raw, dict) else "skip"
    unmatched_action = _validate_choice(unmatched_action, _UNMATCHED_ACTIONS, "unmatched.action")

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
