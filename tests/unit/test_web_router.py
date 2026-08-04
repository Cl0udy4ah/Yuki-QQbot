"""Deterministic web provider routing tests."""

from __future__ import annotations

from qq_ai_bot.config import Settings
from qq_ai_bot.domain.messages import (
    CitationOrigin,
    NativeToolEvent,
    NativeToolStatus,
    NativeToolType,
    ResponseCitation,
)
from qq_ai_bot.web.models import WebMode, WebProvider, WebRouteReason
from qq_ai_bot.web.router import WebProviderRouter


def test_hybrid_router_uses_domain_rules_with_host_boundaries() -> None:
    router = WebProviderRouter(tavily_domains=frozenset({"github.com"}))

    direct = router.select(
        "读取 https://api.github.com/repos/example/project。",
        WebMode.NATIVE_WITH_TAVILY_FALLBACK,
    )
    unrelated = router.select(
        "读取 https://evilgithub.com/project。",
        WebMode.NATIVE_WITH_TAVILY_FALLBACK,
    )

    assert direct is not None
    assert direct.provider is WebProvider.TAVILY
    assert direct.reason is WebRouteReason.DOMAIN_RULE
    assert direct.matched_domain == "github.com"
    assert direct.target_urls == ("https://api.github.com/repos/example/project",)
    assert unrelated is not None
    assert unrelated.provider is WebProvider.NATIVE
    assert unrelated.reason is WebRouteReason.DEFAULT_NATIVE


def test_router_settings_accept_public_environment_name() -> None:
    settings = Settings(
        _env_file=None,
        WEB_TAVILY_DOMAINS="github.com, raw.githubusercontent.com",
    )

    assert settings.web.tavily_domains == frozenset({"github.com", "raw.githubusercontent.com"})


def test_hybrid_router_honors_explicit_provider_without_misreading_conditional_fallback() -> None:
    router = WebProviderRouter()

    tavily = router.select(
        "这次请用 Tavily 搜索官方文档",
        WebMode.NATIVE_WITH_TAVILY_FALLBACK,
    )
    native = router.select(
        "这次只用 DeepSeek 原生搜索",
        WebMode.NATIVE_WITH_TAVILY_FALLBACK,
    )
    conditional = router.select(
        "DeepSeek 打不开就再用 Tavility",
        WebMode.NATIVE_WITH_TAVILY_FALLBACK,
    )

    assert tavily is not None and tavily.provider is WebProvider.TAVILY
    assert tavily.reason is WebRouteReason.USER_OVERRIDE
    assert native is not None and native.provider is WebProvider.NATIVE
    assert native.reason is WebRouteReason.USER_OVERRIDE
    assert not native.fallback_allowed
    assert conditional is not None and conditional.provider is WebProvider.NATIVE
    assert conditional.reason is WebRouteReason.DEFAULT_NATIVE
    assert conditional.fallback_allowed


def test_fixed_modes_ignore_hybrid_rules() -> None:
    router = WebProviderRouter(tavily_domains=frozenset({"github.com"}))
    url = "请用 Tavily 读取 https://github.com/example/project"

    native = router.select(url, WebMode.NATIVE)
    tavily = router.select(url, WebMode.TAVILY)

    assert native is not None and native.provider is WebProvider.NATIVE
    assert native.reason is WebRouteReason.MODE
    assert tavily is not None and tavily.provider is WebProvider.TAVILY
    assert tavily.reason is WebRouteReason.MODE
    assert router.select(url, WebMode.DISABLED) is None


def test_native_failure_and_target_miss_allow_only_one_fallback() -> None:
    router = WebProviderRouter()
    decision = router.select(
        "读取 https://github.com/example/project",
        WebMode.NATIVE_WITH_TAVILY_FALLBACK,
    )
    assert decision is not None

    failed = (
        NativeToolEvent(
            tool_type=NativeToolType.WEB_SEARCH,
            call_id="open-failed",
            status=NativeToolStatus.FAILED,
            action_type="open_page",
            url="https://github.com/example/project",
            error_category="access_denied",
        ),
    )
    assert (
        router.native_terminal_failure(decision, events=failed, citations=())
        is WebRouteReason.NATIVE_ACCESS_DENIED
    )

    opened_other_page = (
        NativeToolEvent(
            tool_type=NativeToolType.WEB_SEARCH,
            call_id="open-other",
            status=NativeToolStatus.COMPLETED,
            action_type="open_page",
            url="https://example.com/other",
        ),
    )
    assert (
        router.native_terminal_failure(decision, events=opened_other_page, citations=())
        is WebRouteReason.TARGET_NOT_OPENED
    )
    matching_citation = (
        ResponseCitation(
            url="https://github.com/example/project#readme",
            origin=CitationOrigin.ANNOTATION,
        ),
    )
    assert (
        router.native_terminal_failure(
            decision,
            events=opened_other_page,
            citations=matching_citation,
        )
        is None
    )

    fallback = router.fallback(decision, WebRouteReason.TARGET_NOT_OPENED)
    assert fallback.provider is WebProvider.TAVILY
    assert fallback.attempt == 2
    assert not fallback.fallback_allowed
    assert not router.can_fallback(fallback)
    assert (
        router.missing_source_failure(
            decision,
            source_display_requested=True,
            source_count=0,
        )
        is WebRouteReason.SOURCE_NOT_RECOVERED
    )


def test_router_ignores_private_urls_and_can_disable_outcome_fallbacks() -> None:
    router = WebProviderRouter(
        tavily_domains=frozenset({"localhost"}),
        fallback_on_access_denied=False,
        fallback_on_target_miss=False,
    )
    decision = router.select(
        "读取 http://localhost:8080/private",
        WebMode.NATIVE_WITH_TAVILY_FALLBACK,
    )
    assert decision is not None
    assert decision.target_urls == ()
    failed = (
        NativeToolEvent(
            tool_type=NativeToolType.WEB_SEARCH,
            call_id="failed",
            status=NativeToolStatus.FAILED,
            action_type="open_page",
        ),
    )
    assert router.native_terminal_failure(decision, events=failed, citations=()) is None
