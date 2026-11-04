"""Telethon to models converter."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from tg_export.format import display_name
from tg_export.models import (
    ActionBotAllowed,
    ActionChannelCreate,
    ActionChannelMigrateFrom,
    ActionChatAddUser,
    ActionChatCreate,
    ActionChatDeletePhoto,
    ActionChatDeleteUser,
    ActionChatEditPhoto,
    ActionChatEditTitle,
    ActionChatJoinedByLink,
    ActionChatMigrateTo,
    ActionContactSignUp,
    ActionCustomAction,
    ActionGameScore,
    ActionGiftPremium,
    ActionGroupCall,
    ActionHistoryClear,
    ActionPaymentSent,
    ActionPhoneCall,
    ActionPinMessage,
    ActionScreenshotTaken,
    ActionSecureValuesSent,
    ActionSetChatTheme,
    ActionSetMessagesTTL,
    ActionTopicCreate,
    ActionTopicEdit,
    Chat,
    ChatType,
    ContactMedia,
    DocumentMedia,
    FileInfo,
    ForwardInfo,
    GameMedia,
    GeoMedia,
    InlineButton,
    InlineButtonType,
    InvoiceMedia,
    Media,
    MediaType,
    Message,
    PhotoMedia,
    PollAnswer,
    PollMedia,
    Reaction,
    ReactionType,
    ServiceAction,
    TextPart,
    TextType,
    UnsupportedMedia,
    VenueMedia,
)

logger = logging.getLogger(__name__)


def peer_id(peer: Any) -> int | None:
    """The numeric id behind a Telethon ``Peer*`` object.

    Each of PeerUser / PeerChat / PeerChannel carries its id under a name of
    its own, and the chain telling them apart was written out five times -- in
    two of them with the branches in a different order, which would answer
    differently for an object carrying more than one of the attributes.
    """
    for attribute in ("channel_id", "chat_id", "user_id"):
        value = getattr(peer, attribute, None)
        if value is not None:
            return value
    return None


def _to_str(val: Any) -> str | None:
    """Convert value to str, handling bytes from Telethon."""
    if val is None:
        return None
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val)


# ---------------------------------------------------------------------------
# Entity type mapping
# ---------------------------------------------------------------------------

# Dispatch here and below goes by class name rather than by isinstance, so this
# module never imports TL classes and survives their absence in another
# Telethon version. The flip side is that a rename upstream raises nothing at
# all -- it just routes the value into the unknown branch. Every name used that
# way is checked against telethon.tl.types by
# tests/test_telethon_contract.py.
ENTITY_MAP = {
    "MessageEntityBold": TextType.bold,
    "MessageEntityItalic": TextType.italic,
    "MessageEntityCode": TextType.code,
    "MessageEntityPre": TextType.pre,
    "MessageEntityUrl": TextType.url,
    "MessageEntityTextUrl": TextType.text_url,
    "MessageEntityMention": TextType.mention,
    "MessageEntityMentionName": TextType.mention_name,
    "MessageEntityHashtag": TextType.hashtag,
    "MessageEntityBotCommand": TextType.bot_command,
    "MessageEntityEmail": TextType.email,
    "MessageEntityPhone": TextType.phone,
    "MessageEntityCashtag": TextType.cashtag,
    "MessageEntityUnderline": TextType.underline,
    "MessageEntityStrike": TextType.strikethrough,
    "MessageEntityBlockquote": TextType.blockquote,
    "MessageEntityBankCard": TextType.bank_card,
    "MessageEntitySpoiler": TextType.spoiler,
    "MessageEntityCustomEmoji": TextType.custom_emoji,
}


# ---------------------------------------------------------------------------
# Entity parsing
# ---------------------------------------------------------------------------


def _utf16_slice(units: bytes, start: int, end: int) -> str:
    """Cut a fragment addressed in UTF-16 code units out of the encoded text."""
    # errors="replace": an offset landing inside a surrogate pair means the
    # server sent boundaries this text does not have; a replacement character
    # is a better outcome than losing the whole message to a decode error.
    return units[start * 2 : end * 2].decode("utf-16-le", errors="replace")


def convert_entities(text: str, entities: list[Any] | None) -> list[TextPart]:
    """Parse Telethon entities into list[TextPart].

    Telegram counts ``offset`` and ``length`` in UTF-16 code units, not in
    Python characters: every character outside the BMP (a colour emoji, part of
    the CJK range, musical signs) takes two units. Indexing the Python string
    directly shifted every offset after the first such character -- the entity
    either failed the bounds check and was dropped together with its formatting,
    or cut out the wrong fragment, leaving a link whose text no longer matched
    its address. Hence the whole scan runs over the UTF-16 encoding.
    """
    if not text:
        return []
    if not entities:
        return [TextPart(type=TextType.text, text=text)]

    units = text.encode("utf-16-le")
    total_units = len(units) // 2

    parts = []
    offset = 0
    for entity in sorted(entities, key=lambda e: e.offset):
        ent_class = entity.__class__.__name__
        ent_type = ENTITY_MAP.get(ent_class, TextType.unknown)
        ent_offset = entity.offset
        ent_length = entity.length

        # Skip overlapping entities (same approach as tdesktop)
        if ent_offset < offset or ent_length <= 0 or ent_offset + ent_length > total_units:
            continue

        if ent_offset > offset:
            parts.append(TextPart(type=TextType.text, text=_utf16_slice(units, offset, ent_offset)))

        ent_text = _utf16_slice(units, ent_offset, ent_offset + ent_length)
        href = getattr(entity, "url", None)
        user_id = getattr(entity, "user_id", None)
        parts.append(TextPart(type=ent_type, text=ent_text, href=href, user_id=user_id))

        offset = ent_offset + ent_length

    if offset < total_units:
        parts.append(TextPart(type=TextType.text, text=_utf16_slice(units, offset, total_units)))

    return parts


def text_links(text: str, entities: list[Any] | None) -> list[dict]:
    """The links hidden under words: ``{offset, length, text, url}`` each.

    Their address lives only in the entity, so a listing printing the bare
    text showed the words and lost the address. A link typed out as an address
    is already in the text and is not listed. ``offset`` and ``length`` stay in
    UTF-16 code units, as Telegram counts them; ``text`` is the fragment they
    cut out. Links overlapping an earlier one or reaching past the text are
    left out, as in `convert_entities`.
    """
    if not text or not entities:
        return []
    units = text.encode("utf-16-le")
    total_units = len(units) // 2
    links = []
    end = 0
    for entity in sorted(entities, key=lambda e: e.offset):
        if entity.__class__.__name__ != "MessageEntityTextUrl":
            continue
        if entity.offset < end or entity.length <= 0 or entity.offset + entity.length > total_units:
            continue
        end = entity.offset + entity.length
        links.append(
            {
                "offset": entity.offset,
                "length": entity.length,
                "text": _utf16_slice(units, entity.offset, end),
                "url": entity.url,
            }
        )
    return links


def with_link_targets(text: str, links: list[dict]) -> str:
    """The text with the address of each link in brackets after its words.

    For a line a person reads. A link whose words already are its address is
    left as it is.
    """
    if not links:
        return text
    units = text.encode("utf-16-le")
    pieces = []
    position = 0
    for link in links:
        end = link["offset"] + link["length"]
        pieces.append(_utf16_slice(units, position, end))
        if link["url"] != link["text"]:
            pieces.append(f" ({link['url']})")
        position = end
    pieces.append(_utf16_slice(units, position, len(units) // 2))
    return "".join(pieces)


# ---------------------------------------------------------------------------
# Media conversion
# ---------------------------------------------------------------------------


def _photo_media(tl_media: Any) -> Media | None:
    """Build a PhotoMedia, taking the size from the largest variant."""
    photo = tl_media.photo
    if photo is None:
        return None
    sizes = getattr(photo, "sizes", []) or []
    largest = sizes[-1] if sizes else None
    w = getattr(largest, "w", 0) if largest else 0
    h = getattr(largest, "h", 0) if largest else 0
    file_size = 0
    if largest is not None:
        ls_cls = largest.__class__.__name__
        if ls_cls == "PhotoSizeProgressive":
            prog_sizes = getattr(largest, "sizes", []) or []
            file_size = max(prog_sizes) if prog_sizes else 0
        elif ls_cls == "PhotoCachedSize":
            file_size = len(getattr(largest, "bytes", b""))
        else:
            file_size = getattr(largest, "size", 0) or 0
    return PhotoMedia(
        type=MediaType.photo,
        file=FileInfo(
            id=photo.id,
            size=file_size,
            name=None,
            mime_type="image/jpeg",
            local_path=None,
        ),
        width=w,
        height=h,
        spoilered=getattr(tl_media, "spoiler", False),
    )


def _document_media(tl_media: Any) -> Media | None:
    """Build a DocumentMedia; the concrete kind comes from the attributes."""
    doc = tl_media.document
    if doc is None:
        return None
    attrs = {a.__class__.__name__: a for a in (doc.attributes or [])}
    media_type, name, duration, w, h = _classify_document(attrs, doc.mime_type)
    return DocumentMedia(
        type=media_type,
        file=FileInfo(
            id=doc.id,
            size=doc.size or 0,
            name=name,
            mime_type=doc.mime_type,
            local_path=None,
        ),
        name=name,
        mime_type=doc.mime_type,
        duration=duration,
        width=w,
        height=h,
        performer=getattr(attrs.get("DocumentAttributeAudio"), "performer", None),
        song_title=getattr(attrs.get("DocumentAttributeAudio"), "title", None),
        sticker_emoji=getattr(attrs.get("DocumentAttributeSticker"), "alt", None),
        spoilered=getattr(tl_media, "spoiler", False),
    )


def _poll_media(tl_media: Any) -> Media | None:
    """Build a PollMedia with the vote count of every answer."""
    poll = tl_media.poll
    results = tl_media.results
    answers = []
    for ans in poll.answers or []:
        ans_text = [
            TextPart(type=TextType.text, text=ans.text.text if hasattr(ans.text, "text") else str(ans.text))
        ]
        voters = 0
        if results and results.results:
            for r in results.results:
                if r.option == ans.option:
                    voters = r.voters or 0
        answers.append(PollAnswer(text=ans_text, voters=voters))
    question_text = poll.question.text if hasattr(poll.question, "text") else str(poll.question)
    return PollMedia(
        type=MediaType.poll,
        file=None,
        question=[TextPart(type=TextType.text, text=question_text)],
        answers=answers,
        total_votes=results.total_voters if results else 0,
        closed=poll.closed or False,
    )


# A factory per Telethon media class, like `_DETAILED_ACTIONS` below: the same
# question -- "which constructor does this class name mean" -- had three
# different answers in this module, one of which the comment there calls the
# worst of them.
_MEDIA_FACTORIES: dict[str, Callable[[Any], Media | None]] = {
    "MessageMediaPhoto": lambda m: _photo_media(m),
    "MessageMediaDocument": lambda m: _document_media(m),
    "MessageMediaPoll": lambda m: _poll_media(m),
    "MessageMediaContact": lambda m: ContactMedia(
        type=MediaType.contact,
        file=None,
        phone=m.phone_number or "",
        first_name=m.first_name or "",
        last_name=m.last_name or "",
        vcard=getattr(m, "vcard", None),
    ),
    "MessageMediaGeo": lambda m: GeoMedia(
        type=MediaType.geo,
        file=None,
        latitude=m.geo.lat,
        longitude=m.geo.long,
    ),
    "MessageMediaVenue": lambda m: VenueMedia(
        type=MediaType.venue,
        file=None,
        latitude=m.geo.lat,
        longitude=m.geo.long,
        title=m.title or "",
        address=m.address or "",
    ),
    "MessageMediaGame": lambda m: GameMedia(
        type=MediaType.game,
        file=None,
        title=m.game.title or "",
        description=m.game.description or "",
        short_name=m.game.short_name or "",
    ),
    "MessageMediaInvoice": lambda m: InvoiceMedia(
        type=MediaType.invoice,
        file=None,
        title=m.title or "",
        description=m.description or "",
        currency=m.currency or "",
        amount=m.total_amount or 0,
        receipt_msg_id=getattr(m, "receipt_msg_id", None),
    ),
}


def convert_media(tl_media: Any) -> Media | None:
    """Convert Telethon media to models.Media subclass."""
    if tl_media is None:
        return None
    build = _MEDIA_FACTORIES.get(tl_media.__class__.__name__)
    if build is None:
        return UnsupportedMedia(type=MediaType.unsupported, file=None)
    return build(tl_media)


def _classify_document(
    attrs: dict[str, Any], mime_type: str | None
) -> tuple[MediaType, str | None, int | None, int | None, int | None]:
    """Classify document type from attributes. Returns (MediaType, name, duration, w, h)."""
    name = None
    duration = None
    w = h = None

    if "DocumentAttributeFilename" in attrs:
        name = attrs["DocumentAttributeFilename"].file_name

    # Before the video branch: what Telegram calls a GIF is an mp4 carrying
    # both DocumentAttributeAnimated and DocumentAttributeVideo, so with the
    # video branch first MediaType.gif and the `gifs/` directory were
    # unreachable -- `media.types: [gif]` downloaded no animations at all,
    # while `[video]` downloaded them along with the videos.
    if "DocumentAttributeAnimated" in attrs or (mime_type and "gif" in mime_type):
        return MediaType.gif, name, None, None, None

    if "DocumentAttributeVideo" in attrs:
        v = attrs["DocumentAttributeVideo"]
        duration = v.duration
        w = v.w
        h = v.h
        if getattr(v, "round_message", False):
            return MediaType.video_note, name, duration, w, h
        return MediaType.video, name, duration, w, h

    if "DocumentAttributeAudio" in attrs:
        a = attrs["DocumentAttributeAudio"]
        duration = a.duration
        if getattr(a, "voice", False):
            return MediaType.voice, name, duration, None, None
        return MediaType.document, name, duration, None, None

    if "DocumentAttributeSticker" in attrs:
        return MediaType.sticker, name, None, None, None

    return MediaType.document, name, duration, w, h


# ---------------------------------------------------------------------------
# Action conversion
# ---------------------------------------------------------------------------


# Service actions that carry no data of their own beyond their kind.
_PLAIN_ACTIONS: dict[str, type[ServiceAction]] = {
    "MessageActionChatEditPhoto": ActionChatEditPhoto,
    "MessageActionChatDeletePhoto": ActionChatDeletePhoto,
    "MessageActionChatAddUser": ActionChatAddUser,
    "MessageActionChatDeleteUser": ActionChatDeleteUser,
    "MessageActionChatJoinedByLink": ActionChatJoinedByLink,
    "MessageActionPinMessage": ActionPinMessage,
    "MessageActionHistoryClear": ActionHistoryClear,
    "MessageActionScreenshotTaken": ActionScreenshotTaken,
    "MessageActionContactSignUp": ActionContactSignUp,
    "MessageActionSecureValuesSent": ActionSecureValuesSent,
}


# Actions that carry fields of their own: a factory per Telethon class name.
# The chain of sixteen `if cls_name == ...` said the same thing at four times
# the length, and `type=` had to be repeated in every branch.
_DETAILED_ACTIONS: dict[str, Callable[[Any], ServiceAction]] = {
    "MessageActionChatCreate": lambda a: ActionChatCreate(title=a.title or ""),
    "MessageActionChatEditTitle": lambda a: ActionChatEditTitle(title=a.title or ""),
    "MessageActionChannelCreate": lambda a: ActionChannelCreate(title=a.title or ""),
    "MessageActionChatMigrateTo": lambda a: ActionChatMigrateTo(channel_id=a.channel_id),
    "MessageActionChannelMigrateFrom": lambda a: ActionChannelMigrateFrom(
        title=a.title or "",
        chat_id=a.chat_id,
    ),
    "MessageActionPhoneCall": lambda a: ActionPhoneCall(
        duration=getattr(a, "duration", None),
        is_video=getattr(a, "video", False),
    ),
    "MessageActionGroupCall": lambda a: ActionGroupCall(duration=getattr(a, "duration", None)),
    "MessageActionGameScore": lambda a: ActionGameScore(score=getattr(a, "score", 0)),
    "MessageActionPaymentSent": lambda a: ActionPaymentSent(
        currency=getattr(a, "currency", ""),
        amount=getattr(a, "total_amount", 0),
    ),
    "MessageActionSetChatTheme": lambda a: ActionSetChatTheme(theme=getattr(a, "emoticon", "")),
    "MessageActionSetMessagesTTL": lambda a: ActionSetMessagesTTL(period=getattr(a, "period", 0)),
    "MessageActionTopicCreate": lambda a: ActionTopicCreate(title=getattr(a, "title", "")),
    "MessageActionTopicEdit": lambda a: ActionTopicEdit(title=getattr(a, "title", None)),
    "MessageActionGiftPremium": lambda a: ActionGiftPremium(
        months=getattr(a, "months", 0),
        currency=getattr(a, "currency", ""),
        amount=getattr(a, "amount", 0),
    ),
    "MessageActionBotAllowed": lambda a: ActionBotAllowed(domain=getattr(a, "domain", None)),
    "MessageActionCustomAction": lambda a: ActionCustomAction(message=getattr(a, "message", "")),
}


def convert_action(tl_action: Any) -> ServiceAction | None:
    """Convert Telethon action to ServiceAction subclass."""
    if tl_action is None:
        return None

    cls_name = tl_action.__class__.__name__

    plain = _PLAIN_ACTIONS.get(cls_name)
    if plain is not None:
        return plain()

    detailed = _DETAILED_ACTIONS.get(cls_name)
    if detailed is not None:
        return detailed(tl_action)

    # Normalise to "ActionXxx" so renderer's elif-chain matches consistently.
    normalised = (
        cls_name.replace("MessageAction", "Action", 1) if cls_name.startswith("MessageAction") else cls_name
    )
    return ServiceAction(type=normalised)


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------


# Three kinds of reaction differ in three fields of one record, so a table says
# it where an if/elif chain repeated the call.
_REACTION_FACTORIES: dict[str, Callable[[Any, int], Reaction]] = {
    "ReactionEmoji": lambda reaction, count: Reaction(
        type=ReactionType.emoji, emoji=reaction.emoticon, document_id=None, count=count
    ),
    "ReactionCustomEmoji": lambda reaction, count: Reaction(
        type=ReactionType.custom_emoji, emoji=None, document_id=reaction.document_id, count=count
    ),
    "ReactionPaid": lambda reaction, count: Reaction(
        type=ReactionType.paid, emoji=None, document_id=None, count=count
    ),
}


def convert_reactions(tl_reactions: Any) -> list[Reaction]:
    """Convert Telethon reactions to list[Reaction]."""
    if tl_reactions is None:
        return []
    results_list = getattr(tl_reactions, "results", None)
    if not results_list:
        return []
    reactions = []
    for r in results_list:
        build = _REACTION_FACTORIES.get(r.reaction.__class__.__name__)
        if build is not None:
            reactions.append(build(r.reaction, r.count))
    return reactions


# ---------------------------------------------------------------------------
# Message conversion
# ---------------------------------------------------------------------------


def _convert_reply(tl_msg: Any) -> tuple[int | None, int | None, int | None]:
    """Return (reply_to_msg_id, reply_to_peer_id, topic_id) of a message."""
    reply_to_msg_id = None
    reply_to_peer_id = None
    topic_id = None
    if tl_msg.reply_to:
        reply_to_msg_id = getattr(tl_msg.reply_to, "reply_to_msg_id", None)
        reply_to_peer_id = getattr(tl_msg.reply_to, "reply_to_peer_id", None)
        if reply_to_peer_id is not None:
            reply_to_peer_id = peer_id(reply_to_peer_id)
        if getattr(tl_msg.reply_to, "forum_topic", False):
            topic_id = reply_to_msg_id
    return reply_to_msg_id, reply_to_peer_id, topic_id


def reply_target(tl_msg: Any) -> tuple[int | None, int | None]:
    """The (message id, peer id) a message answers, for a listing; Nones when none.

    The peer is set only when the answered message lives in another chat. A
    message inside a forum topic carries a reply header pointing at the topic's
    first message even when it answers nothing; that header names the topic,
    not an answer, and gives Nones here. A reply to a story is not a reply to
    a message either.
    """
    header = getattr(tl_msg, "reply_to", None)
    if header is None or header.__class__.__name__ == "MessageReplyStoryHeader":
        return None, None
    if getattr(header, "forum_topic", False) and getattr(header, "reply_to_top_id", None) is None:
        return None, None
    peer = getattr(header, "reply_to_peer_id", None)
    return getattr(header, "reply_to_msg_id", None), peer_id(peer) if peer is not None else None


def reply_mark(reply_to: int | None, reply_to_peer: int | None) -> str:
    """The reply sign of a listing line: ``↪ 4708``, ``↪ 1234567890/4708`` for another chat, or nothing."""
    if reply_to is None:
        return ""
    return f"↪ {reply_to_peer}/{reply_to}" if reply_to_peer is not None else f"↪ {reply_to}"


def _convert_forward(tl_msg: Any) -> ForwardInfo | None:
    """Who the message was forwarded from, when Telegram discloses it."""
    if not tl_msg.fwd_from:
        return None
    fwd = tl_msg.fwd_from
    fwd_from_id = None
    if getattr(fwd, "from_id", None):
        fwd_from_id = peer_id(fwd.from_id)
    return ForwardInfo(
        from_id=fwd_from_id,
        from_name=getattr(fwd, "from_name", None),
        date=getattr(fwd, "date", None),
    )


def _convert_inline_buttons(tl_msg: Any) -> list[list[InlineButton]] | None:
    """Rows of inline buttons attached to a message."""
    if not tl_msg.reply_markup or not hasattr(tl_msg.reply_markup, "rows"):
        return None
    rows = []
    for row in tl_msg.reply_markup.rows:
        rows.append(
            [
                InlineButton(
                    type=_classify_button(kind := _button_kind(btn)),
                    text=btn.text or "",
                    data=_to_str(getattr(kind, "url", None) or getattr(kind, "data", None)),
                )
                for btn in row.buttons
            ]
        )
    return rows


def convert_message(tl_msg: Any, chat_id: int) -> Message:
    """Convert Telethon Message to models.Message."""
    # Sender
    from_id = None
    from_name = ""
    if tl_msg.from_id:
        from_id = peer_id(tl_msg.from_id)

    # Resolve sender name from cached entity
    sender = getattr(tl_msg, "sender", None)
    if sender:
        from_name = _user_display_name(sender)

    # Text with entities
    text_parts = convert_entities(tl_msg.message, tl_msg.entities)

    # Media
    media = convert_media(tl_msg.media)

    # Action
    action = convert_action(tl_msg.action)

    reply_to_msg_id, reply_to_peer_id, topic_id = _convert_reply(tl_msg)

    forwarded_from = _convert_forward(tl_msg)

    # Reactions
    reactions = convert_reactions(tl_msg.reactions)

    inline_buttons = _convert_inline_buttons(tl_msg)

    return Message(
        id=tl_msg.id,
        chat_id=chat_id,
        date=tl_msg.date or datetime(1970, 1, 1),
        edited=tl_msg.edit_date,
        from_id=from_id,
        from_name=from_name,
        text=text_parts,
        media=media,
        action=action,
        reply_to_msg_id=reply_to_msg_id,
        reply_to_peer_id=reply_to_peer_id,
        forwarded_from=forwarded_from,
        reactions=reactions,
        is_outgoing=bool(tl_msg.out),
        signature=tl_msg.post_author,
        via_bot_id=tl_msg.via_bot_id,
        saved_from_chat_id=None,
        inline_buttons=inline_buttons,
        topic_id=topic_id,
        grouped_id=tl_msg.grouped_id,
    )


# Kind of a keyboard button by its Telethon class, like the other tables of this
# module. It used to be built inside the function, which runs once per button of
# every row of every message.
#
# Telethon has two shapes of buttons. Up to 1.44 (layer 227) the kind is the
# class of the button itself, and the url or data are its fields. From 1.45
# (layer 229) every button is a bare KeyboardButton (reply keyboard) or
# KeyboardInlineButton (inline keyboard) whose `type` field holds a
# ButtonType* or InlineButtonType* object with the kind and its payload. Both
# generations stay in the table while pyproject admits Telethon below 1.45.
_BUTTON_TYPES = {
    # Telethon <= 1.44
    "KeyboardButtonUrl": InlineButtonType.url,
    "KeyboardButtonCallback": InlineButtonType.callback,
    "KeyboardButtonGame": InlineButtonType.game,
    "KeyboardButtonBuy": InlineButtonType.buy,
    "KeyboardButtonSwitchInline": InlineButtonType.switch_inline,
    "KeyboardButtonWebView": InlineButtonType.web_view,
    "KeyboardButtonSimpleWebView": InlineButtonType.simple_web_view,
    "KeyboardButtonUserProfile": InlineButtonType.user_profile,
    "KeyboardButtonRequestPhone": InlineButtonType.request_phone,
    "KeyboardButtonRequestGeoLocation": InlineButtonType.request_location,
    "KeyboardButtonRequestPoll": InlineButtonType.request_poll,
    "KeyboardButtonRequestPeer": InlineButtonType.request_peer,
    "KeyboardButtonCopy": InlineButtonType.copy_text,
    # Telethon >= 1.45: the class of `button.type`
    "InlineButtonTypeUrl": InlineButtonType.url,
    "InlineButtonTypeCallback": InlineButtonType.callback,
    "InlineButtonTypeGame": InlineButtonType.game,
    "InlineButtonTypeBuy": InlineButtonType.buy,
    "InlineButtonTypeSwitchInline": InlineButtonType.switch_inline,
    "InlineButtonTypeWebView": InlineButtonType.web_view,
    "ButtonTypeSimpleWebView": InlineButtonType.simple_web_view,
    "InlineButtonTypeUserProfile": InlineButtonType.user_profile,
    "ButtonTypeRequestPhone": InlineButtonType.request_phone,
    "ButtonTypeRequestGeoLocation": InlineButtonType.request_location,
    "ButtonTypeRequestPoll": InlineButtonType.request_poll,
    "ButtonTypeRequestPeer": InlineButtonType.request_peer,
    "InlineButtonTypeCopy": InlineButtonType.copy_text,
}

# Buttons of layer 229 that carry their kind in the `type` field. The reply one
# has the same name as the plain button of the older layer, which has no `type`.
_TYPED_BUTTON_CLASSES = frozenset({"KeyboardButton", "KeyboardInlineButton"})


def _button_kind(btn: Any) -> Any:
    """The object whose class names the kind of the button and whose fields hold its url or data."""
    if btn.__class__.__name__ in _TYPED_BUTTON_CLASSES:
        kind = getattr(btn, "type", None)
        if kind is not None:
            return kind
    return btn


def _classify_button(kind: Any) -> InlineButtonType:
    return _BUTTON_TYPES.get(kind.__class__.__name__, InlineButtonType.default)


# ---------------------------------------------------------------------------
# Chat conversion
# ---------------------------------------------------------------------------


def convert_chat(tl_dialog: Any, folder: str | None = None) -> Chat:
    """Convert Telethon Dialog to models.Chat."""
    entity = tl_dialog.entity

    chat_type = _classify_chat(entity)

    return Chat(
        id=entity.id,
        name=getattr(entity, "title", None) or _user_display_name(entity),
        type=chat_type,
        username=getattr(entity, "username", None),
        folder=folder,
        members_count=getattr(entity, "participants_count", None),
        last_message_date=getattr(tl_dialog, "date", None),
        messages_count=_get_messages_count(tl_dialog),
        is_left=bool(getattr(entity, "left", False)) or _is_forbidden(entity),
        is_archived=False,
        is_forum=getattr(entity, "forum", False),
        migrated_to_id=_extract_migrated_to(entity),
        migrated_from_id=None,
        is_monoforum=getattr(entity, "monoforum", False),
    )


def _get_messages_count(tl_dialog: Any) -> int:
    """Extract approximate messages count from Telethon Dialog.

    Uses top_message ID from the underlying TL dialog as approximation.
    Falls back to 0 if not available.
    """
    # TL Dialog.top_message is the ID of the last message (~= total for most chats)
    try:
        tl = getattr(tl_dialog, "dialog", None)
        if tl is not None:
            top = getattr(tl, "top_message", None)
            if isinstance(top, int) and top > 0:
                return top
    except (TypeError, AttributeError) as e:
        # An entity whose attribute access itself fails: the count then falls
        # back to zero, which is indistinguishable from "top_message did not
        # come" unless the reason is written down, as the neighbouring
        # classification does.
        logger.debug("message count of a dialog is unreadable (%s) -- counted as zero", e)
    return 0


def _extract_migrated_to(entity: Any) -> int | None:
    """Extract channel_id from migrated_to InputChannel, if present."""
    migrated_to = getattr(entity, "migrated_to", None)
    if migrated_to is None:
        return None
    return getattr(migrated_to, "channel_id", None)


# Telegram returns these instead of Chat/Channel when the account was kicked or
# banned: the title is still known, but history and members are inaccessible.
_FORBIDDEN_ENTITY_CLASSES = ("ChatForbidden", "ChannelForbidden")


def _is_forbidden(entity: Any) -> bool:
    return entity.__class__.__name__ in _FORBIDDEN_ENTITY_CLASSES


def _classify_chat(entity: Any) -> ChatType:
    cls_name = entity.__class__.__name__

    if cls_name == "User":
        if getattr(entity, "is_self", False):
            return ChatType.self
        if getattr(entity, "bot", False):
            return ChatType.bot
        return ChatType.personal

    if cls_name in ("Chat", "ChatForbidden"):
        return ChatType.private_group

    if cls_name in ("Channel", "ChannelForbidden"):
        is_megagroup = getattr(entity, "megagroup", False)
        has_username = bool(getattr(entity, "username", None))
        if is_megagroup:
            return ChatType.public_supergroup if has_username else ChatType.private_supergroup
        return ChatType.public_channel if has_username else ChatType.private_channel

    # Unknown entity class: log it instead of silently bucketing as personal,
    # so a new Telegram entity type surfaces in --debug rather than hiding.
    logger.warning("Unknown entity class %r classified as personal", cls_name)
    return ChatType.personal


def _user_display_name(entity: Any) -> str:
    return display_name(entity, missing="Unknown")
