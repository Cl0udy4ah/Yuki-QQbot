"""Conservative detection of standalone requests to send one chat emoji."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field


class EmojiRequestHint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    explicit_request: bool = False
    standalone_request: bool = False
    goal: str = Field(default="", max_length=300)


_NEGATED = re.compile(r"(?:不要|别|不用|无需|不想|禁止|停止).{0,8}(?:表情包?|梗图|动图)")
_ABOUT_EMOJI = re.compile(r"(?:什么意思|是什么意思|解释|分析|识别|怎么看|什么含义)")
_COMPOUND_TASK = re.compile(
    r"(?:回答|解释|查询|搜索|告诉我|并且|同时|然后|顺便|以及).*(?:表情包?|梗图|动图)"
)
_STANDALONE = re.compile(
    r"^(?:请|麻烦)?(?:随便)?(?:给我)?"
    r"(?:发|来|整|甩|丢|送)(?:一|1)?(?:个|张|只|条)?"
    r"(?P<goal>.{0,40}?)(?:表情包?|梗图|动图)"
    r"(?:给我|看看|试试|吧|呀|啊|哦|呗|嘛|啦|一下)?[!！。.]?$"
)


class EmojiRequestDetector:
    """Recognize only self-contained send requests from the current message."""

    def __init__(self, bot_aliases: tuple[str, ...] = ("Yuki", "yuki", "由纪")) -> None:
        address_targets = [r"@\S+"]
        address_targets.extend(re.escape(alias) for alias in bot_aliases if alias.strip())
        self._leading_address = re.compile(
            rf"^\s*(?:{'|'.join(address_targets)})\s*[,，:：]?\s*",
            flags=re.IGNORECASE,
        )

    def detect(self, content: str) -> EmojiRequestHint:
        without_address = self._leading_address.sub("", content, count=1)
        normalized = re.sub(r"\s+", "", without_address).strip("，,：:")
        if not normalized or _NEGATED.search(normalized):
            return EmojiRequestHint()
        if _ABOUT_EMOJI.search(normalized) or "给图片加表情" in normalized:
            return EmojiRequestHint()
        match = _STANDALONE.fullmatch(normalized)
        if match is None:
            return EmojiRequestHint(
                explicit_request=bool(
                    re.search(r"(?:发|来|整|甩|丢|送).*(?:表情包?|梗图|动图)", normalized)
                    and not _COMPOUND_TASK.search(normalized)
                ),
                standalone_request=False,
            )
        goal = match.group("goal").strip("的，, ")
        return EmojiRequestHint(
            explicit_request=True,
            standalone_request=True,
            goal=(goal or "自然回应当前用户")[:300],
        )


__all__ = ["EmojiRequestDetector", "EmojiRequestHint"]
