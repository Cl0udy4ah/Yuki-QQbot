"""Deduplication, rate limiting, rendering, and persistence tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from qq_ai_bot.domain.conversations import ConversationIdentity
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import ConversationRepository, ProcessedEventRepository
from qq_ai_bot.services.deduplication import DeduplicationService
from qq_ai_bot.services.rate_limit import SlidingWindowRateLimiter
from qq_ai_bot.services.renderer import (
    clean_model_output,
    sanitize_input,
    split_daily_chat_sentences,
    split_qq_message,
)


def test_application_cli_import_has_no_service_cycle() -> None:
    """The installed CLI must import the complete application graph."""

    from qq_ai_bot.main import run

    assert callable(run)


@pytest.mark.asyncio
async def test_duplicate_event_is_claimed_once(database: Database) -> None:
    service = DeduplicationService(ProcessedEventRepository(database), ttl_seconds=60)
    assert await service.claim("same-event")
    assert not await service.claim("same-event")


@pytest.mark.asyncio
async def test_expired_events_can_be_cleaned(database: Database) -> None:
    repository = ProcessedEventRepository(database)
    await repository.claim("old", expires_at=datetime.now(UTC) - timedelta(seconds=1))
    assert await repository.cleanup_expired() == 1


@pytest.mark.asyncio
async def test_user_and_group_rate_limits_have_separate_scopes() -> None:
    user_limiter = SlidingWindowRateLimiter(per_user=1, per_group=10)
    assert (await user_limiter.check(user_id="1", group_id="9", category="chat")).allowed
    denied_user = await user_limiter.check(user_id="1", group_id="10", category="chat")
    assert not denied_user.allowed and denied_user.scope == "user"

    group_limiter = SlidingWindowRateLimiter(per_user=10, per_group=1)
    assert (await group_limiter.check(user_id="1", group_id="9", category="chat")).allowed
    denied_group = await group_limiter.check(user_id="2", group_id="9", category="chat")
    assert not denied_group.allowed and denied_group.scope == "group"
    assert (await group_limiter.check(user_id="2", group_id="9", category="command")).allowed


def test_long_reply_splits_by_paragraph_sentence_and_character() -> None:
    text = "第一段。第二句。\n\n" + "😀" * 25
    chunks = split_qq_message(text, limit=10)
    assert chunks
    assert all(len(chunk) <= 10 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_short_plain_chat_splits_into_one_message_per_sentence() -> None:
    chunks = split_daily_chat_sentences(
        "你好！她说“今天也要加油。”明天见？",
        max_characters=240,
        max_messages=4,
    )
    assert chunks == ("你好！", "她说“今天也要加油。”", "明天见？")


@pytest.mark.parametrize(
    "text",
    [
        "- 第一步\n- 第二步",
        "```python\nprint('hello')\n```",
        "| 名称 | 值 |\n|---|---|\n| A | B |",
    ],
)
def test_structured_output_is_not_split_as_daily_chat(text: str) -> None:
    assert split_daily_chat_sentences(
        text,
        max_characters=240,
        max_messages=4,
    ) == (text,)


def test_excess_sentences_are_grouped_at_semantic_boundaries() -> None:
    assert split_daily_chat_sentences(
        "第一句。第二句。第三句。第四句。第五句。",
        max_characters=240,
        max_messages=3,
    ) == ("第一句。 第二句。", "第三句。 第四句。", "第五句。")


def test_natural_line_breaks_can_be_message_boundaries() -> None:
    assert split_daily_chat_sentences(
        "先告诉你一件事\n然后我们再继续",
        max_characters=240,
        max_messages=3,
    ) == ("先告诉你一件事", "然后我们再继续")


def test_long_plain_output_is_not_split_as_daily_chat() -> None:
    text = "第一句。" + "很长" * 120 + "第二句。"
    assert split_daily_chat_sentences(
        text,
        max_characters=240,
        max_messages=4,
    ) == (text,)


def test_markdown_cleanup_and_control_character_sanitization() -> None:
    assert (
        clean_model_output("# 标题\n[链接](https://example.test)", max_characters=100)
        == "标题\n链接 (https://example.test)"
    )
    assert sanitize_input("a\x00b\r\nc") == "ab\nc"


def test_model_output_never_exposes_internal_history_timestamps() -> None:
    text = (
        "[21:10] 先确认一下。[07-27 03:14] 五分钟后提醒你。\n"
        "[07-27 03:13 QQ 2186567848] 这也是内部历史标记。"
    )

    assert clean_model_output(text, max_characters=200) == (
        "先确认一下。五分钟后提醒你。\n这也是内部历史标记。"
    )


def test_model_output_never_exposes_event_identity_envelopes() -> None:
    text = (
        "前缀 [发送者:奶鼠|QQ:2186567848|消息:1742835379|"
        "时间:2026-08-05T15:39:05.884399] 看到了。\n"
        "[发送者:远野|QQ:2186567848|消息:1742835380|回复:Yuki/消息:1742835379] "
        "第二句。"
    )

    assert clean_model_output(text, max_characters=200) == "前缀 看到了。\n第二句。"


@pytest.mark.asyncio
async def test_conversation_isolation_and_clear(database: Database) -> None:
    repository = ConversationRepository(database)
    first = ConversationIdentity.private("1")
    second = ConversationIdentity.private("2")
    await repository.add_message(first, role="user", content="first")
    await repository.add_message(second, role="user", content="second")
    assert await repository.clear(first) == 1
    assert await repository.count_messages(first) == 0
    assert await repository.count_messages(second) == 1


@pytest.mark.asyncio
async def test_database_restart_restores_history(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'restart.db').as_posix()}"
    first_db = Database(url)
    await first_db.create_schema()
    identity = ConversationIdentity.private("42")
    await ConversationRepository(first_db).add_message(identity, role="user", content="durable")
    await first_db.close()

    second_db = Database(url)
    try:
        history = await ConversationRepository(second_db).list_context(
            identity, max_messages=10, max_characters=100
        )
        assert [item.content for item in history] == ["durable"]
    finally:
        await second_db.close()
