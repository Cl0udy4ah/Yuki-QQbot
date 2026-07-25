"""Cautious, debounced autonomous participation in enabled QQ groups."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.conversations import ConversationIdentity, ConversationMode
from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, InboundMessage
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.llm.base import LLMError, LLMProvider
from qq_ai_bot.persistence.repositories import MemoryRepository
from qq_ai_bot.services.chat import ChatService, OutboundSender
from qq_ai_bot.services.concurrency import ConcurrencyManager

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _GroupState:
    messages: deque[InboundMessage] = field(default_factory=lambda: deque(maxlen=20))
    profiles: deque[UserProfileSnapshot] = field(default_factory=lambda: deque(maxlen=20))
    senders: deque[OutboundSender] = field(default_factory=lambda: deque(maxlen=20))
    human_version: int = 0
    last_response_human_version: int = -1
    last_response_at: float = 0.0
    hourly_responses: deque[float] = field(default_factory=deque)
    task: asyncio.Task[None] | None = None


class AutonomousGroupService:
    """Debounce group traffic, gate candidates, ask the model, then chat normally."""

    def __init__(
        self,
        *,
        settings: Settings,
        provider: LLMProvider,
        concurrency: ConcurrencyManager,
        memories: MemoryRepository,
        chat: ChatService,
        runtime_config: RuntimeConfigService | None = None,
    ) -> None:
        self._settings = settings
        self._provider = provider
        self._concurrency = concurrency
        self._memories = memories
        self._chat = chat
        self._runtime_config = runtime_config or RuntimeConfigService(
            settings=settings,
            database=memories._database,
        )
        self._states: dict[str, _GroupState] = {}

    def observe(
        self,
        message: InboundMessage,
        profile: UserProfileSnapshot,
        sender: OutboundSender,
    ) -> None:
        if message.group_id is None:
            return
        state = self._states.setdefault(message.group_id, _GroupState())
        state.messages.append(message)
        state.profiles.append(profile)
        state.senders.append(sender)
        state.human_version += 1
        if state.task is not None and not state.task.done():
            state.task.cancel()
        state.task = asyncio.create_task(
            self._after_silence(message.group_id),
            name=f"autonomous-group-{message.group_id}",
        )

    async def _after_silence(self, group_id: str) -> None:
        try:
            runtime = await self._runtime_config.snapshot(group_id=group_id)
            if not runtime.autonomous.enabled:
                return
            await asyncio.sleep(runtime.autonomous.silence_seconds)
            runtime = await self._runtime_config.snapshot(group_id=group_id)
            if not runtime.autonomous.enabled:
                return
            state = self._states.get(group_id)
            if state is None or not state.messages:
                return
            now = time.monotonic()
            while state.hourly_responses and now - state.hourly_responses[0] >= 3600:
                state.hourly_responses.popleft()
            if state.human_version <= state.last_response_human_version:
                return
            if now - state.last_response_at < runtime.autonomous.cooldown_seconds:
                return
            if len(state.hourly_responses) >= runtime.autonomous.max_per_hour:
                return
            last = state.messages[-1]
            if not await self._is_candidate(last):
                return
            confidence = await self._judge(tuple(state.messages), runtime)
            if confidence < runtime.autonomous.confidence_threshold:
                return
            profile = state.profiles[-1]
            sender = state.senders[-1]
            batch = "\n".join(f"[QQ {item.sender.user_id}] {item.text}" for item in state.messages)
            identity = ConversationIdentity.group(
                group_id,
                last.sender.user_id,
                ConversationMode.SHARED,
            )
            sent = await self._chat.respond(
                last,
                identity,
                profile,
                (),
                f"以下是群聊刚刚的消息，请像普通群友一样谨慎参与：\n{batch}",
                sender,
                autonomous=True,
                runtime_snapshot=await self._runtime_config.snapshot(
                    user_id=last.sender.user_id,
                    group_id=group_id,
                ),
            )
            if sent:
                finished = time.monotonic()
                state.last_response_at = finished
                state.hourly_responses.append(finished)
                state.last_response_human_version = state.human_version
        except asyncio.CancelledError:
            raise
        except (LLMError, OSError, RuntimeError, ValueError, TypeError) as exc:
            logger.warning("autonomous_group_failed exception_category=%s", type(exc).__name__)

    async def _is_candidate(self, message: InboundMessage) -> bool:
        text = message.text.strip()
        if message.reply_sender_user_id and message.reply_sender_user_id == message.bot_user_id:
            return True
        if any(token in text.casefold() for token in ("机器人", "bot", "yuki")):
            return True
        if text.endswith(("?", "？")) or any(
            token in text for token in ("谁", "什么", "怎么", "为何", "为什么", "吗")
        ):
            return True
        if message.group_id:
            memories = await self._memories.list_group(message.group_id, limit=30)
            for memory in memories:
                fragments = [
                    memory.content[index : index + 2]
                    for index in range(max(0, len(memory.content) - 1))
                ]
                if any(fragment.strip() and fragment in text for fragment in fragments):
                    return True
        return False

    async def _judge(
        self,
        messages: tuple[InboundMessage, ...],
        runtime: RuntimeConfigSnapshot,
    ) -> float:
        transcript = [
            {"user_id": item.sender.user_id, "content": item.text} for item in messages[-20:]
        ]
        request = ChatRequest(
            model=runtime.llm.model or "fake",
            temperature=0,
            max_output_tokens=128,
            thinking_enabled=False,
            messages=(
                ChatMessage(
                    role="system",
                    content=(
                        "判断一个像真实群友的机器人此时是否应主动插话。"
                        "只有能自然帮助对话且不会打扰时才参与。"
                        '只输出 JSON：{"confidence":0到1,"reason":"短原因"}。'
                    ),
                ),
                ChatMessage(
                    role="user",
                    content=json.dumps(transcript, ensure_ascii=False),
                ),
            ),
        )
        response = await self._concurrency.run_llm(
            f"autonomous-decision:{messages[-1].group_id}",
            lambda: self._provider.complete(request),
        )
        raw = response.content.strip().strip("`")
        if raw.startswith("json"):
            raw = raw[4:].lstrip()
        payload: Any = json.loads(raw)
        if not isinstance(payload, dict):
            return 0.0
        value = payload.get("confidence")
        if isinstance(value, int | float) and not isinstance(value, bool):
            return max(0.0, min(1.0, float(value)))
        return 0.0

    async def close(self) -> None:
        tasks = [
            state.task
            for state in self._states.values()
            if state.task is not None and not state.task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
