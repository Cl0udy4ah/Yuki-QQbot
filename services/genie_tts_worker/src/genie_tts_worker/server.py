"""Concurrent control-plane, serial-synthesis Unix socket server."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path

from pydantic import ValidationError

from genie_tts_worker.engine import EngineFailure, GenieEngine
from genie_tts_worker.models import (
    CancelRequest,
    ClearReferenceCacheRequest,
    FailureResponse,
    HealthRequest,
    LoadProfileRequest,
    ReloadProfileRequest,
    ShutdownRequest,
    SuccessResponse,
    SynthesizeRequest,
    UnloadProfileRequest,
    WorkerErrorCode,
    WorkerRequest,
    WorkerResponse,
)
from genie_tts_worker.protocol import read_request, write_frame


class GenieWorkerServer:
    def __init__(
        self,
        *,
        socket_path: Path,
        engine: GenieEngine,
        socket_mode: int,
    ) -> None:
        self._socket_path = socket_path
        self._engine = engine
        self._socket_mode = socket_mode
        self._server: asyncio.AbstractServer | None = None
        self._synthesis_lock = asyncio.Lock()
        self._shutdown = asyncio.Event()
        self._shutdown_task: asyncio.Task[None] | None = None
        self._current_request_id: str | None = None
        self._cancelled_request_ids: set[str] = set()

    @property
    def queue_depth(self) -> int:
        return int(self._synthesis_lock.locked())

    async def start(self) -> None:
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._socket_path.unlink(missing_ok=True)
        self._server = await asyncio.start_unix_server(self._handle_client, path=self._socket_path)
        os.chmod(self._socket_path, self._socket_mode)
        try:
            self._engine.initialize()
        except EngineFailure:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            self._socket_path.unlink(missing_ok=True)
            raise

    async def serve_until_shutdown(self) -> None:
        if self._server is None:
            raise RuntimeError("worker server has not started")
        await self._shutdown.wait()
        self._server.close()
        await self._server.wait_closed()
        self._socket_path.unlink(missing_ok=True)

    async def request_shutdown(self) -> None:
        if self._current_request_id is not None:
            await self._stop_engine(self._current_request_id)
        await asyncio.to_thread(self._engine.shutdown)
        self._shutdown.set()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            while not reader.at_eof():
                try:
                    request = await read_request(reader)
                except asyncio.IncompleteReadError:
                    break
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    ValidationError,
                    ValueError,
                ) as exc:
                    await write_frame(
                        writer,
                        FailureResponse(
                            request_id="unknown",
                            error=WorkerErrorCode.INVALID_REQUEST,
                            detail=type(exc).__name__,
                        ),
                    )
                    break
                response = await self._dispatch(request)
                await write_frame(writer, response)
                if isinstance(request, ShutdownRequest):
                    break
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def _dispatch(self, request: WorkerRequest) -> WorkerResponse:
        try:
            if isinstance(request, HealthRequest):
                return self._success(
                    request,
                    ready=True,
                    busy=self._synthesis_lock.locked(),
                    loaded_profile_id=self._engine.loaded_profile_id,
                )
            if isinstance(request, LoadProfileRequest):
                await asyncio.to_thread(
                    self._engine.load_profile,
                    profile_id=request.profile_id,
                    model_relative_path=request.model_relative_path,
                    engine_model_version=request.engine_model_version,
                    language=request.language,
                )
                return self._success(request, loaded_profile_id=request.profile_id)
            if isinstance(request, ReloadProfileRequest):
                await asyncio.to_thread(
                    self._engine.load_profile,
                    profile_id=request.profile_id,
                    model_relative_path=request.model_relative_path,
                    engine_model_version=request.engine_model_version,
                    language=request.language,
                    reload=True,
                )
                return self._success(request, loaded_profile_id=request.profile_id)
            if isinstance(request, UnloadProfileRequest):
                await asyncio.to_thread(self._engine.unload_profile, request.profile_id)
                return self._success(request)
            if isinstance(request, SynthesizeRequest):
                return await self._synthesize(request)
            if isinstance(request, CancelRequest):
                cancelled = await self._stop_engine(request.target_request_id)
                return self._success(request, status="cancelled" if cancelled else "not_running")
            if isinstance(request, ClearReferenceCacheRequest):
                await asyncio.to_thread(self._engine.clear_reference_cache)
                return self._success(request)
            if isinstance(request, ShutdownRequest):
                self._shutdown_task = asyncio.create_task(self.request_shutdown())
                return self._success(request, status="shutting_down")
            raise TypeError(f"unhandled request type: {type(request).__name__}")
        except EngineFailure as exc:
            return FailureResponse(
                request_id=request.request_id,
                error=exc.code,
                detail=self._sanitize_detail(exc.detail),
            )

    async def _synthesize(self, request: SynthesizeRequest) -> WorkerResponse:
        async with self._synthesis_lock:
            self._current_request_id = request.request_id
            try:
                if request.request_id in self._cancelled_request_ids:
                    return self._cancelled(request)
                metadata = await asyncio.to_thread(self._engine.synthesize, request)
                if request.request_id in self._cancelled_request_ids:
                    self._engine.discard_output(metadata.relative_path)
                    return self._cancelled(request)
                return self._success(
                    request,
                    output_relative_path=metadata.relative_path,
                    sample_rate=metadata.sample_rate,
                    channels=metadata.channels,
                    sample_width=metadata.sample_width,
                    duration_milliseconds=metadata.duration_milliseconds,
                    loaded_profile_id=request.profile_id,
                )
            except EngineFailure as exc:
                if request.request_id in self._cancelled_request_ids:
                    return self._cancelled(request)
                return FailureResponse(
                    request_id=request.request_id,
                    error=exc.code,
                    detail=self._sanitize_detail(exc.detail),
                )
            finally:
                self._cancelled_request_ids.discard(request.request_id)
                self._current_request_id = None

    async def _stop_engine(self, request_id: str) -> bool:
        if request_id != self._current_request_id:
            return False
        self._cancelled_request_ids.add(request_id)
        try:
            await asyncio.to_thread(self._engine.stop)
        except EngineFailure:
            return True
        return True

    @staticmethod
    def _success(request: WorkerRequest, **updates: object) -> SuccessResponse:
        return SuccessResponse.model_validate(
            {"request_id": request.request_id, "operation": request.operation, **updates}
        )

    @staticmethod
    def _cancelled(request: WorkerRequest) -> FailureResponse:
        return FailureResponse(
            request_id=request.request_id,
            error=WorkerErrorCode.CANCELLED,
            detail="synthesis was cancelled",
        )

    def _sanitize_detail(self, detail: str) -> str:
        return detail.replace(str(self._socket_path), "<socket>")
