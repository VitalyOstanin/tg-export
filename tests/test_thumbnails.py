"""Синтетические TL-медиа и запрет fallback к оригиналу."""

from __future__ import annotations

from typing import Any

import pytest
from telethon import types, utils

from tg_export.thumbnails import (
    ThumbnailLimits,
    ThumbnailSelection,
    ThumbnailUnavailable,
    fetch_thumbnail,
    select_thumbnail,
)


def photo(*sizes: Any, dc_id: int = 1, video_sizes: list[Any] | None = None) -> types.Photo:
    return types.Photo(
        id=10,
        access_hash=20,
        file_reference=b"reference",
        date=None,
        sizes=list(sizes),
        dc_id=dc_id,
        video_sizes=video_sizes,
    )


def document(*thumbs: Any, dc_id: int = 2, video_thumbs: list[Any] | None = None) -> types.Document:
    return types.Document(
        id=30,
        access_hash=40,
        file_reference=b"reference",
        date=None,
        mime_type="video/mp4",
        size=2 * 1024**3,
        dc_id=dc_id,
        attributes=[],
        thumbs=list(thumbs),
        video_thumbs=video_thumbs,
    )


def static(code: str, dimension: int, size: int) -> types.PhotoSize:
    return types.PhotoSize(type=code, w=dimension, h=dimension, size=size)


class DownloadProbe:
    def __init__(self, *, result: bytes | None = b"image", error: Exception | None = None) -> None:
        self.calls: list[tuple[Any, Any, Any, Any]] = []
        self.result = result
        self.error = error

    async def download_file(self, location: Any, *, file: Any, file_size: Any, dc_id: Any) -> bytes | None:
        self.calls.append((location, file, file_size, dc_id))
        if self.error:
            raise self.error
        return self.result


def test_photo_selects_best_bounded_size_but_never_largest():
    chosen = select_thumbnail(
        photo(static("s", 90, 1_000), static("m", 320, 20_000), static("x", 800, 100_000))
    )
    assert chosen is not None
    assert chosen.kind == "static"
    assert chosen.thumb_type == "m"
    assert chosen.width == 320


@pytest.mark.parametrize(
    "sizes",
    [
        [static("x", 320, 10_000)],
        [static("x", 320, 10_000), static("y", 320, 11_000)],
        [static("x", 800, 100_000), static("s", 90, 200_000)],
        [types.PhotoSizeProgressive(type="p", w=200, h=200, sizes=[1_000, 200_000])],
    ],
)
def test_photo_without_safe_smaller_network_size_has_no_thumbnail(sizes: list[Any]):
    assert select_thumbnail(photo(*sizes)) is None


def test_progressive_uses_largest_stage_and_exact_type():
    chosen = select_thumbnail(
        photo(
            types.PhotoSizeProgressive(type="p", w=300, h=200, sizes=[1_000, 8_000, 40_000]),
            static("x", 800, 150_000),
        )
    )
    assert chosen is not None
    assert chosen.constructor == "PhotoSizeProgressive"
    assert chosen.thumb_type == "p"
    assert chosen.expected_bytes == 40_000


def test_large_video_uses_small_static_thumb_independently_of_original_size():
    chosen = select_thumbnail(
        document(
            static("m", 160, 20_000),
            video_thumbs=[types.VideoSize(type="v", w=160, h=160, size=30_000)],
        )
    )
    assert chosen is not None
    assert chosen.kind == "static"
    assert chosen.thumb_type == "m"
    assert chosen.expected_bytes == 20_000


def test_duplicate_type_including_animated_is_rejected():
    assert (
        select_thumbnail(
            document(
                static("m", 160, 20_000),
                video_thumbs=[types.VideoSize(type="m", w=160, h=160, size=30_000)],
            )
        )
        is None
    )
    assert select_thumbnail(photo(static("m", 100, 1_000), static("m", 800, 100_000))) is None


def test_stripped_and_cached_are_embedded_without_network():
    raw = bytes([1, 16, 16, 0])
    jpeg = utils.stripped_photo_to_jpg(raw)
    for size in (
        types.PhotoStrippedSize(type="i", bytes=raw),
        types.PhotoCachedSize(type="c", w=16, h=16, bytes=jpeg),
    ):
        chosen = select_thumbnail(photo(size))
        assert chosen is not None
        assert chosen.kind == "embedded"
        assert chosen.payload == jpeg
        assert chosen.width == chosen.height == 16


def test_invalid_embedded_and_unsupported_sizes_do_not_trigger_original():
    assert select_thumbnail(photo(types.PhotoStrippedSize(type="i", bytes=b"bad"))) is None
    assert select_thumbnail(photo(types.PhotoCachedSize(type="c", w=16, h=16, bytes=b"bad"))) is None
    assert select_thumbnail(photo(types.PhotoSizeEmpty(type="e"))) is None
    assert select_thumbnail(photo(types.PhotoPathSize(type="j", bytes=b"svg-path"))) is None
    assert select_thumbnail(document()) is None


def test_limits_reject_unbounded_or_oversize_thumbnail():
    with pytest.raises(ValueError):
        ThumbnailLimits(max_bytes=0)
    assert select_thumbnail(document(static("m", 160, 20_000)), ThumbnailLimits(max_bytes=10_000)) is None


@pytest.mark.asyncio
async def test_no_selection_is_rejected_before_download():
    media = photo(static("x", 320, 10_000))
    probe = DownloadProbe()
    with pytest.raises(ThumbnailUnavailable):
        await fetch_thumbnail(probe, media, select_thumbnail(media))
    assert probe.calls == []


@pytest.mark.asyncio
async def test_changed_media_or_forged_original_is_rejected_before_download():
    media = photo(static("m", 160, 10_000), static("x", 800, 100_000))
    chosen = select_thumbnail(media)
    assert chosen is not None
    probe = DownloadProbe()
    changed = photo(static("x", 800, 100_000), static("y", 1200, 200_000))
    forged = ThumbnailSelection("photo", 10, 20, "static", "PhotoSize", "x", 800, 800, 100_000)
    with pytest.raises(ThumbnailUnavailable):
        await fetch_thumbnail(probe, changed, chosen)
    with pytest.raises(ThumbnailUnavailable):
        await fetch_thumbnail(probe, media, forged)
    assert probe.calls == []


@pytest.mark.asyncio
async def test_static_fetch_uses_nonempty_exact_location_and_no_retry_on_error():
    media = document(static("m", 160, 20_000))
    chosen = select_thumbnail(media)
    assert chosen is not None
    probe = DownloadProbe(error=RuntimeError("reference expired"))
    with pytest.raises(RuntimeError, match="reference expired"):
        await fetch_thumbnail(probe, media, chosen)
    assert len(probe.calls) == 1
    location, file, size, dc = probe.calls[0]
    assert isinstance(location, types.InputDocumentFileLocation)
    assert location.thumb_size == "m"
    assert file is bytes
    assert size == 20_000
    assert dc == 2


@pytest.mark.asyncio
async def test_embedded_fetch_and_oversize_response_never_fall_back():
    embedded_media = photo(types.PhotoStrippedSize(type="i", bytes=bytes([1, 16, 16, 0])))
    probe = DownloadProbe()
    selected = select_thumbnail(embedded_media)
    assert selected is not None
    assert (await fetch_thumbnail(probe, embedded_media, selected)).startswith(b"\xff\xd8")
    assert probe.calls == []

    media = document(static("m", 160, 20_000))
    selected = select_thumbnail(media)
    assert selected is not None
    probe = DownloadProbe(result=b"x" * (128 * 1024 + 1))
    with pytest.raises(ThumbnailUnavailable, match="over budget"):
        await fetch_thumbnail(probe, media, selected)
    assert len(probe.calls) == 1
