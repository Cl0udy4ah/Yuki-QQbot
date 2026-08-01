from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy.exc import SQLAlchemyError
from tests.conftest import make_settings

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.emoji.effects import EmojiReplyEffectService
from qq_ai_bot.emoji.models import (
    EmojiPlacement,
    EmojiPreparationStatus,
    EmojiReplyMode,
    EmojiSelectionResult,
    PendingReplyEffect,
)
from qq_ai_bot.emoji.repository import EmojiRepository
from qq_ai_bot.emoji.selector import EmojiSelector
from qq_ai_bot.emoji.storage import EmojiStorage
from qq_ai_bot.persistence.database import Database


class _Selector:
    def __init__(self, result: EmojiSelectionResult | BaseException) -> None:
        self.result = result

    async def select(self, *_args: object, **_kwargs: object) -> EmojiSelectionResult:
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class _Repository:
    def __init__(self, asset: object | None = None, error: Exception | None = None) -> None:
        self.asset = asset
        self.error = error
        self.status_updates: list[tuple[str, object]] = []

    async def get(self, _emoji_id: str) -> object | None:
        if self.error is not None:
            raise self.error
        return self.asset

    async def set_status(self, emoji_id: str, status: object) -> None:
        self.status_updates.append((emoji_id, status))


class _Storage:
    def __init__(self, result: bytes | Exception) -> None:
        self.result = result

    def read(self, _relative_path: str) -> bytes:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _effect() -> PendingReplyEffect:
    return PendingReplyEffect(
        mode=EmojiReplyMode.EMOJI_ONLY,
        placement=EmojiPlacement.ONLY,
        goal="开心",
        explicit_request=True,
        source="planner",
    )


def _inbound() -> InboundMessage:
    return InboundMessage(
        message_id="message-1",
        event_type="message",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity("1001"),
        text="发个表情",
        group_id="2001",
        bot_user_id="9000",
    )


async def _runtime(database: Database):
    return await RuntimeConfigService(
        settings=make_settings(database.url, emoji_enabled=True),
        database=database,
    ).snapshot(group_id="2001")


def _service(
    selector: _Selector,
    repository: _Repository,
    storage: _Storage,
) -> EmojiReplyEffectService:
    return EmojiReplyEffectService(
        selector=cast(EmojiSelector, selector),
        repository=cast(EmojiRepository, repository),
        storage=cast(EmojiStorage, storage),
    )


@pytest.mark.asyncio
async def test_emoji_prepare_ready_contains_one_media_message(database: Database) -> None:
    asset = SimpleNamespace(
        id="emoji-1",
        relative_path="original/emoji.gif",
        description="开心表情",
        mime_type="image/gif",
        animated=True,
    )
    service = _service(
        _Selector(EmojiSelectionResult(emoji_id="emoji-1", selected_by="coarse")),
        _Repository(asset),
        _Storage(b"GIF89a"),
    )

    result = await service.prepare(
        _effect(),
        inbound=_inbound(),
        response_text="",
        runtime=await _runtime(database),
    )

    assert result.status is EmojiPreparationStatus.READY
    assert result.emoji_id == "emoji-1"
    assert result.message is not None
    assert result.message.media[0].content == b"GIF89a"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selector_result", "repository", "storage", "expected"),
    (
        (
            EmojiSelectionResult(reason="empty_pool"),
            _Repository(),
            _Storage(b"unused"),
            EmojiPreparationStatus.NO_CANDIDATE,
        ),
        (
            SQLAlchemyError("query failed"),
            _Repository(),
            _Storage(b"unused"),
            EmojiPreparationStatus.REPOSITORY_UNAVAILABLE,
        ),
        (
            EmojiSelectionResult(emoji_id="missing"),
            _Repository(),
            _Storage(b"unused"),
            EmojiPreparationStatus.ASSET_MISSING,
        ),
    ),
)
async def test_emoji_prepare_returns_typed_failures(
    database: Database,
    selector_result: EmojiSelectionResult | BaseException,
    repository: _Repository,
    storage: _Storage,
    expected: EmojiPreparationStatus,
) -> None:
    service = _service(_Selector(selector_result), repository, storage)
    result = await service.prepare(
        _effect(),
        inbound=_inbound(),
        response_text="",
        runtime=await _runtime(database),
    )
    assert result.status is expected
    assert result.message is None
    assert result.emoji_id is None


@pytest.mark.asyncio
async def test_emoji_prepare_reports_storage_missing(database: Database) -> None:
    asset = SimpleNamespace(
        id="emoji-1",
        relative_path="original/missing.gif",
        description="",
        mime_type="image/gif",
        animated=True,
    )
    repository = _Repository(asset)
    service = _service(
        _Selector(EmojiSelectionResult(emoji_id="emoji-1")),
        repository,
        _Storage(OSError("missing")),
    )
    result = await service.prepare(
        _effect(),
        inbound=_inbound(),
        response_text="",
        runtime=await _runtime(database),
    )
    assert result.status is EmojiPreparationStatus.STORAGE_MISSING
    assert repository.status_updates


@pytest.mark.asyncio
async def test_emoji_prepare_propagates_cancellation(database: Database) -> None:
    service = _service(
        _Selector(asyncio.CancelledError()),
        _Repository(),
        _Storage(b"unused"),
    )
    with pytest.raises(asyncio.CancelledError):
        await service.prepare(
            _effect(),
            inbound=_inbound(),
            response_text="",
            runtime=await _runtime(database),
        )
