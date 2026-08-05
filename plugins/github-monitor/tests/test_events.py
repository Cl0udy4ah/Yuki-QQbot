from __future__ import annotations

from github_monitor.config import NotificationTargetConfig, RepositorySubscription
from github_monitor.events import event_allowed, normalize_event, stable_event_key
from github_monitor.formatter import apply_compare


def test_push_event_is_bounded_stable_and_enriched() -> None:
    raw = {
        "id": "123",
        "type": "PushEvent",
        "actor": {"login": "alice", "type": "User"},
        "created_at": "2026-08-05T10:30:00Z",
        "payload": {
            "ref": "refs/heads/main",
            "before": "a" * 40,
            "head": "b" * 40,
            "size": 2,
            "distinct_size": 2,
            "commits": [{"sha": "b" * 40, "message": "fix notifications"}],
        },
    }
    event = normalize_event("owner/repo", raw)
    assert event is not None
    assert event.branch == "main"
    assert event.event_key == stable_event_key("owner/repo", "PushEvent", "b" * 40)
    enriched = apply_compare(
        event,
        {
            "total_commits": 2,
            "ahead_by": 2,
            "status": "ahead",
            "files": [{"additions": 9, "deletions": 3}],
            "commits": [],
        },
    )
    assert enriched.payload["files_changed"] == 1
    assert "+9 / -3" in enriched.summary


def test_subscription_filters_bot_actor_and_branch() -> None:
    subscription = RepositorySubscription(
        repository="owner/repo",
        branches=frozenset({"main"}),
        targets=(NotificationTargetConfig(target_type="group", target_id="2001"),),
    )
    bot = normalize_event(
        "owner/repo",
        {
            "id": "1",
            "type": "PushEvent",
            "actor": {"login": "dependabot[bot]", "type": "Bot"},
            "created_at": "2026-08-05T10:30:00Z",
            "payload": {"ref": "refs/heads/main", "head": "a"},
        },
    )
    assert bot is not None
    assert not event_allowed(bot, subscription)
