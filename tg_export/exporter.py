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
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

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
    files_downloaded: int = 0
    files_skipped: int = 0
    data_size: int = 0  # bytes downloaded
    errors: list[str] = field(default_factory=list)


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
        main_task = progress.add_task(
            "Chats", total=stats.chats_included,
        )

        def _status_line() -> str:
            elapsed = time.monotonic() - start_time
            elapsed_str = _format_elapsed(elapsed)
            speed_str = _format_speed(stats.data_size, elapsed) if stats.data_size > 0 else ""
            msgs_speed = f"{stats.messages_exported / elapsed:.0f}/s" if elapsed > 0 and stats.messages_exported > 0 else ""

            line = (
                f"  chats: {stats.chats_exported}/{stats.chats_included}  "
                f"msgs: {stats.messages_exported}"
            )
            if msgs_speed:
                line += f" ({msgs_speed})"
            line += f"  files: {stats.files_downloaded}"
            line += f"  data: {_format_size(stats.data_size)}"
            if speed_str:
                line += f" ({speed_str})"
            line += f"  elapsed: {elapsed_str}"
            return line

        def _make_status_table() -> Table:
            table = Table.grid(padding=(0, 2))
            table.add_row(progress)
            table.add_row(_status_line())
            return table

        use_live = console.is_terminal

        live_ctx = Live(_make_status_table(), console=console, refresh_per_second=4) if use_live else None
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
                    progress.advance(main_task)
                    if live_ctx:
                        live_ctx.update(_make_status_table())
                    continue

                chat_dir = resolve_chat_dir(
                    base=output_base,
                    chat_name=chat.name,
                    chat_id=chat.id,
                    folder=chat.folder,
                    is_left=chat.is_left,
                    is_archived=chat.is_archived,
                )

                try:
                    progress.update(main_task, description=f"[dim]{chat.name}[/]")
                    if live_ctx:
                        live_ctx.update(_make_status_table())
                    logger.debug("start chat %s (id=%d, type=%s, msgs~%d)",
                                 chat.name, chat.id, chat.type.value, chat.messages_count or 0)
                    chat_t0 = time.monotonic()
                    msgs_before = stats.messages_exported
                    await self.export_chat(chat, chat_config, chat_dir, stats)
                    chat_msgs = stats.messages_exported - msgs_before
                    logger.debug("done chat %s in %.1fs: %d msgs",
                                 chat.name, time.monotonic() - chat_t0, chat_msgs)
                    stats.chats_exported += 1

                    # Log progress periodically for non-TTY
                    now = time.monotonic()
                    if not use_live and (now - last_log_time >= 10 or stats.chats_exported % 10 == 0):
                        _log(_status_line())
                        last_log_time = now

                except DiskSpaceError as e:
                    _log(f"Disk space error: {e}")
                    stats.errors.append(str(e))
                    break
                except Exception as e:
                    _log(f"Error exporting {chat.name}: {e}")
                    stats.errors.append(f"{chat.name}: {e}")

                progress.advance(main_task)
                if live_ctx:
                    live_ctx.update(_make_status_table())
        finally:
            if live_ctx:
                live_ctx.__exit__(None, None, None)

        if verify:
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

        def _chat_progress() -> str:
            chat_msgs = stats.messages_exported - msgs_before
            elapsed = time.monotonic() - chat_start
            parts = [f"  {chat.name}: {chat_msgs}"]
            if chat_total > 0:
                parts[0] += f"/{chat_total}"
            parts.append("msgs")
            parts.append(f"{stats.files_downloaded} files")
            parts.append(_format_size(stats.data_size))
            if elapsed > 0 and stats.data_size > 0:
                parts.append(f"({_format_speed(stats.data_size, elapsed)})")
            if elapsed > 0 and chat_msgs > 0:
                parts.append(f"({chat_msgs / elapsed:.0f} msg/s)")
            return "  ".join(parts)

        chat_state = await self.state.get_chat_state(chat.id)
        last_msg_id = chat_state["last_msg_id"] if chat_state else 0
        oldest_msg_id = chat_state["oldest_msg_id"] if chat_state else 0
        full_history = bool(chat_state["full_history"]) if chat_state else False

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
                await self._process_media(msg, tl_msg, chat_dir, stats)
                batch.append(msg)
                if msg.id > new_max_id:
                    new_max_id = msg.id
                stats.messages_exported += 1
                if len(batch) >= BATCH_SIZE:
                    await self.state.store_messages_batch(batch)
                    logger.debug("  %s: %d new msgs stored", chat.name, stats.messages_exported)
                    batch.clear()
                now = time.monotonic()
                if now - last_progress_time >= LOG_INTERVAL:
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

            fetched_any = False
            reached_date_from = False
            last_progress_time = time.monotonic()
            async for tl_msg in self.api.iter_messages(chat.id, **p2_kwargs):
                if self._shutdown:
                    break
                if _before_date_from(tl_msg.date):
                    reached_date_from = True
                    break
                fetched_any = True
                msg = convert_message(tl_msg, chat_id=chat.id)
                await self._process_media(msg, tl_msg, chat_dir, stats)
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
                if now - last_progress_time >= LOG_INTERVAL:
                    _log(_chat_progress())
                    last_progress_time = now

            if batch:
                await self.state.store_messages_batch(batch)
                batch.clear()

            if last_msg_id > 0:
                await self.state.set_last_msg_id(chat.id, last_msg_id)
            if current_oldest > 0:
                await self.state.set_oldest_msg_id(chat.id, current_oldest)

            if not self._shutdown:
                if reached_date_from or fetched_any or oldest_msg_id > 0:
                    await self.state.set_full_history(chat.id)
                    logger.debug("  %s: full history complete", chat.name)

        # Render HTML from ALL messages in SQLite
        all_messages = await self.state.load_messages(chat.id)
        if all_messages:
            self.renderer.render_chat(chat, all_messages, chat_dir)
        else:
            logger.debug("  %s: no messages in DB, skipping render", chat.name)

        return stats

    async def _process_media(self, msg: Message, tl_msg, chat_dir: Path, stats: ExportStats):
        """Download media for a message, updating stats."""
        if not msg.media or not self.downloader:
            return
        try:
            local_path, is_new = await self.downloader.download(tl_msg, msg.media, chat_dir)
            if local_path and msg.media.file:
                msg.media.file.local_path = str(local_path)
                if is_new:
                    stats.files_downloaded += 1
                    if local_path.exists():
                        stats.data_size += local_path.stat().st_size
                else:
                    stats.files_skipped += 1
            elif msg.media.file:
                stats.files_skipped += 1
        except Exception as e:
            stats.errors.append(f"Media error msg {msg.id}: {e}")

    async def export_global_data(self, output_base: Path):
        """Export personal_info, userpics, stories, contacts, sessions, etc."""
        if self.config.personal_info:
            try:
                info = await self.api.get_personal_info()
                # Render personal info HTML
            except Exception:
                pass

        if self.config.contacts:
            try:
                contacts = await self.api.get_contacts()
                # Render contacts HTML
            except Exception:
                pass

        if self.config.sessions:
            try:
                sessions, web_sessions = await self.api.get_sessions()
                # Render sessions HTML
            except Exception:
                pass

    async def _verify_files(self, stats: ExportStats):
        """Verify integrity of downloaded files."""
        broken = await self.state.get_files_to_verify()
        if broken:
            console.print(f"[yellow]Found {len(broken)} files to re-download[/]")
            for f in broken:
                stats.errors.append(f"Broken file: {f['local_path']} (status={f['status']})")

    def _handle_shutdown(self):
        now = time.monotonic()
        if self._shutdown and (now - self._first_signal_time) < 3:
            # Second signal within 3s -> force exit
            self._force_shutdown = True
            console.print("\n[bold red]Force shutdown![/]")
            import sys
            sys.exit(1)
        self._shutdown = True
        self._first_signal_time = now
        console.print("\n[yellow]Graceful shutdown requested (Ctrl+C again within 3s to force quit)...[/]")
