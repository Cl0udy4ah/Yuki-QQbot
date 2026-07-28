"""Plan-aware QQ message splitting, pacing, and cooperative supersession."""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.domain.messages import OutboundMessage
from qq_ai_bot.planner.models import DeliveryMode, TurnPlan
from qq_ai_bot.services.renderer import split_daily_chat_sentences, split_qq_message
from qq_ai_bot.services.turn_coordinator import (
    ConversationTurnCoordinator,
    ReplySequenceCancelled,
    TurnToken,
)

RecordOutbound = Callable[[OutboundMessage, Any], Awaitable[None]]
RecordFailure = Callable[[OutboundMessage, Exception], Awaitable[None]]
_FENCED_BLOCK = re.compile(r"(```[^\n]*\n.*?\n```|~~~[^\n]*\n.*?\n~~~)", re.DOTALL)
_BLANK_LINE = re.compile(r"\n[ \t]*\n+")


class OutboundSender(Protocol):
    async def send(self, message: OutboundMessage) -> Any: ...


@dataclass(frozen=True, slots=True)
class ReplySequenceResult:
    planned_messages: int
    sent_messages: int
    cancelled: bool


class ReplySequenceManager:
    """Own the full post-generation delivery sequence outside ChatService."""

    def __init__(
        self,
        coordinator: ConversationTurnCoordinator,
        *,
        random_uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._coordinator = coordinator
        self._random_uniform = random_uniform

    def render(
        self,
        text: str,
        *,
        plan: TurnPlan,
        runtime: RuntimeConfigSnapshot,
    ) -> tuple[str, ...]:
        hard_max = runtime.reply.plan_hard_max_messages
        messages = (
            self._split_blank_line_sections(text, max_messages=hard_max)
            if plan.delivery_mode is not DeliveryMode.STRUCTURED
            else (text,)
        )
        if len(messages) == 1:
            if plan.delivery_mode is DeliveryMode.NATURAL_MULTI:
                target = max(1, min(plan.desired_messages, hard_max))
                messages = split_daily_chat_sentences(
                    text,
                    max_characters=runtime.reply.daily_split_max_characters,
                    max_messages=target,
                )
            elif (
                plan.delivery_mode not in {DeliveryMode.SINGLE, DeliveryMode.CONCISE}
                and runtime.reply.daily_split_enabled
                and plan.delivery_mode is not DeliveryMode.STRUCTURED
            ):
                messages = split_daily_chat_sentences(
                    text,
                    max_characters=runtime.reply.daily_split_max_characters,
                    max_messages=min(plan.desired_messages, hard_max),
                )
        chunks = tuple(
            chunk
            for message in messages
            for chunk in self._split_preserving_structure(
                message,
                limit=runtime.reply.max_qq_message_chars,
            )
        )
        if len(chunks) <= hard_max:
            return chunks
        # MAX_OUTPUT_CHARACTERS should normally keep this unreachable.  Preserve
        # earlier complete chunks and combine the tail rather than dropping text.
        return (*chunks[: hard_max - 1], "\n".join(chunks[hard_max - 1 :]))

    @staticmethod
    def _split_blank_line_sections(text: str, *, max_messages: int) -> tuple[str, ...]:
        """Treat visible empty lines as strong chat boundaries without losing text."""

        sections = tuple(section.strip() for section in _BLANK_LINE.split(text) if section.strip())
        if len(sections) < 2:
            return (text,) if text else ()
        if len(sections) <= max_messages:
            return sections
        groups: list[list[str]] = [[] for _ in range(max_messages)]
        for index, section in enumerate(sections):
            group_index = min(index * max_messages // len(sections), max_messages - 1)
            groups[group_index].append(section)
        # A single newline keeps merged overflow readable without recreating the
        # blank line that should have become a send boundary.
        return tuple("\n".join(group) for group in groups if group)

    async def send(
        self,
        *,
        text: str,
        plan: TurnPlan,
        runtime: RuntimeConfigSnapshot,
        token: TurnToken,
        sender: OutboundSender,
        record_outbound: RecordOutbound,
        record_failure: RecordFailure | None = None,
        before_messages: tuple[OutboundMessage, ...] = (),
        after_messages: tuple[OutboundMessage, ...] = (),
        suppress_text: bool = False,
    ) -> ReplySequenceResult:
        chunks = () if suppress_text else self.render(text, plan=plan, runtime=runtime)
        outbound_messages = [*before_messages]
        outbound_messages.extend(
            OutboundMessage(
                text=chunk,
                reply_to_message_id=(plan.reply_to_message_id if index == 0 else None),
            )
            for index, chunk in enumerate(chunks)
        )
        outbound_messages.extend(after_messages)
        sent = 0
        try:
            async with self._coordinator.track(token, "reply"):
                for index, outbound in enumerate(outbound_messages):
                    if runtime.reply.cancel_on_new_message and not self._coordinator.is_current(
                        token
                    ):
                        raise ReplySequenceCancelled("newer message superseded reply sequence")
                    if index > 0:
                        delay = self._random_uniform(
                            runtime.reply.delay_min_seconds,
                            runtime.reply.delay_max_seconds,
                        )
                        if delay > 0:
                            await asyncio.sleep(delay)
                    try:
                        result = await sender.send(outbound)
                    except Exception as exc:
                        if record_failure is not None:
                            await record_failure(outbound, exc)
                        raise
                    await record_outbound(outbound, result)
                    sent += 1
        except ReplySequenceCancelled:
            return ReplySequenceResult(len(outbound_messages), sent, True)
        return ReplySequenceResult(len(outbound_messages), sent, False)

    @staticmethod
    def _split_preserving_structure(text: str, *, limit: int) -> tuple[str, ...]:
        if len(text) <= limit:
            return (text,) if text else ()
        parts = _FENCED_BLOCK.split(text)
        chunks: list[str] = []
        for part in parts:
            if not part:
                continue
            if part.startswith(("```", "~~~")):
                chunks.extend(ReplySequenceManager._split_fenced_block(part, limit=limit))
            else:
                chunks.extend(split_qq_message(part, limit=limit))
        return tuple(chunk for chunk in chunks if chunk.strip())

    @staticmethod
    def _split_fenced_block(block: str, *, limit: int) -> tuple[str, ...]:
        lines = block.splitlines()
        if len(block) <= limit or len(lines) < 3:
            return (block,)
        opening = lines[0]
        closing = lines[-1]
        body = lines[1:-1]
        available = max(1, limit - len(opening) - len(closing) - 2)
        chunks: list[str] = []
        current: list[str] = []
        current_length = 0
        for line in body:
            extra = len(line) + int(bool(current))
            if current and current_length + extra > available:
                chunks.append(f"{opening}\n{'\n'.join(current)}\n{closing}")
                current = []
                current_length = 0
            if len(line) > available:
                if current:
                    chunks.append(f"{opening}\n{'\n'.join(current)}\n{closing}")
                    current = []
                    current_length = 0
                for index in range(0, len(line), available):
                    chunks.append(f"{opening}\n{line[index : index + available]}\n{closing}")
                continue
            current.append(line)
            current_length += extra
        if current:
            chunks.append(f"{opening}\n{'\n'.join(current)}\n{closing}")
        return tuple(chunks)
