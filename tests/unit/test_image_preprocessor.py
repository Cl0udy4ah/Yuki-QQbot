"""Pillow preprocessing bounds static and animated visual inputs."""

from __future__ import annotations

import hashlib
import io

import pytest
from PIL import Image

from qq_ai_bot.services.image_preprocessor import ImagePreprocessingError, ImagePreprocessor
from qq_ai_bot.vision.models import DownloadedMedia


def _downloaded(content: bytes, content_type: str = "application/octet-stream") -> DownloadedMedia:
    return DownloadedMedia(
        content=content,
        content_type=content_type,
        content_hash=hashlib.sha256(content).hexdigest(),
        byte_size=len(content),
    )


def _image_bytes(
    *,
    size: tuple[int, int] = (32, 24),
    image_format: str = "PNG",
) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", size, (255, 0, 0, 128)).save(buffer, format=image_format)
    return buffer.getvalue()


def test_real_image_format_wins_over_declared_content_type() -> None:
    prepared = ImagePreprocessor().prepare(
        _downloaded(_image_bytes(), "text/html"),
        source="current",
    )

    assert prepared.media_hash
    assert prepared.animated is False
    assert prepared.frames[0].mime_type == "image/jpeg"
    assert prepared.frames[0].data_url.startswith("data:image/jpeg;base64,")


def test_large_resolution_is_scaled_proportionally() -> None:
    prepared = ImagePreprocessor(max_dimension=100, max_pixels=10_000).prepare(
        _downloaded(_image_bytes(size=(400, 200))),
        source="reply",
    )

    assert prepared.frames[0].width == 100
    assert prepared.frames[0].height == 50
    assert prepared.source == "reply"


def test_exif_orientation_is_applied_before_output() -> None:
    buffer = io.BytesIO()
    image = Image.new("RGB", (40, 20), "blue")
    exif = image.getexif()
    exif[274] = 6
    image.save(buffer, format="JPEG", exif=exif)

    prepared = ImagePreprocessor().prepare(
        _downloaded(buffer.getvalue(), "image/jpeg"),
        source="current",
    )

    assert (prepared.frames[0].width, prepared.frames[0].height) == (20, 40)


def test_animated_gif_samples_first_middle_and_last_in_order() -> None:
    frames = [Image.new("RGB", (8, 8), (index * 20, 0, 0)) for index in range(10)]
    buffer = io.BytesIO()
    frames[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=10,
        loop=0,
    )

    prepared = ImagePreprocessor(gif_max_frames=4).prepare(
        _downloaded(buffer.getvalue(), "image/gif"),
        source="current",
        max_frames=4,
    )

    assert prepared.animated is True
    assert [frame.frame_index for frame in prepared.frames] == [0, 3, 6, 9]
    assert all(frame.frame_count == 10 for frame in prepared.frames)


def test_animated_webp_uses_the_same_bounded_sampling_pipeline() -> None:
    frames = [Image.new("RGB", (8, 8), (index * 40, 0, 0)) for index in range(4)]
    buffer = io.BytesIO()
    frames[0].save(
        buffer,
        format="WEBP",
        save_all=True,
        append_images=frames[1:],
        duration=10,
        loop=0,
    )

    prepared = ImagePreprocessor(gif_max_frames=3).prepare(
        _downloaded(buffer.getvalue(), "image/webp"),
        source="current",
        max_frames=3,
    )

    assert prepared.animated is True
    assert [frame.frame_index for frame in prepared.frames] == [0, 2, 3]


def test_corrupt_or_extreme_image_is_rejected_safely() -> None:
    with pytest.raises(ImagePreprocessingError) as corrupt:
        ImagePreprocessor().prepare(_downloaded(b"not an image"), source="current")
    assert corrupt.value.code == "corrupt_image"

    with pytest.raises(ImagePreprocessingError) as extreme:
        ImagePreprocessor().prepare(
            _downloaded(_image_bytes(size=(1000, 1))),
            source="current",
        )
    assert extreme.value.code == "extreme_aspect_ratio"


def test_real_pillow_decompression_bomb_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Lower Pillow's global test threshold so a real, small PNG exercises its
    # DecompressionBombError path without allocating an unsafe image in CI.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    content = _image_bytes(size=(20, 20))

    with pytest.raises(ImagePreprocessingError) as raised:
        ImagePreprocessor().prepare(_downloaded(content), source="current")

    assert raised.value.code == "decompression_bomb"
