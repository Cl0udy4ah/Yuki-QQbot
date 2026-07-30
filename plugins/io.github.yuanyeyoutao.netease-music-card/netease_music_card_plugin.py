"""Search NetEase Music through MCP and send a native QQ music card."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import cast

from pydantic import BaseModel, Field, model_validator

from yuki_plugin_sdk.context import PluginContext
from yuki_plugin_sdk.models import JsonValue, PermissionLevel, RiskClass, StrictModel, TurnOrigin
from yuki_plugin_sdk.registrar import PluginRegistrar, ToolMetadata, ToolRegistration
from yuki_plugin_sdk.results import ToolResult

_SERVER_ID = "netease_music"
_ID_PATTERN = re.compile(r"^[1-9][0-9]{0,19}$")


class ShareMusicInput(StrictModel):
    """A title/artist search or a previously selected NetEase song ID."""

    query: str = Field(default="", max_length=200)
    artist: str = Field(default="", max_length=100)
    song_id: str = Field(default="", max_length=20)

    @model_validator(mode="after")
    def _valid_request(self) -> ShareMusicInput:
        query = self.query.strip()
        artist = self.artist.strip()
        song_id = self.song_id.strip()
        if bool(query) == bool(song_id):
            raise ValueError("provide exactly one of query or song_id")
        if song_id and _ID_PATTERN.fullmatch(song_id) is None:
            raise ValueError("song_id must be a positive NetEase numeric ID")
        if artist and not query:
            raise ValueError("artist can only be used with query")
        return self


class SongCandidate(StrictModel):
    song_id: str
    title: str
    artists: tuple[str, ...] = ()


class ShareMusicOutput(StrictModel):
    status: str = Field(pattern=r"^(sent|selection_required|not_found)$")
    message: str
    selected: SongCandidate | None = None
    candidates: tuple[SongCandidate, ...] = ()


class NetEaseMusicCardPlugin:
    """One focused tool; it does not add a second chat route or model session."""

    def __init__(self) -> None:
        self._context: PluginContext | None = None

    async def register(self, registrar: PluginRegistrar) -> None:
        registrar.register_tool(
            ToolRegistration(
                metadata=ToolMetadata(
                    name="share_netease_music",
                    description=(
                        "仅当用户明确要求发送、分享或点一首网易云歌曲到当前 QQ 会话时调用。"
                        "可按歌曲名和歌手搜索，也可用上次候选中的 song_id 精确发送。"
                        "重名时工具不会擅自发送，而会返回候选供用户选择；只查询歌曲信息时不要调用。"
                    ),
                    permission=PermissionLevel.USER,
                    risk=RiskClass.SEND,
                    allowed_origins=frozenset({TurnOrigin.USER_MESSAGE}),
                    timeout_seconds=30,
                ),
                input_model=ShareMusicInput,
                output_model=ShareMusicOutput,
                handler=self._share,
            )
        )

    async def start(self, context: PluginContext) -> None:
        self._context = context

    async def stop(self) -> None:
        self._context = None

    async def _share(self, raw_request: BaseModel) -> ShareMusicOutput | ToolResult:
        request = ShareMusicInput.model_validate(raw_request.model_dump())
        context = self._running_context()
        if request.song_id.strip():
            candidates_or_error = await self._get_song(context, request.song_id.strip())
            if isinstance(candidates_or_error, ToolResult):
                return candidates_or_error
            candidates = candidates_or_error
            if not candidates:
                return _intermediate_result(
                    ShareMusicOutput(
                        status="not_found",
                        message="没有找到这个网易云歌曲 ID，请重新搜索后再选择",
                    )
                )
            selected = candidates[0]
        else:
            search_query = " ".join(
                value for value in (request.query.strip(), request.artist.strip()) if value
            )
            candidates_or_error = await self._search(context, search_query)
            if isinstance(candidates_or_error, ToolResult):
                return candidates_or_error
            candidates = candidates_or_error
            if not candidates:
                return _intermediate_result(
                    ShareMusicOutput(
                        status="not_found",
                        message="没有搜索到匹配的网易云歌曲，请换一个歌曲名或补充歌手",
                    )
                )
            selected = _select_unambiguous(
                query=request.query,
                artist=request.artist,
                candidates=candidates,
            )
            if selected is None:
                return _intermediate_result(
                    ShareMusicOutput(
                        status="selection_required",
                        message=(
                            "存在重名或结果不够明确，请把候选歌曲和歌手简短列给用户选择；"
                            "用户选定后，用对应 song_id 再调用本工具"
                        ),
                        candidates=candidates,
                    )
                )

        sent = await context.onebot.send_music_card(
            provider="netease",
            resource_id=selected.song_id,
        )
        if not sent.ok:
            return ToolResult(
                ok=False,
                error_code="music_card.send_failed",
                detail=sent.detail or "NapCat 未能发送网易云音乐卡片",
                data={"song": selected.model_dump(mode="json")},
            )
        return ShareMusicOutput(
            status="sent",
            message="网易云音乐卡片已发送到当前会话；不要重复发送文字链接或伪造卡片",
            selected=selected,
        )

    async def _search(
        self,
        context: PluginContext,
        query: str,
    ) -> tuple[SongCandidate, ...] | ToolResult:
        result = await context.mcp.call(
            _SERVER_ID,
            "music_search",
            {
                "query": query,
                "category": "song",
                "page": 1,
                "page_size": 5,
                "detail_level": "summary",
            },
        )
        if not result.ok:
            return _mcp_failure(result, "搜索网易云歌曲失败")
        data = _structured_data(result)
        return _song_candidates(data.get("items")) if data is not None else ()

    async def _get_song(
        self,
        context: PluginContext,
        song_id: str,
    ) -> tuple[SongCandidate, ...] | ToolResult:
        result = await context.mcp.call(
            _SERVER_ID,
            "get_songs",
            {"song_ids": [song_id], "detail_level": "summary"},
        )
        if not result.ok:
            return _mcp_failure(result, "读取网易云歌曲失败")
        data = _structured_data(result)
        return _song_candidates(data.get("songs")) if data is not None else ()

    def _running_context(self) -> PluginContext:
        if self._context is None:
            raise RuntimeError("NetEase music card plugin is not running")
        return self._context


def _structured_data(result: ToolResult | object) -> Mapping[str, JsonValue] | None:
    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        return None
    envelope = data.get("result")
    if not isinstance(envelope, dict):
        return None
    structured = envelope.get("data")
    return cast(Mapping[str, JsonValue], structured) if isinstance(structured, dict) else None


def _song_candidates(value: JsonValue | None) -> tuple[SongCandidate, ...]:
    if not isinstance(value, list):
        return ()
    candidates: list[SongCandidate] = []
    seen: set[str] = set()
    for raw in value[:5]:
        if not isinstance(raw, dict):
            continue
        song_id = str(raw.get("id", "")).strip()
        title = str(raw.get("title", "")).strip()
        if _ID_PATTERN.fullmatch(song_id) is None or not title or song_id in seen:
            continue
        artists_raw = raw.get("artists")
        artists: list[str] = []
        if isinstance(artists_raw, list):
            for artist in artists_raw[:10]:
                if isinstance(artist, dict):
                    name = str(artist.get("name", "")).strip()
                    if name:
                        artists.append(name[:100])
        candidates.append(
            SongCandidate(
                song_id=song_id,
                title=title[:200],
                artists=tuple(artists),
            )
        )
        seen.add(song_id)
    return tuple(candidates)


def _select_unambiguous(
    *,
    query: str,
    artist: str,
    candidates: Sequence[SongCandidate],
) -> SongCandidate | None:
    if len(candidates) == 1:
        return candidates[0]
    query_key = _search_key(query)
    artist_key = _search_key(artist)
    exact_titles = [item for item in candidates if _search_key(item.title) == query_key]
    if artist_key:
        matches = [
            item
            for item in exact_titles
            if any(artist_key in _search_key(name) for name in item.artists)
        ]
        return matches[0] if len(matches) == 1 else None
    if len(exact_titles) == 1:
        return exact_titles[0]
    # A query that exactly names an artist means "send any/top song by this
    # artist" in ordinary chat. Cloud Search already ranks the artist's songs,
    # so selecting its first exact-artist result is deterministic.
    artist_query_matches = [
        item
        for item in candidates
        if any(_search_key(name) == query_key for name in item.artists)
    ]
    if artist_query_matches:
        return artist_query_matches[0]
    combined = [
        item
        for item in candidates
        if _search_key(item.title) in query_key
        and any(_search_key(name) in query_key for name in item.artists)
    ]
    return combined[0] if len(combined) == 1 else None


def _search_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _mcp_failure(result: object, fallback: str) -> ToolResult:
    detail = str(getattr(result, "detail", "")).strip() or fallback
    return ToolResult(
        ok=False,
        error_code="music_card.mcp_failed",
        detail=detail[:1_000],
    )


def _intermediate_result(output: ShareMusicOutput) -> ToolResult:
    """Return a successful lookup that has not yet sent or mutated anything."""

    return ToolResult(
        data={"result": output.model_dump(mode="json")},
        mutation_committed=False,
    )
