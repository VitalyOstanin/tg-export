"""Telethon API wrapper with Takeout support."""

from __future__ import annotations

import contextlib
import importlib.util
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
from telethon.tl.functions.updates import GetStateRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import InputPeerSelf, InputUserSelf

from tg_export.locking import ProcessLock
from tg_export.session import FixedSQLiteSession

logger = logging.getLogger(__name__)


class TgApi:
    def __init__(self, session_path: str | Path, api_id: int, api_hash: str, proxy: tuple | None = None):
        kwargs = {}
        if proxy:
            # Telethon silently ignores `proxy=` when python-socks is missing
            # (only warns), connecting directly and leaking the real IP. Fail
            # fast instead so a configured proxy is never bypassed unnoticed.
            if importlib.util.find_spec("python_socks") is None:
                raise RuntimeError(
                    "Proxy is configured, but the 'python-socks' package is not "
                    "installed. Telethon would ignore the proxy and connect "
                    "directly, exposing the real IP. Install the proxy extra: "
                    "pip install 'tg-export[proxy]' (or uv pip install 'tg-export[proxy]')."
                )
            kwargs["proxy"] = proxy
        session = FixedSQLiteSession(str(session_path))
        self.client = TelegramClient(session, api_id, api_hash, **kwargs)
        self.takeout = None
        self._takeout_stack: contextlib.AsyncExitStack | None = None
        # The state-database lock covers an output directory, not an account,
        # so it never stopped two processes from using one session file --
        # `tg send` next to a running export, or a second export with a
        # different --output. Telethon keeps no lock of its own and answers
        # concurrent use by corrupting the authorisation key in the file.
        self._session_lock = ProcessLock(
            Path(str(session_path)),
            f"Telegram session {session_path} is in use by another tg-export process. "
            f"Wait for it to finish, or use a different account.",
        )

    async def connect(self):
        self._session_lock.acquire()
        try:
            await self.client.connect()
        except BaseException:
            self._session_lock.release()
            raise

    async def disconnect(self):
        # Release the takeout context first: it must not outlive the connection
        # it is proxying. Releasing keeps the takeout session alive on the
        # server -- see stop_takeout.
        with contextlib.suppress(Exception):
            await self.stop_takeout()
        try:
            result = self.client.disconnect()
            if result is not None:
                await result
        finally:
            self._session_lock.release()

    async def start_takeout(self, **kwargs):
        """Open a Takeout session, reusing the one a previous run left behind.

        Telegram answers ``InitTakeoutSessionRequest`` with a cooldown of up to
        24 hours, so an id must never be discarded while it still works. Reuse
        has two conditions imposed by Telethon's ``AccountMethods.takeout``:
        the init request is built whenever the stored id is empty *or* any
        argument is set, and ``_TakeoutClient.__aenter__`` refuses to send that
        request over a live id. Hence the reuse call passes no argument at all,
        and the export parameters are only supplied when a session is created
        from scratch.

        Raises TakeoutInitDelayError when the cooldown is active.
        """
        session = self.client.session
        stored_id = getattr(session, "takeout_id", None) if session is not None else None

        if stored_id is not None:
            if await self._resume_takeout(stored_id):
                return
            await self._discard_takeout_id()

        await self._enter_takeout(kwargs)

    async def _resume_takeout(self, stored_id) -> bool:
        """Return True when the stored takeout_id is still usable.

        Entering the context never reaches the server while an id is stored, so
        a takeout the server has already forgotten would only surface on the
        first export request, deep inside the run. One cheap probe request
        settles it here instead.
        """
        stack = contextlib.AsyncExitStack()
        try:
            takeout = await stack.enter_async_context(self.client.takeout(finalize=False))
            await takeout(GetStateRequest())
        except (TakeoutInvalidError, TakeoutRequiredError, ValueError) as e:
            logger.info("Stored takeout_id=%s is no longer usable (%s); starting a new one.", stored_id, e)
            with contextlib.suppress(Exception):
                await stack.aclose()
            return False
        self._takeout_stack = stack
        self.takeout = takeout
        logger.info("Reusing takeout session id=%s from a previous run.", stored_id)
        return True

    async def _discard_takeout_id(self):
        """Finish a takeout the server no longer honours, locally if need be."""
        try:
            await self.client.end_takeout(success=False)
        except Exception as e:
            logger.debug("end_takeout failed (%s); clearing takeout_id locally.", e)
            if self.client.session is not None:
                self.client.session.takeout_id = None

    async def _enter_takeout(self, kwargs):
        """Create a takeout session with the export parameters.

        finalize=False keeps the session on the server once the context is
        released, which is what makes the id reusable on the next run.
        """
        stack = contextlib.AsyncExitStack()
        self.takeout = await stack.enter_async_context(self.client.takeout(finalize=False, **kwargs))
        self._takeout_stack = stack

    async def stop_takeout(self, success: bool | None = None):
        """Release the takeout context; finish the session only when asked.

        By default the session stays open on the server so the next run reuses
        its id instead of paying the init cooldown. Pass an explicit ``success``
        to finish it for good (``tg takeout clear``).
        """
        stack, self._takeout_stack = self._takeout_stack, None
        self.takeout = None
        if stack is not None:
            await stack.aclose()
        if success is not None:
            await self.client.end_takeout(success=success)

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
