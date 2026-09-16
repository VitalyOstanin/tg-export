"""One way of naming a chat, shared by every command of the `tg` group.

A chat reaches the commands in whatever form the user had at hand: the numeric
id printed by `tg info`, the public name of a channel, a link copied from the
app. Each command used to decide on its own what it accepted -- `messages`,
`info` and `download` took a number and nothing else, `send` tried `int()` and
kept the string when that failed -- so the same chat was spelled differently
depending on which command was being called.

Resolution itself stays with Telegram: this module only decides what the string
means, and `resolve_chat` turns the answer into an entity, naming the two
failures apart that the user can act on -- a name nobody holds, and a chat that
exists but is closed to this account.
"""

from __future__ import annotations

import re
from typing import Any

import click
from telethon.errors.rpcerrorlist import (
    ChannelPrivateError,
    ChatAdminRequiredError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)

from tg_export.errors import ChatResolveError

# A username as Telegram spells it. The length is deliberately not checked:
# Telegram decides what exists, and its own refusal is more accurate than a
# guess here about the shortest name it still serves.
_USERNAME = re.compile(r"^[A-Za-z0-9_]+$")
_NUMERIC = re.compile(r"^-?\d+$")
_SCHEME = re.compile(r"^https?://")
_LINK_HOSTS = ("t.me/", "telegram.me/")

# `https://t.me/c/<internal>/<message>` names a private supergroup by the id
# that has the channel prefix stripped; adding it back gives the id the rest of
# the codebase uses.
_CHANNEL_ID_PREFIX = -1000000000000

_INVITE_REFUSAL = (
    "invite link: tg-export cannot open one. Join the chat in Telegram first, "
    "then name it by its id or username"
)


def parse_chat_ref(value: str) -> int | str:
    """What a chat reference means: a numeric id, or a username without the `@`.

    Raises ValueError with a message meant for the user; the Click parameter
    type below turns it into a usage error.
    """
    text = value.strip()
    if not text:
        raise ValueError("chat reference is empty")

    body = _SCHEME.sub("", text)
    for host in _LINK_HOSTS:
        if body.startswith(host):
            return _from_link(body[len(host) :])

    if text.startswith("+"):
        raise ValueError(_INVITE_REFUSAL)
    if text.startswith("@"):
        return _as_username(text[1:])
    if _NUMERIC.match(text):
        return int(text)
    return _as_username(text)


def _from_link(path: str) -> int | str:
    """The chat a `t.me` path names; the message part of the path is dropped."""
    if path.startswith("+") or path.startswith("joinchat/"):
        raise ValueError(_INVITE_REFUSAL)

    segments = [s for s in path.split("/") if s]
    if not segments:
        raise ValueError("link names no chat")
    if segments[0] == "c":
        if len(segments) < 2 or not segments[1].isdigit():
            raise ValueError("private link carries no chat id")
        return _CHANNEL_ID_PREFIX - int(segments[1])
    return _as_username(segments[0])


def _as_username(name: str) -> str:
    if not _USERNAME.match(name):
        raise ValueError(f"{name!r} is not a chat id, a username or a t.me link")
    return name


class ChatRefParam(click.ParamType):
    """The Click type behind every chat argument of the `tg` group."""

    name = "chat"

    def convert(self, value, param, ctx) -> int | str:
        # Click hands defaults back through convert, and a default is already
        # in the parsed shape.
        if isinstance(value, int):
            return value
        try:
            return parse_chat_ref(value)
        except ValueError as e:
            self.fail(str(e), param, ctx)


CHAT_REF = ChatRefParam()


async def resolve_chat(client, ref: int | str) -> Any:
    """The entity `ref` names, or ChatResolveError saying which wall was hit."""
    try:
        return await client.get_entity(ref)
    except (UsernameNotOccupiedError, UsernameInvalidError) as e:
        raise ChatResolveError(f"no chat named {ref!r} exists") from e
    except (ChannelPrivateError, ChatAdminRequiredError) as e:
        raise ChatResolveError(f"{ref!r} is closed to this account: join it in Telegram to read it") from e
    except ValueError as e:
        # What Telethon raises for a username nobody holds and for an id its
        # session has never seen; the text it carries names which of the two.
        raise ChatResolveError(f"cannot resolve {ref!r}: {e}") from e
