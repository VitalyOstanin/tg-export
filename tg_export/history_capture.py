"""Snapshot a TL message and select a bounded thumbnail intent."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import telethon
from telethon import types

from tg_export.thumbnails import ThumbnailSelection, select_thumbnail

ENCODING_VERSION = 1
THUMBNAIL_POLICY_DIGEST = "selector-v1:320:131072"


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {"$type": "bytes", "base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("Raw dictionary has a non-string key")
        return {key: _json_value(item) for key, item in value.items()}
    raise TypeError(f"unsupported Raw value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class HistoryCapture:
    account_id: int
    chat_id: int
    msg_id: int
    captured_at: str
    telethon_version: str
    raw_json: str | None
    raw_sha256: str | None
    error_code: str | None
    media: types.Photo | types.Document | None
    selection: ThumbnailSelection | None
    source_locator: str | None


def capture_message(tl_msg: Any, *, account_id: int, chat_id: int) -> HistoryCapture:
    """Capture Raw before conversion and select only a bounded thumbnail."""
    msg_id = int(tl_msg.id)
    captured_at = datetime.now(UTC).isoformat()
    try:
        raw = canonical_json(_json_value(tl_msg.to_dict()))
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        error_code = None
    except (AttributeError, TypeError, ValueError, OverflowError):
        raw = None
        digest = None
        error_code = "raw_serialization_failed"

    wrapper = getattr(tl_msg, "media", None)
    value = getattr(wrapper, "photo", None)
    if not isinstance(value, types.Photo):
        value = getattr(wrapper, "document", None)
    media = value if isinstance(value, (types.Photo, types.Document)) else None
    selection = select_thumbnail(media) if media is not None else None
    locator = (
        canonical_json(
            {
                "account_id": str(account_id),
                "chat_id": str(chat_id),
                "msg_id": str(msg_id),
                "peer_type": type(getattr(tl_msg, "peer_id", None)).__name__,
            }
        )
        if media is not None
        else None
    )
    return HistoryCapture(
        account_id,
        chat_id,
        msg_id,
        captured_at,
        telethon.__version__,
        raw,
        digest,
        error_code,
        media,
        selection,
        locator,
    )
