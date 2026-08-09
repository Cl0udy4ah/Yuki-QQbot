"""Secret-free persistence for MCP metadata, artifacts, and invocation telemetry."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select

from qq_ai_bot.mcp.models import MCPServerConfig, MCPToolMetadata
from qq_ai_bot.mcp.redaction import redact_sensitive_data, redact_sensitive_text
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MCPServerStateModel,
    MCPToolCacheModel,
    MemoryToolReceiptModel,
    ToolArtifactModel,
    ToolInvocationModel,
)


def _redact_reflection_result(value: str) -> str:
    """Redact structured secrets before a bounded tool result can become evidence."""

    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return redact_sensitive_text(value)
    return json.dumps(
        redact_sensitive_data(decoded),
        ensure_ascii=False,
        separators=(",", ":"),
    )


class MCPRepository:
    def __init__(
        self,
        database: Database,
        *,
        reflection_excerpt_characters: int = 2000,
        reflection_retention_days: int = 7,
    ) -> None:
        self._database = database
        self._reflection_excerpt_characters = max(1, min(reflection_excerpt_characters, 8000))
        self._reflection_retention_days = max(1, min(reflection_retention_days, 30))

    async def state(self, server_id: str) -> MCPServerStateModel | None:
        async with self._database.sessions() as session:
            return await session.get(MCPServerStateModel, server_id)

    async def save_state(
        self,
        server_id: str,
        config: MCPServerConfig,
        config_hash: str,
        *,
        enabled: bool,
        status: str,
        server_info: dict[str, str] | None = None,
        connected: bool = False,
        refreshed: bool = False,
        error_category: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session:
            row = await session.get(MCPServerStateModel, server_id)
            if row is None:
                row = MCPServerStateModel(
                    server_id=server_id,
                    transport=config.transport.value,
                    config_hash=config_hash,
                    enabled=enabled,
                    lifecycle=config.lifecycle.value,
                    status=status,
                    protocol_version="",
                    server_name="",
                    server_version="",
                    server_instructions="",
                    updated_at=now,
                )
                session.add(row)
            row.transport = config.transport.value
            row.config_hash = config_hash
            row.enabled = enabled
            row.lifecycle = config.lifecycle.value
            row.status = status
            row.last_error_category = error_category
            row.updated_at = now
            if connected:
                row.last_connected_at = now
            if refreshed:
                row.last_refreshed_at = now
            if server_info:
                row.protocol_version = server_info.get("protocol_version", "")[:64]
                row.server_name = server_info.get("server_name", "")[:255]
                row.server_version = server_info.get("server_version", "")[:128]
                row.server_instructions = server_info.get("server_instructions", "")[:8000]
            await session.commit()

    async def set_enabled(self, server_id: str, enabled: bool) -> bool:
        async with self._database.sessions() as session:
            row = await session.get(MCPServerStateModel, server_id)
            if row is None:
                return False
            row.enabled = enabled
            row.status = "disconnected" if enabled else "disabled"
            row.updated_at = datetime.now(UTC)
            await session.commit()
            return True

    async def cached_tools(self, server_id: str) -> tuple[MCPToolMetadata, ...]:
        async with self._database.sessions() as session:
            rows = (
                await session.execute(
                    select(MCPToolCacheModel)
                    .where(MCPToolCacheModel.server_id == server_id)
                    .order_by(MCPToolCacheModel.remote_tool_name)
                )
            ).scalars()
            return tuple(self._metadata(row) for row in rows)

    async def replace_cached_tools(
        self,
        server_id: str,
        tools: tuple[MCPToolMetadata, ...],
    ) -> None:
        async with self._database.sessions() as session:
            await session.execute(
                delete(MCPToolCacheModel).where(MCPToolCacheModel.server_id == server_id)
            )
            session.add_all(
                MCPToolCacheModel(
                    server_id=item.server_id,
                    remote_tool_name=item.remote_tool_name,
                    model_name=item.model_name,
                    description=item.description,
                    compact_description=item.compact_description,
                    input_schema_json=json.dumps(item.input_schema, ensure_ascii=False),
                    output_schema_json=json.dumps(item.output_schema, ensure_ascii=False),
                    annotations_json=json.dumps(item.annotations, ensure_ascii=False),
                    metadata_hash=item.metadata_hash,
                    refreshed_at=item.refreshed_at,
                )
                for item in tools
            )
            await session.commit()

    async def clear_cached_tools(self, server_id: str) -> None:
        async with self._database.sessions() as session:
            await session.execute(
                delete(MCPToolCacheModel).where(MCPToolCacheModel.server_id == server_id)
            )
            await session.commit()

    async def record_invocation(
        self,
        *,
        conversation_key: str,
        provider_id: str,
        tool_name: str,
        success: bool,
        latency_seconds: float,
        result_size: int,
        artifact_created: bool,
        error_category: str | None,
        trigger_message_id: str = "",
        bot_user_id: str = "",
        result_excerpt: str = "",
    ) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            session.add(
                ToolInvocationModel(
                    conversation_key_hash=hashlib.sha256(
                        conversation_key.encode("utf-8")
                    ).hexdigest(),
                    provider_id=provider_id[:128],
                    tool_name=tool_name[:255],
                    success=success,
                    latency_seconds=max(0.0, latency_seconds),
                    result_size=max(0, result_size),
                    artifact_created=artifact_created,
                    error_category=error_category[:128] if error_category else None,
                    created_at=now,
                )
            )
            event = None
            if trigger_message_id and bot_user_id:
                event = await session.scalar(
                    select(ChatEventModel).where(
                        ChatEventModel.bot_user_id == bot_user_id,
                        ChatEventModel.platform_message_id == trigger_message_id,
                    )
                )
            if event is not None:
                conversation_key = (
                    f"group:{event.group_id}"
                    if event.group_id
                    else f"private:{event.private_peer_user_id or event.sender_user_id}"
                )
                redacted = _redact_reflection_result(result_excerpt.strip())
                session.add(
                    MemoryToolReceiptModel(
                        conversation_key_hash=hashlib.sha256(
                            conversation_key.encode("utf-8")
                        ).hexdigest(),
                        trigger_event_id=event.id,
                        bot_user_id=event.bot_user_id,
                        provider_id=provider_id[:128],
                        tool_name=tool_name[:255],
                        success=success,
                        result_excerpt=redacted[: self._reflection_excerpt_characters],
                        result_characters=len(redacted),
                        error_category=error_category[:128] if error_category else None,
                        created_at=now,
                        expires_at=now + timedelta(days=self._reflection_retention_days),
                    )
                )

    @staticmethod
    def _metadata(row: MCPToolCacheModel) -> MCPToolMetadata:
        return MCPToolMetadata(
            server_id=row.server_id,
            remote_tool_name=row.remote_tool_name,
            model_name=row.model_name,
            description=row.description,
            compact_description=row.compact_description,
            input_schema=json.loads(row.input_schema_json),
            output_schema=json.loads(row.output_schema_json),
            annotations=json.loads(row.annotations_json),
            metadata_hash=row.metadata_hash,
            refreshed_at=_as_utc(row.refreshed_at),
        )


class ToolArtifactRepository:
    """Store complete oversized results in bounded files, not SQLite."""

    def __init__(self, database: Database, root: Path, *, retention_seconds: int) -> None:
        if retention_seconds <= 0:
            raise ValueError("artifact retention must be positive")
        self._database = database
        self._root = root
        self._retention = retention_seconds

    def configure_retention(self, retention_seconds: int) -> None:
        if retention_seconds <= 0:
            raise ValueError("artifact retention must be positive")
        self._retention = retention_seconds

    async def write_artifact(
        self,
        *,
        provider_id: str,
        tool_name: str,
        content: str,
        media_type: str,
        retention_seconds: int | None = None,
    ) -> str:
        handle = uuid.uuid4().hex
        relative = f"{handle}.json"
        path = self._root / relative
        encoded = content.encode("utf-8")
        await asyncio.to_thread(self._root.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, encoded)
        retention = retention_seconds if retention_seconds is not None else self._retention
        if retention <= 0:
            raise ValueError("artifact retention must be positive")
        now = datetime.now(UTC)
        async with self._database.sessions() as session:
            session.add(
                ToolArtifactModel(
                    handle_id=handle,
                    provider_id=provider_id[:128],
                    tool_name=tool_name[:255],
                    relative_path=relative,
                    media_type=media_type[:128],
                    byte_size=len(encoded),
                    created_at=now,
                    expires_at=now + timedelta(seconds=retention),
                )
            )
            await session.commit()
        return handle

    async def read(
        self,
        handle_id: str,
        *,
        offset: int = 0,
        limit: int = 8000,
        query: str = "",
    ) -> dict[str, object] | None:
        if offset < 0 or limit <= 0:
            raise ValueError("artifact offset must be non-negative and limit must be positive")
        if not handle_id.isalnum() or len(handle_id) > 64:
            return None
        async with self._database.sessions() as session:
            row = await session.get(ToolArtifactModel, handle_id)
            if row is None or _as_utc(row.expires_at) <= datetime.now(UTC):
                return None
            relative = row.relative_path
        path = (self._root / relative).resolve()
        root = self._root.resolve()
        if root not in path.parents:
            return None
        try:
            content = await asyncio.to_thread(path.read_text, encoding="utf-8")
        except OSError:
            return None
        start = offset
        if query:
            found = content.casefold().find(query.casefold(), offset)
            if found < 0:
                return {
                    "handle": handle_id,
                    "offset": offset,
                    "next_offset": None,
                    "total_characters": len(content),
                    "content": "",
                    "query_matched": False,
                }
            start = found
        end = min(len(content), start + limit)
        return {
            "handle": handle_id,
            "offset": start,
            "next_offset": end if end < len(content) else None,
            "total_characters": len(content),
            "content": content[start:end],
            "query_matched": True if query else None,
        }

    async def cleanup(self) -> int:
        now = datetime.now(UTC)
        async with self._database.sessions() as session:
            rows = tuple(
                (
                    await session.execute(
                        select(ToolArtifactModel).where(ToolArtifactModel.expires_at <= now)
                    )
                ).scalars()
            )
            for row in rows:
                path = (self._root / row.relative_path).resolve()
                if self._root.resolve() in path.parents:
                    try:
                        await asyncio.to_thread(path.unlink, missing_ok=True)
                    except OSError:
                        pass
                await session.delete(row)
            await session.commit()
            return len(rows)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
