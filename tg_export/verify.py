"""Re-download of files that failed verification.

Both entry points -- the `verify` command and `run --verify` -- had their own
copy of this, and the copies disagreed on the one thing that matters: the
export path deleted the broken file before asking for a replacement, so an
interruption between the two left nothing on disk while the database still
pointed at the vanished path. Keeping one implementation here is what stops
the next fix from reaching only one of them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Staging directories are created next to the media they replace, so they need
# a name no export would produce.
STAGING_PREFIX = ".tg-export-verify-"


class RedownloadResult(StrEnum):
    """What re-downloading one broken file ended with.

    The callers report an outcome differently -- one prints lines, the other
    fills the error list of the run -- so the decision of what to say is left
    to them.
    """

    replaced = "replaced"
    no_media = "no_media"
    nothing_downloaded = "nothing_downloaded"


def clean_staging(root: Path) -> None:
    """Remove staging directories left behind by a killed verify run.

    A SIGKILL/SIGTERM in the middle of a download skips TemporaryDirectory
    cleanup. The leftovers are harmless but accumulate next to the media, so
    sweep them at the start of the next run.
    """
    for path in root.rglob(f"{STAGING_PREFIX}*"):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)


async def redownload_broken_file(
    api, state, entry: dict[str, Any], *, tl_msg: Any | None = None
) -> tuple[RedownloadResult, Path | None]:
    """Re-download one broken file, replacing it only after the download succeeded.

    Returns the outcome and, when the file was replaced, where it now is.
    Download failures propagate: the caller decides whether one failed file
    ends the pass.

    ``tl_msg`` is the message the file belongs to, when the caller already
    fetched it in a batch; without it the message is asked for here, one
    round-trip for this single file.

    The old file stays untouched until the new one is fully on disk: deleting
    first meant that an interruption -- a dropped connection, Ctrl+C -- left
    nothing behind while the database still pointed at the vanished path.
    """
    chat_id = entry["chat_id"]
    msg_id = entry["msg_id"]
    local_path = Path(entry["local_path"])

    if tl_msg is None:
        tl_messages = await api.client.get_messages(chat_id, ids=msg_id)
        tl_msg = (
            tl_messages if not isinstance(tl_messages, list) else (tl_messages[0] if tl_messages else None)
        )
    if tl_msg is None or tl_msg.media is None:
        return RedownloadResult.no_media, None

    target_dir = local_path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    # dir=target_dir keeps the staging area on the same filesystem, so the final
    # move is an atomic rename rather than a copy.
    with tempfile.TemporaryDirectory(dir=target_dir, prefix=STAGING_PREFIX) as staging:
        downloaded = await api.download_media(tl_msg, Path(staging))
        if not downloaded:
            return RedownloadResult.nothing_downloaded, None

        downloaded = Path(str(downloaded))
        final_path = target_dir / downloaded.name
        os.replace(downloaded, final_path)

    # Telethon may pick a different name than the one recorded earlier; drop the
    # stale file only now, once its replacement is in place.
    if local_path != final_path and local_path.exists():
        local_path.unlink()

    await state.register_file(
        file_id=entry["file_id"],
        chat_id=chat_id,
        msg_id=msg_id,
        expected_size=entry["expected_size"],
        actual_size=final_path.stat().st_size,
        local_path=str(final_path),
        status="done",
    )
    await state.commit()
    logger.debug("re-downloaded: %s", final_path)
    return RedownloadResult.replaced, final_path


# Telegram takes up to 100 message ids in one request; a chat with more broken
# files is asked for in several batches of this size.
MESSAGE_BATCH = 100


async def fetch_broken_messages(api, entries, *, should_stop=None) -> dict[tuple[int, int], Any]:
    """The messages the broken files belong to, one request per batch of a chat.

    A file used to cost a round-trip of its own, while the list of broken files
    carries both the chat and the message id: grouping by chat turns hundreds
    of round-trips into one per hundred files of a chat.
    """
    by_chat: dict[int, list[int]] = defaultdict(list)
    for entry in entries:
        by_chat[entry["chat_id"]].append(entry["msg_id"])

    found: dict[tuple[int, int], Any] = {}
    for chat_id, msg_ids in by_chat.items():
        for start in range(0, len(msg_ids), MESSAGE_BATCH):
            if should_stop and should_stop():
                return found
            batch = msg_ids[start : start + MESSAGE_BATCH]
            answered = await api.client.get_messages(chat_id, ids=batch)
            if not isinstance(answered, list):
                answered = [answered]
            for msg_id, tl_msg in zip(batch, answered, strict=False):
                if tl_msg is not None:
                    found[(chat_id, msg_id)] = tl_msg
    return found


@dataclass
class RedownloadOutcome:
    """What one broken file ended with, for the caller to report its own way."""

    entry: dict[str, Any]
    result: RedownloadResult | None = None
    path: Path | None = None
    error: Exception | None = None


async def redownload_broken_files(
    api, state, entries, *, concurrency: int, should_stop=None
) -> list[RedownloadOutcome]:
    """Re-download every broken file, in the order they were given.

    The messages are fetched in batches first, and the downloads then run with
    the same bounded parallelism as the export itself -- the `verify` path used
    to do both one file at a time, leaving the network idle for the whole
    round-trip of each.

    Each entry comes back with its outcome, or with the exception it raised:
    one failed file does not end the pass, and what to say about it is left to
    the caller, which reports outcomes differently.
    """
    messages = await fetch_broken_messages(api, entries, should_stop=should_stop)
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def one(entry) -> RedownloadOutcome:
        if should_stop and should_stop():
            return RedownloadOutcome(entry)
        async with semaphore:
            try:
                result, final_path = await redownload_broken_file(
                    api, state, entry, tl_msg=messages.get((entry["chat_id"], entry["msg_id"]))
                )
            except Exception as e:  # noqa: BLE001 - reported per file by the caller
                return RedownloadOutcome(entry, error=e)
            return RedownloadOutcome(entry, result=result, path=final_path)

    return list(await asyncio.gather(*(one(entry) for entry in entries)))
