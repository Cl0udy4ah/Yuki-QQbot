"""Tests for the durable QQ emoji description library."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import EmojiDescriptionRepository


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _observation(description: str) -> dict[str, object]:
    return {
        "items": [],
        "overall_description": description,
        "partial_failure": False,
        "provider": "qwen",
        "model": "qwen3.7-plus",
        "latency_seconds": 0.1,
    }


@pytest.mark.asyncio
async def test_persistent_lookup_increments_hits_without_expiry(database: Database) -> None:
    repository = EmojiDescriptionRepository(database)
    created_at = datetime.now(UTC) - timedelta(days=365)
    keys = ("package:5:pack-aemoji-a", f"content:{_hash('same-emoji')}")

    saved = await repository.save_many(
        keys,
        analysis_mode="meme",
        question_hash="",
        provider="qwen",
        model="qwen3.7-plus",
        prompt_version="vision-v3",
        observation_json=_observation("奶龙开心地挥手"),
        now=created_at,
    )
    assert len(saved) == 2
    assert saved[0].description == "奶龙开心地挥手"

    hit = await repository.find_first(
        keys,
        analysis_mode="meme",
        question_hash="",
        provider="qwen",
        model="qwen3.7-plus",
        prompt_version="vision-v3",
    )

    assert hit is not None
    assert hit.emoji_key == keys[0]
    assert hit.hit_count == 1
    assert hit.created_at.replace(tzinfo=UTC) == created_at
    assert hit.last_used_at.replace(tzinfo=UTC) > created_at


@pytest.mark.asyncio
async def test_mode_question_model_and_prompt_are_exactly_isolated(database: Database) -> None:
    repository = EmojiDescriptionRepository(database)
    key = "emoji:42"
    question_hash = _hash("这是谁")
    await repository.save_many(
        (key,),
        analysis_mode="question",
        question_hash=question_hash,
        provider="qwen",
        model="qwen3.7-plus",
        prompt_version="vision-v3",
        observation_json=_observation("水上由岐"),
    )

    common = {
        "emoji_keys": (key,),
        "analysis_mode": "question",
        "provider": "qwen",
        "model": "qwen3.7-plus",
        "prompt_version": "vision-v3",
    }
    assert await repository.find_first(question_hash=question_hash, **common) is not None
    assert await repository.find_first(question_hash=_hash("写了什么"), **common) is None
    assert (
        await repository.find_first(
            (key,),
            analysis_mode="meme",
            question_hash="",
            provider="qwen",
            model="qwen3.7-plus",
            prompt_version="vision-v3",
        )
        is None
    )
    assert (
        await repository.find_first(
            question_hash=question_hash,
            **(common | {"model": "v2"}),
        )
        is None
    )


@pytest.mark.asyncio
async def test_repository_rejects_unknown_keys_and_embedded_media(database: Database) -> None:
    repository = EmojiDescriptionRepository(database)
    common = {
        "analysis_mode": "meme",
        "question_hash": "",
        "provider": "qwen",
        "model": "qwen3.7-plus",
        "prompt_version": "vision-v3",
    }
    with pytest.raises(ValueError, match="namespace"):
        await repository.save_many(
            ("url:https://example.test/signed",),
            observation_json=_observation("不会保存"),
            **common,
        )
    with pytest.raises(ValueError, match="must not contain image or Base64"):
        await repository.save_many(
            ("emoji:42",),
            observation_json={"image": "base64://AAAA"},
            **common,
        )
