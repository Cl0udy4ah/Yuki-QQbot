"""Bounded OneBot reply sender with quote fallback."""

from __future__ import annotations

import logging

from nonebot.adapters.onebot.v11 import ActionFailed, Bot, Message, MessageEvent, MessageSegment

from qq_ai_bot.domain.messages import OutboundMessage

logger = logging.getLogger(__name__)


class OneBotSendError(RuntimeError):
    """Sanitized outbound transport failure."""


class OneBotSender:
    """Send to the event source, trying a quote exactly once when requested."""

    def __init__(self, bot: Bot, event: MessageEvent) -> None:
        self._bot = bot
        self._event = event

    async def send(self, message: OutboundMessage) -> None:
        """Send one message and downgrade quote failures to a plain reply."""

        if message.reply_to_message_id is not None:
            try:
                reply_id = int(message.reply_to_message_id)
                payload = Message(MessageSegment.reply(reply_id)) + MessageSegment.text(
                    message.text
                )
                await self._bot.send(event=self._event, message=payload)
                return
            except (ActionFailed, ValueError) as exc:
                logger.warning(
                    "quoted_reply_failed fallback=plain exception_category=%s", type(exc).__name__
                )
        try:
            await self._bot.send(event=self._event, message=MessageSegment.text(message.text))
        except Exception as exc:
            logger.error("onebot_send_failed exception_category=%s", type(exc).__name__)
            raise OneBotSendError("OneBot send failed") from exc
