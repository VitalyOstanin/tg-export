"""Telethon API wrapper with Takeout support."""

from __future__ import annotations

import logging
from pathlib import Path

from telethon import TelegramClient
from telethon.errors.rpcerrorlist import TakeoutInvalidError, TakeoutRequiredError
from telethon.tl.functions.account import (
    GetAuthorizationsRequest,
    GetSavedRingtonesRequest,
    GetWebAuthorizationsRequest,
)
from telethon.tl.functions.channels import GetLeftChannelsRequest
from telethon.tl.functions.contacts import GetContactsRequest, GetTopPeersRequest
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import InputPeerSelf, InputUserSelf

logger = logging.getLogger(__name__)


class TgApi:
    def __init__(self, session_path: str | Path, api_id: int, api_hash: str, proxy: tuple | None = None):
        kwargs = {}
        if proxy:
            kwargs["proxy"] = proxy
        self.client = TelegramClient(str(session_path), api_id, api_hash, **kwargs)
        self.takeout = None

    async def connect(self):
        await self.client.connect()

    async def disconnect(self):
        if self.takeout:
            self.takeout = None
        result = self.client.disconnect()
        if result is not None:
            await result

    async def start_takeout(self, **kwargs):
        """Start Takeout session. Raises TakeoutInitDelayError if cooldown active.

        Why: previously errors were classified by string matching on the
        message ("takeout" or "invalidat"); a Telethon update or localised
        message would silently break this. Now we catch the explicit
        TakeoutInvalidError/TakeoutRequiredError types and let everything else
        propagate.
        """
        try:
            takeout_ctx = self.client.takeout(**kwargs)
            self.takeout = await takeout_ctx.__aenter__()
            self._takeout_ctx = takeout_ctx
        except (TakeoutInvalidError, TakeoutRequiredError) as e:
            logger.info("Finishing stale takeout session before creating a new one: %s", e)
            try:
                await self.client.end_takeout(success=False)
            except Exception:
                # If end_takeout also fails, clear takeout_id manually so the
                # next takeout() call doesn't hit the same stale id.
                if self.client.session is not None:
                    self.client.session.takeout_id = None
            takeout_ctx = self.client.takeout(**kwargs)
            self.takeout = await takeout_ctx.__aenter__()
            self._takeout_ctx = takeout_ctx

    async def stop_takeout(self, success: bool = True):
        if hasattr(self, "_takeout_ctx") and self._takeout_ctx:
            await self._takeout_ctx.__aexit__(None, None, None)
            self.takeout = None
            self._takeout_ctx = None

    @property
    def _active_client(self):
        """Return takeout client if available, else regular client."""
        return self.takeout if self.takeout else self.client

    async def iter_dialogs(self, archived: bool | None = None):
        """Iterate dialogs. None=all, False=non-archived only, True=archived only."""
        if archived is None:
            async for dialog in self.client.iter_dialogs():
                yield dialog
        else:
            async for dialog in self.client.iter_dialogs(archived=archived):
                yield dialog

    async def get_left_channels(self):
        result = await self.client(GetLeftChannelsRequest(offset=0))
        return result

    async def get_folders(self) -> list[dict]:
        """Get Telegram folders as list of dicts with name, peer_ids, and type flags."""
        result = await self.client(GetDialogFiltersRequest())
        filters = getattr(result, "filters", result) or []
        folders = []
        for f in filters:
            if not hasattr(f, "title"):
                continue
            raw_title = f.title
            title = raw_title.text if hasattr(raw_title, "text") else str(raw_title)
            peer_ids = []
            for peer in getattr(f, "include_peers", []):
                if hasattr(peer, "channel_id"):
                    peer_ids.append(peer.channel_id)
                elif hasattr(peer, "chat_id"):
                    peer_ids.append(peer.chat_id)
                elif hasattr(peer, "user_id"):
                    peer_ids.append(peer.user_id)
            exclude_ids = []
            for peer in getattr(f, "exclude_peers", []):
                if hasattr(peer, "channel_id"):
                    exclude_ids.append(peer.channel_id)
                elif hasattr(peer, "chat_id"):
                    exclude_ids.append(peer.chat_id)
                elif hasattr(peer, "user_id"):
                    exclude_ids.append(peer.user_id)
            folders.append(
                {
                    "name": title,
                    "peer_ids": peer_ids,
                    "exclude_ids": exclude_ids,
                    "contacts": bool(getattr(f, "contacts", False)),
                    "non_contacts": bool(getattr(f, "non_contacts", False)),
                    "groups": bool(getattr(f, "groups", False)),
                    "broadcasts": bool(getattr(f, "broadcasts", False)),
                    "bots": bool(getattr(f, "bots", False)),
                }
            )
        return folders

    async def iter_messages(self, chat_id: int, **kwargs):
        client = self._active_client
        async for msg in client.iter_messages(chat_id, **kwargs):
            yield msg

    async def iter_topic_messages(self, chat_id: int, topic_id: int, min_id: int = 0):
        client = self._active_client
        async for msg in client.iter_messages(chat_id, reply_to=topic_id, min_id=min_id):
            yield msg

    async def get_forum_topics(self, chat_id: int):
        """Get forum topics for a supergroup."""
        from telethon.tl.functions.channels import (
            GetForumTopicsRequest,  # pyright: ignore[reportAttributeAccessIssue]
        )

        result = await self.client(
            GetForumTopicsRequest(
                channel=chat_id,
                offset_date=0,
                offset_id=0,
                offset_topic=0,
                limit=100,
            )
        )
        return result.topics

    async def download_media(self, message, path: Path, progress_cb=None):
        client = self._active_client
        return await client.download_media(message, file=str(path), progress_callback=progress_cb)  # pyright: ignore[reportArgumentType]

    async def get_personal_info(self):
        result = await self.client(GetFullUserRequest(InputUserSelf()))
        return result

    async def get_contacts(self):
        contacts = await self.client(GetContactsRequest(hash=0))
        return contacts

    async def get_sessions(self):
        sessions = await self.client(GetAuthorizationsRequest())
        web_sessions = await self.client(GetWebAuthorizationsRequest())
        return sessions, web_sessions

    async def get_top_peers(self):
        try:
            result = await self.client(
                GetTopPeersRequest(
                    correspondents=True,
                    bots_pm=False,
                    bots_inline=False,
                    phone_calls=False,
                    forward_users=False,
                    forward_chats=False,
                    groups=False,
                    channels=False,
                    bots_app=False,
                    offset=0,
                    limit=100,
                    hash=0,
                )
            )
            return result
        except Exception:
            return None

    async def iter_userpics(self):
        async for photo in self.client.iter_profile_photos("me"):
            yield photo

    async def get_stories(self):
        """Get pinned and archived stories."""
        from telethon.tl.functions.stories import (
            GetPinnedStoriesRequest,
            GetStoriesArchiveRequest,
        )

        pinned = await self.client(
            GetPinnedStoriesRequest(
                peer=InputPeerSelf(),
                offset_id=0,
                limit=100,
            )
        )
        archived = await self.client(
            GetStoriesArchiveRequest(
                peer=InputPeerSelf(),
                offset_id=0,
                limit=100,
            )
        )
        return pinned, archived

    async def get_ringtones(self):
        """Get saved ringtones."""
        result = await self.client(GetSavedRingtonesRequest(hash=0))
        return result
