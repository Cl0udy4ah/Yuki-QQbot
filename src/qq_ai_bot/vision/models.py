"""Immutable provider-neutral models for bounded visual analysis."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MediaSource = Literal["current", "reply"]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class MediaReference(_FrozenModel):
    """A trusted image reference extracted from one real OneBot event."""

    message_id: str | None = None
    segment_index: int | None = Field(default=None, ge=0)
    source: MediaSource = "current"
    file: str | None = Field(default=None, repr=False)
    url: str | None = Field(default=None, repr=False)
    summary: str | None = Field(default=None, repr=False)
    sub_type: str | None = None
    declared_size: int | None = Field(default=None, ge=0)
    emoji_id: str | None = None
    emoji_package_id: str | None = None


class DownloadedMedia(_FrozenModel):
    """A bounded in-memory media object; it is never persisted."""

    content: bytes = Field(repr=False)
    content_type: str | None
    content_hash: str
    byte_size: int = Field(ge=0)


class PreparedFrame(_FrozenModel):
    """One normalized frame ready for an OpenAI-compatible vision API."""

    content_hash: str
    mime_type: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    frame_index: int = Field(ge=0)
    frame_count: int = Field(gt=0)
    data_url: str = Field(repr=False)


class PreparedVisualInput(_FrozenModel):
    """Prepared frames belonging to one source image."""

    media_hash: str
    frames: tuple[PreparedFrame, ...]
    animated: bool
    source: MediaSource
    summary_hint: str | None = None


class VisualItemObservation(_FrozenModel):
    """Structured observation for one input image, not a final chat answer."""

    index: int = Field(ge=1)
    description: str = Field(max_length=1200)
    ocr_text: str = Field(default="", max_length=2000)
    expression: str = Field(default="", max_length=1200)
    meme_intent: str = Field(default="", max_length=1200)
    notable_objects: tuple[str, ...] = Field(default=(), max_length=20)
    uncertainty: str = Field(default="", max_length=1200)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class VisualObservation(_FrozenModel):
    """Complete provider result supplied to the text model as untrusted data."""

    items: tuple[VisualItemObservation, ...]
    overall_description: str = Field(max_length=2000)
    partial_failure: bool = False
    provider: str = "unknown"
    model: str = "unknown"
    latency_seconds: float = Field(default=0.0, ge=0.0)
