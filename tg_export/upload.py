"""Uploading one file over several connections to the datacentre at once.

Telethon sends the parts of a file one at a time, waiting for each to be
acknowledged before the next leaves. That makes the round trip the limit:
through a proxy answering in about a second, a 256 KB part per round trip is
some 130 KB/s no matter how wide the line is. The official client does not
work that way -- it opens several connections to the upload datacentre, keeps
about a megabyte in flight on each and decides how many to hold from how fast
the answers come back. This module does the same, so a large file leaves at
the speed of the line rather than the speed of the round trip.

The numbers below are the ones the official client uses, kept together here so
a change to them is a change to a documented value rather than to a magic
constant buried in the loop.

Only files above `BIG_FILE_FROM` take this path: below it a file is a couple
of parts, the round trips do not add up to anything, and Telethon's own
uploader also computes the md5 the small-file request carries.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import time
from collections.abc import Awaitable, Callable, Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

from telethon import functions
from telethon.errors import FloodWaitError
from telethon.network import MTProtoSender
from telethon.tl.alltlobjects import LAYER
from telethon.tl.functions.upload import SaveBigFilePartRequest
from telethon.tl.types import InputFileBig

logger = logging.getLogger(__name__)

# Part sizes Telegram accepts, smallest first. The size chosen for a file is
# the first one that keeps the number of parts within MAX_PARTS.
PART_SIZES = (32 * 1024, 64 * 1024, 128 * 1024, 256 * 1024, 512 * 1024)

# A file may be split into no more than this many parts. With the largest part
# size it puts the ceiling of a single upload at 2 GB, which is also where
# Telegram stops accepting files from a non-premium account.
MAX_PARTS = 4000

# Where the client stops using the smallest part sizes: below a megabyte a
# file is cut into 32 KB parts, up to 32 MB into 64 KB ones, and past that the
# choice starts at 128 KB.
SMALL_FILE_UP_TO = 1024 * 1024
MEDIUM_FILE_UP_TO = 32 * 1024 * 1024

# Above this size Telegram wants the "big file" requests, which carry no md5
# and may arrive in any order -- the property this module relies on to keep
# several parts in flight.
BIG_FILE_FROM = 10 * 1024 * 1024


@dataclass(frozen=True)
class UploadLimits:
    """How wide the upload may open and what counts as a fast answer.

    Defaults follow the official desktop client, with two departures.  The
    ceiling is four rather than eight: measurements on this project's own line
    showed the throughput flat from two connections onwards and Telegram
    answering the widest runs with a one-second flood wait.  And the width
    starts at the floor instead of at one, because the client's rule -- open
    another connection once every current one answered within a second --
    never fires on a line whose round trip is already a second, and the upload
    would then crawl through the whole file on a single connection.

    The floor is two, which is where that same measurement has the throughput
    flatten, so opening there costs nothing and narrowing stays possible: a
    floor equal to the ceiling would turn the answer to a flood wait -- drop a
    connection -- into a no-op.

    A megabyte stays in flight on each, growth continues only while answers
    come back within a second, and one connection is dropped when an answer
    takes eight or more, down to the floor.
    """

    # The width the upload opens with and never falls below.
    min_connections: int = 2
    max_connections: int = 4
    in_flight_per_connection: int = 1024 * 1024
    fast_response: float = 1.0
    slow_response: float = 8.0
    # A connection is only called fast on an answer that had enough data in
    # flight behind it: a lone small part comes back quickly on any line and
    # would otherwise argue for opening one more connection.
    accept_as_fast_from: int = 512 * 1024
    # After dropping a connection the upload is left alone for this long, so
    # that the queue drains at the narrower width before the next verdict.
    settle_after_shrink: float = 8.0
    # How many times one part is re-sent before the upload gives up on it.
    part_attempts: int = 3
    # How long one part may stay unanswered. A connection that dies quietly --
    # the proxy drops it, the datacentre closes it -- leaves its requests
    # waiting for an answer that will never come, and the upload would sit
    # there for as long as the process lives.
    part_timeout: float = 60.0
    # A wait longer than this is reported rather than slept through: at that
    # point the account is being told to stop, not to slow down.
    max_flood_wait: float = 60.0


class ConnectionCountPolicy:
    """Decides how many connections carry the parts, from response times.

    The rule is the client's: a connection is marked fast when its answer came
    back under `fast_response` with at least `accept_as_fast_from` bytes in
    flight, and the width grows by one only once every open connection carries
    such a mark. Anything slower clears the marks, so growth stops; an answer
    at or above `slow_response` drops a connection, at most once per
    `settle_after_shrink` seconds so the queue has time to drain.
    """

    def __init__(self, limits: UploadLimits, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._limits = limits
        self._clock = clock
        self._count = limits.min_connections
        self._fast: set[int] = set()
        self._last_shrink: float | None = None

    @property
    def count(self) -> int:
        """How many connections the upload should be using right now."""
        return self._count

    def record(self, *, connection: int, duration: float, in_flight: int) -> None:
        """Take one finished part into account: its connection, time and load."""
        limits = self._limits
        if duration >= limits.slow_response:
            self._fast.clear()
            self._shrink()
            return
        if duration >= limits.fast_response:
            # Neither fast nor slow: hold the width where it is. Clearing the
            # marks is what stops growth -- a single fast answer among slowish
            # ones must not be enough to open another connection.
            self._fast.clear()
            return
        if in_flight >= limits.accept_as_fast_from:
            self._fast.add(connection)

    def _shrink(self) -> None:
        if self._count <= self._limits.min_connections:
            return
        now = self._clock()
        if self._last_shrink is not None and now - self._last_shrink < self._limits.settle_after_shrink:
            return
        self._count -= 1
        self._last_shrink = now
        self._fast = {index for index in self._fast if index < self._count}
        logger.debug("upload: slow answer, down to %d connections", self._count)

    def grow_if_all_fast(self) -> bool:
        """Open one more connection when every current one answered fast.

        Returns whether the width grew, which the caller logs; the new count is
        readable through `count` either way.
        """
        if self._count >= self._limits.max_connections:
            return False
        if len(self._fast) < self._count:
            return False
        self._count += 1
        self._fast.clear()
        logger.debug("upload: fast answers, up to %d connections", self._count)
        return True


def choose_part_size(file_size: int) -> int:
    """The part size to cut a file of this size into.

    The smallest sizes are reserved for small files: a 5 MB file cut into
    32 KB parts is 160 round trips for no reason, so the floor is 64 KB past a
    megabyte. Past 32 MB the file is large enough that the number of round
    trips decides the speed, so it takes the largest size outright instead of
    the smallest one that fits within MAX_PARTS -- 128 KB parts turn a 283 MB
    file into 2159 answers to wait for, against 540 at 512 KB.
    """
    if file_size <= 0:
        raise ValueError(f"file size must be positive, got {file_size}")
    if file_size < SMALL_FILE_UP_TO:
        allowed = PART_SIZES
    elif file_size <= MEDIUM_FILE_UP_TO:
        allowed = PART_SIZES[1:]
    else:
        allowed = PART_SIZES[-1:]
    for part_size in allowed:
        if (file_size + part_size - 1) // part_size <= MAX_PARTS:
            return part_size
    largest = PART_SIZES[-1]
    raise ValueError(
        f"file of {file_size} bytes needs more than {MAX_PARTS} parts of {largest} bytes; "
        f"Telegram accepts no more than {MAX_PARTS * largest} bytes in one file"
    )


def part_count(file_size: int, part_size: int) -> int:
    """How many parts of `part_size` a file of `file_size` is cut into."""
    return (file_size + part_size - 1) // part_size


class Sender(Protocol):
    """The part of an MTProto sender this module uses."""

    def send(self, request: Any) -> Awaitable[Any]:
        """Queue one request and return an awaitable for its answer."""
        ...

    async def disconnect(self) -> None:
        """Close the connection."""
        ...


ConnectFn = Callable[[], Awaitable[Sender]]


async def connect_upload_sender(client: Any) -> Sender:
    """Open one more connection to the account's datacentre.

    Telethon caches a single borrowed sender per datacentre, so asking it
    repeatedly yields the same connection; a second connection has to be built
    here. The authorisation key of the session is reused as it stands -- an
    exported authorisation is what a *different* datacentre needs, while the
    account's own accepts the key it already issued -- and the layer is
    announced the way the library announces it on its main connection.
    """
    dc = await client._get_dc(client.session.dc_id)
    sender = MTProtoSender(client.session.auth_key, loggers=client._log)
    await sender.connect(
        client._connection(
            dc.ip_address,
            dc.port,
            dc.id,
            loggers=client._log,
            proxy=client._proxy,
            local_addr=client._local_addr,
        )
    )
    # A copy: the library rewrites `query` on its own init request when it
    # exports a sender, and this must not disturb the client's object.
    init_request = copy.copy(client._init_request)
    init_request.query = functions.help.GetConfigRequest()
    # `send` is typed as returning a future or a list -- the list is the shape
    # for a batch of requests, which this call never passes.
    await cast(Awaitable[Any], sender.send(functions.InvokeWithLayerRequest(LAYER, init_request)))
    return cast(Sender, sender)


@dataclass
class _Connection:
    """One open connection and how much of the file is in flight on it."""

    index: int
    sender: Sender
    in_flight: int = 0
    # Set when a part on this connection timed out or the link failed: no new
    # part goes on it until it has been opened again.
    broken: bool = False


@dataclass
class _Part:
    """One part of the file, waiting to be sent or being sent."""

    index: int
    data: bytes
    attempts: int = 0


@dataclass
class _Pool:
    """The open connections, opened lazily as the policy asks for more."""

    connect: ConnectFn
    connections: list[_Connection] = field(default_factory=list)

    async def resize(self, wanted: int) -> None:
        """Open or close connections until there are `wanted` of them.

        Closing takes the last connection, which the caller only asks for once
        nothing is in flight on it.
        """
        while len(self.connections) < wanted:
            sender = await self.connect()
            self.connections.append(_Connection(index=len(self.connections), sender=sender))
        while len(self.connections) > wanted:
            spare = self.connections.pop()
            await spare.sender.disconnect()

    def free(self, part_size: int, limits: UploadLimits) -> _Connection | None:
        """A connection with room for one more part, or None when all are full."""
        for connection in self.connections:
            if connection.broken:
                continue
            if connection.in_flight + part_size <= limits.in_flight_per_connection:
                return connection
        return None

    async def reopen(self, connection: _Connection) -> None:
        """Replace one connection's sender with a freshly opened one."""
        try:
            await connection.sender.disconnect()
        except Exception:  # the old link is already gone in the usual case
            logger.debug("upload: closing broken connection %d failed", connection.index)
        connection.sender = await self.connect()
        connection.broken = False

    def idle_tail(self) -> bool:
        """Whether the last connection has nothing in flight and may be closed."""
        return bool(self.connections) and self.connections[-1].in_flight == 0

    async def close(self) -> None:
        """Close every connection, ignoring failures on the way out."""
        for connection in self.connections:
            try:
                await connection.sender.disconnect()
            except Exception:  # a failed close must not fail the upload
                logger.debug("upload: closing connection %d failed", connection.index, exc_info=True)
        self.connections.clear()


async def _awaited(sender: Sender, request: Any) -> Any:
    """Await what `send` returns, so `wait_for` has a coroutine to time out."""
    return await sender.send(request)


def _read_parts(path: Path, part_size: int) -> Generator[bytes, None, None]:
    """Yield the file's parts in order, reading one at a time."""
    with path.open("rb") as handle:
        while chunk := handle.read(part_size):
            yield chunk


class ParallelUploader:
    """Sends one file's parts over as many connections as the line allows."""

    def __init__(
        self,
        client: Any,
        *,
        limits: UploadLimits | None = None,
        connect: ConnectFn | None = None,
    ) -> None:
        self._client = client
        self._limits = limits or UploadLimits()
        self._connect = connect or (lambda: connect_upload_sender(client))
        # Until when Telegram asked the upload to hold off. A flood wait
        # answers one part, but the pause it names applies to the account:
        # sending the remaining parts meanwhile only earns more of them.
        self._pause_until = 0.0

    async def upload(
        self,
        path: Path,
        *,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> InputFileBig:
        """Upload `path` and return the handle `send_file` takes as `file`."""
        file_size = path.stat().st_size
        part_size = choose_part_size(file_size)
        total_parts = part_count(file_size, part_size)
        file_id = int.from_bytes(os.urandom(8), "little", signed=True)
        policy = ConnectionCountPolicy(self._limits)
        pool = _Pool(connect=self._connect)
        try:
            # The floor, not one connection: the width only ever grows on
            # answers that came back inside a second, so a line slower than
            # that would carry the whole file on a single connection.
            await pool.resize(policy.count)
            await self._run(
                path=path,
                file_id=file_id,
                part_size=part_size,
                total_parts=total_parts,
                file_size=file_size,
                policy=policy,
                pool=pool,
                progress_callback=progress_callback,
            )
        finally:
            await pool.close()
        return InputFileBig(id=file_id, parts=total_parts, name=path.name)

    async def _run(
        self,
        *,
        path: Path,
        file_id: int,
        part_size: int,
        total_parts: int,
        file_size: int,
        policy: ConnectionCountPolicy,
        pool: _Pool,
        progress_callback: Callable[[int, int], None] | None,
    ) -> None:
        """The send loop: fill the open connections, then wait for answers."""
        reader = _PartReader(path, part_size)
        retries: list[_Part] = []
        in_flight: dict[asyncio.Task, tuple[_Connection, _Part, float]] = {}
        sent_bytes = 0
        try:
            while True:
                await self._wait_out_the_pause(in_flight)
                await self._fill(
                    pool=pool,
                    reader=reader,
                    retries=retries,
                    in_flight=in_flight,
                    file_id=file_id,
                    part_size=part_size,
                    total_parts=total_parts,
                )
                if not in_flight:
                    # Nothing is in flight, but the upload is only over once
                    # the file is read out and no part is waiting for another
                    # attempt: during a flood wait the fill puts nothing on
                    # the wire, and stopping here would drop what is left.
                    if reader.exhausted and not retries:
                        break
                    continue
                done, _ = await asyncio.wait(in_flight.keys(), return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    connection, part, started = in_flight.pop(task)
                    connection.in_flight -= len(part.data)
                    sent = await self._settle(task, part, connection, policy, started, retries)
                    if sent:
                        sent_bytes += len(part.data)
                        if progress_callback is not None:
                            progress_callback(sent_bytes, file_size)
                await self._heal(pool, in_flight, retries)
                await self._adjust(pool, policy)
        finally:
            reader.close()
            for task in in_flight:
                task.cancel()

    async def _wait_out_the_pause(self, in_flight: dict[asyncio.Task, Any]) -> None:
        """Sleep off a flood wait once the parts already sent have landed."""
        if in_flight:
            return
        left = self._pause_until - time.monotonic()
        if left > 0:
            await asyncio.sleep(left)

    async def _fill(
        self,
        *,
        pool: _Pool,
        reader: _PartReader,
        retries: list[_Part],
        in_flight: dict[asyncio.Task, tuple[_Connection, _Part, float]],
        file_id: int,
        part_size: int,
        total_parts: int,
    ) -> None:
        """Put parts on every connection that still has room for one."""
        if time.monotonic() < self._pause_until:
            return
        while True:
            connection = pool.free(part_size, self._limits)
            if connection is None:
                return
            part = retries.pop(0) if retries else await reader.next_part()
            if part is None:
                return
            request = SaveBigFilePartRequest(
                file_id=file_id,
                file_part=part.index,
                file_total_parts=total_parts,
                bytes=part.data,
            )
            connection.in_flight += len(part.data)
            task = asyncio.ensure_future(
                asyncio.wait_for(_awaited(connection.sender, request), self._limits.part_timeout)
            )
            in_flight[task] = (connection, part, time.monotonic())

    async def _settle(
        self,
        task: asyncio.Task,
        part: _Part,
        connection: _Connection,
        policy: ConnectionCountPolicy,
        started: float,
        retries: list[_Part],
    ) -> bool:
        """Record one finished part; queue it again when it failed.

        Returns whether the part is done with, so the caller counts it towards
        the progress exactly once.
        """
        duration = time.monotonic() - started
        try:
            task.result()
        except FloodWaitError as error:
            if error.seconds > self._limits.max_flood_wait:
                raise
            logger.debug("upload: flood wait for %s s", error.seconds)
            # The wait is Telegram asking for a pause on this account, not a
            # slow line: narrow the upload as a slow answer would, hold every
            # connection back until the pause is over, and queue the part for
            # another attempt after it.
            policy.record(
                connection=connection.index,
                duration=self._limits.slow_response,
                in_flight=connection.in_flight,
            )
            self._pause_until = max(self._pause_until, time.monotonic() + error.seconds)
            retries.append(part)
            return False
        except (TimeoutError, ConnectionError, OSError) as error:
            # The connection, not the part, is what failed here: it is opened
            # again before anything else goes on it, and the part waits for
            # the new one.
            connection.broken = True
            part.attempts += 1
            if part.attempts >= self._limits.part_attempts:
                raise
            logger.debug(
                "upload: connection %d lost part %d (%s), opening it again",
                connection.index,
                part.index,
                type(error).__name__,
            )
            retries.append(part)
            return False
        except Exception:
            part.attempts += 1
            if part.attempts >= self._limits.part_attempts:
                raise
            logger.debug(
                "upload: part %d failed on attempt %d, sending it again",
                part.index,
                part.attempts,
                exc_info=True,
            )
            retries.append(part)
            return False
        policy.record(
            connection=connection.index,
            duration=duration,
            # The load this answer came back under: what was still in flight
            # on the connection plus the part that has just left it.
            in_flight=connection.in_flight + len(part.data),
        )
        return True

    async def _heal(
        self,
        pool: _Pool,
        in_flight: dict[asyncio.Task, tuple[_Connection, _Part, float]],
        retries: list[_Part],
    ) -> None:
        """Open a fresh connection in place of every broken one.

        Whatever was still in flight on it is cancelled and queued again: those
        requests were sitting on a link that is gone, and their answers are
        never coming.
        """
        for connection in pool.connections:
            if not connection.broken:
                continue
            for task, (owner, part, _started) in list(in_flight.items()):
                if owner is connection:
                    task.cancel()
                    del in_flight[task]
                    retries.append(part)
            connection.in_flight = 0
            await pool.reopen(connection)

    async def _adjust(self, pool: _Pool, policy: ConnectionCountPolicy) -> None:
        """Bring the number of open connections to what the policy asks for."""
        policy.grow_if_all_fast()
        wanted = policy.count
        if wanted > len(pool.connections):
            await pool.resize(wanted)
            return
        # Closing is only safe once the connection being dropped carries
        # nothing: a sender disconnected under a pending request loses the
        # part with it.
        while len(pool.connections) > wanted and pool.idle_tail():
            await pool.resize(len(pool.connections) - 1)


class _PartReader:
    """Reads the file part by part, off the event loop."""

    def __init__(self, path: Path, part_size: int) -> None:
        self._parts = _read_parts(path, part_size)
        self._next_index = 0
        self._exhausted = False

    @property
    def exhausted(self) -> bool:
        """Whether the file has been read to its end."""
        return self._exhausted

    async def next_part(self) -> _Part | None:
        """The next part of the file, or None once the file is read out.

        Reading blocks -- on a spinning disk or a busy one for long enough to
        stall every connection at once -- so it happens in a worker thread,
        the way the rest of the package reads files.
        """
        if self._exhausted:
            return None
        data = await asyncio.to_thread(next, self._parts, None)
        if data is None:
            self._exhausted = True
            return None
        part = _Part(index=self._next_index, data=data)
        self._next_index += 1
        return part

    def close(self) -> None:
        """Drop the open file handle the generator holds."""
        self._parts.close()


async def upload_file(
    client: Any,
    path: Path,
    *,
    limits: UploadLimits | None = None,
    connect: ConnectFn | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> InputFileBig | Any:
    """Upload one file, in parallel when it is large enough to be worth it.

    Returns the handle to pass to `send_file` as `file`: this module's own for
    a big file, Telethon's for a small one.
    """
    file_size = path.stat().st_size
    if file_size <= BIG_FILE_FROM:
        return await client.upload_file(str(path), progress_callback=progress_callback)
    uploader = ParallelUploader(client, limits=limits, connect=connect)
    return await uploader.upload(path, progress_callback=progress_callback)
