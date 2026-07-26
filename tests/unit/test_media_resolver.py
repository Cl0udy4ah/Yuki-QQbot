"""Media resolution enforces SSRF, byte, redirect, and Base64 limits."""

from __future__ import annotations

import base64
from typing import Any

import httpx
import pytest

from qq_ai_bot.services.media_resolver import MediaResolutionError, MediaResolver
from qq_ai_bot.vision.models import MediaReference


async def _public_host(_host: str, _port: int) -> tuple[str, ...]:
    return ("93.184.216.34",)


@pytest.mark.asyncio
async def test_http_image_is_streamed_and_hashed() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/image"
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "images.example"
        assert request.extensions["sni_hostname"] == "images.example"
        return httpx.Response(200, headers={"Content-Type": "image/png"}, content=b"png-data")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    resolver = MediaResolver(client=client, host_resolver=_public_host)
    result = await resolver.resolve(MediaReference(url="https://images.example/image"))

    assert result.content == b"png-data"
    assert result.content_type == "image/png"
    assert result.byte_size == 8
    assert len(result.content_hash) == 64
    await client.aclose()


@pytest.mark.asyncio
async def test_plain_http_image_is_supported_after_public_host_validation() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.scheme == "http"
        assert request.url.host == "93.184.216.34"
        assert request.headers["Host"] == "images.example"
        return httpx.Response(200, headers={"Content-Type": "image/jpeg"}, content=b"jpeg")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    resolver = MediaResolver(client=client, host_resolver=_public_host)

    result = await resolver.resolve(MediaReference(url="http://images.example/plain.jpg"))

    assert result.content == b"jpeg"
    assert result.content_type == "image/jpeg"
    await client.aclose()


@pytest.mark.asyncio
async def test_localhost_and_private_ip_are_rejected() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: None))
    resolver = MediaResolver(client=client)
    with pytest.raises(MediaResolutionError, match="本地"):
        await resolver.resolve(MediaReference(url="http://localhost/image.png"))
    with pytest.raises(MediaResolutionError, match="私有"):
        await resolver.resolve(MediaReference(url="http://192.168.1.2/image.png"))
    await resolver.close()  # externally supplied client remains caller-owned
    await client.aclose()


@pytest.mark.asyncio
async def test_redirect_target_is_revalidated() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    resolver = MediaResolver(client=client, host_resolver=_public_host)

    with pytest.raises(MediaResolutionError) as raised:
        await resolver.resolve(MediaReference(url="https://images.example/start"))
    assert raised.value.code == "private_url"
    assert calls == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_stream_stops_at_byte_limit() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"0123456789")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    resolver = MediaResolver(
        client=client,
        host_resolver=_public_host,
        max_download_bytes=5,
    )
    with pytest.raises(MediaResolutionError) as raised:
        await resolver.resolve(MediaReference(url="https://images.example/large"))
    assert raised.value.code == "too_large"
    await client.aclose()


@pytest.mark.asyncio
async def test_inline_base64_is_validated_before_and_after_decode() -> None:
    resolver = MediaResolver(max_download_bytes=4)
    result = await resolver.resolve(
        MediaReference(file=f"base64://{base64.b64encode(b'abcd').decode()}")
    )
    assert result.content == b"abcd"

    with pytest.raises(MediaResolutionError) as raised:
        await resolver.resolve(MediaReference(file="base64://%%%"))
    assert raised.value.code == "invalid_base64"
    with pytest.raises(MediaResolutionError) as raised:
        await resolver.resolve(MediaReference(file=f"base64://{' ' * 5000}YQ=="))
    assert raised.value.code == "too_large"
    await resolver.close()


class _Gateway:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_api(self, action: str, params: dict[str, Any]) -> Any:
        self.calls.append((action, params))
        return self.payload


@pytest.mark.asyncio
async def test_file_identifier_uses_get_image_without_opening_local_path() -> None:
    encoded = base64.b64encode(b"image").decode()
    gateway = _Gateway({"data": {"base64": encoded, "file": "C:\\napcat\\image.jpg"}})
    resolver = MediaResolver()

    result = await resolver.resolve(MediaReference(file="napcat-file-id"), gateway=gateway)

    assert result.content == b"image"
    assert gateway.calls == [("get_image", {"file": "napcat-file-id"})]
    await resolver.close()
