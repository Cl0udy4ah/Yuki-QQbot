"""Real SQLite coverage for effective emoji scope selection."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import update

from qq_ai_bot.emoji.db_models import EmojiAssetModel
from qq_ai_bot.emoji.models import EmojiLifecycleStatus
from qq_ai_bot.emoji.repository import EmojiRepository
from qq_ai_bot.emoji.storage import EmojiStorage
from qq_ai_bot.persistence.database import Database


async def _asset(
    repository: EmojiRepository,
    storage: EmojiStorage,
    *,
    color: str,
) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (24, 20), color).save(buffer, format="PNG")
    content = buffer.getvalue()
    media = storage.inspect(content, near_duplicate_enabled=False)
    storage.persist(content, media)
    asset, _ = await repository.record_candidate(
        media,
        source_event_id=None,
        user_id=None,
        group_id=None,
        source_sub_type="emoji",
        source_emoji_id="",
        source_package_id="",
    )
    return asset.id


async def _select(
    repository: EmojiRepository,
    *,
    group_id: str | None,
    cooldown_after: datetime | None = None,
) -> dict[str, float]:
    rows = await repository.selectable(
        actor_user_id="10001",
        group_id=group_id,
        cooldown_after=cooldown_after or datetime.now(UTC),
        scope_cooldown_after=None,
        limit=20,
    )
    return {asset.id: weight for asset, weight in rows}


@pytest.mark.asyncio
async def test_global_scope_is_visible_in_private_and_multiple_groups(
    database: Database,
    tmp_path: Path,
) -> None:
    repository = EmojiRepository(database)
    emoji_id = await _asset(repository, EmojiStorage(tmp_path / "emoji"), color="red")
    await repository.adopt_scope(emoji_id, scope_type="global", weight=1.5)

    assert (await _select(repository, group_id=None))[emoji_id] == 1.5
    assert (await _select(repository, group_id="group-a"))[emoji_id] == 1.5
    assert (await _select(repository, group_id="group-b"))[emoji_id] == 1.5
    assert await repository.enabled_in_scope(emoji_id, group_id=None)
    assert await repository.enabled_in_scope(emoji_id, group_id="group-a")


@pytest.mark.asyncio
async def test_group_disabled_override_is_isolated_and_precedes_global(
    database: Database,
    tmp_path: Path,
) -> None:
    repository = EmojiRepository(database)
    emoji_id = await _asset(repository, EmojiStorage(tmp_path / "emoji"), color="green")
    await repository.adopt_scope(emoji_id, scope_type="global", weight=2.0)
    await repository.set_group_enabled(emoji_id, group_id="group-a", enabled=False)

    assert emoji_id not in await _select(repository, group_id="group-a")
    assert emoji_id in await _select(repository, group_id="group-b")
    assert emoji_id in await _select(repository, group_id=None)
    assert not await repository.enabled_in_scope(emoji_id, group_id="group-a")
    assert await repository.enabled_in_scope(emoji_id, group_id="group-b")
    assert await repository.enabled_in_scope(emoji_id, group_id=None)


@pytest.mark.asyncio
async def test_group_only_scope_and_maximum_enabled_weight(
    database: Database,
    tmp_path: Path,
) -> None:
    repository = EmojiRepository(database)
    storage = EmojiStorage(tmp_path / "emoji")
    group_only = await _asset(repository, storage, color="blue")
    weighted = await _asset(repository, storage, color="yellow")
    await repository.adopt_scope(
        group_only,
        scope_type="group",
        scope_id="group-a",
        weight=3.0,
    )
    await repository.adopt_scope(weighted, scope_type="global", weight=1.0)
    await repository.adopt_scope(
        weighted,
        scope_type="group",
        scope_id="group-a",
        weight=4.0,
    )

    group_a = await _select(repository, group_id="group-a")
    assert group_a[group_only] == 3.0
    assert group_a[weighted] == 4.0
    assert group_only not in await _select(repository, group_id="group-b")
    assert group_only not in await _select(repository, group_id=None)
    assert await repository.enabled_in_scope(group_only, group_id="group-a")
    assert not await repository.enabled_in_scope(group_only, group_id="group-b")


@pytest.mark.asyncio
async def test_non_adopted_assets_and_recent_usage_are_not_selectable(
    database: Database,
    tmp_path: Path,
) -> None:
    repository = EmojiRepository(database)
    storage = EmojiStorage(tmp_path / "emoji")
    non_adopted = await _asset(repository, storage, color="purple")
    recent = await _asset(repository, storage, color="orange")
    await repository.adopt_scope(non_adopted, scope_type="global")
    await repository.adopt_scope(recent, scope_type="global")
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(EmojiAssetModel)
            .where(EmojiAssetModel.id == non_adopted)
            .values(status=EmojiLifecycleStatus.RECOGNIZED.value)
        )
    await repository.mark_used(
        recent,
        actor_user_id="10001",
        group_id="group-a",
        trigger_message_id="trigger",
        source="test",
    )

    selected = await _select(
        repository,
        group_id="group-a",
        cooldown_after=datetime.now(UTC) - timedelta(seconds=60),
    )
    assert non_adopted not in selected
    assert recent not in selected


@pytest.mark.asyncio
async def test_adopted_count_counts_exact_scope_not_effective_group_pool(
    database: Database,
    tmp_path: Path,
) -> None:
    repository = EmojiRepository(database)
    emoji_id = await _asset(repository, EmojiStorage(tmp_path / "emoji"), color="black")
    await repository.adopt_scope(emoji_id, scope_type="global")

    assert await repository.adopted_count() == 1
    assert await repository.adopted_count(group_id="group-a") == 0
    assert emoji_id in await _select(repository, group_id="group-a")
