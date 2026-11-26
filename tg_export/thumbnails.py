"""Conservative thumbnail selection and exact-size fetching.

This module is opt-in. The legacy exporter still downloads originals until a
future media policy routes its work through this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from telethon import types, utils


class ThumbnailUnavailable(ValueError):
    """No safe thumbnail exists or the selected source has changed."""


@dataclass(frozen=True)
class ThumbnailLimits:
    max_dimension: int = 320
    max_bytes: int = 128 * 1024

    def __post_init__(self) -> None:
        if self.max_dimension <= 0 or self.max_bytes <= 0:
            raise ValueError("thumbnail limits must be positive")


@dataclass(frozen=True)
class ThumbnailSelection:
    media_kind: Literal["photo", "document"]
    media_id: int
    access_hash: int = field(repr=False)
    kind: Literal["static", "embedded"]
    constructor: str
    thumb_type: str | None
    width: int
    height: int
    expected_bytes: int
    payload: bytes | None = field(default=None, repr=False)


def _media(value: Any) -> types.Photo | types.Document | None:
    if isinstance(value, types.Message):
        value = value.media
    if isinstance(value, types.MessageMediaPhoto):
        value = value.photo
    elif isinstance(value, types.MessageMediaDocument):
        value = value.document
    return value if isinstance(value, (types.Photo, types.Document)) else None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Read JPEG frame dimensions without decoding image pixels."""
    if len(data) < 4 or not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        return None
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            return None
        while pos < len(data) and data[pos] == 0xFF:
            pos += 1
        if pos >= len(data):
            return None
        marker = data[pos]
        pos += 1
        if marker in (0xD9, 0xDA):
            return None
        if marker in (0x01, *range(0xD0, 0xD8)):
            continue
        if pos + 2 > len(data):
            return None
        length = int.from_bytes(data[pos : pos + 2], "big")
        if length < 2 or pos + length > len(data):
            return None
        if marker in (*range(0xC0, 0xC4), *range(0xC5, 0xC8), *range(0xC9, 0xCC), *range(0xCD, 0xD0)):
            if length < 7:
                return None
            height = int.from_bytes(data[pos + 3 : pos + 5], "big")
            width = int.from_bytes(data[pos + 5 : pos + 7], "big")
            return (width, height) if width > 0 and height > 0 else None
        pos += length
    return None


def _embedded(size: Any, limits: ThumbnailLimits) -> tuple[bytes, int, int] | None:
    if isinstance(size, types.PhotoCachedSize):
        if len(size.bytes) > limits.max_bytes:
            return None
        data = bytes(size.bytes)
    elif isinstance(size, types.PhotoStrippedSize):
        if len(size.bytes) < 3 or len(size.bytes) > limits.max_bytes or size.bytes[0] != 1:
            return None
        data = utils.stripped_photo_to_jpg(size.bytes)
    else:
        return None
    if not data or len(data) > limits.max_bytes:
        return None
    dimensions = _jpeg_dimensions(data)
    if dimensions is None or max(dimensions) > limits.max_dimension:
        return None
    return data, *dimensions


def _static_size(size: Any) -> int | None:
    if isinstance(size, types.PhotoSize):
        return size.size if isinstance(size.size, int) and size.size > 0 else None
    if (
        isinstance(size, types.PhotoSizeProgressive)
        and size.sizes
        and all(isinstance(part, int) and part > 0 for part in size.sizes)
    ):
        return max(size.sizes)
    return None


def select_thumbnail(value: Any, limits: ThumbnailLimits = ThumbnailLimits()) -> ThumbnailSelection | None:
    """Choose a bounded static thumbnail, then an embedded JPEG; never an original."""
    media = _media(value)
    if media is None:
        return None
    is_photo = isinstance(media, types.Photo)
    sizes = list((media.sizes if is_photo else media.thumbs) or [])
    video_sizes = list((media.video_sizes if is_photo else media.video_thumbs) or [])
    network = [
        size
        for size in sizes
        if isinstance(size, (types.PhotoSize, types.PhotoSizeProgressive))
        and isinstance(size.w, int)
        and isinstance(size.h, int)
        and size.w > 0
        and size.h > 0
    ]
    # Telethon dispatches by the type code, not by the selected TL object.
    # Reject codes shared with another static, cached, or animated variant.
    codes = [getattr(size, "type", None) for size in [*sizes, *video_sizes]]
    largest_photo_area = max((size.w * size.h for size in network), default=0) if is_photo else 0
    candidates: list[ThumbnailSelection] = []
    kind: Literal["photo", "document"] = "photo" if is_photo else "document"
    for size in network:
        byte_count = _static_size(size)
        code = size.type
        if (
            not code
            or codes.count(code) != 1
            or (is_photo and size.w * size.h >= largest_photo_area)
            or max(size.w, size.h) > limits.max_dimension
            or byte_count is None
            or byte_count > limits.max_bytes
        ):
            continue
        candidates.append(
            ThumbnailSelection(
                kind,
                media.id,
                media.access_hash,
                "static",
                type(size).__name__,
                code,
                size.w,
                size.h,
                byte_count,
            )
        )
    if candidates:
        return max(
            candidates,
            key=lambda item: (
                item.width * item.height,
                item.expected_bytes,
                item.thumb_type or "",
                item.constructor,
            ),
        )

    embedded: list[ThumbnailSelection] = []
    for size in sizes:
        found = _embedded(size, limits)
        if found is None:
            continue
        data, width, height = found
        embedded.append(
            ThumbnailSelection(
                kind,
                media.id,
                media.access_hash,
                "embedded",
                type(size).__name__,
                getattr(size, "type", None),
                width,
                height,
                len(data),
                data,
            )
        )
    if embedded:
        return max(embedded, key=lambda item: (item.width * item.height, item.expected_bytes))
    return None


async def fetch_thumbnail(
    client: Any,
    value: Any,
    selection: ThumbnailSelection | None,
    limits: ThumbnailLimits = ThumbnailLimits(),
) -> bytes:
    """Fetch only a revalidated exact variant; failures never retry as original."""
    if selection is None or select_thumbnail(value, limits) != selection:
        raise ThumbnailUnavailable("thumbnail missing or source changed")
    if selection.kind == "embedded":
        assert selection.payload is not None
        return selection.payload

    media = _media(value)
    assert media is not None
    code = selection.thumb_type
    if not code:
        raise ThumbnailUnavailable("empty thumbnail type would request original")
    if isinstance(media, types.Photo):
        location = types.InputPhotoFileLocation(media.id, media.access_hash, media.file_reference, code)
    else:
        location = types.InputDocumentFileLocation(media.id, media.access_hash, media.file_reference, code)
    result = await client.download_file(
        location, file=bytes, file_size=selection.expected_bytes, dc_id=media.dc_id
    )
    if not isinstance(result, bytes) or not result or len(result) > limits.max_bytes:
        raise ThumbnailUnavailable("thumbnail response missing or over budget")
    return result
