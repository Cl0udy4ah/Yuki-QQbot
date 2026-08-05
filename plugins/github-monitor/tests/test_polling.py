from __future__ import annotations

import asyncio

import pytest
from github_monitor.config import (
    GitHubMonitorConfig,
    NotificationTargetConfig,
    RepositorySubscription,
)
from github_monitor.models import GitHubAPIResponse
from github_monitor.polling import GitHubPoller
from github_monitor.state import load_repository_state

from yuki_plugin_sdk.testing import FakePluginContext


class StubClient:
    async def repository_events(self, *_args: object, **_kwargs: object) -> GitHubAPIResponse:
        return GitHubAPIResponse(
            status_code=200,
            headers={"etag": '"baseline"'},
            body=[
                {
                    "id": "100",
                    "type": "WatchEvent",
                    "actor": {"login": "alice", "type": "User"},
                    "created_at": "2026-08-05T10:30:00Z",
                    "payload": {"action": "started"},
                }
            ],
        )


@pytest.mark.asyncio
async def test_first_baseline_marks_cursor_and_only_publishes_enabled_notice() -> None:
    context = FakePluginContext(plugin_id="github-monitor")
    config = GitHubMonitorConfig(
        repositories=(
            RepositorySubscription(
                repository="owner/repo",
                targets=(
                    NotificationTargetConfig(
                        target_type="group",
                        target_id="2001",
                    ),
                ),
            ),
        )
    )
    for key, value in config.model_dump(mode="json").items():
        await context.config.set(key, value)
    poller = GitHubPoller(context, asyncio.Event())
    poller._client = StubClient()  # type: ignore[assignment]
    await poller.poll_repository(config.repositories[0], config)
    state = await load_repository_state(context, "owner/repo")
    assert state.last_event_id == "100"
    assert state.etag == '"baseline"'
    assert len(context.notifications.published) == 1
    assert context.notifications.published[0].event_type == "monitor_enabled"
