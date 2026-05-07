"""Telethon to models converter."""

from __future__ import annotations

from datetime import datetime
from typing import Any

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


def convert_entities(text: str, entities: list | None) -> list[TextPart]:
    """Parse Telethon entities into list[TextPart]."""
    if not text:
        return []
    if not entities:
        return [TextPart(type=TextType.text, text=text)]

    parts = []
    offset = 0
    for entity in sorted(entities, key=lambda e: e.offset):
        ent_class = entity.__class__.__name__
        ent_type = ENTITY_MAP.get(ent_class, TextType.unknown)
        ent_offset = entity.offset
        ent_length = entity.length

        # Skip overlapping entities (same approach as tdesktop)
        if ent_offset < offset or ent_length <= 0 or ent_offset + ent_length > len(text):
            continue

        # Add plain text before this entity
        if ent_offset > offset:
            parts.append(TextPart(type=TextType.text, text=text[offset:ent_offset]))

        ent_text = text[ent_offset : ent_offset + ent_length]
        href = getattr(entity, "url", None)
        user_id = getattr(entity, "user_id", None)
        parts.append(TextPart(type=ent_type, text=ent_text, href=href, user_id=user_id))

        offset = ent_offset + ent_length

    # Remaining text
    if offset < len(text):
        parts.append(TextPart(type=TextType.text, text=text[offset:]))

    return parts


# ---------------------------------------------------------------------------
# Media conversion
# ---------------------------------------------------------------------------


def convert_media(tl_media: Any) -> Media | None:
    """Convert Telethon media to models.Media subclass."""
    if tl_media is None:
        return None

    cls_name = tl_media.__class__.__name__

    if cls_name == "MessageMediaPhoto":
        photo = tl_media.photo
        if photo is None:
            return None
        sizes = getattr(photo, "sizes", []) or []
        largest = sizes[-1] if sizes else None
        w = getattr(largest, "w", 0) if largest else 0
        h = getattr(largest, "h", 0) if largest else 0
        # Extract file size from PhotoSize variant
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

    if cls_name == "MessageMediaDocument":
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

    if cls_name == "MessageMediaContact":
        return ContactMedia(
            type=MediaType.contact,
            file=None,
            phone=tl_media.phone_number or "",
            first_name=tl_media.first_name or "",
            last_name=tl_media.last_name or "",
            vcard=getattr(tl_media, "vcard", None),
        )

    if cls_name == "MessageMediaGeo":
        geo = tl_media.geo
        return GeoMedia(
            type=MediaType.geo,
            file=None,
            latitude=geo.lat,
            longitude=geo.long,
        )

    if cls_name == "MessageMediaVenue":
        geo = tl_media.geo
        return VenueMedia(
            type=MediaType.venue,
            file=None,
            latitude=geo.lat,
            longitude=geo.long,
            title=tl_media.title or "",
            address=tl_media.address or "",
        )

    if cls_name == "MessageMediaPoll":
        poll = tl_media.poll
        results = tl_media.results
        answers = []
        for ans in poll.answers or []:
            ans_text = [
                TextPart(
                    type=TextType.text, text=ans.text.text if hasattr(ans.text, "text") else str(ans.text)
                )
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

    if cls_name == "MessageMediaGame":
        game = tl_media.game
        return GameMedia(
            type=MediaType.game,
            file=None,
            title=game.title or "",
            description=game.description or "",
            short_name=game.short_name or "",
        )

    if cls_name == "MessageMediaInvoice":
        return InvoiceMedia(
            type=MediaType.invoice,
            file=None,
            title=tl_media.title or "",
            description=tl_media.description or "",
            currency=tl_media.currency or "",
            amount=tl_media.total_amount or 0,
            receipt_msg_id=getattr(tl_media, "receipt_msg_id", None),
        )

    return UnsupportedMedia(type=MediaType.unsupported, file=None)


def _classify_document(attrs: dict, mime_type: str | None) -> tuple:
    """Classify document type from attributes. Returns (MediaType, name, duration, w, h)."""
    name = None
    duration = None
    w = h = None

    if "DocumentAttributeFilename" in attrs:
        name = attrs["DocumentAttributeFilename"].file_name

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

    if "DocumentAttributeAnimated" in attrs or (mime_type and "gif" in mime_type):
        return MediaType.gif, name, None, None, None

    return MediaType.document, name, duration, w, h


# ---------------------------------------------------------------------------
# Action conversion
# ---------------------------------------------------------------------------


def convert_action(tl_action: Any) -> ServiceAction | None:
    """Convert Telethon action to ServiceAction subclass."""
    if tl_action is None:
        return None

    cls_name = tl_action.__class__.__name__

    if cls_name == "MessageActionChatCreate":
        return ActionChatCreate(type="ActionChatCreate", title=tl_action.title or "")
    if cls_name == "MessageActionChatEditTitle":
        return ActionChatEditTitle(type="ActionChatEditTitle", title=tl_action.title or "")
    if cls_name == "MessageActionChatEditPhoto":
        return ActionChatEditPhoto(type="ActionChatEditPhoto")
    if cls_name == "MessageActionChatDeletePhoto":
        return ActionChatDeletePhoto(type="ActionChatDeletePhoto")
    if cls_name == "MessageActionChatAddUser":
        return ActionChatAddUser(type="ActionChatAddUser")
    if cls_name == "MessageActionChatDeleteUser":
        return ActionChatDeleteUser(type="ActionChatDeleteUser")
    if cls_name == "MessageActionChatJoinedByLink":
        return ActionChatJoinedByLink(type="ActionChatJoinedByLink")
    if cls_name == "MessageActionChannelCreate":
        return ActionChannelCreate(type="ActionChannelCreate", title=tl_action.title or "")
    if cls_name == "MessageActionChatMigrateTo":
        return ActionChatMigrateTo(type="ActionChatMigrateTo", channel_id=tl_action.channel_id)
    if cls_name == "MessageActionChannelMigrateFrom":
        return ActionChannelMigrateFrom(
            type="ActionChannelMigrateFrom",
            title=tl_action.title or "",
            chat_id=tl_action.chat_id,
        )
    if cls_name == "MessageActionPinMessage":
        return ActionPinMessage(type="ActionPinMessage")
    if cls_name == "MessageActionHistoryClear":
        return ActionHistoryClear(type="ActionHistoryClear")
    if cls_name == "MessageActionPhoneCall":
        return ActionPhoneCall(
            type="ActionPhoneCall",
            duration=getattr(tl_action, "duration", None),
            is_video=getattr(tl_action, "video", False),
        )
    if cls_name == "MessageActionGroupCall":
        return ActionGroupCall(
            type="ActionGroupCall",
            duration=getattr(tl_action, "duration", None),
        )
    if cls_name == "MessageActionScreenshotTaken":
        return ActionScreenshotTaken(type="ActionScreenshotTaken")
    if cls_name == "MessageActionContactSignUp":
        return ActionContactSignUp(type="ActionContactSignUp")
    if cls_name == "MessageActionGameScore":
        return ActionGameScore(
            type="ActionGameScore",
            score=getattr(tl_action, "score", 0),
        )
    if cls_name == "MessageActionPaymentSent":
        return ActionPaymentSent(
            type="ActionPaymentSent",
            currency=getattr(tl_action, "currency", ""),
            amount=getattr(tl_action, "total_amount", 0),
        )
    if cls_name == "MessageActionSetChatTheme":
        return ActionSetChatTheme(
            type="ActionSetChatTheme",
            theme=getattr(tl_action, "emoticon", ""),
        )
    if cls_name == "MessageActionSetMessagesTTL":
        return ActionSetMessagesTTL(
            type="ActionSetMessagesTTL",
            period=getattr(tl_action, "period", 0),
        )
    if cls_name == "MessageActionTopicCreate":
        return ActionTopicCreate(
            type="ActionTopicCreate",
            title=getattr(tl_action, "title", ""),
        )
    if cls_name == "MessageActionTopicEdit":
        return ActionTopicEdit(
            type="ActionTopicEdit",
            title=getattr(tl_action, "title", None),
        )
    if cls_name == "MessageActionGiftPremium":
        return ActionGiftPremium(
            type="ActionGiftPremium",
            months=getattr(tl_action, "months", 0),
            currency=getattr(tl_action, "currency", ""),
            amount=getattr(tl_action, "amount", 0),
        )
    if cls_name == "MessageActionBotAllowed":
        return ActionBotAllowed(
            type="ActionBotAllowed",
            domain=getattr(tl_action, "domain", None),
        )
    if cls_name == "MessageActionSecureValuesSent":
        return ActionSecureValuesSent(type="ActionSecureValuesSent")
    if cls_name == "MessageActionCustomAction":
        return ActionCustomAction(
            type="ActionCustomAction",
            message=getattr(tl_action, "message", ""),
        )

    # Normalise to "ActionXxx" so renderer's elif-chain matches consistently.
    normalised = (
        cls_name.replace("MessageAction", "Action", 1) if cls_name.startswith("MessageAction") else cls_name
    )
    return ServiceAction(type=normalised)


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------


def convert_reactions(tl_reactions: Any) -> list[Reaction]:
    """Convert Telethon reactions to list[Reaction]."""
    if tl_reactions is None:
        return []
    results_list = getattr(tl_reactions, "results", None)
    if not results_list:
        return []
    reactions = []
    for r in results_list:
        reaction = r.reaction
        cls_name = reaction.__class__.__name__
        if cls_name == "ReactionEmoji":
            reactions.append(
                Reaction(
                    type=ReactionType.emoji,
                    emoji=reaction.emoticon,
                    document_id=None,
                    count=r.count,
                )
            )
        elif cls_name == "ReactionCustomEmoji":
            reactions.append(
                Reaction(
                    type=ReactionType.custom_emoji,
                    emoji=None,
                    document_id=reaction.document_id,
                    count=r.count,
                )
            )
        elif cls_name == "ReactionPaid":
            reactions.append(
                Reaction(
                    type=ReactionType.paid,
                    emoji=None,
                    document_id=None,
                    count=r.count,
                )
            )
    return reactions


# ---------------------------------------------------------------------------
# Message conversion
# ---------------------------------------------------------------------------


def convert_message(tl_msg: Any, chat_id: int) -> Message:
    """Convert Telethon Message to models.Message."""
    # Sender
    from_id = None
    from_name = ""
    if tl_msg.from_id:
        if hasattr(tl_msg.from_id, "user_id"):
            from_id = tl_msg.from_id.user_id
        elif hasattr(tl_msg.from_id, "channel_id"):
            from_id = tl_msg.from_id.channel_id

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

    # Reply
    reply_to_msg_id = None
    reply_to_peer_id = None
    topic_id = None
    if tl_msg.reply_to:
        reply_to_msg_id = getattr(tl_msg.reply_to, "reply_to_msg_id", None)
        reply_to_peer_id = getattr(tl_msg.reply_to, "reply_to_peer_id", None)
        if reply_to_peer_id is not None:
            if hasattr(reply_to_peer_id, "channel_id"):
                reply_to_peer_id = reply_to_peer_id.channel_id
            elif hasattr(reply_to_peer_id, "chat_id"):
                reply_to_peer_id = reply_to_peer_id.chat_id
            elif hasattr(reply_to_peer_id, "user_id"):
                reply_to_peer_id = reply_to_peer_id.user_id
        if getattr(tl_msg.reply_to, "forum_topic", False):
            topic_id = reply_to_msg_id

    # Forward
    forwarded_from = None
    if tl_msg.fwd_from:
        fwd = tl_msg.fwd_from
        fwd_from_id = None
        fwd_from_name = getattr(fwd, "from_name", None)
        if hasattr(fwd, "from_id") and fwd.from_id:
            if hasattr(fwd.from_id, "user_id"):
                fwd_from_id = fwd.from_id.user_id
            elif hasattr(fwd.from_id, "channel_id"):
                fwd_from_id = fwd.from_id.channel_id
        forwarded_from = ForwardInfo(
            from_id=fwd_from_id,
            from_name=fwd_from_name,
            date=getattr(fwd, "date", None),
        )

    # Reactions
    reactions = convert_reactions(tl_msg.reactions)

    # Inline buttons
    inline_buttons = None
    if tl_msg.reply_markup and hasattr(tl_msg.reply_markup, "rows"):
        inline_buttons = []
        for row in tl_msg.reply_markup.rows:
            btn_row = []
            for btn in row.buttons:
                btn_type = _classify_button(btn)
                btn_row.append(
                    InlineButton(
                        type=btn_type,
                        text=btn.text or "",
                        data=_to_str(getattr(btn, "url", None) or getattr(btn, "data", None)),
                    )
                )
            inline_buttons.append(btn_row)

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


def _classify_button(btn: Any) -> InlineButtonType:
    cls_name = btn.__class__.__name__
    mapping = {
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
    }
    return mapping.get(cls_name, InlineButtonType.default)


# ---------------------------------------------------------------------------
# Chat conversion
# ---------------------------------------------------------------------------


def convert_chat(tl_dialog: Any, folder: str | None = None) -> Chat:
    """Convert Telethon Dialog to models.Chat."""
    entity = tl_dialog.entity

    chat_type = _classify_chat(entity, tl_dialog)

    return Chat(
        id=entity.id,
        name=getattr(entity, "title", None) or _user_display_name(entity),
        type=chat_type,
        username=getattr(entity, "username", None),
        folder=folder,
        members_count=getattr(entity, "participants_count", None),
        last_message_date=getattr(tl_dialog, "date", None),
        messages_count=_get_messages_count(tl_dialog),
        is_left=getattr(entity, "left", False),
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
    except (TypeError, AttributeError):
        pass
    return 0


def _extract_migrated_to(entity: Any) -> int | None:
    """Extract channel_id from migrated_to InputChannel, if present."""
    migrated_to = getattr(entity, "migrated_to", None)
    if migrated_to is None:
        return None
    return getattr(migrated_to, "channel_id", None)


def _classify_chat(entity: Any, dialog: Any) -> ChatType:
    cls_name = entity.__class__.__name__

    if cls_name == "User":
        if getattr(entity, "is_self", False):
            return ChatType.self
        if getattr(entity, "bot", False):
            return ChatType.bot
        return ChatType.personal

    if cls_name == "Chat":
        return ChatType.private_group

    if cls_name == "Channel":
        is_megagroup = getattr(entity, "megagroup", False)
        has_username = bool(getattr(entity, "username", None))
        if is_megagroup:
            return ChatType.public_supergroup if has_username else ChatType.private_supergroup
        return ChatType.public_channel if has_username else ChatType.private_channel

    return ChatType.personal


def _user_display_name(entity: Any) -> str:
    first = getattr(entity, "first_name", "") or ""
    last = getattr(entity, "last_name", "") or ""
    return f"{first} {last}".strip() or "Unknown"
