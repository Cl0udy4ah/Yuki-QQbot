"""SSRF, redirect, response-size, and plugin-concurrency HTTP boundaries."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx
import pytest

from qq_ai_bot.plugin_host.http_client import BoundHttpFacade, SafeHttpClient
from yuki_plugin_sdk.errors import PluginPermissionError
from yuki_plugin_sdk.permissions import PluginPermission

Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


def _client(
    handler: Handler, resolver: Callable[..., Awaitable[tuple[str, ...]]]
) -> SafeHttpClient:
    transport = httpx.MockTransport(handler)
    return SafeHttpClient(
        timeout_seconds=1,
        max_response_bytes=32,
        client=httpx.AsyncClient(transport=transport),
        resolver=resolver,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "address"),
    [
        ("http://localhost/status", "127.0.0.1"),
        ("http://private.example/status", "10.0.0.8"),
        ("http://link-local.example/status", "169.254.169.254"),
        ("http://ipv6-private.example/status", "fd00::1"),
    ],
)
async def test_non_public_targets_are_rejected_before_transport(
    url: str,
    address: str,
) -> None:
    transport_called = False

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal transport_called
        transport_called = True
        return httpx.Response(200)

    async def resolver(_host: str, _port: int) -> tuple[str, ...]:
        return (address,)

    client = _client(handler, resolver)
    with pytest.raises(PluginPermissionError, match="non-public"):
        await client.request("GET", url, allowed_hosts=None)
    assert not transport_called


@pytest.mark.asyncio
async def test_request_connects_to_validated_ip_and_preserves_host_sni() -> None:
    requests: list[httpx.Request] = []
    resolver_calls: list[tuple[str, int]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"ok")

    async def resolver(host: str, port: int) -> tuple[str, ...]:
        resolver_calls.append((host, port))
        return ("93.184.216.34",)

    client = _client(handler, resolver)
    result = await client.request(
        "GET",
        "https://api.example:8443/data?api_key=must-not-leak",
        allowed_hosts=frozenset({"api.example"}),
        headers={
            "Accept": "application/json",
            "Authorization": "Bearer must-not-leak",
            "Cookie": "session=must-not-leak",
        },
    )

    assert result.ok
    assert result.data == {
        "status_code": 200,
        "body": "ok",
        "content_type": "text/plain",
        "url": "https://api.example:8443/data",
        "headers": {},
    }
    assert resolver_calls == [("api.example", 8443)]
    assert len(requests) == 1
    request = requests[0]
    assert request.url.host == "93.184.216.34"
    assert request.url.port == 8443
    assert request.headers["host"] == "api.example:8443"
    assert request.extensions["sni_hostname"] == "api.example"
    assert "authorization" not in request.headers
    assert "cookie" not in request.headers


@pytest.mark.asyncio
async def test_safe_response_headers_and_304_are_exposed() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            304,
            headers={
                "ETag": '"next"',
                "X-RateLimit-Remaining": "42",
                "X-GitHub-Request-Id": "request-1",
                "Set-Cookie": "must-not-leak",
            },
        )

    async def resolver(_host: str, _port: int) -> tuple[str, ...]:
        return ("93.184.216.34",)

    result = await _client(handler, resolver).request(
        "GET",
        "https://api.example/data",
        allowed_hosts=frozenset({"api.example"}),
    )

    assert result.ok
    assert result.data["status_code"] == 304
    assert result.data["headers"] == {
        "etag": '"next"',
        "x-ratelimit-remaining": "42",
        "x-github-request-id": "request-1",
    }


@pytest.mark.asyncio
async def test_bound_credential_is_injected_only_on_same_origin() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(302, headers={"location": "https://other.example/final"})
        return httpx.Response(200)

    async def resolver(host: str, _port: int) -> tuple[str, ...]:
        return {
            "api.example": ("93.184.216.34",),
            "other.example": ("93.184.216.35",),
        }[host]

    class Secrets:
        @staticmethod
        def get(name: str) -> str:
            assert name == "GITHUB_TOKEN"
            return "must-not-leak"

    facade = BoundHttpFacade(
        client=_client(handler, resolver),
        approved_permissions=(PluginPermission.NETWORK_HTTP_ALLOWLISTED,),
        allowed_hosts=("api.example", "other.example"),
        secrets=Secrets(),
    )
    result = await facade.request(
        "GET",
        "https://api.example/start",
        auth_secret="GITHUB_TOKEN",
    )

    assert result.ok
    assert requests[0].headers["authorization"] == "Bearer must-not-leak"
    assert "authorization" not in requests[1].headers


@pytest.mark.asyncio
async def test_redirect_is_resolved_again_and_private_rebinding_is_rejected() -> None:
    request_count = 0
    resolution_count = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(302, headers={"location": "/next"})

    async def resolver(_host: str, _port: int) -> tuple[str, ...]:
        nonlocal resolution_count
        resolution_count += 1
        if resolution_count == 1:
            return ("93.184.216.34",)
        return ("127.0.0.1",)

    client = _client(handler, resolver)
    with pytest.raises(PluginPermissionError, match="non-public"):
        await client.request(
            "GET",
            "https://public.example/start",
            allowed_hosts=frozenset({"public.example"}),
        )
    assert resolution_count == 2
    assert request_count == 1


@pytest.mark.asyncio
async def test_redirect_to_private_host_is_rejected_before_second_request() -> None:
    requested_hosts: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(request.url.host)
        return httpx.Response(
            302,
            headers={"location": "http://internal.example/latest"},
        )

    async def resolver(host: str, _port: int) -> tuple[str, ...]:
        return ("10.1.2.3",) if host == "internal.example" else ("93.184.216.34",)

    client = _client(handler, resolver)
    with pytest.raises(PluginPermissionError, match="non-public"):
        await client.request("GET", "https://public.example/start", allowed_hosts=None)
    assert requested_hosts == ["93.184.216.34"]


@pytest.mark.asyncio
async def test_cross_origin_redirect_drops_caller_headers() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(302, headers={"location": "https://other.example/final"})
        if len(requests) == 2:
            return httpx.Response(302, headers={"location": "https://public.example/done"})
        return httpx.Response(200, content=b"done")

    async def resolver(host: str, _port: int) -> tuple[str, ...]:
        return {
            "public.example": ("93.184.216.34",),
            "other.example": ("93.184.216.35",),
        }[host]

    client = _client(handler, resolver)
    result = await client.request(
        "GET",
        "https://public.example/start",
        allowed_hosts=None,
        headers={"X-Api-Key": "must-not-leak"},
        body=b"must-not-leak",
    )

    assert result.ok
    assert requests[0].headers["x-api-key"] == "must-not-leak"
    assert "x-api-key" not in requests[1].headers
    assert requests[1].headers["host"] == "other.example"
    assert requests[1].content == b""
    assert "x-api-key" not in requests[2].headers
    assert requests[2].content == b""


@pytest.mark.asyncio
async def test_response_body_limit_is_enforced_on_streamed_content() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 33)

    async def resolver(_host: str, _port: int) -> tuple[str, ...]:
        return ("93.184.216.34",)

    client = _client(handler, resolver)
    result = await client.request("GET", "https://public.example/large", allowed_hosts=None)
    assert not result.ok
    assert result.error_code == "http.response_too_large"
    assert "x" * 8 not in result.detail


@pytest.mark.asyncio
async def test_transport_error_does_not_expose_url_or_secret() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("api_key=must-not-leak", request=request)

    async def resolver(_host: str, _port: int) -> tuple[str, ...]:
        return ("93.184.216.34",)

    client = _client(handler, resolver)
    result = await client.request(
        "GET",
        "https://public.example/data?api_key=must-not-leak",
        allowed_hosts=None,
    )
    assert not result.ok
    assert result.error_code == "http.request_failed"
    assert "must-not-leak" not in result.detail
    assert not result.data


@pytest.mark.asyncio
async def test_httpx_request_log_redacts_plugin_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"ok")

    async def resolver(_host: str, _port: int) -> tuple[str, ...]:
        return ("93.184.216.34",)

    client = _client(handler, resolver)
    with caplog.at_level(logging.INFO, logger="httpx"):
        result = await client.request(
            "GET",
            "https://public.example/data?api_key=must-not-leak",
            allowed_hosts=None,
        )

    assert result.ok
    assert "[plugin-url-redacted]" in caplog.text
    assert "must-not-leak" not in caplog.text
    assert "93.184.216.34" not in caplog.text


@pytest.mark.asyncio
async def test_bound_facade_enforces_per_plugin_http_concurrency() -> None:
    active = 0
    maximum_active = 0
    first_pair_entered = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        if active == 2:
            first_pair_entered.set()
        await first_pair_entered.wait()
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, content=b"ok")

    async def resolver(_host: str, _port: int) -> tuple[str, ...]:
        return ("93.184.216.34",)

    facade = BoundHttpFacade(
        client=_client(handler, resolver),
        approved_permissions=(PluginPermission.NETWORK_HTTP_ALLOWLISTED,),
        allowed_hosts=("public.example",),
        http_concurrency=2,
    )
    results = await asyncio.gather(
        *(facade.request("GET", f"https://public.example/{index}") for index in range(6))
    )

    assert all(result.ok for result in results)
    assert maximum_active == 2


def test_bound_facade_rejects_invalid_concurrency_limit() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    async def resolver(_host: str, _port: int) -> tuple[str, ...]:
        return ("93.184.216.34",)

    with pytest.raises(ValueError, match="concurrency"):
        BoundHttpFacade(
            client=_client(handler, resolver),
            approved_permissions=(PluginPermission.NETWORK_HTTP_UNRESTRICTED,),
            allowed_hosts=(),
            http_concurrency=0,
        )
