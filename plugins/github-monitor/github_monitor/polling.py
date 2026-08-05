"""Bounded multi-repository polling and idempotent publication."""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime, timedelta

from yuki_plugin_sdk.context import PluginContext
from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest

from .client import GitHubClient
from .config import GitHubMonitorConfig, RepositorySubscription, load_config
from .errors import GitHubAPIError
from .events import event_allowed, normalize_event, stable_event_key
from .formatter import apply_compare, external_payload, notification_text
from .models import NormalizedGitHubEvent, RepositoryState
from .renderer import render_event_card
from .state import load_repository_state, save_repository_state

AGENT_INTENT = "根据当前主会话关系和仓库事件，自然说一句真实反应；不要复述完整卡片。"


class GitHubPoller:
    def __init__(self, context: PluginContext, stop: asyncio.Event) -> None:
        self._context = context
        self._stop = stop
        self._client = GitHubClient(context)

    async def run(self) -> None:
        while not self._stop.is_set():
            config = await load_config(self._context)
            started = datetime.now(UTC)
            for subscription in config.repositories:
                if self._stop.is_set():
                    break
                if not subscription.enabled:
                    continue
                try:
                    await self.poll_repository(subscription, config)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._context.logger.warning(
                        "github_poll_failed repository=%s error_category=%s",
                        subscription.repository,
                        type(exc).__name__,
                    )
            elapsed = (datetime.now(UTC) - started).total_seconds()
            delay = max(1.0, config.poll_interval_seconds - elapsed)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def poll_repository(
        self,
        subscription: RepositorySubscription,
        config: GitHubMonitorConfig,
    ) -> None:
        state = await load_repository_state(self._context, subscription.repository)
        now = datetime.now(UTC)
        if state.paused_until is not None and state.paused_until > now:
            return
        self._context.logger.info("github_poll_started repository=%s", subscription.repository)
        try:
            response = await self._client.repository_events(
                subscription.repository,
                per_page=config.events_per_repository,
                etag=state.etag,
                last_modified=state.last_modified,
            )
        except GitHubAPIError as exc:
            await self._record_failure(subscription.repository, state, exc)
            return
        state = state.model_copy(
            update={
                "last_poll_at": now,
                "rate_limit_remaining": response.rate_limit.remaining,
                "rate_limit_reset_at": response.rate_limit.reset_at,
                "last_request_id": response.rate_limit.request_id,
            }
        )
        if response.status_code == 304:
            await save_repository_state(
                self._context,
                subscription.repository,
                self._successful_state(state, now),
            )
            return
        raw_events = list(response.body) if isinstance(response.body, list) else []
        first_page_ids = {str(item.get("id", "")) for item in raw_events if isinstance(item, dict)}
        if (
            'rel="next"' in response.headers.get("link", "")
            and state.last_event_id
            and state.last_event_id not in first_page_ids
        ):
            try:
                second = await self._client.repository_events(
                    subscription.repository,
                    per_page=config.events_per_repository,
                    page=2,
                )
            except GitHubAPIError as exc:
                await self._record_failure(subscription.repository, state, exc)
                return
            if isinstance(second.body, list):
                raw_events.extend(second.body)
        normalized: list[NormalizedGitHubEvent] = []
        for raw in raw_events:
            try:
                event = normalize_event(subscription.repository, raw)
            except (TypeError, ValueError):
                self._context.logger.info(
                    "github_event_filtered repository=%s reason=invalid_event",
                    subscription.repository,
                )
                continue
            if event is not None and event_allowed(event, subscription):
                normalized.append(event)
        normalized.sort(key=lambda item: (item.created_at, item.github_event_id))

        if not state.last_event_id:
            if not normalized:
                updated = self._with_response_metadata(state, response.headers, now)
                await save_repository_state(
                    self._context,
                    subscription.repository,
                    self._successful_state(updated, now),
                )
                return
            if config.initial_sync_mode == "baseline":
                newest = normalized[-1]
                await self._publish_enabled(subscription, now)
                updated = self._advance(state, newest)
                updated = self._with_response_metadata(updated, response.headers, now)
                await save_repository_state(
                    self._context,
                    subscription.repository,
                    self._successful_state(updated, now).model_copy(
                        update={"baseline_notified": True}
                    ),
                )
                return
            candidates = normalized[-config.replay_recent_limit :]
        else:
            cursor_index = next(
                (
                    index
                    for index, event in enumerate(normalized)
                    if event.github_event_id == state.last_event_id
                ),
                None,
            )
            if cursor_index is not None:
                candidates = normalized[cursor_index + 1 :]
            else:
                candidates = [
                    event
                    for event in normalized
                    if state.last_event_created_at is None
                    or event.created_at > state.last_event_created_at
                    or (
                        event.created_at == state.last_event_created_at
                        and event.github_event_id > state.last_event_id
                    )
                ]
        backlog = len(candidates) > config.max_events_per_poll
        candidates = candidates[-config.max_events_per_poll :]
        working = state.model_copy(update={"backlog_truncated": backlog})
        for event in candidates:
            enriched = await self._enrich_push(event)
            await self.publish_event(subscription, enriched)
            working = self._advance(working, event)
            await save_repository_state(self._context, subscription.repository, working)
        working = self._with_response_metadata(working, response.headers, now)
        working = self._successful_state(working, now)
        if response.rate_limit.remaining is not None and response.rate_limit.remaining <= 100:
            pause_seconds = 300 if response.rate_limit.remaining else 900
            pause_until = response.rate_limit.reset_at or now + timedelta(seconds=pause_seconds)
            working = working.model_copy(
                update={"paused_until": pause_until + timedelta(seconds=random.randint(1, 30))}
            )
        await save_repository_state(self._context, subscription.repository, working)
        self._context.logger.info(
            "github_poll_completed repository=%s events=%d backlog_truncated=%s",
            subscription.repository,
            len(candidates),
            backlog,
        )

    async def publish_event(
        self,
        subscription: RepositorySubscription,
        event: NormalizedGitHubEvent,
    ) -> None:
        media_handle = ""
        if any(t.send_card for t in subscription.targets):
            try:
                rendered = await asyncio.to_thread(render_event_card, event)
                if rendered is not None:
                    png, filename = rendered
                    handle = await self._context.media.create_artifact(
                        data=png,
                        content_type="image/png",
                        filename=filename,
                        ttl_seconds=86_400,
                    )
                    media_handle = handle.handle_id
            except Exception as exc:
                self._context.logger.warning(
                    "github_card_render_failed repository=%s error_category=%s",
                    subscription.repository,
                    type(exc).__name__,
                )
        for target in subscription.targets:
            receipt = await self._context.notifications.publish(
                PublishNotificationRequest(
                    event_key=event.event_key,
                    event_type=event.event_type,
                    external_source="github",
                    target=NotificationTarget(
                        target_type=target.target_type,
                        target_id=target.target_id,
                    ),
                    occurred_at=event.created_at,
                    summary=event.summary,
                    payload=external_payload(event),
                    text=notification_text(event) if target.send_text else "",
                    media_handles=(media_handle,) if target.send_card and media_handle else (),
                    ask_agent=target.ask_agent,
                    agent_intent=AGENT_INTENT if target.ask_agent else "",
                )
            )
            self._context.logger.info(
                "github_event_published repository=%s event_type=%s deduplicated=%s",
                event.repository,
                event.event_type,
                receipt.deduplicated,
            )

    async def _enrich_push(self, event: NormalizedGitHubEvent) -> NormalizedGitHubEvent:
        if (
            event.event_type != "PushEvent"
            or event.push_deleted
            or not event.push_before
            or not event.push_head
            or set(event.push_before) == {"0"}
        ):
            return event
        try:
            response = await self._client.compare(
                event.repository,
                event.push_before,
                event.push_head,
            )
            return apply_compare(event, response.body)
        except GitHubAPIError as exc:
            self._context.logger.info(
                "github_compare_failed repository=%s error_category=%s",
                event.repository,
                exc.category,
            )
            return event

    async def _publish_enabled(
        self,
        subscription: RepositorySubscription,
        occurred_at: datetime,
    ) -> None:
        event_key = stable_event_key(
            subscription.repository,
            "monitor_enabled",
            "baseline",
        )
        for target in subscription.targets:
            await self._context.notifications.publish(
                PublishNotificationRequest(
                    event_key=event_key,
                    event_type="monitor_enabled",
                    external_source="github",
                    target=NotificationTarget(
                        target_type=target.target_type,
                        target_id=target.target_id,
                    ),
                    occurred_at=occurred_at,
                    summary=f"已启用 {subscription.repository} 的 GitHub 监控",
                    payload={"repository": subscription.repository, "baseline": True},
                    text=f"GitHub 监控已启用：{subscription.repository}",
                )
            )

    async def _record_failure(
        self,
        repository: str,
        state: RepositoryState,
        error: GitHubAPIError,
    ) -> None:
        now = datetime.now(UTC)
        failures = state.consecutive_failures + 1
        state = state.model_copy(
            update={
                "rate_limit_remaining": error.remaining,
                "rate_limit_reset_at": error.reset_at,
            }
        )
        delay = min(3600, 30 * (2 ** min(failures - 1, 6)))
        if error.category == "token_invalid":
            delay = 3600
        elif error.category == "rate_limited" and (
            error.reset_at is not None or error.retry_after_seconds is not None
        ):
            pause = error.reset_at or now + timedelta(seconds=error.retry_after_seconds or 60)
            await save_repository_state(
                self._context,
                repository,
                state.model_copy(
                    update={
                        "last_poll_at": now,
                        "consecutive_failures": failures,
                        "paused_until": pause + timedelta(seconds=random.randint(1, 30)),
                    }
                ),
            )
            return
        await save_repository_state(
            self._context,
            repository,
            state.model_copy(
                update={
                    "last_poll_at": now,
                    "consecutive_failures": failures,
                    "paused_until": now + timedelta(seconds=delay),
                }
            ),
        )

    @staticmethod
    def _advance(state: RepositoryState, event: NormalizedGitHubEvent) -> RepositoryState:
        return state.model_copy(
            update={
                "last_event_id": event.github_event_id,
                "last_event_created_at": event.created_at,
            }
        )

    @staticmethod
    def _with_response_metadata(
        state: RepositoryState,
        headers: dict[str, str],
        now: datetime,
    ) -> RepositoryState:
        return state.model_copy(
            update={
                "etag": headers.get("etag", state.etag),
                "last_modified": headers.get("last-modified", state.last_modified),
                "last_poll_at": now,
            }
        )

    @staticmethod
    def _successful_state(state: RepositoryState, now: datetime) -> RepositoryState:
        return state.model_copy(
            update={
                "last_success_at": now,
                "consecutive_failures": 0,
                "paused_until": None,
            }
        )
