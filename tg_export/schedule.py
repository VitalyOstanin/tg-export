"""The moment a scheduled message is sent: parsing it and checking it against Telegram.

`tg send --schedule` hands the moment to Telegram as `schedule_date`, and the
message waits in the chat's server-side queue until then. What the server does
with a moment it does not like is not always a refusal, so the bounds it is
known to apply are checked here, before anything is uploaded:

- a moment less than ten seconds ahead is not scheduled at all -- the message
  is sent at once, as an ordinary one (core.telegram.org/api/scheduled-messages);
- a moment further than about a year ahead is refused with
  SCHEDULE_DATE_TOO_LATE; the server allows "a year and a few hours", and the
  check here stops at 365 days so as not to depend on the few hours.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from typing import overload

# Telegram sends a message scheduled closer than this at once.
MIN_LEAD = timedelta(seconds=10)
# The server allows a year and a few hours; the few hours are not relied on.
MAX_LEAD = timedelta(days=365)

_RELATIVE = re.compile(r"^\+((?:\d+[smhdw])+)$")
_RELATIVE_PART = re.compile(r"(\d+)([smhdw])")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
_CLOCK = re.compile(r"^(?:(tomorrow)\s+)?(\d{1,2}):(\d{2})$")
_BARE_DATE = re.compile(r"^\d{4}-?\d{2}-?\d{2}$")

FORMATS_HELP = (
    "+30m, +2h, +1d12h (from now); 10:00 (the next such time); tomorrow 10:00; "
    "2026-12-31 18:00 or 2026-12-31T18:00+03:00 (ISO 8601, local time zone when none is given)"
)


def parse_schedule(value: str, now: datetime) -> datetime:
    """The moment ``value`` names, as an aware datetime.

    ``now`` is an aware datetime in the local time zone. A moment named without
    a zone is local time with the offset the system zone has on that date, not
    now's. Raises ValueError with a message meant for the user.
    """
    text = value.strip()
    if not text:
        raise ValueError("the time is empty")

    relative = _RELATIVE.match(text.lower())
    if relative:
        try:
            delta = timedelta()
            for amount, unit in _RELATIVE_PART.findall(relative.group(1)):
                delta += timedelta(**{_UNITS[unit]: int(amount)})
            return now + delta
        except OverflowError:
            # Past the range of a date: a typo of extra zeros, not a moment.
            raise ValueError(f"{text!r} is too far ahead to be a date") from None

    clock = _CLOCK.match(text.lower())
    if clock:
        tomorrow, hours, minutes = clock.groups()
        try:
            at = time(int(hours), int(minutes))
        except ValueError:
            raise ValueError(f"{text!r} is not a time of day") from None
        day = now.date() + timedelta(days=1 if tomorrow else 0)
        # astimezone() on a naive value takes the offset the local zone has on
        # that day, which differs from now's across a daylight saving switch.
        moment = datetime.combine(day, at).astimezone()
        # A bare time that has already passed today means the same time tomorrow.
        if not tomorrow and moment <= now:
            moment = datetime.combine(day + timedelta(days=1), at).astimezone()
        return moment

    if _BARE_DATE.match(text):
        # fromisoformat takes a bare date as midnight -- a guess the user did
        # not make.
        raise ValueError(f"{text!r} names no time: expected one of {FORMATS_HELP}")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"{text!r} is not a time tg-export understands: expected one of {FORMATS_HELP}"
        ) from None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment


def check_schedule(moment: datetime, now: datetime) -> None:
    """Refuse a moment Telegram would not schedule; ValueError carries the reason."""
    lead = moment - now
    if lead < MIN_LEAD:
        if lead <= timedelta():
            raise ValueError(f"{describe(moment)} is in the past")
        raise ValueError(
            f"{describe(moment)} is less than {int(MIN_LEAD.total_seconds())} seconds ahead: "
            f"Telegram would send the message at once instead of scheduling it"
        )
    if lead > MAX_LEAD:
        raise ValueError(
            f"{describe(moment)} is more than {MAX_LEAD.days} days ahead: Telegram schedules "
            f"messages at most about a year in advance"
        )


@overload
def describe(moment: datetime | None) -> str: ...
@overload
def describe(moment: datetime | None, *, missing: str) -> str: ...
@overload
def describe(moment: datetime | None, *, missing: None) -> str | None: ...
def describe(moment: datetime | None, *, missing: str | None = "?") -> str | None:
    """The moment in the local time zone, to the minute, with its offset; ``missing`` when there is none."""
    if moment is None:
        return missing
    return moment.astimezone().isoformat(sep=" ", timespec="minutes")


def local_now() -> datetime:
    return datetime.now().astimezone()


def explain_refusal(error: Exception) -> str | None:
    """What a refusal of Telegram to schedule means for the user; None for other errors.

    The library's own texts name the error but not what to do about it, and one
    of them -- "bots are not allowed to schedule messages" -- reads as if the
    recipient were the problem.
    """
    from telethon.errors.rpcerrorlist import (
        ScheduleBotNotAllowedError,
        ScheduleDateInvalidError,
        ScheduleDateTooLateError,
        ScheduleTooMuchError,
    )

    if isinstance(error, ScheduleTooMuchError):
        return (
            "the chat already holds as many scheduled messages as Telegram keeps (about 100); "
            "see them with `tg scheduled list` and delete some with `tg scheduled delete`"
        )
    if isinstance(error, ScheduleDateTooLateError):
        return "Telegram refused the time as too far ahead: it schedules at most about a year in advance"
    if isinstance(error, ScheduleDateInvalidError):
        return "Telegram refused the time as invalid: it may have passed while the files were uploading"
    if isinstance(error, ScheduleBotNotAllowedError):
        return "Telegram does not allow scheduling messages from this account or into this chat"
    return None
