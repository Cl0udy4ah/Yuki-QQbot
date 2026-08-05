"""Persistent delivery worker for Host-owned plugin notifications."""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import Protocol

from nonebot import get_bots

from qq_ai_bot.automation.gateway import ProactiveGatewayError
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.plugin_host.media_artifacts import PluginMediaArtifactStore
from qq_ai_bot.plugin_host.notification_repository import (
    OutboxRecord,
    PluginNotificationRepository,
)
from yuki_plugin_sdk.errors import PluginPermissionError

logger = logging.getLogger(__name__)


class NotificationTransport(Protocol):
    async def send_text(
        self, *, bot_user_id: str, target_type: str, target_id: str, text: str
    ) -> str: ...

    async def send_media(
        self,
        *,
        bot_user_id: str,
        target_type: str,
        target_id: str,
        local_path: Path,
    ) -> str: ...


class OneBotNotificationTransport:
    async def send_text(
        self, *, bot_user_id: str, target_type: str, target_id: str, text: str
    ) -> str:
        return await self._send(
            bot_user_id=bot_user_id,
            target_type=target_type,
            target_id=target_id,
            message=text,
        )

    async def send_media(
        self,
        *,
        bot_user_id: str,
        target_type: str,
        target_id: str,
        local_path: Path,
    ) -> str:
        content = await asyncio.to_thread(local_path.read_bytes)
        encoded = base64.b64encode(content).decode("ascii")
        del content
        try:
            return await self._send(
                bot_user_id=bot_user_id,
                target_type=target_type,
                target_id=target_id,
                message=[{"type": "image", "data": {"file": f"base64://{encoded}"}}],
            )
        finally:
            del encoded

    async def _send(
        self,
        *,
        bot_user_id: str,
        target_type: str,
        target_id: str,
        message: object,
    ) -> str:
        try:
            bots = get_bots()
        except (RuntimeError, ValueError) as exc:
            raise ProactiveGatewayError("bot_unavailable") from exc
        bot = next(
            (
                candidate
                for candidate in bots.values()
                if str(getattr(candidate, "self_id", "")) == bot_user_id
            ),
            None,
        )
        if bot is None:
            raise ProactiveGatewayError("bot_unavailable")
        action = "send_group_msg" if target_type == "group" else "send_private_msg"
        key = "group_id" if target_type == "group" else "user_id"
        try:
            result = await bot.call_api(action, **{key: target_id, "message": message})
        except Exception as exc:
            raise ProactiveGatewayError("onebot_transport_uncertain", uncertain=True) from exc
        message_id: object | None = None
        if isinstance(result, str | int):
            message_id = result
        elif isinstance(result, dict):
            message_id = result.get("message_id") or result.get("id")
        if message_id is None or not str(message_id).strip():
            raise ProactiveGatewayError("onebot_receipt_missing", uncertain=True)
        return str(message_id)[:128]


class PluginNotificationOutboxWorker:
    def __init__(
        self,
        *,
        repository: PluginNotificationRepository,
        artifacts: PluginMediaArtifactStore,
        ledger: EventLedgerRepository,
        transport: NotificationTransport | None = None,
    ) -> None:
        self._repository = repository
        self._artifacts = artifacts
        self._ledger = ledger
        self._transport = transport or OneBotNotificationTransport()
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="plugin-notification-outbox")

    async def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            await self._task
            self._task = None

    def wake(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        while not self._stop.is_set():
            item = await self._repository.claim_outbox()
            if item is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                except TimeoutError:
                    pass
                continue
            await self._deliver(item)

    async def _deliver(self, item: OutboxRecord) -> None:
        if (
            await self._repository.grant_creator(
                plugin_id=item.plugin_id,
                target_type=item.target_type,
                target_id=item.target_id,
            )
            is None
        ):
            await self._repository.finish_outbox(
                item.id, status="cancelled", error_category="target_grant_revoked"
            )
            return
        try:
            if item.part_type == "media":
                if item.media_handle_id is None:
                    raise PluginPermanentDeliveryError("media_handle_missing")
                artifact = await self._artifacts.resolve(
                    plugin_id=item.plugin_id,
                    handle_id=item.media_handle_id,
                )
                message_id = await self._transport.send_media(
                    bot_user_id=item.bot_user_id,
                    target_type=item.target_type,
                    target_id=item.target_id,
                    local_path=artifact.local_path,
                )
            else:
                message_id = await self._transport.send_text(
                    bot_user_id=item.bot_user_id,
                    target_type=item.target_type,
                    target_id=item.target_id,
                    text=item.text,
                )
        except (PluginPermanentDeliveryError, PluginPermissionError) as exc:
            category = (
                exc.category if isinstance(exc, PluginPermanentDeliveryError) else "media_invalid"
            )
            await self._repository.finish_outbox(item.id, status="failed", error_category=category)
            return
        except ProactiveGatewayError as exc:
            if exc.uncertain:
                await self._repository.finish_outbox(
                    item.id,
                    status="uncertain",
                    error_category=exc.category,
                )
            else:
                await self._repository.retry_outbox(item.id, error_category=exc.category)
            return
        except Exception as exc:
            logger.warning(
                "notification_outbox_delivery_failed plugin_id=%s part_type=%s error_category=%s",
                item.plugin_id,
                item.part_type,
                type(exc).__name__,
            )
            await self._repository.retry_outbox(item.id, error_category=type(exc).__name__)
            return
        try:
            await self._repository.finish_outbox(
                item.id,
                status="sent",
                platform_message_id=message_id,
            )
            await self._record_outbound(item, message_id)
        except Exception as exc:
            logger.exception(
                "notification_post_send_record_failed plugin_id=%s error_category=%s",
                item.plugin_id,
                type(exc).__name__,
            )
            await self._repository.finish_outbox(
                item.id,
                status="uncertain",
                platform_message_id=message_id,
                error_category="post_send_persistence_failed",
            )
            return
        logger.info(
            "notification_outbox_delivery plugin_id=%s part_type=%s status=sent attempts=%d",
            item.plugin_id,
            item.part_type,
            item.attempts,
        )

    async def _record_outbound(self, item: OutboxRecord, message_id: str) -> None:
        scope = ScopeType(item.target_type)
        source = await self._ledger.get_event(item.source_event_id)
        summary = source.content if source is not None else "插件外部事件通知"
        content = item.text if item.part_type != "media" else ""
        segments: tuple[dict[str, object], ...]
        if item.part_type == "media":
            segments = (
                {
                    "type": "image",
                    "data": {
                        "media_handle_id": item.media_handle_id,
                        "summary": summary[:500],
                    },
                },
            )
        else:
            segments = ({"type": "text", "data": {"text": content}},)
        await self._ledger.append(
            bot_user_id=item.bot_user_id,
            platform_message_id=message_id,
            scope_type=scope,
            sender_user_id=item.bot_user_id,
            direction="outbound",
            content=content,
            segments=segments,
            group_id=item.target_id if scope is ScopeType.GROUP else None,
            private_peer_user_id=item.target_id if scope is ScopeType.PRIVATE else None,
            sender_is_bot=True,
            origin="plugin_background",
        )


class PluginPermanentDeliveryError(RuntimeError):
    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category
