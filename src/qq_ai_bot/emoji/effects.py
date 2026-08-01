"""Resolve queued emoji intents into transport-neutral outbound media."""

from __future__ import annotations

import logging

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.domain.messages import AttachmentKind, InboundMessage, OutboundMedia, OutboundMessage
from qq_ai_bot.emoji.models import (
    EmojiLifecycleStatus,
    EmojiReplyMode,
    EmojiSelectionRequest,
    PendingReplyEffect,
)
from qq_ai_bot.emoji.repository import EmojiRepository
from qq_ai_bot.emoji.selector import EmojiSelector
from qq_ai_bot.emoji.storage import EmojiStorage
from qq_ai_bot.services.plugin_events import LifecycleEventPublisher, publish_notification
from yuki_plugin_sdk.events import EventName

logger = logging.getLogger(__name__)


class EmojiReplyEffectService:
    def __init__(
        self,
        *,
        selector: EmojiSelector,
        repository: EmojiRepository,
        storage: EmojiStorage,
        event_publisher: LifecycleEventPublisher | None = None,
    ) -> None:
        self._selector = selector
        self._repository = repository
        self._storage = storage
        self._event_publisher = event_publisher

    def set_event_publisher(self, publisher: LifecycleEventPublisher) -> None:
        self._event_publisher = publisher

    async def prepare(
        self,
        effect: PendingReplyEffect,
        *,
        inbound: InboundMessage,
        response_text: str,
        runtime: RuntimeConfigSnapshot,
    ) -> OutboundMessage | None:
        if effect.mode is EmojiReplyMode.NONE:
            return None
        await publish_notification(
            self._event_publisher,
            EventName.EMOJI_QUEUED,
            {"source": effect.source, "mode": effect.mode.value},
        )
        selection = await self._selector.select(
            EmojiSelectionRequest(
                actor_user_id=inbound.sender.user_id,
                group_id=inbound.group_id,
                reply_text=response_text[:4000],
                goal=effect.goal,
                emotion=effect.emotion,
                explicit_request=effect.explicit_request,
                mode=effect.mode,
                placement=effect.placement,
            ),
            runtime=runtime.emoji,
            vision_runtime=runtime.vision,
        )
        if selection.emoji_id is None:
            logger.warning(
                "emoji_reply_prepare_declined reason=%s source=%s mode=%s",
                selection.reason,
                effect.source,
                effect.mode.value,
            )
            return None
        asset = await self._repository.get(selection.emoji_id)
        if asset is None:
            logger.warning("emoji_reply_asset_missing_from_repository")
            return None
        try:
            content = self._storage.read(asset.relative_path)
        except RuntimeError:
            await self._repository.set_status(asset.id, EmojiLifecycleStatus.MISSING)
            await publish_notification(
                self._event_publisher,
                EventName.EMOJI_MISSING,
                {"emoji_id": asset.id},
            )
            logger.warning("emoji_reply_asset_missing_from_storage emoji_id=%s", asset.id)
            return None
        summary = asset.description or "Yuki 发送了一张表情图片"
        return OutboundMessage(
            media=(
                OutboundMedia(
                    kind=AttachmentKind.IMAGE,
                    content=content,
                    mime_type=asset.mime_type,
                    summary=summary,
                    emoji_id=asset.id,
                    animated=asset.animated,
                ),
            )
        )

    async def record_success(
        self,
        message: OutboundMessage,
        *,
        inbound: InboundMessage,
        source: str,
    ) -> None:
        for media in message.media:
            if media.emoji_id:
                await self._repository.mark_used(
                    media.emoji_id,
                    actor_user_id=inbound.sender.user_id,
                    group_id=inbound.group_id,
                    trigger_message_id=inbound.message_id,
                    source=source,
                )
                await publish_notification(
                    self._event_publisher,
                    EventName.EMOJI_SENT,
                    {
                        "emoji_id": media.emoji_id,
                        "scope_type": inbound.scope_type.value,
                        "source": source,
                    },
                )

    async def record_failure(self, message: OutboundMessage, *, source: str) -> None:
        for media in message.media:
            if media.emoji_id:
                await publish_notification(
                    self._event_publisher,
                    EventName.EMOJI_SEND_FAILED,
                    {"emoji_id": media.emoji_id, "source": source},
                )
