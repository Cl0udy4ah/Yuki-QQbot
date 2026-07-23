"""Plain OneBot message sender."""

from __future__ import annotations

import logging

from nonebot.adapters.onebot.v11 import Bot, MessageEvent, MessageSegment

from qq_ai_bot.domain.messages import OutboundMessage

logger = logging.getLogger(__name__)


class OneBotSendError(RuntimeError):
    """Sanitized outbound transport failure."""


class OneBotSender:
    """Send plain text to the event source without quoting another message."""

    def __init__(self, bot: Bot, event: MessageEvent) -> None:
        self._bot = bot
        self._event = event

    async def send(self, message: OutboundMessage) -> None:
        """Send one plain text message."""

        try:
            await self._bot.send(event=self._event, message=MessageSegment.text(message.text))
        except Exception as exc:
            logger.error("onebot_send_failed exception_category=%s", type(exc).__name__)
            raise OneBotSendError("OneBot send failed") from exc
