"""Telethon API wrapper with Takeout support."""

from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator

from telethon import TelegramClient
from telethon.errors import TakeoutInitDelayError
from telethon.tl.functions.contacts import GetContactsRequest
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.functions.channels import GetLeftChannelsRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.functions.account import GetAuthorizationsRequest, GetWebAuthorizationsRequest
from telethon.tl.types import InputUserSelf


class TgApi:
    def __init__(self, session_path: str | Path, api_id: int, api_hash: str):
        self.client = TelegramClient(str(session_path), api_id, api_hash)
        self.takeout = None

    async def connect(self):
        await self.client.connect()

    async def disconnect(self):
        if self.takeout:
            self.takeout = None
        await self.client.disconnect()

    async def start_takeout(self, **kwargs):
        """Start Takeout session. Raises TakeoutInitDelayError if cooldown active."""
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

    async def get_folders(self) -> dict[str, list[int]]:
        """Get Telegram folders as {name: [chat_ids]}."""
        result = await self.client(GetDialogFiltersRequest())
        filters = getattr(result, "filters", result) or []
        folders = {}
        for f in filters:
            if hasattr(f, "title") and hasattr(f, "include_peers"):
                raw_title = f.title
                title = raw_title.text if hasattr(raw_title, "text") else str(raw_title)
                peer_ids = []
                for peer in f.include_peers:
                    if hasattr(peer, "channel_id"):
                        peer_ids.append(peer.channel_id)
                    elif hasattr(peer, "chat_id"):
                        peer_ids.append(peer.chat_id)
                    elif hasattr(peer, "user_id"):
                        peer_ids.append(peer.user_id)
                folders[title] = peer_ids
        return folders

    async def iter_messages(self, chat_id: int, min_id: int = 0):
        client = self._active_client
        async for msg in client.iter_messages(chat_id, min_id=min_id):
            yield msg

    async def iter_topic_messages(self, chat_id: int, topic_id: int, min_id: int = 0):
        client = self._active_client
        async for msg in client.iter_messages(chat_id, reply_to=topic_id, min_id=min_id):
            yield msg

    async def get_forum_topics(self, chat_id: int):
        """Get forum topics for a supergroup."""
        from telethon.tl.functions.channels import GetForumTopicsRequest
        result = await self.client(GetForumTopicsRequest(
            channel=chat_id, offset_date=0, offset_id=0, offset_topic=0, limit=100,
        ))
        return result.topics

    async def download_media(self, message, path: Path, progress_cb=None):
        client = self._active_client
        return await client.download_media(message, file=str(path), progress_callback=progress_cb)

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

    async def iter_userpics(self):
        async for photo in self.client.iter_profile_photos("me"):
            yield photo

    async def iter_stories(self):
        # Stories API via raw request if available
        pass

    async def iter_profile_music(self):
        # Profile music not directly available via standard API
        pass
