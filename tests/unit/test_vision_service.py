"""Vision orchestration, selection, cache, and independent rate-limit tests."""

from __future__ import annotations

import asyncio
import base64
import io

import pytest
from PIL import Image
from sqlalchemy import func, select

from qq_ai_bot.admin.models import VisionRuntimeConfig
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    AttachmentKind,
    InboundMessage,
    MessageAttachment,
    SenderIdentity,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import EmojiDescriptionModel
from qq_ai_bot.persistence.repositories import (
    EmojiDescriptionRepository,
    MediaAnalysisRepository,
)
from qq_ai_bot.services.image_preprocessor import ImagePreprocessor
from qq_ai_bot.services.media_resolver import MediaResolver
from qq_ai_bot.services.vision_rate_limit import VisionRateLimiter
from qq_ai_bot.services.vision_service import (
    DEFAULT_VISUAL_QUESTION,
    VisionProcessingError,
    VisionService,
    _emoji_keys,
    _is_explicit_emoji,
)
from qq_ai_bot.vision.fake import FakeVisionProvider
from qq_ai_bot.vision.models import (
    MediaReference,
    PreparedVisualInput,
    VisionAnalysisOptions,
    VisualObservation,
)


def test_emoji_file_hash_is_stable_but_sub_type_alone_is_not_an_emoji_signal() -> None:
    digest = "F708282432DBEF6A26F24B82054D4C91"
    first = MediaReference(file=f"{digest}.jpg", summary="[动画表情]", sub_type="1")
    second = MediaReference(file=f"/cache/{digest.lower()}.png", summary="贴纸")
    photo = MediaReference(file=f"{digest}.jpg")

    assert _emoji_keys(first) == (f"file:{digest.lower()}",)
    assert _emoji_keys(second) == _emoji_keys(first)
    assert _emoji_keys(photo) == _emoji_keys(first)
    assert _is_explicit_emoji(first)
    assert _is_explicit_emoji(second)
    assert not _is_explicit_emoji(photo)


class BlockingVisionProvider(FakeVisionProvider):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.call_count = 0

    async def analyze(
        self,
        inputs: tuple[PreparedVisualInput, ...],
        question: str,
        *,
        options: VisionAnalysisOptions | None = None,
    ) -> VisualObservation:
        self.call_count += 1
        self.started.set()
        await self.release.wait()
        return await super().analyze(inputs, question, options=options)


def _inline_png(color: tuple[int, int, int] = (255, 0, 0)) -> str:
    output = io.BytesIO()
    Image.new("RGB", (8, 6), color).save(output, format="PNG")
    return "base64://" + base64.b64encode(output.getvalue()).decode("ascii")


def _inline_gif() -> str:
    output = io.BytesIO()
    frames = [Image.new("RGB", (8, 6), (index * 30, 10, 20)) for index in range(6)]
    frames[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=30,
        loop=0,
    )
    return "base64://" + base64.b64encode(output.getvalue()).decode("ascii")


def _message(
    *,
    message_id: str = "1",
    text: str = "",
    current: tuple[MessageAttachment, ...] = (),
    reply: tuple[MessageAttachment, ...] = (),
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        event_type="message:private:friend",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001"),
        text=text,
        bot_user_id="9999",
        attachments=current,
        reply_attachments=reply,
        reply_to_message_id="8" if reply else None,
    )


def _image(
    file: str,
    index: int,
    *,
    source: str = "current",
    summary: str | None = None,
    sub_type: str | None = None,
    emoji_id: str | None = None,
    emoji_package_id: str | None = None,
) -> MessageAttachment:
    return MessageAttachment(
        kind=AttachmentKind.IMAGE,
        label="image",
        segment_index=index,
        source=source,
        file=file,
        summary=summary,
        sub_type=sub_type,
        emoji_id=emoji_id,
        emoji_package_id=emoji_package_id,
    )


def _runtime(
    *,
    maximum: int = 3,
    user_rate: int = 4,
    max_frames: int = 8,
    gif_frames: int = 4,
) -> VisionRuntimeConfig:
    return VisionRuntimeConfig(
        max_images_per_turn=maximum,
        max_frames_per_turn=max_frames,
        gif_max_frames=gif_frames,
        thinking_enabled=True,
        thinking_budget=3072,
        low_confidence_retry_threshold=0.65,
        per_user_requests_per_minute=user_rate,
        per_group_requests_per_minute=12,
        analysis_retention_days=7,
    )


def _service(database: Database, provider: FakeVisionProvider) -> VisionService:
    return VisionService(
        provider=provider,
        resolver=MediaResolver(),
        preprocessor=ImagePreprocessor(),
        analyses=MediaAnalysisRepository(database),
        rate_limiter=VisionRateLimiter(),
        emoji_descriptions=EmojiDescriptionRepository(database),
    )


def test_current_images_win_over_reply_and_keep_order() -> None:
    message = _message(
        current=(_image("a", 3), _image("b", 5), _image("c", 8)),
        reply=(_image("old", 1, source="reply"),),
    )

    selected = VisionService.select_references(message, maximum=2)

    assert [item.file for item in selected] == ["a", "b"]
    assert [item.segment_index for item in selected] == [3, 5]
    assert all(item.source == "current" for item in selected)


def test_reply_images_are_used_only_without_current_images() -> None:
    message = _message(
        reply=(_image("old-a", 1, source="reply"), _image("old-b", 2, source="reply"))
    )

    selected = VisionService.select_references(message, maximum=3)

    assert [item.file for item in selected] == ["old-a", "old-b"]
    assert all(item.source == "reply" for item in selected)


@pytest.mark.asyncio
async def test_one_request_handles_multiple_images_and_uses_default_question(
    database: Database,
) -> None:
    provider = FakeVisionProvider()
    service = _service(database, provider)
    message = _message(current=(_image(_inline_png(), 0), _image(_inline_png((0, 0, 255)), 1)))

    observation = await service.analyze(
        message,
        question="",
        runtime=_runtime(),
        gateway=None,
        source_event_id=None,
        conversation_key="private:1001",
    )

    assert len(provider.requests) == 1
    assert len(provider.requests[0][0]) == 2
    assert provider.requests[0][1] == DEFAULT_VISUAL_QUESTION
    assert provider.request_options[0].analysis_mode == "general"
    assert len(observation.items) == 2
    await service.close()
    assert provider.closed


@pytest.mark.asyncio
async def test_character_question_selects_reasoning_mode(database: Database) -> None:
    provider = FakeVisionProvider()
    service = _service(database, provider)
    message = _message(text="这是谁？", current=(_image(_inline_png(), 0),))

    await service.analyze(
        message,
        question=message.text,
        runtime=_runtime(),
        gateway=None,
        source_event_id=None,
        conversation_key="private:1001",
    )

    assert provider.request_options[0].analysis_mode == "character"
    assert provider.request_options[0].thinking_enabled
    assert provider.request_options[0].thinking_budget == 3072
    await service.close()


@pytest.mark.asyncio
async def test_total_frame_limit_applies_across_all_animated_images(database: Database) -> None:
    provider = FakeVisionProvider()
    service = _service(database, provider)
    message = _message(current=tuple(_image(_inline_gif(), index) for index in range(3)))

    observation = await service.analyze(
        message,
        question="这些动图在做什么？",
        runtime=_runtime(),
        gateway=None,
        source_event_id=None,
        conversation_key="private:1001",
    )

    assert len(provider.requests) == 1
    inputs = provider.requests[0][0]
    assert sum(len(item.frames) for item in inputs) == 8
    assert len(inputs) == 2
    assert observation.partial_failure
    await service.close()


@pytest.mark.asyncio
async def test_cache_hit_skips_provider_and_does_not_consume_second_quota(
    database: Database,
) -> None:
    provider = FakeVisionProvider()
    service = _service(database, provider)
    first = _message(message_id="1", text="这是什么", current=(_image(_inline_png(), 0),))
    second = _message(message_id="2", text="这是什么", current=(_image(_inline_png(), 0),))

    await service.analyze(
        first,
        question=first.text,
        runtime=_runtime(user_rate=1),
        gateway=None,
        source_event_id=None,
        conversation_key="private:1001",
    )
    await service.analyze(
        second,
        question=second.text,
        runtime=_runtime(user_rate=1),
        gateway=None,
        source_event_id=None,
        conversation_key="private:1001",
    )

    assert len(provider.requests) == 1
    with pytest.raises(VisionProcessingError, match="频繁") as exc_info:
        await service.analyze(
            _message(message_id="3", text="图里有什么", current=(_image(_inline_png(), 0),)),
            question="图里有什么",
            runtime=_runtime(user_rate=1),
            gateway=None,
            source_event_id=None,
            conversation_key="private:1001",
        )
    assert exc_info.value.code == "rate_limited"
    await service.close()


@pytest.mark.asyncio
async def test_ordinary_photo_is_not_written_to_persistent_emoji_library(
    database: Database,
) -> None:
    service = _service(database, FakeVisionProvider())
    await service.analyze(
        _message(current=(_image(_inline_png(), 0, sub_type="1"),)),
        question="",
        runtime=_runtime(),
        gateway=None,
        source_event_id=None,
        conversation_key="private:1001",
    )

    async with database.sessions() as session:
        count = await session.scalar(select(func.count(EmojiDescriptionModel.id)))
    assert count == 0
    await service.close()


@pytest.mark.asyncio
async def test_same_qq_emoji_uses_persistent_description_before_media_resolution(
    database: Database,
) -> None:
    first_provider = FakeVisionProvider()
    first_service = _service(database, first_provider)
    first_image = _image(
        _inline_png(),
        0,
        summary="[动画表情]",
        sub_type="1",
        emoji_id="emoji-1",
        emoji_package_id="package-2",
    )
    await first_service.analyze(
        _message(message_id="emoji-first", current=(first_image,)),
        question="",
        runtime=_runtime(),
        gateway=None,
        source_event_id=None,
        conversation_key="private:1001",
    )
    await first_service.close()

    second_provider = FakeVisionProvider()
    second_service = _service(database, second_provider)
    same_identity_with_expired_resource = _image(
        "base64://%%%",
        0,
        summary="[动画表情]",
        sub_type="1",
        emoji_id="emoji-1",
        emoji_package_id="package-2",
    )
    observation = await second_service.analyze(
        _message(message_id="emoji-second", current=(same_identity_with_expired_resource,)),
        question="",
        runtime=_runtime(),
        gateway=None,
        source_event_id=None,
        conversation_key="private:1001",
    )

    assert observation.overall_description
    assert second_provider.requests == []
    await second_service.close()


@pytest.mark.asyncio
async def test_persistent_emoji_description_never_crosses_questions(database: Database) -> None:
    provider = FakeVisionProvider()
    service = _service(database, provider)
    identity = {
        "summary": "[动画表情]",
        "sub_type": "1",
        "emoji_id": "emoji-question",
        "emoji_package_id": "package-question",
    }
    await service.analyze(
        _message(current=(_image(_inline_png(), 0, **identity),)),
        question="这是谁",
        runtime=_runtime(),
        gateway=None,
        source_event_id=None,
        conversation_key="private:1001",
    )

    with pytest.raises(VisionProcessingError) as exc_info:
        await service.analyze(
            _message(
                message_id="different-question",
                current=(_image("base64://%%%", 0, **identity),),
            ),
            question="图片里写了什么",
            runtime=_runtime(),
            gateway=None,
            source_event_id=None,
            conversation_key="private:1001",
        )
    assert exc_info.value.code == "invalid_base64"
    await service.close()


@pytest.mark.asyncio
async def test_cache_varies_with_summary_hint_and_hot_frame_limits(database: Database) -> None:
    provider = FakeVisionProvider()
    service = _service(database, provider)
    gif = _inline_gif()

    await service.analyze(
        _message(message_id="hint-a", current=(_image(gif, 0, summary="提示 A"),)),
        question="",
        runtime=_runtime(max_frames=2, gif_frames=2),
        gateway=None,
        source_event_id=None,
        conversation_key="private:1001",
    )
    await service.analyze(
        _message(message_id="hint-b", current=(_image(gif, 0, summary="提示 B"),)),
        question="",
        runtime=_runtime(max_frames=2, gif_frames=2),
        gateway=None,
        source_event_id=None,
        conversation_key="private:1001",
    )
    await service.analyze(
        _message(message_id="hint-c", current=(_image(gif, 0, summary="提示 B"),)),
        question="",
        runtime=_runtime(max_frames=8, gif_frames=4),
        gateway=None,
        source_event_id=None,
        conversation_key="private:1001",
    )

    assert len(provider.requests) == 3
    assert [len(request[0][0].frames) for request in provider.requests] == [2, 2, 4]
    await service.close()


@pytest.mark.asyncio
async def test_partial_resolution_result_does_not_pollute_content_cache(
    database: Database,
) -> None:
    provider = FakeVisionProvider()
    service = _service(database, provider)
    attachments = (_image(_inline_png(), 0), _image("base64://%%%", 1))

    for message_id in ("partial-a", "partial-b"):
        observation = await service.analyze(
            _message(message_id=message_id, current=attachments),
            question="比较图片",
            runtime=_runtime(),
            gateway=None,
            source_event_id=None,
            conversation_key="private:1001",
        )
        assert observation.partial_failure

    assert len(provider.requests) == 2
    await service.close()


@pytest.mark.asyncio
async def test_invalid_inline_image_fails_without_provider_call(database: Database) -> None:
    provider = FakeVisionProvider()
    service = _service(database, provider)

    with pytest.raises(VisionProcessingError) as exc_info:
        await service.analyze(
            _message(current=(_image("base64://%%%", 0),)),
            question="",
            runtime=_runtime(),
            gateway=None,
            source_event_id=None,
            conversation_key="private:1001",
        )

    assert exc_info.value.code == "invalid_base64"
    assert provider.requests == []
    await service.close()


@pytest.mark.asyncio
async def test_close_waits_for_active_analysis_before_closing_clients(
    database: Database,
) -> None:
    provider = BlockingVisionProvider()
    service = _service(database, provider)
    analysis = asyncio.create_task(
        service.analyze(
            _message(current=(_image(_inline_png(), 0),)),
            question="",
            runtime=_runtime(),
            gateway=None,
            source_event_id=None,
            conversation_key="private:1001",
        )
    )
    await provider.started.wait()

    closing = asyncio.create_task(service.close())
    await asyncio.sleep(0)
    assert not closing.done()
    assert not provider.closed

    provider.release.set()
    await analysis
    await closing
    assert provider.closed


@pytest.mark.asyncio
async def test_identical_concurrent_requests_share_one_provider_call(
    database: Database,
) -> None:
    provider = BlockingVisionProvider()
    service = VisionService(
        provider=provider,
        resolver=MediaResolver(),
        preprocessor=ImagePreprocessor(),
        analyses=MediaAnalysisRepository(database),
        rate_limiter=VisionRateLimiter(),
        global_concurrency=2,
    )
    image = _inline_png()

    first = asyncio.create_task(
        service.analyze(
            _message(message_id="shared-1", current=(_image(image, 0),)),
            question="这是什么？",
            runtime=_runtime(),
            gateway=None,
            source_event_id=None,
            conversation_key="private:1001",
        )
    )
    second = asyncio.create_task(
        service.analyze(
            _message(message_id="shared-2", current=(_image(image, 0),)),
            question="这是什么？",
            runtime=_runtime(),
            gateway=None,
            source_event_id=None,
            conversation_key="private:1001",
        )
    )
    await provider.started.wait()
    await asyncio.sleep(0.02)

    assert provider.call_count == 1
    provider.release.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert first_result.overall_description == second_result.overall_description
    assert len(provider.requests) == 1
    await service.close()


@pytest.mark.asyncio
async def test_visual_queue_timeout_and_full_are_distinct(database: Database) -> None:
    provider = BlockingVisionProvider()
    service = VisionService(
        provider=provider,
        resolver=MediaResolver(),
        preprocessor=ImagePreprocessor(),
        analyses=MediaAnalysisRepository(database),
        rate_limiter=VisionRateLimiter(),
        global_concurrency=1,
        queue_max_pending=1,
        queue_timeout_seconds=0.05,
    )
    first = asyncio.create_task(
        service.analyze(
            _message(message_id="queue-running", current=(_image(_inline_png(), 0),)),
            question="",
            runtime=_runtime(),
            gateway=None,
            source_event_id=None,
            conversation_key="private:1001",
        )
    )
    await provider.started.wait()
    queued = asyncio.create_task(
        service.analyze(
            _message(
                message_id="queue-waiting",
                current=(_image(_inline_png((0, 255, 0)), 0),),
            ),
            question="",
            runtime=_runtime(),
            gateway=None,
            source_event_id=None,
            conversation_key="private:1002",
        )
    )
    for _ in range(20):
        if service.queue_depth == 1:
            break
        await asyncio.sleep(0)
    assert service.queue_depth == 1
    assert service.running_count == 1

    with pytest.raises(VisionProcessingError) as full_info:
        await service.analyze(
            _message(
                message_id="queue-full",
                current=(_image(_inline_png((0, 0, 255)), 0),),
            ),
            question="",
            runtime=_runtime(),
            gateway=None,
            source_event_id=None,
            conversation_key="private:1003",
        )
    assert full_info.value.code == "queue_full"
    with pytest.raises(VisionProcessingError) as timeout_info:
        await queued
    assert timeout_info.value.code == "queue_timeout"
    assert service.queue_depth == 0

    provider.release.set()
    await first
    await service.close()
