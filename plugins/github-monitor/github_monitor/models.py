"""Internal normalized GitHub data."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from yuki_plugin_sdk.models import JsonValue, StrictModel


class RateLimitState(StrictModel):
    remaining: int | None = None
    reset_at: datetime | None = None
    retry_after_seconds: int | None = None
    request_id: str = ""


class GitHubAPIResponse(StrictModel):
    status_code: int
    body: JsonValue = None
    headers: dict[str, str] = Field(default_factory=dict)
    rate_limit: RateLimitState = Field(default_factory=RateLimitState)


class RepositoryState(StrictModel):
    last_event_id: str = ""
    last_event_created_at: datetime | None = None
    etag: str = ""
    last_modified: str = ""
    last_poll_at: datetime | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    paused_until: datetime | None = None
    rate_limit_remaining: int | None = None
    rate_limit_reset_at: datetime | None = None
    last_request_id: str = ""
    backlog_truncated: bool = False
    baseline_notified: bool = False


class NormalizedGitHubEvent(StrictModel):
    github_event_id: str
    repository: str
    event_type: str
    actor: str
    created_at: datetime
    action: str = ""
    branch: str = ""
    number: int | None = None
    title: str = ""
    url: str = ""
    summary: str
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    event_key: str
    is_bot: bool = False
    is_draft: bool = False
    push_before: str = ""
    push_head: str = ""
    push_deleted: bool = False
