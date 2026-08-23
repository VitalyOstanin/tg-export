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
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from tg_export.media import STAGING_PREFIX, TargetRegistry, download_with_retries
from tg_export.models import FileStatus

logger = logging.getLogger(__name__)


class RedownloadResult(StrEnum):
    """What re-downloading one broken file ended with.

    The callers report an outcome differently -- one prints lines, the other
    fills the error list of the run -- so the decision of what to say is left
    to them.
    """

    replaced = "replaced"
    no_media = "no_media"
    nothing_downloaded = "nothing_downloaded"


async def redownload_broken_file(
    api, state, entry: dict[str, Any], *, tl_msg: Any | None = None, targets: TargetRegistry | None = None
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

    ``targets`` is the registry the destination name is taken from, shared by
    everything writing into the same output tree. Downloading into an empty
    staging directory means Telethon always answers with the base name, so two
    files the export had told apart as `photo.jpg` and `photo (1).jpg` would
    otherwise both be moved onto `photo.jpg`.
    """
    chat_id = entry["chat_id"]
    msg_id = entry["msg_id"]
    local_path = Path(entry["local_path"])

    if tl_msg is None:
        from tg_export.api import one_message

        tl_msg = one_message(await api.client.get_messages(chat_id, ids=msg_id))
    if tl_msg is None or tl_msg.media is None:
        return RedownloadResult.no_media, None

    if targets is None:
        targets = TargetRegistry()
    target_dir = local_path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    # dir=target_dir keeps the staging area on the same filesystem, so the final
    # move is an atomic rename rather than a copy.
    with tempfile.TemporaryDirectory(dir=target_dir, prefix=STAGING_PREFIX) as staging:
        # The same retry policy the export downloads under: a dropped
        # connection or a flood wait must not turn a file this pass exists to
        # restore into a failure of the pass.
        downloaded = await download_with_retries(
            lambda: api.download_media(tl_msg, Path(staging)), msg_id=msg_id
        )
        if not downloaded:
            return RedownloadResult.nothing_downloaded, None

        downloaded = Path(downloaded)
        with targets.claim(target_dir, downloaded.name, 0, reuse=False, replacing=local_path) as (
            final_path,
            _,
        ):
            os.replace(downloaded, final_path)

            # Telethon may pick a different name than the one recorded earlier;
            # drop the stale file only now, once its replacement is in place,
            # and only while nobody else holds that name.
            if local_path != final_path:
                targets.drop_unclaimed(local_path)

    await state.register_file(
        file_id=entry["file_id"],
        chat_id=chat_id,
        msg_id=msg_id,
        expected_size=entry["expected_size"],
        actual_size=final_path.stat().st_size,
        local_path=str(final_path),
        status=FileStatus.done,
    )
    await state.commit()
    logger.debug("re-downloaded: %s", final_path)
    return RedownloadResult.replaced, final_path


# Telegram takes up to 100 message ids in one request; a chat with more broken
# files is asked for in several batches of this size.
MESSAGE_BATCH = 100


async def fetch_broken_messages(api, numbered, *, should_stop=None):
    """Yield each request's entries together with the messages it answered.

    A file used to cost a round-trip of its own, while the list of broken files
    carries both the chat and the message id: grouping by chat turns hundreds
    of round-trips into one per hundred files of a chat.

    The batches are handed over one by one rather than collected first, so the
    caller can start downloading what the first request answered while the
    rest are still being asked for.
    """
    by_chat: dict[int, list[tuple[int, dict]]] = defaultdict(list)
    for index, entry in numbered:
        by_chat[entry["chat_id"]].append((index, entry))

    for chat_id, chat_entries in by_chat.items():
        for start in range(0, len(chat_entries), MESSAGE_BATCH):
            if should_stop and should_stop():
                return
            batch = chat_entries[start : start + MESSAGE_BATCH]
            msg_ids = [entry["msg_id"] for _, entry in batch]
            answered = await api.client.get_messages(chat_id, ids=msg_ids)
            if not isinstance(answered, list):
                answered = [answered]
            found: dict[tuple[int, int], Any] = {
                (chat_id, msg_id): tl_msg
                for msg_id, tl_msg in zip(msg_ids, answered, strict=False)
                if tl_msg is not None
            }
            yield batch, found


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

    Each batch of messages starts its downloads as soon as it arrives, and they
    run with the same bounded parallelism as the export itself -- the `verify`
    path used to do both one file at a time, leaving the network idle for the
    whole round-trip of each.

    Each entry comes back with its outcome, or with the exception it raised:
    one failed file does not end the pass, and what to say about it is left to
    the caller, which reports outcomes differently.
    """
    numbered = list(enumerate(entries))
    semaphore = asyncio.Semaphore(max(1, concurrency))
    # One registry for the whole pass: the names the parallel downloads compete
    # for are in the same directories.
    targets = TargetRegistry()

    async def one(entry, tl_msg) -> RedownloadOutcome:
        if should_stop and should_stop():
            return RedownloadOutcome(entry)
        async with semaphore:
            try:
                result, final_path = await redownload_broken_file(
                    api, state, entry, tl_msg=tl_msg, targets=targets
                )
            except Exception as e:  # noqa: BLE001 - reported per file by the caller
                return RedownloadOutcome(entry, error=e)
            return RedownloadOutcome(entry, result=result, path=final_path)

    started: list[tuple[int, asyncio.Task]] = []
    async for batch, messages in fetch_broken_messages(api, numbered, should_stop=should_stop):
        for index, entry in batch:
            task = asyncio.create_task(one(entry, messages.get((entry["chat_id"], entry["msg_id"]))))
            started.append((index, task))

    outcomes: list[RedownloadOutcome] = [RedownloadOutcome(entry) for _, entry in numbered]
    if started:
        for (index, _), outcome in zip(
            started, await asyncio.gather(*(task for _, task in started)), strict=True
        ):
            outcomes[index] = outcome
    return outcomes
