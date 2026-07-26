"""Provider-neutral visual analysis package."""

from qq_ai_bot.vision.base import VisionConfigurationError, VisionError, VisionProvider
from qq_ai_bot.vision.fake import FakeVisionProvider
from qq_ai_bot.vision.models import (
    DownloadedMedia,
    MediaReference,
    MediaSource,
    PreparedFrame,
    PreparedVisualInput,
    VisualItemObservation,
    VisualObservation,
)
from qq_ai_bot.vision.qwen import QwenVisionProvider

__all__ = [
    "DownloadedMedia",
    "FakeVisionProvider",
    "MediaReference",
    "MediaSource",
    "PreparedFrame",
    "PreparedVisualInput",
    "QwenVisionProvider",
    "VisionConfigurationError",
    "VisionError",
    "VisionProvider",
    "VisualItemObservation",
    "VisualObservation",
]
