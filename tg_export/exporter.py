"""Main export loop with progress tracking."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import re
import signal
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from rich.live import Live
from rich.markup import escape
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TransferSpeedColumn,
)
from rich.table import Table
from rich.text import Text

from tg_export.api import TgApi
from tg_export.config import ChatExportConfig, Config

# Bound here as a module global on purpose: exporter.console is what the
# rest of the code and the tests reach for; the declaration is in
# tg_export.console.
from tg_export.console import console
from tg_export.converter import convert_message
from tg_export.format import format_size, format_speed
from tg_export.html.renderer import HtmlRenderer
from tg_export.media import DiskSpaceError, DownloadProgress, MediaDownloader
from tg_export.models import Chat, ForumTopic, Message
from tg_export.state import ExportState

logger = logging.getLogger(__name__)

# How many messages accumulate before one write to the state database, and how
# long between progress lines when the live display is off.
BATCH_SIZE = 500
LOG_INTERVAL = 3  # seconds


def _log(msg: str):
    """Print with immediate flush (works in non-TTY / redirected output)."""
    console.print(msg, markup=False, highlight=False, soft_wrap=True)


def chat_export_line(
    chat_name: str,
    chat_type: str,
    folder: str | None,
    is_left: bool = False,
    is_archived: bool = False,
) -> str:
    """Build the `export: <chat>` console line with markup-safe substitutions."""
    if is_left:
        folder_str = " [left]"
    elif is_archived:
        folder_str = " [archived]"
    elif folder:
        folder_str = f" [{escape(folder)}]"
    else:
        folder_str = ""
    return f"  [green]export[/]: {escape(chat_name)} ({escape(chat_type)}){folder_str}"


def chat_progress_description(chat_name: str) -> str:
    """Build the per-chat description for the main Progress task."""
    return f"[cyan]{escape(chat_name)}[/]"


def describe_error(error: BaseException) -> str:
    """Name the exception along with its text.

    ``str(TimeoutError())`` and ``str(ValueError())`` are empty, and a bare
    ``str(KeyError("peer_id"))`` is just the key: a report built on the text
    alone ended after the colon and said nothing about what went wrong.
    """
    text = str(error)
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


def chat_error_line(chat_name: str, error: BaseException, chat_id: int | None = None) -> str:
    """Build the `Error exporting <chat> (id=<id>): <e>` line, escaping both."""
    suffix = f" (id={chat_id})" if chat_id is not None else ""
    return f"Error exporting {escape(chat_name)}{suffix}: {escape(describe_error(error))}"


def disk_space_error_line(error: BaseException) -> str:
    """Build the `Disk space error: <e>` line, escaping the error text."""
    return f"Disk space error: {escape(str(error))}"


def file_progress_description(filename: str) -> str:
    """Build a Progress description for a file download (markup-safe)."""
    return escape(filename)


def _format_elapsed(elapsed_s: float) -> str:
    """Format elapsed seconds as h:mm:ss or m:ss."""
    elapsed_s = int(elapsed_s)
    if elapsed_s >= 3600:
        h = elapsed_s // 3600
        m = (elapsed_s % 3600) // 60
        s = elapsed_s % 60
        return f"{h}:{m:02d}:{s:02d}"
    m = elapsed_s // 60
    s = elapsed_s % 60
    return f"{m}:{s:02d}"


@dataclass(frozen=True)
class ChatCounters:
    """Counters accumulated since the current chat started.

    Every field here names a counter of :class:`ExportStats`; the difference is
    computed from a snapshot taken by ``begin_chat``. Declaring them once in a
    dataclass replaces nine near-identical properties whose counter names were
    repeated a third time as string keys of a snapshot dict -- a typo in one of
    those keys used to yield a silent zero.
    """

    messages_exported: int = 0
    files_downloaded: int = 0
    files_existing: int = 0
    files_reused_chat: int = 0
    files_reused_tdesktop: int = 0
    files_reused_sibling: int = 0
    files_skipped_by_size: int = 0
    files_skipped_by_type: int = 0
    data_size: int = 0


@dataclass(frozen=True)
class ChatStart:
    """Everything ``begin_chat`` fixes about the chat now being exported.

    The Live refresh thread reads these values while the event loop moves on to
    the next chat. Written field by field, the reader could pick up the new
    ``messages_in_db`` next to the counters snapshot of the previous chat and
    draw a progress bar far past its own total. One frozen object published by a
    single attribute assignment has no half-updated state to observe.
    """

    counters: ChatCounters = ChatCounters()
    messages_in_db: int = 0
    messages_total: int = 0
    started_at: float = 0.0


@dataclass(frozen=True)
class ChatView:
    """Consistent view of the current chat for one redraw of the status."""

    messages_in_db: int
    messages_total: int
    elapsed: float
    counters: ChatCounters

    @property
    def messages_done(self) -> int:
        """Messages of this chat already on disk, old and new together."""
        return self.messages_in_db + self.counters.messages_exported


@dataclass
class ExportStats:
    chats_total: int = 0
    chats_included: int = 0
    chats_skipped: int = 0
    chats_exported: int = 0
    messages_exported: int = 0
    files_downloaded: int = 0
    files_existing: int = 0  # already downloaded in previous runs
    files_reused_chat: int = 0  # reused from another chat (same account)
    files_reused_tdesktop: int = 0  # reused from tdesktop export
    files_reused_sibling: int = 0  # reused from sibling account
    files_skipped_by_size: int = 0  # exceeded max_file_size
    files_skipped_by_type: int = 0  # media type not in config
    data_size: int = 0  # bytes downloaded
    errors: list[str] = field(default_factory=list)
    # What the current chat started from; replaced whole, never edited in place.
    _chat_start: ChatStart = field(default_factory=ChatStart)

    def begin_chat(self, messages_in_db: int, messages_total: int):
        """Fix what the next chat starts from, in one publication.

        started_at is part of it: the rates on the status line divide per-chat
        counters, so they need the start of this chat -- dividing them by the
        whole run's elapsed time made the reported speed fall the longer the
        export went on.
        """
        self._chat_start = ChatStart(
            counters=ChatCounters(**{f.name: getattr(self, f.name) for f in fields(ChatCounters)}),
            messages_in_db=messages_in_db,
            messages_total=messages_total,
            started_at=time.monotonic(),
        )

    def chat_view(self) -> ChatView:
        """Read the current chat off one ChatStart, safe from the refresh thread.

        The running counters are read after ``_chat_start`` and could belong to
        the next chat already; the re-read below catches exactly that case and
        starts over. A retry is cheaper than a lock in the export loop, which
        increments these counters per message while the display reads them
        twice a second.
        """
        while True:
            start = self._chat_start
            counters = ChatCounters(
                **{
                    f.name: getattr(self, f.name) - getattr(start.counters, f.name)
                    for f in fields(ChatCounters)
                }
            )
            if self._chat_start is not start:
                continue
            elapsed = time.monotonic() - start.started_at if start.started_at else 0.0
            return ChatView(start.messages_in_db, start.messages_total, elapsed, counters)

    @property
    def messages_total(self) -> int:
        """Messages the current chat has in Telegram, 0 when unknown."""
        return self._chat_start.messages_total

    @property
    def messages_in_db(self) -> int:
        """Messages of the current chat already stored before this run."""
        return self._chat_start.messages_in_db

    @property
    def chat_elapsed(self) -> float:
        """Seconds since the current chat started, 0 before the first chat."""
        return self.chat_view().elapsed

    @property
    def per_chat(self) -> ChatCounters:
        """Counters of the current chat: current values minus the snapshot."""
        return self.chat_view().counters


_BIDI_CONTROL_CHARS = "".join(
    chr(c)
    for c in (
        0x200B,
        0x200C,
        0x200D,
        0x200E,
        0x200F,
        0x202A,
        0x202B,
        0x202C,
        0x202D,
        0x202E,
        0x2066,
        0x2067,
        0x2068,
        0x2069,
        0xFEFF,
    )
)
_BIDI_REMOVE_RE = re.compile("[" + re.escape(_BIDI_CONTROL_CHARS) + "]")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_name(name: str) -> str:
    """Make a Telegram-controlled string safe to use as a path component.

    Why: chat/folder names come from Telegram and may contain "..", control
    characters, RTL/bidi overrides, or be empty. Each of these can produce
    unintended paths or visually mislead the user.
    """
    name = unicodedata.normalize("NFKC", name)
    name = _BIDI_REMOVE_RE.sub("", name)
    name = _CONTROL_CHARS_RE.sub("_", name)
    name = name.strip()
    name = re.sub(r'[/\\:*?"<>|]', "_", name)
    name = name.replace(" ", "_")
    if name in ("", ".", ".."):
        return "_"
    # Limit length so result fits ext4/HFS+ limit (255 bytes); reserve some
    # bytes for the appended _<chat_id>.
    encoded = name.encode("utf-8")
    if len(encoded) > 200:
        encoded = encoded[:200]
        # Avoid splitting a multi-byte UTF-8 sequence
        try:
            name = encoded.decode("utf-8")
        except UnicodeDecodeError:
            name = encoded.decode("utf-8", errors="ignore")
    return name


def resolve_chat_dir(
    base: Path,
    chat_name: str,
    chat_id: int,
    folder: str | None,
    is_left: bool,
    is_archived: bool = False,
) -> Path:
    """Resolve output directory for a chat."""
    dir_name = f"{sanitize_name(chat_name)}_{chat_id}"
    if is_left:
        return base / "left" / dir_name
    if is_archived:
        return base / "archived" / dir_name
    if folder:
        return base / "folders" / sanitize_name(folder) / dir_name
    return base / "unfiled" / dir_name


def resolve_monoforum_dir(
    base: Path,
    channel_name: str,
    channel_id: int,
    monoforum_name: str,
    monoforum_id: int,
    folder: str | None,
) -> Path:
    """Resolve directory for monoforum inside channel folder."""
    channel_dir_name = f"{sanitize_name(channel_name)}_{channel_id}"
    mono_dir_name = f"{sanitize_name(monoforum_name)}_{monoforum_id}"
    if folder:
        return base / "folders" / sanitize_name(folder) / channel_dir_name / mono_dir_name
    return base / "unfiled" / channel_dir_name / mono_dir_name


def should_combine_migration(chat: Chat) -> bool:
    """Check if chat has migrated to a supergroup."""
    return chat.migrated_to_id is not None


def group_by_topic(messages: list[Message], topics: list[ForumTopic]) -> dict[int, list[Message]]:
    """Group messages by topic_id."""
    grouped: dict[int, list[Message]] = {}
    for topic in topics:
        grouped[topic.id] = []
    for msg in messages:
        tid = msg.topic_id or 0
        if tid not in grouped:
            grouped[tid] = []
        grouped[tid].append(msg)
    return grouped


class _MediaPipeline:
    """Keeps several media downloads of one chat in flight at a time.

    Awaiting each download inside the message loop made the configured
    `concurrent_downloads` meaningless: a full request-wait-response round trip
    to Telegram sat between two files, and the message iterator did not fetch
    the next batch while a file was being downloaded.

    A message may only be stored once its media is on disk -- the record
    carries the local path -- so the pipeline hands a message back only after
    its own download finished, and always in arrival order: the download
    awaited is the oldest one in flight.
    """

    def __init__(self, exporter: Exporter, chat_dir: Path, stats: ExportStats, chat_id: int, limit: int):
        self._exporter = exporter
        self._chat_dir = chat_dir
        self._stats = stats
        self._chat_id = chat_id
        # A window of one is exactly the previous sequential behaviour.
        self._limit = max(1, limit)
        self._pending: deque[tuple[asyncio.Task, Message]] = deque()

    async def __aenter__(self) -> _MediaPipeline:
        return self

    async def __aexit__(self, *exc_info) -> bool:
        # Whatever is still in flight when the loop is left -- a break, an
        # error, a cancellation -- must not outlive the chat export.
        for task, _ in self._pending:
            task.cancel()
        if self._pending:
            await asyncio.gather(*(task for task, _ in self._pending), return_exceptions=True)
            self._pending.clear()
        return False

    async def submit(self, msg: Message, tl_msg) -> list[Message]:
        """Start this message's download; return messages that are now ready."""
        task = asyncio.create_task(
            self._exporter._process_media(msg, tl_msg, self._chat_dir, self._stats, chat_id=self._chat_id)
        )
        self._pending.append((task, msg))
        ready = []
        while len(self._pending) >= self._limit:
            ready.append(await self._take_oldest())
        return ready

    async def drain(self) -> list[Message]:
        """Wait for every download still in flight."""
        ready = []
        while self._pending:
            ready.append(await self._take_oldest())
        return ready

    async def _take_oldest(self) -> Message:
        task, msg = self._pending.popleft()
        # _process_media records its own failures in stats and does not raise.
        await task
        return msg


def phase_two_kwargs(iter_kwargs: dict[str, Any], *, oldest_msg_id: int, last_msg_id: int) -> dict[str, Any]:
    """Arguments for the descending pass over a chat.

    Telethon applies ``offset_date`` only when ``offset_id`` is zero, so the
    date is dropped whenever an id anchor exists. With no lower bound recorded
    but a known upper one -- what an interruption between the phases leaves
    behind -- the pass starts at ``last_msg_id``: it used to start at the newest
    message instead and walk the whole chat again.
    """
    kwargs = dict(iter_kwargs)
    anchor = oldest_msg_id or last_msg_id
    if anchor > 0:
        kwargs.pop("offset_date", None)
        kwargs["offset_id"] = anchor
    return kwargs


class StatusView:
    """Two status lines of the live display, formatted from ExportStats.

    Was three closures inside ``Exporter.run`` capturing ``stats``,
    ``start_time`` and the progress widgets, which made them unreadable and
    untestable apart from a 259-line body.
    """

    def __init__(self, stats: ExportStats, start_time: float):
        self.stats = stats
        self.start_time = start_time

    def line1(self) -> str:
        """Chats, messages, transferred bytes and elapsed time."""
        stats = self.stats
        elapsed = time.monotonic() - self.start_time
        elapsed_str = _format_elapsed(elapsed)
        # One view for the whole line: this runs on the Live refresh thread,
        # and reading the counters and the chat totals separately let a chat
        # boundary fall between them.
        view = stats.chat_view()
        # Both rates below count what this chat produced, so they are measured
        # from the start of this chat and not from the start of the run.
        chat_elapsed = view.elapsed
        chat_data = view.counters.data_size
        speed_str = format_speed(chat_data, chat_elapsed) if chat_data > 0 else ""

        # Per-chat message counts
        chat_msgs = view.counters.messages_exported
        msgs_done = view.messages_done
        msgs_str = f"[cyan]{msgs_done}"
        if view.messages_total > 0:
            msgs_str += f"/{view.messages_total}"
        msgs_str += "[/]"
        if chat_msgs > 0:
            msgs_str += f" ([green]+{chat_msgs}[/]"
            if chat_elapsed > 0:
                msgs_str += f", [green]{chat_msgs / chat_elapsed:.0f}/s[/]"
            msgs_str += ")"

        line = f"  chats: [cyan]{stats.chats_exported}/{stats.chats_included}[/] | msgs: {msgs_str}"
        line += f" | data: [cyan]{format_size(chat_data)}[/]"
        if speed_str:
            line += f" ([green]{speed_str}[/])"
        line += f" | elapsed: {elapsed_str}"
        return line

    def line2(self) -> str:
        """Where the files of the current chat came from."""
        stats = self.stats.per_chat
        parts = [f"  files: [cyan]{stats.files_downloaded}[/] downloaded"]
        if stats.files_existing:
            parts.append(f"[green]{stats.files_existing}[/] existing")
        if stats.files_reused_chat:
            parts.append(f"[green]{stats.files_reused_chat}[/] from_chat")
        if stats.files_reused_tdesktop:
            parts.append(f"[green]{stats.files_reused_tdesktop}[/] from_tdesktop")
        if stats.files_reused_sibling:
            parts.append(f"[green]{stats.files_reused_sibling}[/] from_sibling")
        skipped = []
        if stats.files_skipped_by_size:
            skipped.append(f"[yellow]{stats.files_skipped_by_size}[/] by_size")
        if stats.files_skipped_by_type:
            skipped.append(f"[yellow]{stats.files_skipped_by_type}[/] by_type")
        if skipped:
            parts.append(f"skipped: {', '.join(skipped)}")
        return " | ".join(parts)

    def lines(self) -> str:
        return f"{self.line1()}\n{self.line2()}"


class Exporter:
    def __init__(
        self,
        api: TgApi,
        state: ExportState,
        config: Config,
        renderer: HtmlRenderer,
        downloader: MediaDownloader,
        account: str,
        quiet: bool = False,
    ):
        self.api = api
        self.state = state
        self.config = config
        self.renderer = renderer
        self.downloader = downloader
        self.account = account
        # When True, progress (Live display) and routine status lines are
        # suppressed; errors and shutdown notices are still printed.
        self.quiet = quiet
        self._shutdown = False
        self._force_shutdown = False
        self._first_signal_time: float = 0
        self._use_live: bool = False
        # Signal number that triggered shutdown (SIGINT / SIGTERM), or None.
        # The CLI maps it to exit code 128 + signum (130 for SIGINT, 143 for SIGTERM).
        self._shutdown_signal: int | None = None
        # Set by run(); the only task a forced shutdown cancels.
        self._main_task: asyncio.Task | None = None

    @property
    def shutdown_requested(self) -> bool:
        """A shutdown signal arrived; the run is finishing what it can."""
        return self._shutdown

    @property
    def force_shutdown(self) -> bool:
        """A second signal arrived; nothing more is written or rendered."""
        return self._force_shutdown

    @property
    def shutdown_signal(self) -> int | None:
        """Signal that stopped the run, for the ``128 + signum`` exit code."""
        return self._shutdown_signal

    def _report_exported(self, count: int, noun: str, failed: int = 0) -> None:
        """Report what a section of global data produced, failures included.

        A success counter without a failure counter reads as completeness: 10
        of 60 profile photos printed as `exported: 10 profile photos`.
        """
        line = f"  [green]exported[/]: {count} {noun}"
        if failed:
            line += f", {failed} failed"
            logger.warning("%s: %d of %d could not be downloaded", noun, failed, count + failed)
        self._status_print(line)

    def _status_print(self, *args, **kwargs) -> None:
        """Print a non-essential status line unless running in quiet mode."""
        if self.quiet:
            return
        console.print(*args, **kwargs)

    def _snapshot_active_downloads(self) -> dict[int, DownloadProgress]:
        """Read a consistent copy of the downloader's active downloads."""
        return self.downloader.snapshot_active_downloads()

    def _build_status_table(
        self,
        progress: Progress,
        main_task: TaskID,
        file_progress: Progress,
        file_tasks: dict[int, TaskID],
        stats: ExportStats,
        line1: str,
        line2: str,
    ) -> Table:
        """Build the live status table.

        Called from the Live refresh thread; reads ``active_downloads`` via a
        lock-protected snapshot to avoid ``RuntimeError: dictionary changed size
        during iteration`` when the event loop mutates it concurrently.
        """
        view = stats.chat_view()
        if view.messages_total > 0:
            progress.update(main_task, completed=view.messages_done, total=view.messages_total)
        else:
            progress.update(main_task, completed=view.messages_done, total=None)

        active = self._snapshot_active_downloads()
        for msg_id in list(file_tasks):
            if msg_id not in active:
                file_progress.remove_task(file_tasks.pop(msg_id))
        for msg_id, dl in active.items():
            desc = file_progress_description(dl.filename)
            if msg_id not in file_tasks:
                file_tasks[msg_id] = file_progress.add_task(
                    desc,
                    total=dl.total or None,
                    completed=dl.received,
                )
            else:
                file_progress.update(
                    file_tasks[msg_id],
                    description=desc,
                    completed=dl.received,
                    total=dl.total or None,
                )

        table = Table.grid()
        table.add_row(progress)
        t1 = Text.from_markup(line1)
        t1.no_wrap = True
        t1.overflow = "ellipsis"
        table.add_row(t1)
        t2 = Text.from_markup(line2)
        t2.no_wrap = True
        t2.overflow = "ellipsis"
        table.add_row(t2)
        table.add_row(file_progress)
        return table

    def _install_signal_handlers(self) -> None:
        """Arrange for Ctrl+C and SIGTERM to stop the export gracefully."""
        # The task to cancel on a forced shutdown. Cancelling every task
        # instead would also cancel the inner tasks asyncio.shield creates,
        # undoing the protection exactly in the case it exists for.
        self._main_task = asyncio.current_task()

        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, functools.partial(self._handle_shutdown, signum))
            except NotImplementedError:
                # Windows event loops do not implement it; there Ctrl+C arrives
                # as a KeyboardInterrupt, which the CLI turns into exit code 130.
                logger.debug("signal handler for %s is not available on this platform", signum)

    def _select_chats(self, chat_list: list[Chat], stats: ExportStats) -> list[tuple[Chat, ChatExportConfig]]:
        """Keep the chats the config asks for; count the rest as skipped."""
        included: list[tuple[Chat, ChatExportConfig]] = []
        for chat in chat_list:
            # Check left/archived actions before resolve_chat_config
            if chat.is_left and self.config.left_channels_action == "skip":
                stats.chats_skipped += 1
                continue
            if chat.is_archived and self.config.archived_action == "skip":
                stats.chats_skipped += 1
                continue

            chat_config = self.config.resolve_chat_config(
                chat_id=chat.id,
                chat_name=chat.name,
                folder=chat.folder,
                chat_type=chat.type.value,
            )
            if chat_config is None:
                stats.chats_skipped += 1
            else:
                included.append((chat, chat_config))
        stats.chats_total = len(chat_list)
        stats.chats_included = len(included)
        return included

    def _announce_start(self, stats: ExportStats, *, dry_run: bool) -> None:
        """Print what the run is about to do, before the live display starts."""
        mode_str = "[bold yellow]DRY-RUN[/]" if dry_run else "[bold green]EXPORT[/]"
        self._status_print(
            f"\n{mode_str}: {stats.chats_included} chats to export, "
            f"{stats.chats_skipped} skipped (total {stats.chats_total})"
        )
        if self.config.defaults.date_from or self.config.defaults.date_to:
            df = self.config.defaults.date_from or "..."
            dt = self.config.defaults.date_to or "..."
            self._status_print(f"[dim]date range: {df} — {dt}[/]")
        self._status_print(f"[dim]started at {datetime.now().strftime('%H:%M:%S')}[/]\n")

    def _make_progress_widgets(self):
        """Build the two Progress widgets of the live display."""
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        )
        main_task = progress.add_task("", total=None)

        # Separate progress for file downloads
        file_progress = Progress(
            TextColumn("    [dim]{task.description}[/]"),
            BarColumn(bar_width=20),
            DownloadColumn(binary_units=True),
            TransferSpeedColumn(),
            console=console,
        )
        # Track which msg_ids have progress tasks
        file_tasks: dict[int, TaskID] = {}  # msg_id -> task_id
        return progress, main_task, file_progress, file_tasks

    async def _export_chat_entry(
        self,
        chat: Chat,
        chat_config: ChatExportConfig,
        output_base: Path,
        stats: ExportStats,
        progress: Progress,
        main_task: TaskID,
    ) -> bool:
        """Export one chat. Returns False when the whole run must stop.

        A failure of a single chat is recorded and the run goes on; running out
        of disk space or a forced shutdown ends it.
        """
        chat_dir = resolve_chat_dir(
            base=output_base,
            chat_name=chat.name,
            chat_id=chat.id,
            folder=chat.folder,
            is_left=chat.is_left,
            is_archived=chat.is_archived,
        )

        # Save chat metadata to DB for future renderers
        await self.state.cache_catalog(
            chat_id=chat.id,
            name=chat.name,
            chat_type=chat.type.value,
            folder=chat.folder,
            members_count=chat.members_count,
            messages_count=chat.messages_count or 0,
            last_message_date=chat.last_message_date,
            is_left=chat.is_left,
            is_archived=chat.is_archived,
            is_forum=chat.is_forum,
            is_monoforum=chat.is_monoforum,
        )

        try:
            progress.update(main_task, description=chat_progress_description(chat.name))
            logger.debug(
                "start chat %s (id=%d, type=%s, msgs~%d)",
                chat.name,
                chat.id,
                chat.type.value,
                chat.messages_count or 0,
            )

            # Remove orphaned files (on disk but not in DB)
            await self._cleanup_orphaned_files(chat.id, chat_dir)

            # Load tdesktop index for this chat (off the loop -- regex/HTML parsing).
            for idx in self.downloader.tdesktop_indexes:
                await asyncio.to_thread(idx.load_chat_index, chat.name)

            chat_t0 = time.monotonic()
            msgs_before = stats.messages_exported
            await self.export_chat(chat, chat_config, chat_dir, stats)
            chat_msgs = stats.messages_exported - msgs_before
            logger.debug("done chat %s in %.1fs: %d msgs", chat.name, time.monotonic() - chat_t0, chat_msgs)
            stats.chats_exported += 1

            # Unload tdesktop index to free memory
            for idx in self.downloader.tdesktop_indexes:
                idx.unload_chat_index()

        except DiskSpaceError as e:
            console.print(disk_space_error_line(e))
            stats.errors.append(str(e))
            return False
        except asyncio.CancelledError:
            console.print("[yellow]Force shutdown during export...[/]")
            return False
        except Exception as e:
            logger.warning("Export failed: chat_id=%s name=%s", chat.id, chat.name, exc_info=True)
            console.print(chat_error_line(chat.name, e, chat_id=chat.id))
            stats.errors.append(f"{chat.name} (id={chat.id}): {describe_error(e)}")
        return True

    async def run(
        self,
        dry_run: bool = False,
        verify: bool = False,
        chat_list: list[Chat] | None = None,
    ) -> ExportStats:
        """Main export loop."""
        stats = ExportStats()

        self._install_signal_handlers()

        if chat_list is None:
            return stats

        output_base = Path(self.config.output.path)

        included_chats = self._select_chats(chat_list, stats)

        start_time = time.monotonic()
        self._announce_start(stats, dry_run=dry_run)

        progress, main_task, file_progress, file_tasks = self._make_progress_widgets()
        status = StatusView(stats, start_time)

        def _build_status_table_local() -> Table:
            return self._build_status_table(
                progress=progress,
                main_task=main_task,
                file_progress=file_progress,
                file_tasks=file_tasks,
                stats=stats,
                line1=status.line1(),
                line2=status.line2(),
            )

        # Quiet mode disables the Live progress display; non-TTY also disables it.
        use_live = console.is_terminal and not self.quiet
        self._use_live = use_live

        live_cm: contextlib.AbstractContextManager[object] = (
            Live(
                console=console,
                refresh_per_second=2,
                get_renderable=_build_status_table_local,
            )
            if use_live
            else contextlib.nullcontext()
        )

        try:
            with live_cm:
                last_log_time = start_time
                for chat, chat_config in included_chats:
                    if self._shutdown:
                        console.print("[yellow]Shutdown requested, saving state...[/]")
                        break

                    if dry_run:
                        self._status_print(
                            chat_export_line(
                                chat_name=chat.name,
                                chat_type=chat.type.value,
                                folder=chat.folder,
                                is_left=chat.is_left,
                                is_archived=chat.is_archived,
                            )
                        )
                        stats.chats_exported += 1
                        continue

                    if not await self._export_chat_entry(
                        chat, chat_config, output_base, stats, progress, main_task
                    ):
                        break

                    # Log progress periodically for non-TTY (suppressed in quiet mode)
                    now = time.monotonic()
                    if (
                        not use_live
                        and not self.quiet
                        and (now - last_log_time >= 10 or stats.chats_exported % 10 == 0)
                    ):
                        _log(Text.from_markup(status.lines()).plain)
                        last_log_time = now

        except asyncio.CancelledError:
            self._force_shutdown = True

        # Export global data (personal info, contacts, sessions, etc.)
        if not dry_run and not self._force_shutdown and not self._shutdown:
            try:
                self._status_print("\n[cyan]Exporting global data...[/]")
                await self.export_global_data()
            except Exception as e:
                logger.warning("Failed to export global data: %s", e, exc_info=True)

        if verify and not self._force_shutdown:
            await self._verify_files(stats)

        return stats

    async def export_chat(
        self,
        chat: Chat,
        chat_config: ChatExportConfig,
        chat_dir: Path,
        stats: ExportStats,
    ):
        """Export a single chat with batch processing.

        Updates stats in-place so Live widget reflects real-time progress.

        Two-phase fetch:
        1. New messages: iter_messages(min_id=last_msg_id) — newest first
        2. Old messages: if not full_history, iter_messages(offset_id=oldest_msg_id)
           — continues fetching older messages from where we left off
        """
        chat_start = time.monotonic()
        date_from = chat_config.date_from
        date_to = chat_config.date_to
        has_date_filter = bool(date_from or date_to)
        chat_total = await self._count_chat_messages(chat, has_date_filter=has_date_filter)

        chat_state = await self.state.get_chat_state(chat.id)
        last_msg_id = chat_state["last_msg_id"] if chat_state else 0
        oldest_msg_id = chat_state["oldest_msg_id"] if chat_state else 0
        full_history = bool(chat_state["full_history"]) if chat_state else False

        # Count messages already in DB and init per-chat snapshot
        messages_in_db = await self.state.count_messages(chat.id)
        stats.begin_chat(
            messages_in_db=messages_in_db,
            messages_total=chat_total if not has_date_filter else 0,
        )

        def progress_line() -> str:
            return self._chat_progress_line(chat, stats, chat_total, chat_start)

        def before_date_from(msg_date) -> bool:
            """True if message is before date_from (should stop)."""
            if not date_from or not msg_date:
                return False
            return msg_date.date() < date_from

        # Build iter_messages kwargs for date filtering
        iter_kwargs: dict = {}
        if date_to:
            # Start from messages at date_to end-of-day
            iter_kwargs["offset_date"] = datetime.combine(date_to + timedelta(days=1), datetime.min.time())

        if last_msg_id > 0:
            await self._fetch_new_messages(
                chat,
                chat_dir,
                stats,
                last_msg_id=last_msg_id,
                iter_kwargs=iter_kwargs,
                before_date_from=before_date_from,
                progress_line=progress_line,
                keep_service=chat_config.export_service_messages,
            )

        if not full_history and not self._shutdown:
            await self._fetch_old_messages(
                chat,
                chat_dir,
                stats,
                last_msg_id=last_msg_id,
                oldest_msg_id=oldest_msg_id,
                iter_kwargs=iter_kwargs,
                before_date_from=before_date_from,
                progress_line=progress_line,
                keep_service=chat_config.export_service_messages,
            )
        else:
            # Phase 2 skipped (full_history already True or shutdown). Still
            # refresh messages_count: phase 1 batches may have added new messages.
            msg_count = await self.state.count_messages(chat.id)
            if msg_count > 0:
                await self.state.update_messages_count(chat.id, msg_count)

        await self._render_chat_html(chat, chat_dir, stats)
        return stats

    @staticmethod
    def _pages_are_current(chat_dir: Path, stats: ExportStats) -> bool:
        """True when the pages on disk already show everything the database holds.

        The render used to run on every chat of every run: all pages were
        deleted and rebuilt month by month -- loading each month from SQLite and
        deserialising the JSON columns of every message -- even when the run
        brought neither a message nor a file. Anything written during this chat
        changes what a page shows, so any non-zero counter forces the rebuild.
        """
        chat = stats.per_chat
        written = (
            chat.messages_exported
            + chat.files_downloaded
            + chat.files_reused_chat
            + chat.files_reused_tdesktop
            + chat.files_reused_sibling
        )
        if written:
            return False
        return any(chat_dir.glob("messages*.html")) if chat_dir.is_dir() else False

    @staticmethod
    def _keep_message(msg: Message, *, export_service_messages: bool) -> bool:
        """Whether a message belongs in the export.

        `export_service_messages: false` used to change nothing: the option was
        parsed, carried into every ChatExportConfig and read by no one.
        """
        return export_service_messages or msg.action is None

    async def _count_chat_messages(self, chat: Chat, *, has_date_filter: bool) -> int:
        """Total number of messages in the chat, for the progress bar.

        Telegram cannot count messages inside a date range, so a filtered
        export shows the running count without a total.
        """
        try:
            if has_date_filter:
                return 0
            result = await self.api.client.get_messages(chat.id, limit=0)
            return getattr(result, "total", 0) or 0
        except Exception as e:
            logger.debug("chat %s: message count unavailable (%s), using the catalog value", chat.id, e)
            return chat.messages_count or 0

    def _chat_progress_line(self, chat: Chat, stats: ExportStats, chat_total: int, chat_start: float) -> str:
        """One progress line for a chat, used when the live display is off."""
        chat_msgs = stats.per_chat.messages_exported
        elapsed = time.monotonic() - chat_start
        parts = [f"  {chat.name}: {chat_msgs}"]
        if chat_total > 0:
            parts[0] += f"/{chat_total}"
        parts.append("msgs")
        parts.append(f"{stats.per_chat.files_downloaded} files")
        parts.append(format_size(stats.per_chat.data_size))
        if elapsed > 0 and chat_msgs > 0:
            parts.append(f"({chat_msgs / elapsed:.0f} msg/s)")
        return "  ".join(parts)

    async def _fetch_new_messages(
        self,
        chat: Chat,
        chat_dir: Path,
        stats: ExportStats,
        *,
        last_msg_id: int,
        iter_kwargs: dict[str, Any],
        before_date_from,
        progress_line,
        keep_service: bool = True,
    ) -> None:
        """Phase 1: everything newer than the stored pointer, newest first."""
        new_max_id = last_msg_id
        batch: list[Message] = []
        last_progress_time = time.monotonic()
        p1_kwargs = {"min_id": last_msg_id}
        if "offset_date" in iter_kwargs:
            p1_kwargs["offset_date"] = iter_kwargs["offset_date"]
        async with _MediaPipeline(self, chat_dir, stats, chat.id, self._download_window()) as media:
            async for tl_msg in self.api.iter_messages(chat.id, **p1_kwargs):
                if self._shutdown:
                    break
                if before_date_from(tl_msg.date):
                    break
                msg = convert_message(tl_msg, chat_id=chat.id)
                # The pointer still advances over a skipped message: it was
                # seen, and re-fetching it on the next run would change nothing.
                if msg.id > new_max_id:
                    new_max_id = msg.id
                if not self._keep_message(msg, export_service_messages=keep_service):
                    continue
                # A message joins the batch only once its own media is on
                # disk, so the stored record carries the local path.
                batch.extend(await media.submit(msg, tl_msg))
                stats.messages_exported += 1
                if len(batch) >= BATCH_SIZE:
                    await self.state.store_messages_batch(batch)
                    logger.debug("  %s: %d new msgs stored", chat.name, stats.messages_exported)
                    batch.clear()
                now = time.monotonic()
                if not self._use_live and now - last_progress_time >= LOG_INTERVAL:
                    _log(progress_line())
                    last_progress_time = now
            batch.extend(await media.drain())
        if batch:
            await self.state.store_messages_batch(batch)
            batch.clear()
        # Only a phase 1 that ran to the end may move the pointer. It walks
        # from the newest message down to last_msg_id, so "everything above
        # this id is exported" holds only once the walk finished. On a
        # shutdown mid-walk the untouched interval between the old pointer
        # and the interruption point would never be fetched again: phase 2
        # descends from oldest_msg_id and never enters it. Leaving the
        # pointer alone costs a re-fetch of the messages already stored --
        # store_messages_batch upserts them and their media is recognised as
        # already downloaded.
        if new_max_id > last_msg_id and not self._shutdown:
            await self.state.set_last_msg_id(chat.id, new_max_id)
        logger.debug("  %s: phase 1 done", chat.name)

    async def _fetch_old_messages(
        self,
        chat: Chat,
        chat_dir: Path,
        stats: ExportStats,
        *,
        last_msg_id: int,
        oldest_msg_id: int,
        iter_kwargs: dict[str, Any],
        before_date_from,
        progress_line,
        keep_service: bool = True,
    ) -> None:
        """Phase 2: continue downward from the oldest message fetched so far."""
        batch: list[Message] = []
        current_oldest = oldest_msg_id
        phase2_max_id = last_msg_id
        # The date_from/date_to bounds are still enforced per-message via
        # before_date_from below.
        p2_kwargs = phase_two_kwargs(iter_kwargs, oldest_msg_id=oldest_msg_id, last_msg_id=last_msg_id)

        reached_date_from = False
        iterator_exhausted = False
        last_progress_time = time.monotonic()
        async with _MediaPipeline(self, chat_dir, stats, chat.id, self._download_window()) as media:
            async for tl_msg in self.api.iter_messages(chat.id, **p2_kwargs):
                if self._shutdown:
                    break
                if before_date_from(tl_msg.date):
                    reached_date_from = True
                    break
                msg = convert_message(tl_msg, chat_id=chat.id)
                if current_oldest == 0 or msg.id < current_oldest:
                    current_oldest = msg.id
                if msg.id > phase2_max_id:
                    phase2_max_id = msg.id
                if not self._keep_message(msg, export_service_messages=keep_service):
                    continue
                batch.extend(await media.submit(msg, tl_msg))
                stats.messages_exported += 1
                if len(batch) >= BATCH_SIZE:
                    await self.state.store_messages_batch(batch)
                    logger.debug(
                        "  %s: %d msgs stored (oldest=%d)",
                        chat.name,
                        stats.messages_exported,
                        current_oldest,
                    )
                    batch.clear()
                now = time.monotonic()
                if not self._use_live and now - last_progress_time >= LOG_INTERVAL:
                    _log(progress_line())
                    last_progress_time = now
            else:
                # for/else: iterator exhausted naturally (no break)
                iterator_exhausted = True
            batch.extend(await media.drain())

        if batch:
            await self.state.store_messages_batch(batch)
            batch.clear()

        # Why atomic commit: phase 2 used to issue 4 separate commits
        # (last/oldest/full_history/messages_count). A network/process
        # interruption between them left inconsistent state -- e.g.
        # set_oldest_msg_id firing on a chat with no row and failing the
        # INSERT with NOT NULL constraint failed: export_state.last_msg_id.
        new_full = not self._shutdown and (reached_date_from or iterator_exhausted)
        await self.state.commit_phase_progress(
            chat_id=chat.id,
            last_msg_id=max(last_msg_id, phase2_max_id),
            oldest_msg_id=current_oldest if current_oldest > 0 else oldest_msg_id,
            full_history=new_full,
            messages_count=await self.state.count_messages(chat.id),
        )
        if new_full:
            logger.debug("  %s: full history complete", chat.name)

    async def _render_chat_html(self, chat: Chat, chat_dir: Path, stats: ExportStats) -> None:
        """Render the chat pages, one month at a time, off the event loop."""
        if self._pages_are_current(chat_dir, stats):
            logger.debug("  %s: nothing new, keeping the rendered pages", chat.name)
            return
        # Streaming month-by-month from SQLite avoids loading the full message
        # list into memory.
        month_keys = await self.state.list_message_months(chat.id)
        if not month_keys:
            logger.debug("  %s: no messages in DB, skipping render", chat.name)
            return

        # Why: Jinja2 render is CPU-bound and blocks the event loop;
        # _load_messages_for_month_sync uses a sync sqlite connection inside the
        # worker thread to avoid mixing aiosqlite with to_thread.
        db_path = self.state.db_path
        chat_id = chat.id

        def _render():
            from tg_export.state import _load_messages_for_month_sync

            load_month = lambda key: _load_messages_for_month_sync(db_path, chat_id, key)  # noqa: E731
            # Why should_stop: render runs inside asyncio.to_thread; the
            # worker thread cannot be cancelled by task.cancel, so without
            # a checkpoint between months the default executor blocks
            # asyncio shutdown until the entire chat is rendered.
            self.renderer.render_chat_streaming(
                chat, month_keys, load_month, chat_dir, should_stop=lambda: self._shutdown
            )

        await asyncio.to_thread(_render)

    def _download_window(self) -> int:
        """How many media downloads may be in flight at once.

        Comes from `concurrent_downloads`; anything unusable falls back to one,
        which is the sequential behaviour.
        """
        limit = getattr(getattr(self.downloader, "config", None), "concurrent_downloads", 1)
        return limit if isinstance(limit, int) and limit > 0 else 1

    async def _process_media(
        self, msg: Message, tl_msg, chat_dir: Path, stats: ExportStats, chat_id: int = 0
    ):
        """Download media for a message, updating stats."""
        if not msg.media or not self.downloader:
            return
        try:
            local_path, status = await self.downloader.download(tl_msg, msg.media, chat_dir, chat_id=chat_id)
            if local_path and msg.media.file:
                msg.media.file.local_path = str(local_path)
            if status == "downloaded":
                stats.files_downloaded += 1
                if local_path and local_path.exists():
                    stats.data_size += local_path.stat().st_size
            elif status == "existing":
                stats.files_existing += 1
            elif status == "reused_chat":
                stats.files_reused_chat += 1
                if local_path and local_path.exists():
                    stats.data_size += local_path.stat().st_size
            elif status == "reused_tdesktop":
                stats.files_reused_tdesktop += 1
                if local_path and local_path.exists():
                    stats.data_size += local_path.stat().st_size
            elif status == "reused_sibling":
                stats.files_reused_sibling += 1
                if local_path and local_path.exists():
                    stats.data_size += local_path.stat().st_size
            elif status == "skipped_by_size":
                stats.files_skipped_by_size += 1
            elif status == "skipped_by_type":
                stats.files_skipped_by_type += 1
        except Exception as e:
            # Without this line the count in the summary was all that survived:
            # the exception never reached logging, so no log level could
            # recover what had failed and why.
            logger.warning("Media error: chat_id=%s msg_id=%s", chat_id, msg.id, exc_info=True)
            stats.errors.append(f"Media error msg {msg.id} (chat {chat_id}): {describe_error(e)}")

    async def export_global_data(self):
        """Export personal_info, userpics, stories, contacts, sessions, etc."""
        if self.config.personal_info:
            try:
                await self._export_personal_info()
            except Exception as e:
                logger.warning("Failed to export personal info: %s", e, exc_info=True)

        if self.config.contacts:
            try:
                await self._export_contacts()
            except Exception as e:
                logger.warning("Failed to export contacts: %s", e, exc_info=True)

        if self.config.sessions:
            try:
                await self._export_sessions()
            except Exception as e:
                logger.warning("Failed to export sessions: %s", e, exc_info=True)

        if self.config.userpics:
            try:
                await self._export_userpics()
            except Exception as e:
                logger.warning("Failed to export userpics: %s", e, exc_info=True)

        if self.config.stories:
            try:
                await self._export_stories()
            except Exception as e:
                logger.warning("Failed to export stories: %s", e, exc_info=True)

        # The page holds the saved ringtones, which is what profile_music names;
        # the flags used to be joined by "or", so profile_music: false changed
        # nothing while other_data kept its default of true.
        if self.config.profile_music:
            try:
                await self._export_other_data()
            except Exception as e:
                logger.warning("Failed to export other data: %s", e, exc_info=True)

    async def _export_personal_info(self):
        """Fetch and render personal info."""
        # cast(Any): Telethon get_personal_info() returns a Union without stubs,
        # so Pyright rejects the .full_user/.users attributes. Access is safe --
        # below we use getattr with defaults.
        result = cast(Any, await self.api.get_personal_info())
        full_user = result.full_user
        user = result.users[0] if result.users else None

        photo_path = None
        if user and getattr(user, "photo", None):
            photos_dir = self.renderer.output_dir / "profile_photos"
            photos_dir.mkdir(parents=True, exist_ok=True)
            try:
                path = await self.api.client.download_profile_photo(
                    "me",
                    file=str(photos_dir / "current"),
                )
                if path:
                    photo_path = f"profile_photos/{Path(path).name}"
            except Exception as e:
                logger.debug("Failed to download profile photo: %s", e)

        user_data = {
            "first_name": getattr(user, "first_name", "") or "",
            "last_name": getattr(user, "last_name", "") or "",
            "username": getattr(user, "username", "") or "",
            "phone": getattr(user, "phone", "") or "",
            "bio": getattr(full_user, "about", "") or "",
            "user_id": getattr(user, "id", ""),
            "premium": bool(getattr(user, "premium", False)),
            "photo_path": photo_path,
        }
        # to_thread: jinja renders these pages synchronously while the Telegram
        # connection is live; in the loop thread nothing else is served meanwhile.
        # The chat render already runs this way.
        await asyncio.to_thread(self.renderer.render_personal_info, user_data)
        self._status_print("  [green]exported[/]: personal info")

    async def _export_contacts(self):
        """Fetch and render contacts list."""
        result = await self.api.get_contacts()
        users_by_id = {u.id: u for u in getattr(result, "users", [])}

        contacts = []
        for c in getattr(result, "contacts", []):
            user = users_by_id.get(c.user_id)
            if user:
                name = (
                    f"{getattr(user, 'first_name', '') or ''} {getattr(user, 'last_name', '') or ''}".strip()
                )
                contacts.append(
                    {
                        "name": name or str(c.user_id),
                        "username": getattr(user, "username", "") or "",
                        "phone": getattr(user, "phone", "") or "",
                    }
                )

        frequent = []
        # cast(Any): same as _export_personal_info -- Telethon API without stubs.
        top_result = cast(Any, await self.api.get_top_peers())
        if top_result and hasattr(top_result, "categories"):
            for cat in top_result.categories:
                for top_peer in cat.peers:
                    peer_id = None
                    if hasattr(top_peer.peer, "user_id"):
                        peer_id = top_peer.peer.user_id
                    elif hasattr(top_peer.peer, "chat_id"):
                        peer_id = top_peer.peer.chat_id
                    elif hasattr(top_peer.peer, "channel_id"):
                        peer_id = top_peer.peer.channel_id
                    user = users_by_id.get(peer_id)
                    name = ""
                    if user:
                        name = f"{getattr(user, 'first_name', '') or ''} {getattr(user, 'last_name', '') or ''}".strip()
                    frequent.append(
                        {
                            "name": name or str(peer_id),
                            "rating": f"{top_peer.rating:.2f}",
                        }
                    )

        await asyncio.to_thread(self.renderer.render_contacts, contacts, frequent)
        self._status_print(f"  [green]exported[/]: {len(contacts)} contacts, {len(frequent)} frequent")

    async def _export_sessions(self):
        """Fetch and render active sessions."""
        sessions_result, web_result = await self.api.get_sessions()

        app_sessions = []
        for auth in getattr(sessions_result, "authorizations", []):
            date_active = getattr(auth, "date_active", None)
            if isinstance(date_active, datetime):
                date_str = date_active.strftime("%Y-%m-%d %H:%M")
            elif date_active:
                date_str = datetime.fromtimestamp(date_active).strftime("%Y-%m-%d %H:%M")
            else:
                date_str = ""
            app_sessions.append(
                {
                    "app_name": getattr(auth, "app_name", ""),
                    "app_version": getattr(auth, "app_version", ""),
                    "device_model": getattr(auth, "device_model", ""),
                    "platform": getattr(auth, "platform", ""),
                    "system_version": getattr(auth, "system_version", ""),
                    "ip": getattr(auth, "ip", ""),
                    "country": getattr(auth, "country", ""),
                    "date_active": date_str,
                    "current": bool(getattr(auth, "current", False)),
                }
            )

        web_sessions = []
        for web_auth in getattr(web_result, "authorizations", []):
            date_active = getattr(web_auth, "date_active", None)
            if isinstance(date_active, datetime):
                date_str = date_active.strftime("%Y-%m-%d %H:%M")
            elif date_active:
                date_str = datetime.fromtimestamp(date_active).strftime("%Y-%m-%d %H:%M")
            else:
                date_str = ""
            web_sessions.append(
                {
                    "domain": getattr(web_auth, "domain", ""),
                    "browser": getattr(web_auth, "browser", ""),
                    "platform": getattr(web_auth, "platform", ""),
                    "ip": getattr(web_auth, "ip", ""),
                    "region": getattr(web_auth, "region", ""),
                    "date_active": date_str,
                }
            )

        await asyncio.to_thread(self.renderer.render_sessions, app_sessions, web_sessions)
        self._status_print(
            f"  [green]exported[/]: {len(app_sessions)} app sessions, {len(web_sessions)} web sessions"
        )

    async def _export_userpics(self):
        """Fetch and render profile photos."""
        photos_dir = self.renderer.output_dir / "profile_photos"
        photos_dir.mkdir(parents=True, exist_ok=True)

        photos = []
        seq = 0
        failed = 0
        async for photo in self.api.iter_userpics():
            # Use a separate per-iteration counter for the filename so a failed
            # download does not cause the next photo to reuse the same name.
            seq += 1
            try:
                path = await self.api.client.download_media(
                    photo,
                    file=str(photos_dir / f"photo_{seq}"),
                )
                if path:
                    date_str = ""
                    if hasattr(photo, "date") and photo.date:
                        date_str = photo.date.strftime("%Y-%m-%d %H:%M")
                    photos.append(
                        {
                            "path": f"profile_photos/{Path(str(path)).name}",
                            "date": date_str,
                        }
                    )
            except Exception as e:
                failed += 1
                logger.debug("Failed to download userpic %d: %s", seq, e)

        await asyncio.to_thread(self.renderer.render_userpics, photos)
        self._report_exported(len(photos), "profile photos", failed)

    async def _export_stories(self):
        """Fetch and render stories."""
        stories_dir = self.renderer.output_dir / "stories"
        stories_dir.mkdir(parents=True, exist_ok=True)

        try:
            pinned, archived = await self.api.get_stories()
        except Exception as e:
            logger.warning("Stories API not available: %s", e)
            await asyncio.to_thread(self.renderer.render_stories, [])
            return

        # Combine pinned + archived, deduplicate by id
        all_stories = {}
        for story_item in getattr(pinned, "stories", []):
            all_stories[story_item.id] = story_item
        for story_item in getattr(archived, "stories", []):
            all_stories.setdefault(story_item.id, story_item)

        stories = []
        failed = 0
        for idx, (story_id, item) in enumerate(sorted(all_stories.items())):
            photo_path = None
            video_path = None
            caption = ""

            if hasattr(item, "caption") and item.caption:
                caption = item.caption
            elif hasattr(item, "message") and item.message:
                caption = item.message

            media = getattr(item, "media", None)
            if media:
                try:
                    path = await self.api.client.download_media(
                        media,
                        file=str(stories_dir / f"story_{idx}"),
                    )
                    if path:
                        path_obj = Path(str(path))
                        rel = f"stories/{path_obj.name}"
                        if any(path_obj.suffix.lower() in ext for ext in [".mp4", ".mov", ".avi"]):
                            video_path = rel
                        else:
                            photo_path = rel
                except Exception as e:
                    failed += 1
                    logger.debug("Failed to download story %d: %s", story_id, e)

            date_str = ""
            if hasattr(item, "date") and item.date:
                date_str = item.date.strftime("%Y-%m-%d %H:%M")

            stories.append(
                {
                    "photo_path": photo_path,
                    "video_path": video_path,
                    "caption": caption,
                    "date": date_str,
                }
            )

        await asyncio.to_thread(self.renderer.render_stories, stories)
        self._report_exported(len(stories), "stories", failed)

    async def _export_other_data(self):
        """Fetch and render ringtones and other data."""
        ringtones_dir = self.renderer.output_dir / "ringtones"
        ringtones = []
        failed = 0

        try:
            result = await self.api.get_ringtones()
            doc_list = list(getattr(result, "ringtones", []) or [])
            if doc_list:
                ringtones_dir.mkdir(parents=True, exist_ok=True)
                for idx, doc in enumerate(doc_list):
                    name = f"ringtone_{idx}"
                    for attr in getattr(doc, "attributes", []):
                        if hasattr(attr, "file_name") and attr.file_name:
                            name = attr.file_name
                            break

                    path = None
                    try:
                        path = await self.api.client.download_media(
                            doc,
                            file=str(ringtones_dir / f"ringtone_{idx}"),
                        )
                    except Exception as e:
                        failed += 1
                        logger.debug("Failed to download ringtone %d: %s", idx, e)

                    size_str = ""
                    if hasattr(doc, "size") and doc.size:
                        size_str = format_size(doc.size)

                    ringtones.append(
                        {
                            "name": name,
                            "path": f"ringtones/{Path(str(path)).name}" if path else None,
                            "size": size_str,
                        }
                    )
        except Exception as e:
            logger.warning("Failed to fetch ringtones: %s", e)

        await asyncio.to_thread(self.renderer.render_other_data, {"ringtones": ringtones})
        if ringtones or failed:
            self._report_exported(len(ringtones), "ringtones", failed)

    async def _verify_files(self, stats: ExportStats):
        """Verify integrity of downloaded files and re-download broken ones."""
        broken = await self.state.get_files_to_verify()
        if not broken:
            return

        self._status_print(f"[yellow]Found {len(broken)} files to re-download[/]")
        errors_before = len(stats.errors)
        redownloaded = 0
        for f in broken:
            if self._shutdown:
                break
            chat_id = f["chat_id"]
            msg_id = f["msg_id"]
            local_path = Path(f["local_path"])
            try:
                # Get original message from Telegram
                tl_messages = await self.api.client.get_messages(chat_id, ids=msg_id)
                tl_msg = (
                    tl_messages
                    if not isinstance(tl_messages, list)
                    else (tl_messages[0] if tl_messages else None)
                )
                if tl_msg is None or tl_msg.media is None:
                    stats.errors.append(f"Cannot re-download: msg {msg_id} not found or no media")
                    continue

                # Remove broken file
                if local_path.exists():
                    local_path.unlink()

                # Re-download to same directory
                target_dir = local_path.parent
                target_dir.mkdir(parents=True, exist_ok=True)
                path = await self.api.download_media(tl_msg, target_dir)
                if path:
                    actual_size = Path(str(path)).stat().st_size
                    await self.state.register_file(
                        file_id=f["file_id"],
                        chat_id=chat_id,
                        msg_id=msg_id,
                        expected_size=f["expected_size"],
                        actual_size=actual_size,
                        local_path=str(path),
                        status="done",
                    )
                    redownloaded += 1
                    logger.debug("re-downloaded: %s", path)
                else:
                    stats.errors.append(f"Re-download failed: {local_path}")
            except Exception as e:
                stats.errors.append(f"Re-download error for {local_path}: {e}")
                logger.debug("verify re-download error: %s", e)

        if redownloaded:
            self._status_print(f"[green]Re-downloaded {redownloaded}/{len(broken)} files[/]")
        # Only the failures of this pass: the shared list already holds the
        # errors of the export itself, and printing its length reported "100
        # files still have issues" after a verification that found none.
        failed_here = len(stats.errors) - errors_before
        if failed_here:
            console.print(f"[red]{failed_here} files still have issues[/]")

    async def _cleanup_orphaned_files(self, chat_id: int, chat_dir: Path):
        """Remove media files on disk that have no record in DB.

        These are typically partial downloads from interrupted exports.
        Why: previously path comparison was string-equal between Telethon's
        cwd-relative local_path and Path.iterdir() output; running tg-export
        from a different cwd between sessions made every legitimate file look
        orphaned. Now both sides are resolved to absolute Paths before the
        set-membership test, and the iterdir loop runs in a worker thread to
        avoid blocking the event loop on chats with thousands of media files.
        """
        from tg_export.media import MEDIA_SUBDIRS

        if not chat_dir.exists():
            return
        known_paths_raw = await self.state.get_known_paths(chat_id)
        await asyncio.to_thread(
            self._cleanup_orphaned_files_sync,
            list(MEDIA_SUBDIRS.values()),
            chat_dir,
            known_paths_raw,
        )
        return  # noqa: B012  - placeholder to keep async signature stable

    @staticmethod
    def _cleanup_orphaned_files_sync(
        subdir_names: list[str],
        chat_dir: Path,
        known_paths_raw: set[str],
    ):
        # Normalise DB paths: Telethon stores cwd-relative paths; resolve to
        # absolute against the original cwd or chat_dir as a fallback.
        known_resolved: set[Path] = set()
        cwd = Path.cwd()
        chat_dir_resolved = chat_dir.resolve()
        for raw in known_paths_raw:
            if raw.startswith("<") and raw.endswith(">"):
                continue  # synthetic markers like "<skipped_by_size>"
            p = Path(raw)
            if p.is_absolute():
                try:
                    known_resolved.add(p.resolve())
                except OSError:
                    known_resolved.add(p)
                continue
            for base in (cwd, chat_dir):
                candidate = base / p
                try:
                    resolved = candidate.resolve()
                except OSError:
                    continue
                if resolved.exists():
                    known_resolved.add(resolved)
                    break
            else:
                # Fallback: store both possible resolutions to avoid false
                # orphan-deletion when neither base maps to an existing file.
                known_resolved.add((cwd / p).resolve())
                known_resolved.add((chat_dir / p).resolve())

        removed = 0
        for subdir_name in subdir_names:
            subdir = chat_dir / subdir_name
            if not subdir.is_dir():
                continue
            for f in subdir.iterdir():
                if not f.is_file():
                    continue
                try:
                    f_resolved = f.resolve()
                except OSError:
                    continue
                # Safety: only delete files actually under chat_dir
                if not f_resolved.is_relative_to(chat_dir_resolved):
                    continue
                if f_resolved not in known_resolved:
                    f.unlink()
                    removed += 1
                    logger.debug("removed orphaned file: %s", f)
        if removed:
            logger.info("removed %d orphaned files in %s", removed, chat_dir.name)

    def _handle_shutdown(self, signum: int | None = None):
        now = time.monotonic()
        if signum is not None:
            self._shutdown_signal = signum
        if self._shutdown and (now - self._first_signal_time) < 3:
            # Second signal within 3s -> force exit via cancelling current task
            self._force_shutdown = True
            console.print("\n[bold red]Force shutdown![/]")
            if self._main_task is not None:
                self._main_task.cancel()
            return
        self._shutdown = True
        self._first_signal_time = now
        console.print("\n[yellow]Graceful shutdown requested (Ctrl+C again within 3s to force quit)...[/]")
