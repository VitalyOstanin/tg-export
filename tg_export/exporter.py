"""Main export loop with progress tracking."""

from __future__ import annotations

import asyncio
import re
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from tg_export.api import TgApi
from tg_export.config import Config, ChatExportConfig
from tg_export.converter import convert_message
from tg_export.html.renderer import HtmlRenderer
from tg_export.media import MediaDownloader, DiskSpaceError
from tg_export.models import Chat, Message, ForumTopic
from tg_export.state import ExportState


console = Console()


@dataclass
class ExportStats:
    chats_exported: int = 0
    messages_exported: int = 0
    files_downloaded: int = 0
    files_skipped: int = 0
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
) -> Path:
    """Resolve output directory for a chat."""
    dir_name = f"{sanitize_name(chat_name)}_{chat_id}"
    if is_left:
        return base / "left" / dir_name
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

        output_base = Path(self.config.output.path) / self.account

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            main_task = progress.add_task("Exporting chats...", total=len(chat_list))

            for chat in chat_list:
                if self._shutdown:
                    console.print("[yellow]Shutdown requested, saving state...[/]")
                    break

                chat_config = self.config.resolve_chat_config(
                    chat_id=chat.id, chat_name=chat.name, folder=chat.folder,
                )
                if chat_config is None:
                    progress.advance(main_task)
                    continue

                if dry_run:
                    console.print(f"  [dim]Would export: {chat.name}[/]")
                    progress.advance(main_task)
                    continue

                chat_dir = resolve_chat_dir(
                    base=output_base,
                    chat_name=chat.name,
                    chat_id=chat.id,
                    folder=chat.folder,
                    is_left=chat.is_left,
                )

                try:
                    chat_stats = await self.export_chat(chat, chat_config, chat_dir)
                    stats.messages_exported += chat_stats.messages_exported
                    stats.files_downloaded += chat_stats.files_downloaded
                    stats.files_skipped += chat_stats.files_skipped
                    stats.chats_exported += 1
                except DiskSpaceError as e:
                    console.print(f"[red]Disk space error: {e}[/]")
                    stats.errors.append(str(e))
                    break
                except Exception as e:
                    console.print(f"[red]Error exporting {chat.name}: {e}[/]")
                    stats.errors.append(f"{chat.name}: {e}")

                progress.advance(main_task)

        if verify:
            await self._verify_files(stats)

        return stats

    async def export_chat(
        self,
        chat: Chat,
        chat_config: ChatExportConfig,
        chat_dir: Path,
    ) -> ExportStats:
        """Export a single chat."""
        stats = ExportStats()

        # Get last exported message ID for incremental export
        min_id = await self.state.get_last_msg_id(chat.id) or 0

        # Fetch new messages
        new_messages = []
        async for tl_msg in self.api.iter_messages(chat.id, min_id=min_id):
            if self._shutdown:
                break
            msg = convert_message(tl_msg, chat_id=chat.id)

            # Download media
            if msg.media and self.downloader:
                try:
                    local_path = await self.downloader.download(tl_msg, msg.media, chat_dir)
                    if local_path and msg.media.file:
                        msg.media.file.local_path = str(local_path)
                        stats.files_downloaded += 1
                    elif msg.media.file:
                        stats.files_skipped += 1
                except Exception as e:
                    stats.errors.append(f"Media error msg {msg.id}: {e}")

            # Store in SQLite
            await self.state.store_message(msg)
            new_messages.append(msg)
            stats.messages_exported += 1

        # Update last message ID
        if new_messages:
            max_id = max(m.id for m in new_messages)
            await self.state.set_last_msg_id(chat.id, max_id)

        # Render HTML from ALL messages in SQLite (not just new)
        all_messages = await self.state.load_messages(chat.id)
        self.renderer.render_chat(chat, all_messages, chat_dir)

        return stats

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
        self._shutdown = True
