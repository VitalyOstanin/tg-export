"""Main export loop with progress tracking."""

from __future__ import annotations

import asyncio
import logging
import re
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.progress import (
    Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn,
    DownloadColumn, TransferSpeedColumn, TaskID,
)

from tg_export.api import TgApi
from tg_export.config import Config, ChatExportConfig
from tg_export.converter import convert_message
from tg_export.html.renderer import HtmlRenderer
from tg_export.media import MediaDownloader, DiskSpaceError
from tg_export.models import Chat, Message, ForumTopic
from tg_export.state import ExportState


console = Console()


def _log(msg: str):
    """Print with immediate flush (works in non-TTY / redirected output)."""
    print(msg, flush=True)


def _format_size(size_bytes: int) -> str:
    """Format bytes as human-readable size."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024**2:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024**3:
        return f"{size_bytes / 1024**2:.1f} MB"
    return f"{size_bytes / 1024**3:.2f} GB"


def _strip_markup(text: str) -> str:
    """Remove Rich markup tags like [cyan], [/], [green] etc."""
    return re.sub(r'\[/?[a-z_ ]*\]', '', text)


def _format_speed(bytes_count: int, elapsed_s: float) -> str:
    """Format download speed as human-readable string."""
    if elapsed_s <= 0:
        return "-- B/s"
    bps = bytes_count / elapsed_s
    if bps < 1024:
        return f"{bps:.0f} B/s"
    if bps < 1024**2:
        return f"{bps / 1024:.1f} KB/s"
    if bps < 1024**3:
        return f"{bps / 1024**2:.1f} MB/s"
    return f"{bps / 1024**3:.2f} GB/s"


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


@dataclass
class ExportStats:
    chats_total: int = 0
    chats_included: int = 0
    chats_skipped: int = 0
    chats_exported: int = 0
    messages_exported: int = 0
    messages_total: int = 0     # total messages in current chat (0 = unknown)
    messages_in_db: int = 0     # messages already in DB before this run
    files_downloaded: int = 0
    files_existing: int = 0       # already downloaded in previous runs
    files_reused_chat: int = 0    # reused from another chat (same account)
    files_reused_tdesktop: int = 0  # reused from tdesktop export
    files_reused_sibling: int = 0   # reused from sibling account
    files_skipped_by_size: int = 0  # exceeded max_file_size
    files_skipped_by_type: int = 0  # media type not in config
    data_size: int = 0  # bytes downloaded
    errors: list[str] = field(default_factory=list)
    # Snapshot of global counters at start of current chat (for per-chat display)
    _chat_snapshot: dict = field(default_factory=dict)

    def begin_chat(self, messages_in_db: int, messages_total: int):
        """Reset per-chat tracking at start of each chat."""
        self.messages_in_db = messages_in_db
        self.messages_total = messages_total
        self._chat_snapshot = {
            "messages_exported": self.messages_exported,
            "files_downloaded": self.files_downloaded,
            "files_existing": self.files_existing,
            "files_reused_chat": self.files_reused_chat,
            "files_reused_tdesktop": self.files_reused_tdesktop,
            "files_reused_sibling": self.files_reused_sibling,
            "files_skipped_by_size": self.files_skipped_by_size,
            "files_skipped_by_type": self.files_skipped_by_type,
            "data_size": self.data_size,
        }

    @property
    def chat_messages_new(self) -> int:
        return self.messages_exported - self._chat_snapshot.get("messages_exported", 0)

    @property
    def chat_files_downloaded(self) -> int:
        return self.files_downloaded - self._chat_snapshot.get("files_downloaded", 0)

    @property
    def chat_files_existing(self) -> int:
        return self.files_existing - self._chat_snapshot.get("files_existing", 0)

    @property
    def chat_files_reused_chat(self) -> int:
        return self.files_reused_chat - self._chat_snapshot.get("files_reused_chat", 0)

    @property
    def chat_files_reused_tdesktop(self) -> int:
        return self.files_reused_tdesktop - self._chat_snapshot.get("files_reused_tdesktop", 0)

    @property
    def chat_files_reused_sibling(self) -> int:
        return self.files_reused_sibling - self._chat_snapshot.get("files_reused_sibling", 0)

    @property
    def chat_files_skipped_by_size(self) -> int:
        return self.files_skipped_by_size - self._chat_snapshot.get("files_skipped_by_size", 0)

    @property
    def chat_files_skipped_by_type(self) -> int:
        return self.files_skipped_by_type - self._chat_snapshot.get("files_skipped_by_type", 0)

    @property
    def chat_data_size(self) -> int:
        return self.data_size - self._chat_snapshot.get("data_size", 0)


def sanitize_name(name: str) -> str:
    """Replace special characters with _ and strip."""
    name = name.strip()
    name = re.sub(r'[/\\:*?"<>|]', '_', name)
    name = name.replace(' ', '_')
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


class Exporter:
    def __init__(
        self,
        api: TgApi,
        state: ExportState,
        config: Config,
        renderer: HtmlRenderer,
        downloader: MediaDownloader,
        account: str,
    ):
        self.api = api
        self.state = state
        self.config = config
        self.renderer = renderer
        self.downloader = downloader
        self.account = account
        self._shutdown = False
        self._force_shutdown = False
        self._first_signal_time: float = 0

    async def run(
        self,
        dry_run: bool = False,
        verify: bool = False,
        chat_list: list[Chat] | None = None,
    ) -> ExportStats:
        """Main export loop."""
        stats = ExportStats()

        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGINT, self._handle_shutdown)
        loop.add_signal_handler(signal.SIGTERM, self._handle_shutdown)

        if chat_list is None:
            return stats

        output_base = Path(self.config.output.path)

        # Pre-scan: count included vs skipped chats
        included_chats: list[tuple[Chat, ChatExportConfig]] = []
        for chat in chat_list:
            # Check left/archived actions before resolve_chat_config
            if chat.is_left and self.config.left_channels_action == "skip":
                stats.chats_skipped += 1
                continue
            if chat.is_archived and self.config.archived_action == "skip":
                stats.chats_skipped += 1
                continue

            chat_config = self.config.resolve_chat_config(
                chat_id=chat.id, chat_name=chat.name, folder=chat.folder,
                chat_type=chat.type.value,
            )
            if chat_config is None:
                stats.chats_skipped += 1
            else:
                included_chats.append((chat, chat_config))
        stats.chats_total = len(chat_list)
        stats.chats_included = len(included_chats)

        start_time = time.monotonic()
        start_dt = datetime.now()
        mode_str = "[bold yellow]DRY-RUN[/]" if dry_run else "[bold green]EXPORT[/]"
        console.print(
            f"\n{mode_str}: {stats.chats_included} chats to export, "
            f"{stats.chats_skipped} skipped (total {stats.chats_total})"
        )
        if self.config.defaults.date_from or self.config.defaults.date_to:
            df = self.config.defaults.date_from or "..."
            dt = self.config.defaults.date_to or "..."
            console.print(f"[dim]date range: {df} — {dt}[/]")
        console.print(f"[dim]started at {start_dt.strftime('%H:%M:%S')}[/]\n")

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
            DownloadColumn(),
            TransferSpeedColumn(),
            console=console,
        )
        # Track which msg_ids have progress tasks
        file_tasks: dict[int, TaskID] = {}  # msg_id -> task_id

        def _status_lines() -> str:
            elapsed = time.monotonic() - start_time
            elapsed_str = _format_elapsed(elapsed)
            chat_data = stats.chat_data_size
            speed_str = _format_speed(chat_data, elapsed) if chat_data > 0 else ""

            # Per-chat message counts
            chat_msgs = stats.chat_messages_new
            msgs_done = stats.messages_in_db + chat_msgs
            msgs_str = f"[cyan]{msgs_done}"
            if stats.messages_total > 0:
                msgs_str += f"/{stats.messages_total}"
            msgs_str += "[/]"
            if chat_msgs > 0:
                msgs_str += f" ([green]+{chat_msgs}[/]"
                if elapsed > 0:
                    msgs_str += f", [green]{chat_msgs / elapsed:.0f}/s[/]"
                msgs_str += ")"

            # Line 1: chats, messages, data, elapsed
            line1 = f"  chats: [cyan]{stats.chats_exported}/{stats.chats_included}[/] | msgs: {msgs_str}"
            line1 += f" | data: [cyan]{_format_size(chat_data)}[/]"
            if speed_str:
                line1 += f" ([green]{speed_str}[/])"
            line1 += f" | elapsed: {elapsed_str}"

            # Line 2: file counts
            parts = [f"  files: [cyan]{stats.chat_files_downloaded}[/] downloaded"]
            if stats.chat_files_existing:
                parts.append(f"[green]{stats.chat_files_existing}[/] existing")
            if stats.chat_files_reused_chat:
                parts.append(f"[green]{stats.chat_files_reused_chat}[/] from_chat")
            if stats.chat_files_reused_tdesktop:
                parts.append(f"[green]{stats.chat_files_reused_tdesktop}[/] from_tdesktop")
            if stats.chat_files_reused_sibling:
                parts.append(f"[green]{stats.chat_files_reused_sibling}[/] from_sibling")
            skipped = []
            if stats.chat_files_skipped_by_size:
                skipped.append(f"[yellow]{stats.chat_files_skipped_by_size}[/] by_size")
            if stats.chat_files_skipped_by_type:
                skipped.append(f"[yellow]{stats.chat_files_skipped_by_type}[/] by_type")
            if skipped:
                parts.append(f"skipped: {', '.join(skipped)}")
            line2 = " | ".join(parts)

            return f"{line1}\n{line2}"

        def _make_status_table() -> Table:
            # Sync progress bar: per-chat progress
            completed = stats.messages_in_db + stats.chat_messages_new
            if stats.messages_total > 0:
                progress.update(main_task, completed=completed,
                                total=stats.messages_total)
            else:
                progress.update(main_task, completed=completed,
                                total=None)

            # Sync file download sub-bars
            active = self.downloader.active_downloads
            # Remove finished tasks
            for msg_id in list(file_tasks):
                if msg_id not in active:
                    file_progress.remove_task(file_tasks.pop(msg_id))
            # Add/update active tasks
            for msg_id, dl in active.items():
                if msg_id not in file_tasks:
                    tid = file_progress.add_task(
                        dl.filename, total=dl.total or None,
                        completed=dl.received,
                    )
                    file_tasks[msg_id] = tid
                else:
                    file_progress.update(
                        file_tasks[msg_id],
                        description=dl.filename,
                        completed=dl.received,
                        total=dl.total or None,
                    )

            table = Table.grid()
            table.add_row(progress)
            table.add_row(_status_lines())
            if file_tasks:
                table.add_row(file_progress)
            return table

        use_live = console.is_terminal
        self._use_live = use_live

        live_ctx = Live(console=console, refresh_per_second=2, get_renderable=_make_status_table) if use_live else None
        if live_ctx:
            live_ctx.__enter__()

        try:
            last_log_time = start_time
            for chat, chat_config in included_chats:
                if self._shutdown:
                    console.print("[yellow]Shutdown requested, saving state...[/]")
                    break

                folder_str = f" [{chat.folder}]" if chat.folder else ""

                if dry_run:
                    console.print(f"  [green]export[/]: {chat.name} ({chat.type.value}){folder_str}")
                    stats.chats_exported += 1
                    continue

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
                    chat_id=chat.id, name=chat.name,
                    chat_type=chat.type.value, folder=chat.folder,
                    members_count=chat.members_count,
                    messages_count=chat.messages_count or 0,
                    last_message_date=chat.last_message_date,
                    is_left=chat.is_left, is_archived=chat.is_archived,
                    is_forum=chat.is_forum,
                    is_monoforum=getattr(chat, 'is_monoforum', False),
                )

                try:
                    progress.update(main_task, description=f"[cyan]{chat.name}[/]")
                    logger.debug("start chat %s (id=%d, type=%s, msgs~%d)",
                                 chat.name, chat.id, chat.type.value, chat.messages_count or 0)

                    # Load tdesktop index for this chat
                    for idx in self.downloader.tdesktop_indexes:
                        idx.load_chat_index(chat.name)

                    chat_t0 = time.monotonic()
                    msgs_before = stats.messages_exported
                    await self.export_chat(chat, chat_config, chat_dir, stats)
                    chat_msgs = stats.messages_exported - msgs_before
                    logger.debug("done chat %s in %.1fs: %d msgs",
                                 chat.name, time.monotonic() - chat_t0, chat_msgs)
                    stats.chats_exported += 1

                    # Unload tdesktop index to free memory
                    for idx in self.downloader.tdesktop_indexes:
                        idx.unload_chat_index()

                    # Log progress periodically for non-TTY
                    now = time.monotonic()
                    if not use_live and (now - last_log_time >= 10 or stats.chats_exported % 10 == 0):
                        _log(_strip_markup(_status_lines()))
                        last_log_time = now

                except DiskSpaceError as e:
                    _log(f"Disk space error: {e}")
                    stats.errors.append(str(e))
                    break
                except asyncio.CancelledError:
                    console.print("[yellow]Force shutdown during export...[/]")
                    break
                except Exception as e:
                    _log(f"Error exporting {chat.name}: {e}")
                    stats.errors.append(f"{chat.name}: {e}")

        except asyncio.CancelledError:
            self._force_shutdown = True

        finally:
            if live_ctx:
                live_ctx.__exit__(None, None, None)

        # Export global data (personal info, contacts, sessions, etc.)
        if not dry_run and not self._force_shutdown and not self._shutdown:
            try:
                console.print("\n[cyan]Exporting global data...[/]")
                await self.export_global_data(output_base)
            except Exception as e:
                logger.warning("Failed to export global data: %s", e)

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
        BATCH_SIZE = 500
        LOG_INTERVAL = 3  # seconds between progress logs
        chat_start = time.monotonic()
        msgs_before = stats.messages_exported

        # Date range filtering
        date_from = chat_config.date_from
        date_to = chat_config.date_to

        # Get total message count for progress display
        # Note: Telegram API does not support counting messages in a date range,
        # so with date filters we show only current count without total
        has_date_filter = bool(date_from or date_to)
        try:
            if has_date_filter:
                chat_total = 0  # unknown — API can't count by date range
            else:
                result = await self.api.client.get_messages(chat.id, limit=0)
                chat_total = getattr(result, 'total', 0) or 0
        except Exception:
            chat_total = chat.messages_count or 0

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

        def _chat_progress() -> str:
            chat_msgs = stats.chat_messages_new
            elapsed = time.monotonic() - chat_start
            parts = [f"  {chat.name}: {chat_msgs}"]
            if chat_total > 0:
                parts[0] += f"/{chat_total}"
            parts.append("msgs")
            parts.append(f"{stats.chat_files_downloaded} files")
            parts.append(_format_size(stats.chat_data_size))
            if elapsed > 0 and chat_msgs > 0:
                parts.append(f"({chat_msgs / elapsed:.0f} msg/s)")
            return "  ".join(parts)

        # Build iter_messages kwargs for date filtering
        from datetime import timedelta
        iter_kwargs: dict = {}
        if date_to:
            # Start from messages at date_to end-of-day
            iter_kwargs["offset_date"] = datetime.combine(
                date_to + timedelta(days=1), datetime.min.time()
            )

        def _before_date_from(msg_date) -> bool:
            """True if message is before date_from (should stop)."""
            if not date_from or not msg_date:
                return False
            return msg_date.date() < date_from

        # Phase 1: fetch new messages (id > last_msg_id)
        if last_msg_id > 0:
            new_max_id = last_msg_id
            batch: list[Message] = []
            last_progress_time = time.monotonic()
            p1_kwargs = {"min_id": last_msg_id}
            if date_to:
                p1_kwargs["offset_date"] = iter_kwargs["offset_date"]
            async for tl_msg in self.api.iter_messages(chat.id, **p1_kwargs):
                if self._shutdown:
                    break
                if _before_date_from(tl_msg.date):
                    break
                msg = convert_message(tl_msg, chat_id=chat.id)
                await self._process_media(msg, tl_msg, chat_dir, stats, chat_id=chat.id)
                batch.append(msg)
                if msg.id > new_max_id:
                    new_max_id = msg.id
                stats.messages_exported += 1
                if len(batch) >= BATCH_SIZE:
                    await self.state.store_messages_batch(batch)
                    logger.debug("  %s: %d new msgs stored", chat.name, stats.messages_exported)
                    batch.clear()
                now = time.monotonic()
                if not self._use_live and now - last_progress_time >= LOG_INTERVAL:
                    _log(_chat_progress())
                    last_progress_time = now
            if batch:
                await self.state.store_messages_batch(batch)
                batch.clear()
            if new_max_id > last_msg_id:
                await self.state.set_last_msg_id(chat.id, new_max_id)
            logger.debug("  %s: phase 1 done", chat.name)

        # Phase 2: fetch old messages (continuing from oldest_msg_id downward)
        if not full_history and not self._shutdown:
            batch = []
            current_oldest = oldest_msg_id
            p2_kwargs = dict(iter_kwargs)  # includes offset_date if set
            if oldest_msg_id > 0:
                p2_kwargs["offset_id"] = oldest_msg_id
            elif last_msg_id > 0:
                # Continue from where phase 1 left off (but not first run)
                pass

            reached_date_from = False
            iterator_exhausted = False
            last_progress_time = time.monotonic()
            async for tl_msg in self.api.iter_messages(chat.id, **p2_kwargs):
                if self._shutdown:
                    break
                if _before_date_from(tl_msg.date):
                    reached_date_from = True
                    break
                msg = convert_message(tl_msg, chat_id=chat.id)
                await self._process_media(msg, tl_msg, chat_dir, stats, chat_id=chat.id)
                batch.append(msg)
                if current_oldest == 0 or msg.id < current_oldest:
                    current_oldest = msg.id
                if last_msg_id == 0 and msg.id > last_msg_id:
                    last_msg_id = msg.id
                stats.messages_exported += 1
                if len(batch) >= BATCH_SIZE:
                    await self.state.store_messages_batch(batch)
                    logger.debug("  %s: %d msgs stored (oldest=%d)",
                                 chat.name, stats.messages_exported, current_oldest)
                    batch.clear()
                now = time.monotonic()
                if not self._use_live and now - last_progress_time >= LOG_INTERVAL:
                    _log(_chat_progress())
                    last_progress_time = now
            else:
                # for/else: iterator exhausted naturally (no break)
                iterator_exhausted = True

            if batch:
                await self.state.store_messages_batch(batch)
                batch.clear()

            if last_msg_id > 0:
                await self.state.set_last_msg_id(chat.id, last_msg_id)
            if current_oldest > 0:
                await self.state.set_oldest_msg_id(chat.id, current_oldest)

            if not self._shutdown:
                if reached_date_from or iterator_exhausted:
                    await self.state.set_full_history(chat.id)
                    logger.debug("  %s: full history complete", chat.name)

        # Update messages_count in export_state
        msg_count = await self.state.count_messages(chat.id)
        if msg_count > 0:
            await self.state.update_messages_count(chat.id, msg_count)

        # Render HTML from ALL messages in SQLite
        all_messages = await self.state.load_messages(chat.id)
        if all_messages:
            self.renderer.render_chat(chat, all_messages, chat_dir)
        else:
            logger.debug("  %s: no messages in DB, skipping render", chat.name)

        return stats

    async def _process_media(self, msg: Message, tl_msg, chat_dir: Path, stats: ExportStats,
                             chat_id: int = 0):
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
            stats.errors.append(f"Media error msg {msg.id}: {e}")

    async def export_global_data(self, output_base: Path):
        """Export personal_info, userpics, stories, contacts, sessions, etc."""
        if self.config.personal_info:
            try:
                await self._export_personal_info()
            except Exception as e:
                logger.warning("Failed to export personal info: %s", e)

        if self.config.contacts:
            try:
                await self._export_contacts()
            except Exception as e:
                logger.warning("Failed to export contacts: %s", e)

        if self.config.sessions:
            try:
                await self._export_sessions()
            except Exception as e:
                logger.warning("Failed to export sessions: %s", e)

        if self.config.userpics:
            try:
                await self._export_userpics()
            except Exception as e:
                logger.warning("Failed to export userpics: %s", e)

        if self.config.stories:
            try:
                await self._export_stories()
            except Exception as e:
                logger.warning("Failed to export stories: %s", e)

        if self.config.other_data or self.config.profile_music:
            try:
                await self._export_other_data()
            except Exception as e:
                logger.warning("Failed to export other data: %s", e)

    async def _export_personal_info(self):
        """Fetch and render personal info."""
        result = await self.api.get_personal_info()
        full_user = result.full_user
        user = result.users[0] if result.users else None

        photo_path = None
        if user and getattr(user, "photo", None):
            photos_dir = self.renderer.output_dir / "profile_photos"
            photos_dir.mkdir(parents=True, exist_ok=True)
            try:
                path = await self.api.client.download_profile_photo(
                    "me", file=str(photos_dir / "current"),
                )
                if path:
                    photo_path = f"profile_photos/{Path(path).name}"
            except Exception:
                pass

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
        self.renderer.render_personal_info(user_data)
        console.print("  [green]exported[/]: personal info")

    async def _export_contacts(self):
        """Fetch and render contacts list."""
        result = await self.api.get_contacts()
        users_by_id = {u.id: u for u in getattr(result, "users", [])}

        contacts = []
        for c in getattr(result, "contacts", []):
            user = users_by_id.get(c.user_id)
            if user:
                name = f"{getattr(user, 'first_name', '') or ''} {getattr(user, 'last_name', '') or ''}".strip()
                contacts.append({
                    "name": name or str(c.user_id),
                    "username": getattr(user, "username", "") or "",
                    "phone": getattr(user, "phone", "") or "",
                })

        frequent = []
        top_result = await self.api.get_top_peers()
        if top_result and hasattr(top_result, "categories"):
            for cat in top_result.categories:
                for tp in cat.peers:
                    peer_id = None
                    if hasattr(tp.peer, "user_id"):
                        peer_id = tp.peer.user_id
                    elif hasattr(tp.peer, "chat_id"):
                        peer_id = tp.peer.chat_id
                    elif hasattr(tp.peer, "channel_id"):
                        peer_id = tp.peer.channel_id
                    user = users_by_id.get(peer_id)
                    name = ""
                    if user:
                        name = f"{getattr(user, 'first_name', '') or ''} {getattr(user, 'last_name', '') or ''}".strip()
                    frequent.append({
                        "name": name or str(peer_id),
                        "rating": f"{tp.rating:.2f}",
                    })

        self.renderer.render_contacts(contacts, frequent)
        console.print(f"  [green]exported[/]: {len(contacts)} contacts, {len(frequent)} frequent")

    async def _export_sessions(self):
        """Fetch and render active sessions."""
        sessions_result, web_result = await self.api.get_sessions()

        app_sessions = []
        for auth in getattr(sessions_result, "authorizations", []):
            date_active = datetime.fromtimestamp(auth.date_active) if auth.date_active else None
            app_sessions.append({
                "app_name": getattr(auth, "app_name", ""),
                "app_version": getattr(auth, "app_version", ""),
                "device_model": getattr(auth, "device_model", ""),
                "platform": getattr(auth, "platform", ""),
                "system_version": getattr(auth, "system_version", ""),
                "ip": getattr(auth, "ip", ""),
                "country": getattr(auth, "country", ""),
                "date_active": date_active.strftime("%Y-%m-%d %H:%M") if date_active else "",
                "current": bool(getattr(auth, "current", False)),
            })

        web_sessions = []
        for wa in getattr(web_result, "authorizations", []):
            date_active = datetime.fromtimestamp(wa.date_active) if wa.date_active else None
            web_sessions.append({
                "domain": getattr(wa, "domain", ""),
                "browser": getattr(wa, "browser", ""),
                "platform": getattr(wa, "platform", ""),
                "ip": getattr(wa, "ip", ""),
                "region": getattr(wa, "region", ""),
                "date_active": date_active.strftime("%Y-%m-%d %H:%M") if date_active else "",
            })

        self.renderer.render_sessions(app_sessions, web_sessions)
        console.print(f"  [green]exported[/]: {len(app_sessions)} app sessions, {len(web_sessions)} web sessions")

    async def _export_userpics(self):
        """Fetch and render profile photos."""
        photos_dir = self.renderer.output_dir / "profile_photos"
        photos_dir.mkdir(parents=True, exist_ok=True)

        photos = []
        idx = 0
        async for photo in self.api.iter_userpics():
            try:
                path = await self.api.client.download_media(
                    photo, file=str(photos_dir / f"photo_{idx}"),
                )
                if path:
                    date_str = ""
                    if hasattr(photo, "date") and photo.date:
                        date_str = photo.date.strftime("%Y-%m-%d %H:%M")
                    photos.append({
                        "path": f"profile_photos/{Path(path).name}",
                        "date": date_str,
                    })
                    idx += 1
            except Exception as e:
                logger.debug("Failed to download userpic %d: %s", idx, e)

        self.renderer.render_userpics(photos)
        console.print(f"  [green]exported[/]: {len(photos)} profile photos")

    async def _export_stories(self):
        """Fetch and render stories."""
        stories_dir = self.renderer.output_dir / "stories"
        stories_dir.mkdir(parents=True, exist_ok=True)

        try:
            pinned, archived = await self.api.get_stories()
        except Exception as e:
            logger.warning("Stories API not available: %s", e)
            self.renderer.render_stories([])
            return

        # Combine pinned + archived, deduplicate by id
        all_stories = {}
        for story_item in getattr(pinned, "stories", []):
            all_stories[story_item.id] = story_item
        for story_item in getattr(archived, "stories", []):
            all_stories.setdefault(story_item.id, story_item)

        stories = []
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
                        media, file=str(stories_dir / f"story_{idx}"),
                    )
                    if path:
                        rel = f"stories/{Path(path).name}"
                        if any(Path(path).suffix.lower() in ext for ext in [".mp4", ".mov", ".avi"]):
                            video_path = rel
                        else:
                            photo_path = rel
                except Exception as e:
                    logger.debug("Failed to download story %d: %s", story_id, e)

            date_str = ""
            if hasattr(item, "date") and item.date:
                date_str = item.date.strftime("%Y-%m-%d %H:%M")

            stories.append({
                "photo_path": photo_path,
                "video_path": video_path,
                "caption": caption,
                "date": date_str,
            })

        self.renderer.render_stories(stories)
        console.print(f"  [green]exported[/]: {len(stories)} stories")

    async def _export_other_data(self):
        """Fetch and render ringtones and other data."""
        ringtones_dir = self.renderer.output_dir / "ringtones"
        ringtones = []

        try:
            result = await self.api.get_ringtones()
            if hasattr(result, "ringtones"):
                ringtones_dir.mkdir(parents=True, exist_ok=True)
                for idx, doc in enumerate(result.ringtones):
                    name = f"ringtone_{idx}"
                    for attr in getattr(doc, "attributes", []):
                        if hasattr(attr, "file_name") and attr.file_name:
                            name = attr.file_name
                            break

                    path = None
                    try:
                        path = await self.api.client.download_media(
                            doc, file=str(ringtones_dir / f"ringtone_{idx}"),
                        )
                    except Exception as e:
                        logger.debug("Failed to download ringtone %d: %s", idx, e)

                    size_str = ""
                    if hasattr(doc, "size") and doc.size:
                        size_str = _format_size(doc.size)

                    ringtones.append({
                        "name": name,
                        "path": f"ringtones/{Path(path).name}" if path else None,
                        "size": size_str,
                    })
        except Exception as e:
            logger.warning("Failed to fetch ringtones: %s", e)

        self.renderer.render_other_data({"ringtones": ringtones})
        if ringtones:
            console.print(f"  [green]exported[/]: {len(ringtones)} ringtones")

    async def _verify_files(self, stats: ExportStats):
        """Verify integrity of downloaded files and re-download broken ones."""
        broken = await self.state.get_files_to_verify()
        if not broken:
            return

        console.print(f"[yellow]Found {len(broken)} files to re-download[/]")
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
                tl_msg = tl_messages if not isinstance(tl_messages, list) else (tl_messages[0] if tl_messages else None)
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
                    actual_size = Path(path).stat().st_size
                    await self.state.register_file(
                        file_id=f["file_id"], chat_id=chat_id, msg_id=msg_id,
                        expected_size=f["expected_size"], actual_size=actual_size,
                        local_path=str(path), status="done",
                    )
                    redownloaded += 1
                    logger.debug("re-downloaded: %s", path)
                else:
                    stats.errors.append(f"Re-download failed: {local_path}")
            except Exception as e:
                stats.errors.append(f"Re-download error for {local_path}: {e}")
                logger.debug("verify re-download error: %s", e)

        if redownloaded:
            console.print(f"[green]Re-downloaded {redownloaded}/{len(broken)} files[/]")
        if stats.errors:
            console.print(f"[red]{len(stats.errors)} files still have issues[/]")

    def _handle_shutdown(self):
        now = time.monotonic()
        if self._shutdown and (now - self._first_signal_time) < 3:
            # Second signal within 3s -> force exit via cancelling current task
            self._force_shutdown = True
            console.print("\n[bold red]Force shutdown![/]")
            for task in asyncio.all_tasks():
                task.cancel()
            return
        self._shutdown = True
        self._first_signal_time = now
        console.print("\n[yellow]Graceful shutdown requested (Ctrl+C again within 3s to force quit)...[/]")
