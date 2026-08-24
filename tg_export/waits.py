"""Waits that hold the export still, and how they are made visible.

Telegram answers too many requests with a delay it names itself, and until it
runs out nothing arrives: the progress bar stops, the counters stop, and a run
that is behaving correctly is indistinguishable from a hung one. Telethon
sleeps through the short waits on its own and says so at INFO level, which the
CLI holds at WARNING for libraries -- so by default the pause had no visible
cause at all.

Two things happen here. `WaitBoard` collects the waits currently in force so
that the live status can count them down; `FloodWaitNotices` picks the sleep
notice out of the telethon log, puts it on the board and lets that one record
through without lifting the rest of the library log with it.

The board is read by the display thread and written by the event loop, hence
the lock. Waits noted by the filter carry no end of their own -- telethon does
not report waking up -- so every wait expires by its own deadline instead.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# A wait shorter than this is not worth a line in the log: retries after a
# network blip start at one second, and Telegram hands out three-second flood
# waits by the dozen. The status line shows them all -- there a countdown that
# disappears at once costs nothing.
MIN_LOGGED_WAIT_SECONDS = 5


@dataclass(frozen=True)
class Wait:
    """One pause: why it happens, what it holds up and until when."""

    reason: str
    what: str
    seconds: float
    started_at: float

    @property
    def until(self) -> float:
        """Monotonic time the wait is expected to end at."""
        return self.started_at + self.seconds

    def remaining(self, now: float) -> float:
        """Seconds left of the wait, never negative."""
        return max(0.0, self.until - now)


class WaitBoard:
    """The waits in force right now, written by the loop, read by the display."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waits: list[Wait] = []

    def note(self, *, reason: str, what: str, seconds: float, now: float | None = None) -> Wait:
        """Record a wait somebody else is already sleeping through.

        Used for the sleep telethon performs inside itself: the end of it is
        not reported, so the wait is dropped by its own deadline.
        """
        wait = Wait(reason=reason, what=what, seconds=seconds, started_at=self._now(now))
        with self._lock:
            self._waits.append(wait)
        return wait

    @contextlib.contextmanager
    def waiting(self, *, reason: str, what: str, seconds: float) -> Iterator[Wait]:
        """Hold a wait on the board for as long as the caller sleeps.

        The deadline is only an estimate -- a sleep can be cut short or run
        long -- so the wait is removed when the block ends rather than when its
        time is up.
        """
        wait = self.note(reason=reason, what=what, seconds=seconds)
        if seconds >= MIN_LOGGED_WAIT_SECONDS:
            logger.warning("waiting %ds on %s: %s", int(seconds), what, reason)
        try:
            yield wait
        finally:
            self.forget(wait)

    def forget(self, wait: Wait) -> None:
        """Drop one wait, whether or not its deadline has passed."""
        with self._lock, contextlib.suppress(ValueError):
            self._waits.remove(wait)

    def pending(self, now: float | None = None) -> list[Wait]:
        """Waits still in force, the one with the most time left first."""
        moment = self._now(now)
        with self._lock:
            self._waits = [w for w in self._waits if w.remaining(moment) > 0]
            live = list(self._waits)
        return sorted(live, key=lambda w: w.remaining(moment), reverse=True)

    @staticmethod
    def _now(now: float | None) -> float:
        """The caller's moment, or the current one."""
        return time.monotonic() if now is None else now


# The board the export uses. A single instance rather than an argument threaded
# through the call chain: one of the two writers is a logging filter installed
# on a telethon logger, which is reached by no object of this package.
WAITS = WaitBoard()

# The telethon logger that reports a sleep on a flood wait, and the shape of
# that record: `_fmt_flood` in telethon/client/users.py builds
# ('Sleeping%s for %ds (%s) on %s flood wait', early, delay, timedelta, name).
# A contract test checks that the format is still spelled that way.
FLOOD_WAIT_LOGGER = "telethon.client.users"
_FLOOD_WAIT_PREFIX = "Sleeping"
_FLOOD_WAIT_SUFFIX = " flood wait"
_FLOOD_WAIT_ARGS = 4
_FLOOD_WAIT_SECONDS_ARG = 1
_FLOOD_WAIT_REQUEST_ARG = 3
FLOOD_WAIT_REASON = "flood wait"


def flood_wait_of(record: logging.LogRecord) -> Wait | None:
    """The wait a telethon record announces, or None for any other record.

    The unformatted message and its arguments are read, not the rendered text:
    the numbers are still numbers there, and no wording of the message has to
    be parsed back.
    """
    msg = record.msg
    if (
        not isinstance(msg, str)
        or not msg.startswith(_FLOOD_WAIT_PREFIX)
        or not msg.endswith(_FLOOD_WAIT_SUFFIX)
    ):
        return None
    args = record.args
    if not isinstance(args, tuple) or len(args) != _FLOOD_WAIT_ARGS:
        return None
    seconds = args[_FLOOD_WAIT_SECONDS_ARG]
    what = args[_FLOOD_WAIT_REQUEST_ARG]
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not isinstance(what, str):
        return None
    return Wait(reason=FLOOD_WAIT_REASON, what=what, seconds=float(seconds), started_at=time.monotonic())


class FloodWaitNotices(logging.Filter):
    """Keeps the sleep notices of telethon, drops the rest of its INFO log.

    Installed on the logger itself rather than on a handler: a filter of the
    logger runs before the record reaches the handlers of the root, so one
    lowered logger does not turn the whole library log on.
    """

    def __init__(self, board: WaitBoard, *, level: int):
        super().__init__()
        self._board = board
        self._level = level

    def filter(self, record: logging.LogRecord) -> bool:
        """Note the wait, then decide whether the record stays visible."""
        wait = flood_wait_of(record)
        if wait is not None:
            self._board.note(reason=wait.reason, what=wait.what, seconds=wait.seconds)
            return True
        return record.levelno >= self._level


def watch_flood_waits(level: int, *, board: WaitBoard | None = None) -> FloodWaitNotices:
    """Make the sleeps of telethon visible at the level the user asked for.

    The logger is lowered to INFO so that the record is created at all, and the
    filter takes back everything the user did not ask to see. Called after the
    library loggers are set, since it overrides the level of one of them.
    """
    notices = FloodWaitNotices(board if board is not None else WAITS, level=level)
    telethon_logger = logging.getLogger(FLOOD_WAIT_LOGGER)
    telethon_logger.setLevel(min(level, logging.INFO))
    telethon_logger.addFilter(notices)
    return notices
