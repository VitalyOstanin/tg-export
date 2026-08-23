"""Telethon API wrapper with Takeout support."""

from __future__ import annotations

import contextlib
import importlib.util
import logging
from pathlib import Path
from typing import Any

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

from tg_export.auth import ProxyTuple
from tg_export.locking import ProcessLock
from tg_export.session import FixedSQLiteSession

logger = logging.getLogger(__name__)

# Safety cap on the left-channel paging loop.
_MAX_LEFT_CHANNEL_PAGES = 100

# Page size asked of the RPC methods that return a slice (top peers, stories,
# ringtones). Telegram may return fewer; it never returns more.
_DEFAULT_PAGE_LIMIT = 100


def one_message(result):
    """The single message `get_messages(..., ids=<one id>)` returned, or None.

    Telethon answers with the message itself for a single id and with a list
    for several, and the callers asking for one id normalised that twice, in
    two different shapes.
    """
    if isinstance(result, list):
        return result[0] if result else None
    return result


class TgApi:
    def __init__(self, session_path: str | Path, api_id: int, api_hash: str, proxy: ProxyTuple | None = None):
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
                    "`uv sync --extra proxy` in a checkout of this project, or "
                    "`pip install 'tg-export[proxy]'` when tg-export comes from PyPI."
                )
            kwargs["proxy"] = proxy
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
        # Taken here rather than in connect(): building the session writes to
        # the file three times over -- clearing takeout_id and tmp_auth_key,
        # rewriting the whole sessions row from Telethon's in-memory copy, then
        # dropping the backup table. Under the old order those writes happened
        # before it turned out the file belonged to another process, which
        # could put a stale authorisation key back on disk and take the backup
        # table out from under a neighbour's initialisation.
        self._session_lock.acquire()
        try:
            session = FixedSQLiteSession(str(session_path))
            self.client = TelegramClient(session, api_id, api_hash, **kwargs)
        except BaseException:
            self._session_lock.release()
            raise
        self.takeout = None
        self._takeout_stack: contextlib.AsyncExitStack | None = None

    async def __aenter__(self) -> TgApi:
        """Connect and hand the client over; the caller cannot forget to close it."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.disconnect()

    async def connect(self):
        # Already held since __init__; the call matters only for a client
        # reconnected after disconnect(), which releases it.
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
        try:
            await self.stop_takeout()
        except Exception as e:
            # Releasing the takeout context has a server-side effect, so its
            # failure is worth a line; it still must not replace the outcome of
            # the run the caller is finishing.
            logger.debug("takeout context was not released cleanly: %s", e)
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
        to finish it for good (``takeout clear``).
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

    async def get_left_channels(self) -> list[Any]:
        """Return every left channel, following the offset pages.

        The server answers with a slice, so one request at offset 0 drops
        everything past the first page without a word. Paging stops on an
        empty page, on the announced total, or when a page repeats ids
        already seen -- that last case means the server ignored the offset.
        """
        channels: list = []
        seen: set[int] = set()
        offset = 0
        for _ in range(_MAX_LEFT_CHANNEL_PAGES):
            result = await self.client(GetLeftChannelsRequest(offset=offset))
            page = list(getattr(result, "chats", None) or [])
            if not page:
                break
            fresh = [ch for ch in page if getattr(ch, "id", 0) not in seen]
            if not fresh:
                break
            seen.update(getattr(ch, "id", 0) for ch in fresh)
            channels.extend(fresh)
            offset += len(page)
            count = getattr(result, "count", None)
            if count is not None and offset >= count:
                break
        else:
            logger.warning(
                "Left channels: stopped after %d pages, later ones are missing from the catalog",
                _MAX_LEFT_CHANNEL_PAGES,
            )
        return channels

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
                    limit=_DEFAULT_PAGE_LIMIT,
                    hash=0,
                )
            )
            return result
        except Exception as e:
            # Frequent contacts are an optional page: report why they are
            # missing instead of rendering an empty section without a word.
            logger.warning(
                "Top peers are unavailable (%s); the frequent contacts section stays empty",
                e,
                exc_info=True,
            )
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
                limit=_DEFAULT_PAGE_LIMIT,
            )
        )
        archived = await self.client(
            GetStoriesArchiveRequest(
                peer=InputPeerSelf(),
                offset_id=0,
                limit=_DEFAULT_PAGE_LIMIT,
            )
        )
        return pinned, archived

    async def get_ringtones(self):
        """Get saved ringtones."""
        result = await self.client(GetSavedRingtonesRequest(hash=0))
        return result
