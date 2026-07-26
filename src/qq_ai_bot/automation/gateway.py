"""Proactive OneBot gateway bound to one exact connected bot account."""

from __future__ import annotations

import logging
import time
import uuid
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


class FakeOneBotProactiveGateway:
    """Network-free test gateway with deterministic sent-message capture."""

    def __init__(self, *, connected: bool = True) -> None:
        self._connected = connected
        self.private_messages: list[tuple[str, str]] = []
        self.group_messages: list[tuple[str, str]] = []
        self.calls: list[tuple[str, dict[str, object]]] = []

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
