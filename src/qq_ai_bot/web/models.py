"""Immutable provider-neutral web search models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Literal

WebSearchTopic = Literal["general", "news"]
WebSearchTimeRange = Literal["day", "week", "month", "year"]


class WebMode(StrEnum):
    """Configured backend strategy for model-approved web access."""

    DISABLED = "disabled"
    NATIVE = "native"
    TAVILY = "tavily"
    NATIVE_WITH_TAVILY_FALLBACK = "native_with_tavily_fallback"


class WebProvider(StrEnum):
    """Concrete provider selected for one web-capable Agent run."""

    NATIVE = "native"
    TAVILY = "tavily"


class WebRouteReason(StrEnum):
    """Stable, non-sensitive explanation for a provider routing decision."""

    MODE = "mode"
    USER_OVERRIDE = "user_override"
    DOMAIN_RULE = "domain_rule"
    DEFAULT_NATIVE = "default_native"
    NATIVE_TIMEOUT = "native_timeout"
    NATIVE_UNAVAILABLE = "native_unavailable"
    NATIVE_EMPTY = "native_empty"
    NATIVE_ACCESS_DENIED = "native_access_denied"
    TARGET_NOT_OPENED = "target_not_opened"
    SOURCE_NOT_RECOVERED = "source_not_recovered"


@dataclass(frozen=True, slots=True)
class WebRouteDecision:
    """One bounded and auditable provider selection for the current turn."""

    provider: WebProvider
    reason: WebRouteReason
    fallback_allowed: bool
    attempt: int = 1
    matched_domain: str | None = None
    target_urls: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class WebSearchRequest:
    """A bounded query sent to a configured web search provider."""

    query: str
    topic: WebSearchTopic = "general"
    time_range: WebSearchTimeRange | None = None
    start_date: date | None = None
    end_date: date | None = None
    max_results: int = 5
    extract_max_results: int | None = None


@dataclass(frozen=True, slots=True)
class WebSearchSource:
    """One real provider-returned source and its query-relevant content."""

    source_id: str
    title: str
    url: str
    domain: str
    snippet: str
    relevant_content: str
    published_at: datetime | None = None
    provider_score: float | None = None


@dataclass(frozen=True, slots=True)
class WebSearchResponse:
    """A complete search/extract operation returned to the Agent layer."""

    query: str
    sources: tuple[WebSearchSource, ...]
    provider_request_id: str | None
    latency_seconds: float
    partial_failure: bool = False
