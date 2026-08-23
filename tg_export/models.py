"""Data models for tg-export."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum, StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ChatType(StrEnum):
    self = "self"
    replies = "replies"
    verify_codes = "verify_codes"
    personal = "personal"
    bot = "bot"
    private_group = "private_group"
    private_supergroup = "private_supergroup"
    public_supergroup = "public_supergroup"
    private_channel = "private_channel"
    public_channel = "public_channel"


class MediaType(StrEnum):
    photo = "photo"
    video = "video"
    document = "document"
    voice = "voice"
    video_note = "video_note"
    sticker = "sticker"
    gif = "gif"
    contact = "contact"
    geo = "geo"
    venue = "venue"
    poll = "poll"
    game = "game"
    invoice = "invoice"
    todo_list = "todo_list"
    giveaway = "giveaway"
    paid_media = "paid_media"
    unsupported = "unsupported"


class TextType(StrEnum):
    text = "text"
    unknown = "unknown"
    mention = "mention"
    hashtag = "hashtag"
    bot_command = "bot_command"
    url = "url"
    email = "email"
    bold = "bold"
    italic = "italic"
    code = "code"
    pre = "pre"
    text_url = "text_url"
    mention_name = "mention_name"
    phone = "phone"
    cashtag = "cashtag"
    underline = "underline"
    strikethrough = "strikethrough"
    blockquote = "blockquote"
    bank_card = "bank_card"
    spoiler = "spoiler"
    custom_emoji = "custom_emoji"


class ReactionType(StrEnum):
    emoji = "emoji"
    custom_emoji = "custom_emoji"
    paid = "paid"


class FileStatus(StrEnum):
    """What the `files.status` column of the state database records.

    `done` -- the file is on disk and its size matches; `partial` -- it is on
    disk but shorter than announced; the two skipped values mean no file was
    fetched at all, by decision of the config. The column carries no CHECK
    (adding one to existing databases would mean rebuilding the table), so this
    is the only place the vocabulary is written down. Two of the values are also
    outcomes of a download attempt and are spelled the same in
    :class:`tg_export.media.DownloadStatus`.
    """

    done = "done"
    partial = "partial"
    skipped_by_size = "skipped_by_size"
    skipped_by_type = "skipped_by_type"


# Statuses that mean the file was never fetched: the config decided against it.
SKIPPED_FILE_STATUSES = (FileStatus.skipped_by_size, FileStatus.skipped_by_type)


class InlineButtonType(StrEnum):
    default = "default"
    url = "url"
    callback = "callback"
    callback_with_password = "callback_with_password"
    request_phone = "request_phone"
    request_location = "request_location"
    request_poll = "request_poll"
    request_peer = "request_peer"
    switch_inline = "switch_inline"
    switch_inline_same = "switch_inline_same"
    game = "game"
    buy = "buy"
    auth = "auth"
    web_view = "web_view"
    simple_web_view = "simple_web_view"
    user_profile = "user_profile"
    copy_text = "copy_text"


# ---------------------------------------------------------------------------
# Helper dataclasses
# ---------------------------------------------------------------------------


@dataclass
class FileInfo:
    id: int
    size: int
    name: str | None
    mime_type: str | None
    local_path: str | None


@dataclass
class TextPart:
    type: TextType
    text: str
    href: str | None = None
    user_id: int | None = None


@dataclass
class Reaction:
    type: ReactionType
    emoji: str | None
    document_id: int | None
    count: int
    recent: list[int] | None = None


@dataclass
class ForwardInfo:
    from_id: int | None
    from_name: str | None
    date: datetime | None
    saved_from_chat_id: int | None = None
    show_as_original: bool = False


@dataclass
class InlineButton:
    type: InlineButtonType
    text: str
    data: str | None = None


@dataclass
class PollAnswer:
    text: list[TextPart]
    voters: int
    chosen: bool = False


@dataclass
class TodoItem:
    id: int
    text: str
    completed: bool = False


@dataclass
class ForumTopic:
    id: int
    title: str
    icon_emoji: str | None
    is_closed: bool
    is_pinned: bool
    messages_count: int


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------


@dataclass
class Chat:
    id: int
    name: str
    type: ChatType
    username: str | None
    folder: str | None
    members_count: int | None
    last_message_date: datetime | None
    messages_count: int
    is_left: bool
    is_archived: bool
    is_forum: bool
    migrated_to_id: int | None
    migrated_from_id: int | None
    is_monoforum: bool


# ---------------------------------------------------------------------------
# Media hierarchy
# ---------------------------------------------------------------------------


@dataclass
class Media:
    type: MediaType
    file: FileInfo | None


@dataclass
class PhotoMedia(Media):
    width: int = 0
    height: int = 0
    spoilered: bool = False


@dataclass
class DocumentMedia(Media):
    name: str | None = None
    mime_type: str | None = None
    duration: int | None = None
    width: int | None = None
    height: int | None = None
    performer: str | None = None
    song_title: str | None = None
    sticker_emoji: str | None = None
    spoilered: bool = False
    ttl: int | None = None


@dataclass
class ContactMedia(Media):
    phone: str = ""
    first_name: str = ""
    last_name: str = ""
    vcard: str | None = None


@dataclass
class GeoMedia(Media):
    latitude: float = 0.0
    longitude: float = 0.0


@dataclass
class VenueMedia(Media):
    latitude: float = 0.0
    longitude: float = 0.0
    title: str = ""
    address: str = ""


@dataclass
class PollMedia(Media):
    question: list[TextPart] = field(default_factory=list)
    answers: list[PollAnswer] = field(default_factory=list)
    total_votes: int = 0
    closed: bool = False


@dataclass
class GameMedia(Media):
    title: str = ""
    description: str = ""
    short_name: str = ""


@dataclass
class InvoiceMedia(Media):
    title: str = ""
    description: str = ""
    currency: str = ""
    amount: int = 0
    receipt_msg_id: int | None = None


@dataclass
class TodoListMedia(Media):
    title: str = ""
    items: list[TodoItem] = field(default_factory=list)
    others_can_append: bool = False
    others_can_complete: bool = False


@dataclass
class GiveawayMedia(Media):
    is_results: bool = False


@dataclass
class PaidMedia(Media):
    stars_amount: int = 0


@dataclass
class UnsupportedMedia(Media):
    pass


# Media class registry for deserialization
_MEDIA_CLASSES: dict[str, type[Media]] = {
    "photo": PhotoMedia,
    "video": DocumentMedia,
    "document": DocumentMedia,
    "voice": DocumentMedia,
    "video_note": DocumentMedia,
    "sticker": DocumentMedia,
    "gif": DocumentMedia,
    "contact": ContactMedia,
    "geo": GeoMedia,
    "venue": VenueMedia,
    "poll": PollMedia,
    "game": GameMedia,
    "invoice": InvoiceMedia,
    "todo_list": TodoListMedia,
    "giveaway": GiveawayMedia,
    "paid_media": PaidMedia,
    "unsupported": UnsupportedMedia,
}


# ---------------------------------------------------------------------------
# Service actions
# ---------------------------------------------------------------------------


@dataclass
class ServiceAction:
    """Base class for service actions.

    ``type`` defaults to the class name. Writing it out next to the class left
    a rename silently behind, and the value travels into the state database and
    into the templates, where it is matched as a string.
    """

    type: str = ""

    def __post_init__(self) -> None:
        self.type = self.type or type(self).__name__


# Groups / channels
@dataclass
class ActionChatCreate(ServiceAction):
    title: str = ""
    users: list[str] = field(default_factory=list)


@dataclass
class ActionChatEditTitle(ServiceAction):
    title: str = ""


@dataclass
class ActionChatEditPhoto(ServiceAction):
    photo: FileInfo | None = None


@dataclass
class ActionChatDeletePhoto(ServiceAction):
    pass


@dataclass
class ActionChatAddUser(ServiceAction):
    users: list[str] = field(default_factory=list)


@dataclass
class ActionChatDeleteUser(ServiceAction):
    user: str = ""


@dataclass
class ActionChatJoinedByLink(ServiceAction):
    inviter: str = ""


@dataclass
class ActionChatJoinedByRequest(ServiceAction):
    pass


@dataclass
class ActionChannelCreate(ServiceAction):
    title: str = ""


@dataclass
class ActionChatMigrateTo(ServiceAction):
    channel_id: int = 0


@dataclass
class ActionChannelMigrateFrom(ServiceAction):
    title: str = ""
    chat_id: int = 0


# Messages
@dataclass
class ActionPinMessage(ServiceAction):
    message_id: int = 0


@dataclass
class ActionHistoryClear(ServiceAction):
    pass


# Calls
@dataclass
class ActionPhoneCall(ServiceAction):
    duration: int | None = None
    discard_reason: str | None = None
    is_video: bool = False


@dataclass
class ActionGroupCall(ServiceAction):
    duration: int | None = None


@dataclass
class ActionInviteToGroupCall(ServiceAction):
    users: list[str] = field(default_factory=list)


@dataclass
class ActionGroupCallScheduled(ServiceAction):
    schedule_date: datetime | None = None


# Payments
@dataclass
class ActionGameScore(ServiceAction):
    game: str = ""
    score: int = 0


@dataclass
class ActionPaymentSent(ServiceAction):
    currency: str = ""
    amount: int = 0


@dataclass
class ActionPaymentRefunded(ServiceAction):
    currency: str = ""
    amount: int = 0


@dataclass
class ActionPaidMessagesRefunded(ServiceAction):
    count: int = 0
    stars: int = 0


@dataclass
class ActionPaidMessagesPrice(ServiceAction):
    stars: int = 0


# Security
@dataclass
class ActionScreenshotTaken(ServiceAction):
    pass


@dataclass
class ActionBotAllowed(ServiceAction):
    domain: str | None = None


@dataclass
class ActionSecureValuesSent(ServiceAction):
    types: list[str] = field(default_factory=list)


# Contacts
@dataclass
class ActionContactSignUp(ServiceAction):
    pass


@dataclass
class ActionPhoneNumberRequest(ServiceAction):
    pass


@dataclass
class ActionGeoProximityReached(ServiceAction):
    from_id: int = 0
    to_id: int = 0
    distance: int = 0


# Themes / decoration
@dataclass
class ActionTopicCreate(ServiceAction):
    title: str = ""
    icon_emoji: str | None = None


@dataclass
class ActionTopicEdit(ServiceAction):
    title: str | None = None
    icon_emoji: str | None = None
    is_closed: bool | None = None


@dataclass
class ActionSetChatTheme(ServiceAction):
    theme: str = ""


@dataclass
class ActionSetMessagesTTL(ServiceAction):
    period: int = 0


@dataclass
class ActionSetChatWallPaper(ServiceAction):
    pass


# Platform
@dataclass
class ActionWebViewDataSent(ServiceAction):
    text: str = ""


@dataclass
class ActionRequestedPeer(ServiceAction):
    peer_id: int = 0


# Gifts / premium
@dataclass
class ActionGiftPremium(ServiceAction):
    months: int = 0
    currency: str = ""
    amount: int = 0


@dataclass
class ActionGiftCredits(ServiceAction):
    stars: int = 0
    currency: str = ""
    amount: int = 0


@dataclass
class ActionStarGift(ServiceAction):
    gift_id: int = 0


@dataclass
class ActionGiftCode(ServiceAction):
    months: int = 0
    via_giveaway: bool = False


# Giveaways
@dataclass
class ActionGiveawayLaunch(ServiceAction):
    pass


@dataclass
class ActionGiveawayResults(ServiceAction):
    winners_count: int = 0
    unclaimed_count: int = 0


@dataclass
class ActionPrizeStars(ServiceAction):
    stars: int = 0


# Suggested posts
@dataclass
class ActionSuggestedPostApproval(ServiceAction):
    pass


@dataclass
class ActionSuggestedPostSuccess(ServiceAction):
    pass


@dataclass
class ActionSuggestedPostRefund(ServiceAction):
    pass


# Other
@dataclass
class ActionCustomAction(ServiceAction):
    message: str = ""


@dataclass
class ActionSuggestProfilePhoto(ServiceAction):
    photo: FileInfo | None = None


@dataclass
class ActionBoostApply(ServiceAction):
    boosts: int = 0


@dataclass
class ActionNoForwardsToggle(ServiceAction):
    enabled: bool = False


@dataclass
class ActionNoForwardsRequest(ServiceAction):
    pass


@dataclass
class ActionNewCreatorPending(ServiceAction):
    pass


@dataclass
class ActionChangeCreator(ServiceAction):
    pass


@dataclass
class ActionSuggestBirthday(ServiceAction):
    pass


@dataclass
class ActionTodoCompletions(ServiceAction):
    completed_ids: list[int] = field(default_factory=list)
    incompleted_ids: list[int] = field(default_factory=list)


@dataclass
class ActionTodoAppendTasks(ServiceAction):
    items: list[TodoItem] = field(default_factory=list)


# Action class registry for deserialization
_ACTION_CLASSES: dict[str, type[ServiceAction]] = {
    cls.__name__: cls
    for cls in [
        ActionChatCreate,
        ActionChatEditTitle,
        ActionChatEditPhoto,
        ActionChatDeletePhoto,
        ActionChatAddUser,
        ActionChatDeleteUser,
        ActionChatJoinedByLink,
        ActionChatJoinedByRequest,
        ActionChannelCreate,
        ActionChatMigrateTo,
        ActionChannelMigrateFrom,
        ActionPinMessage,
        ActionHistoryClear,
        ActionPhoneCall,
        ActionGroupCall,
        ActionInviteToGroupCall,
        ActionGroupCallScheduled,
        ActionGameScore,
        ActionPaymentSent,
        ActionPaymentRefunded,
        ActionPaidMessagesRefunded,
        ActionPaidMessagesPrice,
        ActionScreenshotTaken,
        ActionBotAllowed,
        ActionSecureValuesSent,
        ActionContactSignUp,
        ActionPhoneNumberRequest,
        ActionGeoProximityReached,
        ActionTopicCreate,
        ActionTopicEdit,
        ActionSetChatTheme,
        ActionSetMessagesTTL,
        ActionSetChatWallPaper,
        ActionWebViewDataSent,
        ActionRequestedPeer,
        ActionGiftPremium,
        ActionGiftCredits,
        ActionStarGift,
        ActionGiftCode,
        ActionGiveawayLaunch,
        ActionGiveawayResults,
        ActionPrizeStars,
        ActionSuggestedPostApproval,
        ActionSuggestedPostSuccess,
        ActionSuggestedPostRefund,
        ActionCustomAction,
        ActionSuggestProfilePhoto,
        ActionBoostApply,
        ActionNoForwardsToggle,
        ActionNoForwardsRequest,
        ActionNewCreatorPending,
        ActionChangeCreator,
        ActionSuggestBirthday,
        ActionTodoCompletions,
        ActionTodoAppendTasks,
    ]
}


# ---------------------------------------------------------------------------
# JSON serialization
# ---------------------------------------------------------------------------


def encode_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return {"__datetime__": obj.isoformat()}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    raise TypeError(f"Cannot serialize {type(obj)}")


def decode_hook(d: dict[str, Any]) -> dict[str, Any] | datetime:
    if "__datetime__" in d:
        return datetime.fromisoformat(d["__datetime__"])
    return d


def media_to_dict(media: Media) -> dict[str, Any]:
    d = asdict(media)
    d["__media_class__"] = type(media).__name__
    return d


def media_from_dict(d: dict[str, Any]) -> Media:
    class_name = d.pop("__media_class__", None)
    if class_name is None:
        media_type = d.get("type", "unsupported")
        cls = _MEDIA_CLASSES.get(media_type, UnsupportedMedia)
    else:
        # Find class by name
        for c in _MEDIA_CLASSES.values():
            if c.__name__ == class_name:
                cls = c
                break
        else:
            cls = UnsupportedMedia

    # Reconstruct enums and nested objects
    if "type" in d:
        d["type"] = MediaType(d["type"])
    if "file" in d and isinstance(d["file"], dict):
        d["file"] = FileInfo(**d["file"])
    if "question" in d and isinstance(d.get("question"), list):
        d["question"] = [
            TextPart(type=TextType(p["type"]), **{k: v for k, v in p.items() if k != "type"})
            for p in d["question"]
        ]
    if "answers" in d and isinstance(d.get("answers"), list):
        answers = []
        for a in d["answers"]:
            text_parts = [
                TextPart(type=TextType(p["type"]), **{k: v for k, v in p.items() if k != "type"})
                for p in a.get("text", [])
            ]
            answers.append(
                PollAnswer(text=text_parts, voters=a.get("voters", 0), chosen=a.get("chosen", False))
            )
        d["answers"] = answers
    if "items" in d and isinstance(d.get("items"), list) and cls in (TodoListMedia,):
        d["items"] = [TodoItem(**item) for item in d["items"]]

    return cls(**d)


def action_to_dict(action: ServiceAction) -> dict[str, Any]:
    d = asdict(action)
    d["__action_class__"] = type(action).__name__
    return d


def action_from_dict(d: dict[str, Any]) -> ServiceAction:
    class_name = d.pop("__action_class__", None)
    cls = _ACTION_CLASSES.get(class_name, ServiceAction)

    if "photo" in d and isinstance(d["photo"], dict):
        d["photo"] = FileInfo(**d["photo"])
    if "schedule_date" in d and isinstance(d["schedule_date"], dict) and "__datetime__" in d["schedule_date"]:
        d["schedule_date"] = datetime.fromisoformat(d["schedule_date"]["__datetime__"])
    if "items" in d and isinstance(d["items"], list):
        d["items"] = [TodoItem(**item) if isinstance(item, dict) else item for item in d["items"]]

    return cls(**d)


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------


@dataclass
class Message:
    id: int
    chat_id: int
    date: datetime
    edited: datetime | None
    from_id: int | None
    from_name: str
    text: list[TextPart]
    media: Media | None
    action: ServiceAction | None
    reply_to_msg_id: int | None
    reply_to_peer_id: int | None
    forwarded_from: ForwardInfo | None
    reactions: list[Reaction]
    is_outgoing: bool
    signature: str | None
    via_bot_id: int | None
    saved_from_chat_id: int | None
    inline_buttons: list[list[InlineButton]] | None
    topic_id: int | None
    grouped_id: int | None
