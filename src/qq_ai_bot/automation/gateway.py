"""Proactive OneBot gateway bound to one exact connected bot account."""

from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Protocol

from nonebot import get_bots

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.persistence.repositories import AgentActionRepository, EventLedgerRepository

logger = logging.getLogger(__name__)


class ProactiveGatewayError(RuntimeError):
    """Sanitized proactive transport failure with stable category."""

    def __init__(self, category: str, *, uncertain: bool = False) -> None:
        super().__init__(category)
        self.category = category
        self.uncertain = uncertain


class ProactiveGateway(Protocol):
    @property
    def connected(self) -> bool: ...

    async def send_private(self, user_id: str, text: str) -> object: ...

    async def send_group(self, group_id: str, text: str) -> object: ...

    async def send_emoji(
        self,
        *,
        user_id: str | None,
        group_id: str | None,
        content: bytes,
        mime_type: str,
        emoji_id: str,
        summary: str,
    ) -> object: ...

    async def send_voice(
        self,
        *,
        user_id: str | None,
        group_id: str | None,
        local_path: str,
        spoken_text: str,
        generation_id: int,
        profile_id: str,
        reference_key: str,
        duration_milliseconds: int,
    ) -> object: ...

    async def call_api(self, action: str, params: dict[str, object]) -> object: ...


class OneBotProactiveGateway:
    """Send without a MessageEvent and persist successful outgoing QQ messages."""

    def __init__(
        self,
        *,
        bot_user_id: str,
        creator_user_id: str,
        automation_id: int,
        automation_run_id: int,
        ledger: EventLedgerRepository,
        actions: AgentActionRepository,
    ) -> None:
        self._bot_user_id = bot_user_id
        self._creator_user_id = creator_user_id
        self._automation_id = automation_id
        self._automation_run_id = automation_run_id
        self._ledger = ledger
        self._actions = actions

    @property
    def connected(self) -> bool:
        return self._find_bot() is not None

    async def send_private(self, user_id: str, text: str) -> object:
        result = await self._invoke("send_private_msg", {"user_id": user_id, "message": text})
        await self._record_message(
            result,
            scope_type=ScopeType.PRIVATE,
            private_peer_user_id=user_id,
            group_id=None,
            text=text,
        )
        return result

    async def send_group(self, group_id: str, text: str) -> object:
        result = await self._invoke("send_group_msg", {"group_id": group_id, "message": text})
        await self._record_message(
            result,
            scope_type=ScopeType.GROUP,
            private_peer_user_id=None,
            group_id=group_id,
            text=text,
        )
        return result

    async def call_api(self, action: str, params: dict[str, object]) -> object:
        started = time.perf_counter()
        try:
            result = await self._invoke(action, params)
        except ProactiveGatewayError as exc:
            await self._actions.record(
                actor_user_id=self._creator_user_id,
                action=action,
                success=False,
                duration_seconds=time.perf_counter() - started,
                error_category=exc.category,
            )
            raise
        await self._actions.record(
            actor_user_id=self._creator_user_id,
            action=action,
            success=True,
            duration_seconds=time.perf_counter() - started,
        )
        return result

    async def send_emoji(
        self,
        *,
        user_id: str | None,
        group_id: str | None,
        content: bytes,
        mime_type: str,
        emoji_id: str,
        summary: str,
    ) -> object:
        if (user_id is None) == (group_id is None):
            raise ProactiveGatewayError("invalid_emoji_target")
        encoded = base64.b64encode(content).decode("ascii")
        message = [{"type": "image", "data": {"file": f"base64://{encoded}"}}]
        action = "send_group_msg" if group_id is not None else "send_private_msg"
        target_key = "group_id" if group_id is not None else "user_id"
        target_value = group_id if group_id is not None else user_id
        result = await self._invoke(action, {target_key: str(target_value), "message": message})
        await self._record_media_message(
            result,
            user_id=user_id,
            group_id=group_id,
            emoji_id=emoji_id,
            mime_type=mime_type,
            summary=summary,
        )
        return result

    async def send_voice(
        self,
        *,
        user_id: str | None,
        group_id: str | None,
        local_path: str,
        spoken_text: str,
        generation_id: int,
        profile_id: str,
        reference_key: str,
        duration_milliseconds: int,
    ) -> object:
        if (user_id is None) == (group_id is None):
            raise ProactiveGatewayError("invalid_speech_target")
        content = await asyncio.to_thread(Path(local_path).read_bytes)
        encoded = base64.b64encode(content).decode("ascii")
        message = [{"type": "record", "data": {"file": f"base64://{encoded}"}}]
        del content, encoded
        action = "send_group_msg" if group_id is not None else "send_private_msg"
        target_key = "group_id" if group_id is not None else "user_id"
        target_value = group_id if group_id is not None else user_id
        result = await self._invoke(action, {target_key: str(target_value), "message": message})
        await self._record_voice_message(
            result,
            user_id=user_id,
            group_id=group_id,
            spoken_text=spoken_text,
            generation_id=generation_id,
            profile_id=profile_id,
            reference_key=reference_key,
            duration_milliseconds=duration_milliseconds,
        )
        return result

    async def _invoke(self, action: str, params: dict[str, object]) -> object:
        bot = self._find_bot()
        if bot is None:
            raise ProactiveGatewayError("bot_unavailable")
        try:
            return await bot.call_api(action, **params)
        except Exception as exc:
            # Once the API invocation started, a transport break cannot prove whether
            # QQ accepted a send. The executor therefore never retries send actions.
            logger.error(
                "automation_onebot_failed automation_id=%d run_id=%d action=%s category=%s",
                self._automation_id,
                self._automation_run_id,
                action,
                type(exc).__name__,
            )
            raise ProactiveGatewayError(
                "onebot_transport_uncertain",
                uncertain=action in {"send_private_msg", "send_group_msg"},
            ) from exc

    def _find_bot(self) -> Any | None:
        try:
            bots = get_bots()
        except (RuntimeError, ValueError):
            return None
        for bot in bots.values():
            if str(getattr(bot, "self_id", "")) == self._bot_user_id:
                return bot
        return None

    async def _record_message(
        self,
        result: object,
        *,
        scope_type: ScopeType,
        private_peer_user_id: str | None,
        group_id: str | None,
        text: str,
    ) -> None:
        message_id: object | None = None
        if isinstance(result, str | int):
            message_id = result
        elif isinstance(result, dict):
            message_id = result.get("message_id") or result.get("id")
        await self._ledger.append(
            bot_user_id=self._bot_user_id,
            platform_message_id=str(message_id or f"automation-{uuid.uuid4()}")[:128],
            scope_type=scope_type,
            sender_user_id=self._bot_user_id,
            direction="outbound",
            content=text,
            segments=({"type": "text", "data": {"text": text}},),
            group_id=group_id,
            private_peer_user_id=private_peer_user_id,
            sender_is_bot=True,
            origin="scheduled_automation",
            automation_id=self._automation_id,
            automation_run_id=self._automation_run_id,
        )

    async def _record_media_message(
        self,
        result: object,
        *,
        user_id: str | None,
        group_id: str | None,
        emoji_id: str,
        mime_type: str,
        summary: str,
    ) -> None:
        message_id: object | None = None
        if isinstance(result, str | int):
            message_id = result
        elif isinstance(result, dict):
            message_id = result.get("message_id") or result.get("id")
        content = f"[表情：{summary}]"
        await self._ledger.append(
            bot_user_id=self._bot_user_id,
            platform_message_id=str(message_id or f"automation-{uuid.uuid4()}")[:128],
            scope_type=ScopeType.GROUP if group_id is not None else ScopeType.PRIVATE,
            sender_user_id=self._bot_user_id,
            direction="outbound",
            content=content,
            segments=(
                {
                    "type": "image",
                    "data": {
                        "emoji_id": emoji_id,
                        "mime_type": mime_type,
                        "summary": summary,
                    },
                },
            ),
            group_id=group_id,
            private_peer_user_id=user_id,
            sender_is_bot=True,
            origin="scheduled_automation",
            automation_id=self._automation_id,
            automation_run_id=self._automation_run_id,
        )

    async def _record_voice_message(
        self,
        result: object,
        *,
        user_id: str | None,
        group_id: str | None,
        spoken_text: str,
        generation_id: int,
        profile_id: str,
        reference_key: str,
        duration_milliseconds: int,
    ) -> None:
        message_id: object | None = None
        if isinstance(result, str | int):
            message_id = result
        elif isinstance(result, dict):
            message_id = result.get("message_id") or result.get("id")
        await self._ledger.append(
            bot_user_id=self._bot_user_id,
            platform_message_id=str(message_id or f"automation-{uuid.uuid4()}")[:128],
            scope_type=ScopeType.GROUP if group_id is not None else ScopeType.PRIVATE,
            sender_user_id=self._bot_user_id,
            direction="outbound",
            content=spoken_text,
            segments=(
                {
                    "type": "record",
                    "data": {
                        "generation_id": generation_id,
                        "profile_id": profile_id,
                        "reference_key": reference_key,
                        "duration_milliseconds": duration_milliseconds,
                    },
                },
            ),
            group_id=group_id,
            private_peer_user_id=user_id,
            sender_is_bot=True,
            origin="scheduled_automation",
            automation_id=self._automation_id,
            automation_run_id=self._automation_run_id,
        )


class FakeOneBotProactiveGateway:
    """Network-free test gateway with deterministic sent-message capture."""

    def __init__(self, *, connected: bool = True) -> None:
        self._connected = connected
        self.private_messages: list[tuple[str, str]] = []
        self.group_messages: list[tuple[str, str]] = []
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.emojis: list[tuple[str, str, str]] = []
        self.voices: list[tuple[str, str, str]] = []

    @property
    def connected(self) -> bool:
        return self._connected

    async def send_private(self, user_id: str, text: str) -> object:
        if not self._connected:
            raise ProactiveGatewayError("bot_unavailable")
        self.private_messages.append((user_id, text))
        return {"message_id": len(self.private_messages)}

    async def send_group(self, group_id: str, text: str) -> object:
        if not self._connected:
            raise ProactiveGatewayError("bot_unavailable")
        self.group_messages.append((group_id, text))
        return {"message_id": len(self.group_messages)}

    async def call_api(self, action: str, params: dict[str, object]) -> object:
        if not self._connected:
            raise ProactiveGatewayError("bot_unavailable")
        self.calls.append((action, params))
        return {"ok": True}

    async def send_emoji(
        self,
        *,
        user_id: str | None,
        group_id: str | None,
        content: bytes,
        mime_type: str,
        emoji_id: str,
        summary: str,
    ) -> object:
        if not self._connected:
            raise ProactiveGatewayError("bot_unavailable")
        scope = "group" if group_id is not None else "private"
        target = group_id if group_id is not None else user_id
        self.emojis.append((scope, str(target), emoji_id))
        return {"message_id": len(self.emojis)}

    async def send_voice(
        self,
        *,
        user_id: str | None,
        group_id: str | None,
        local_path: str,
        spoken_text: str,
        generation_id: int,
        profile_id: str,
        reference_key: str,
        duration_milliseconds: int,
    ) -> object:
        if not self._connected:
            raise ProactiveGatewayError("bot_unavailable")
        scope = "group" if group_id is not None else "private"
        target = group_id if group_id is not None else user_id
        self.voices.append((scope, str(target), profile_id))
        return {"message_id": len(self.voices)}
