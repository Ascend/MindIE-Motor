# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException, status
from fastapi.responses import JSONResponse, Response

from motor.common.http.http_client import HTTPClientPool
from motor.common.logger import get_logger
from motor.common.resources.dispatch import (
    MOTOR_DISPATCH_KEY,
    MOTOR_PREFILL_RESULT_KEY,
    DispatchStopReason,
    DispatchStopRequest,
    DispatchStopResponse,
    DispatchStopState,
    MotorDispatch,
    PrefillResult,
)
from motor.engine_server.core.config import IConfig
from motor.engine_server.core.vllm.prefill_context_validation import PrefillContextCheck
from motor.engine_server.core.errors.sanitizer import sanitize_error_message

logger = get_logger(__name__)


@dataclass(frozen=True)
class DispatchResponseContext:
    api: str
    raw_path: str
    request_body: dict[str, Any]
    dispatch: MotorDispatch | None
    stream: bool
    client_return_token_ids: bool = False
    client_expects_chat_shape: bool = False


@dataclass(frozen=True)
class PrefillBodyCacheEntry:
    body: dict[str, Any]
    dispatch: MotorDispatch
    cached_at: float


@dataclass(frozen=True)
class DispatchMetaserverRequest:
    engine_body: dict[str, Any]
    dispatch: MotorDispatch | None = None


class DispatchPeerStopClient:
    def __init__(self, config: IConfig) -> None:
        self._tls_config = None
        endpoint_config = config.get_endpoint_config()
        deploy_config = getattr(endpoint_config, "deploy_config", None)
        if deploy_config is not None:
            self._tls_config = getattr(deploy_config, "infer_tls_config", None)

    async def stop_peer(
        self,
        dispatch: MotorDispatch,
        reason: DispatchStopReason = DispatchStopReason.PEER_FAILED,
        timeout: float = 1.0,
    ) -> DispatchStopResponse | None:
        peer = dispatch.endpoints.decode if dispatch.role == "prefill" else dispatch.endpoints.prefill
        if peer is None:
            return None

        request = DispatchStopRequest(
            root_request_id=dispatch.root_request_id,
            engine_request_id=dispatch.engine_request_id,
            attempt_seq=dispatch.attempt_seq,
            pair_id=dispatch.pair_id,
            reason=reason.value,
            sent_at_ms=int(time.time() * 1000),
        )
        try:
            parsed = urlparse(peer.url)
            host = parsed.hostname
            port = parsed.port
            if not host or port is None:
                raise ValueError(f"Invalid dispatch peer url: {peer.url}")
            client = await HTTPClientPool().get_client(
                ip=host,
                port=str(port),
                tls_config=self._tls_config,
            )
            response = await client.post(
                "/v1/dispatch/stop",
                json=request.model_dump(mode="json"),
                timeout=timeout,
            )
            response.raise_for_status()
            return DispatchStopResponse.model_validate(response.json())
        except httpx.HTTPError as e:
            logger.warning(
                "Peer dispatch stop failed root_request_id=%s attempt_seq=%s peer=%s reason=%s error=%s",
                dispatch.root_request_id,
                dispatch.attempt_seq,
                peer.url,
                reason.value,
                e,
            )
        except Exception as e:
            logger.warning(
                "Peer dispatch stop response invalid root_request_id=%s attempt_seq=%s peer=%s error=%s",
                dispatch.root_request_id,
                dispatch.attempt_seq,
                peer.url,
                e,
            )
        return None


class DispatchAttemptRegistry:
    """In-memory endpoint-side attempt state used by stop and stale guards."""

    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self._lock = asyncio.Lock()
        self._states: dict[tuple[str, int], str] = {}
        self._dispatches: dict[tuple[str, int], MotorDispatch] = {}
        self._updated_at: dict[tuple[str, int], float] = {}
        self._prefill_entries: dict[str, PrefillBodyCacheEntry] = {}
        self._prefill_waiters: dict[str, asyncio.Event] = {}
        self._ttl_seconds = ttl_seconds

    async def activate(self, dispatch: MotorDispatch) -> None:
        async with self._lock:
            now = time.monotonic()
            self._cleanup_locked(now)
            key = (dispatch.root_request_id, dispatch.attempt_seq)
            self._states[key] = "active"
            self._dispatches[key] = dispatch
            self._updated_at[key] = now

    async def cache_prefill_body(self, dispatch: MotorDispatch, body: dict[str, Any]) -> None:
        async with self._lock:
            now = time.monotonic()
            self._cleanup_locked(now)
            self._prefill_entries[dispatch.engine_request_id] = PrefillBodyCacheEntry(
                body=body.copy(),
                dispatch=dispatch,
                cached_at=now,
            )
            waiter = self._prefill_waiters.pop(dispatch.engine_request_id, None)
            if waiter is not None:
                waiter.set()

    async def get_prefill_body(self, engine_request_id: str) -> dict[str, Any] | None:
        entry = await self.get_prefill_entry(engine_request_id)
        if entry is None:
            return None
        return entry.body.copy()

    async def get_prefill_entry(self, engine_request_id: str) -> PrefillBodyCacheEntry | None:
        async with self._lock:
            now = time.monotonic()
            self._cleanup_locked(now)
            entry = self._prefill_entries.get(engine_request_id)
            if entry is None:
                return None
            if now - entry.cached_at > self._ttl_seconds:
                self._prefill_entries.pop(engine_request_id, None)
                return None
            return PrefillBodyCacheEntry(
                body=entry.body.copy(),
                dispatch=entry.dispatch,
                cached_at=entry.cached_at,
            )

    async def wait_prefill_body(self, engine_request_id: str, timeout_seconds: float) -> dict[str, Any] | None:
        entry = await self.wait_prefill_entry(engine_request_id, timeout_seconds)
        if entry is None:
            return None
        return entry.body.copy()

    async def wait_prefill_entry(self, engine_request_id: str, timeout_seconds: float) -> PrefillBodyCacheEntry | None:
        cached = await self.get_prefill_entry(engine_request_id)
        if cached is not None:
            return cached

        async with self._lock:
            now = time.monotonic()
            self._cleanup_locked(now)
            entry = self._prefill_entries.get(engine_request_id)
            if entry is not None:
                if now - entry.cached_at <= self._ttl_seconds:
                    return PrefillBodyCacheEntry(
                        body=entry.body.copy(),
                        dispatch=entry.dispatch,
                        cached_at=entry.cached_at,
                    )
                self._prefill_entries.pop(engine_request_id, None)
            event = self._prefill_waiters.get(engine_request_id)
            if event is None:
                event = asyncio.Event()
                self._prefill_waiters[engine_request_id] = event

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            async with self._lock:
                if self._prefill_waiters.get(engine_request_id) is event:
                    self._prefill_waiters.pop(engine_request_id, None)
            return None
        return await self.get_prefill_entry(engine_request_id)

    async def stop(self, stop_request: DispatchStopRequest) -> DispatchStopState:
        key = (stop_request.root_request_id, stop_request.attempt_seq)
        async with self._lock:
            now = time.monotonic()
            self._cleanup_locked(now)
            current = self._states.get(key)
            if current is None:
                return DispatchStopState.NOT_FOUND
            dispatch = self._dispatches.get(key)
            if dispatch is not None and not self._matches_stop_request(dispatch, stop_request):
                return DispatchStopState.STALE
            if dispatch is not None:
                self._prefill_entries.pop(dispatch.engine_request_id, None)
                waiter = self._prefill_waiters.pop(dispatch.engine_request_id, None)
                if waiter is not None:
                    waiter.set()
            if current == "stopped":
                return DispatchStopState.ALREADY_STOPPED
            if current == "done":
                return DispatchStopState.ALREADY_DONE
            self._states[key] = "stopped"
            self._updated_at[key] = now
            return DispatchStopState.STOPPED

    async def is_stopped(self, dispatch: MotorDispatch) -> bool:
        key = (dispatch.root_request_id, dispatch.attempt_seq)
        async with self._lock:
            now = time.monotonic()
            self._cleanup_locked(now)
            return self._states.get(key) == "stopped"

    async def finish(self, dispatch: MotorDispatch) -> None:
        async with self._lock:
            now = time.monotonic()
            self._cleanup_locked(now)
            key = (dispatch.root_request_id, dispatch.attempt_seq)
            self._prefill_entries.pop(dispatch.engine_request_id, None)
            if self._states.get(key) != "stopped":
                self._states[key] = "done"
                self._dispatches[key] = dispatch
                self._updated_at[key] = now

    def _cleanup_locked(self, now: float) -> None:
        expired_keys = [
            key
            for key, updated_at in self._updated_at.items()
            if self._states.get(key) in ("done", "stopped") and now - updated_at > self._ttl_seconds
        ]
        for key in expired_keys:
            dispatch = self._dispatches.pop(key, None)
            self._states.pop(key, None)
            self._updated_at.pop(key, None)
            if dispatch is not None:
                self._prefill_entries.pop(dispatch.engine_request_id, None)
                waiter = self._prefill_waiters.pop(dispatch.engine_request_id, None)
                if waiter is not None:
                    waiter.set()

        expired_prefill_keys = [
            engine_request_id
            for engine_request_id, entry in self._prefill_entries.items()
            if now - entry.cached_at > self._ttl_seconds
        ]
        for engine_request_id in expired_prefill_keys:
            self._prefill_entries.pop(engine_request_id, None)

    @staticmethod
    def _matches_stop_request(dispatch: MotorDispatch, stop_request: DispatchStopRequest) -> bool:
        if dispatch.pair_id != stop_request.pair_id:
            return False
        return stop_request.engine_request_id is None or stop_request.engine_request_id == dispatch.engine_request_id


class DispatchAdapter:
    """EngineServer dispatch adapter base.

    The adapter validates and strips the internal dispatch envelope, injects
    engine-specific request fields, and normalizes dispatch responses before
    they leave the EngineServer.
    """

    def __init__(self, config: IConfig) -> None:
        self._config = config
        endpoint_config = config.get_endpoint_config()
        self._local_role = getattr(endpoint_config, "role", "union")
        self.engine_type = getattr(endpoint_config, "engine_type", "unknown")
        self._registry = DispatchAttemptRegistry()
        self._peer_stop_client = DispatchPeerStopClient(config)

    async def adapt_request_body(self, body: dict[str, Any]) -> tuple[dict[str, Any], MotorDispatch | None]:
        dispatch_data = body.get(MOTOR_DISPATCH_KEY)
        if dispatch_data is None:
            return body, None

        self._reject_legacy_dispatch_fields(body)

        try:
            dispatch = MotorDispatch.model_validate(dispatch_data)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid {MOTOR_DISPATCH_KEY}: {e}",
            ) from e

        self._validate_role(dispatch)
        await self._registry.activate(dispatch)

        try:
            engine_body = body.copy()
            engine_body.pop(MOTOR_DISPATCH_KEY, None)
            prefill_result = self._pop_and_validate_prefill_result(engine_body, dispatch)
            if prefill_result is not None:
                engine_body = await self._consume_prefill_result(engine_body, dispatch, prefill_result)
            engine_body = await self._adapt_engine_body(engine_body, dispatch)
            return engine_body, dispatch
        except Exception:
            await self.stop_peer(dispatch)
            await self.finish_dispatch(dispatch)
            raise

    def get_prefill_context_check(
        self,
        dispatch: MotorDispatch | None,
    ) -> PrefillContextCheck | None:
        """Return a post-tokenization context check for this request, if needed."""
        return None

    async def maybe_prepare_response(
        self, body: dict[str, Any], dispatch: MotorDispatch | None
    ) -> dict[str, Any] | None:
        return None

    async def should_finish_prepared_response(
        self,
        prepared: dict[str, Any],
        dispatch: MotorDispatch | None,
    ) -> bool:
        return True

    async def prepare_metaserver_body(self, body: dict[str, Any]) -> dict[str, Any]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Metaserver is not available for this endpoint.",
        )

    async def prepare_metaserver_request(self, body: dict[str, Any]) -> DispatchMetaserverRequest:
        return DispatchMetaserverRequest(engine_body=await self.prepare_metaserver_body(body))

    async def normalize_response(self, response: Response, context: DispatchResponseContext) -> Response:
        return response

    async def normalize_stream_chunk(
        self,
        chunk: bytes | str,
        context: DispatchResponseContext,
        state: dict[str, Any],
    ) -> bytes | str | None:
        return chunk

    def register_error_handlers(self, app: Any) -> None:
        """Install engine-specific FastAPI handlers.

        The endpoint must not branch on engine type.  Adapters own both the
        error format and the decision whether an application-level handler is
        required (for example, validation errors raised before a route runs).
        """

    def map_serving_exception(self, exc: Exception, *, has_dispatch: bool) -> Exception:
        """Apply the generic serving exception policy for this engine."""
        from motor.engine_server.core.serving_error import map_serving_exception

        return map_serving_exception(exc, map_unknown_to_http_500=not has_dispatch)

    def map_stream_error(self, exc: Exception, context: DispatchResponseContext) -> str | None:
        """Return a serialized SSE error payload after response headers are sent.

        Non-vLLM engines must still emit a structured event instead of raising,
        because the HTTP status can no longer be changed once streaming starts.
        """
        if isinstance(exc, HTTPException):
            message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            payload = {
                "error": {
                    "message": sanitize_error_message(message),
                    "type": "EngineError",
                    "code": "engine_error",
                }
            }
        else:
            payload = {
                "error": {
                    "message": sanitize_error_message(str(exc)),
                    "type": "EngineError",
                    "code": "engine_error",
                }
            }
        return json.dumps(payload, separators=(",", ":"))

    def map_engine_error(self, exc: Exception, context: DispatchResponseContext) -> Response | HTTPException:
        """Return the engine-native error representation.

        The base adapter is shared by non-vLLM engines.  Keep the historical
        FastAPI semantics here; vLLM overrides this to return its OpenAI error
        envelope.
        """
        if isinstance(exc, HTTPException):
            return exc
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "message": sanitize_error_message(str(exc)),
                    "type": "EngineError",
                    "code": "engine_error",
                }
            },
        )

    async def handle_stop(self, body: dict[str, Any]) -> DispatchStopResponse:
        try:
            stop_request = DispatchStopRequest.model_validate(body)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid dispatch stop request: {e}",
            ) from e

        state = await self._registry.stop(stop_request)
        return DispatchStopResponse(
            root_request_id=stop_request.root_request_id,
            attempt_seq=stop_request.attempt_seq,
            accepted=state
            in (
                DispatchStopState.STOPPED,
                DispatchStopState.ALREADY_STOPPED,
                DispatchStopState.ALREADY_DONE,
            ),
            state=state,
        )

    async def finish_dispatch(self, dispatch: MotorDispatch | None) -> None:
        if dispatch is not None:
            await self._registry.finish(dispatch)

    async def is_dispatch_stopped(self, dispatch: MotorDispatch | None) -> bool:
        if dispatch is None:
            return False
        return await self._registry.is_stopped(dispatch)

    async def stop_peer(
        self,
        dispatch: MotorDispatch | None,
        reason: DispatchStopReason = DispatchStopReason.PEER_FAILED,
    ) -> DispatchStopResponse | None:
        if dispatch is None:
            return None
        return await self._peer_stop_client.stop_peer(dispatch, reason)

    async def _adapt_engine_body(self, body: dict[str, Any], dispatch: MotorDispatch) -> dict[str, Any]:
        return body

    async def _consume_prefill_result(
        self,
        body: dict[str, Any],
        dispatch: MotorDispatch,
        prefill_result: PrefillResult,
    ) -> dict[str, Any]:
        return body

    def _validate_role(self, dispatch: MotorDispatch) -> None:
        if self._local_role in ("union", "both"):
            return
        if self._local_role != dispatch.role:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(f"Dispatch role {dispatch.role} does not match endpoint role {self._local_role}"),
            )

    @staticmethod
    def _reject_legacy_dispatch_fields(body: dict[str, Any]) -> None:
        legacy_fields = [key for key in body if key == "kv_transfer_params" or key.startswith("bootstrap_")]
        if legacy_fields:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Legacy engine dispatch fields are not allowed with "
                    f"{MOTOR_DISPATCH_KEY}: {', '.join(sorted(legacy_fields))}"
                ),
            )

    @staticmethod
    def _pop_and_validate_prefill_result(body: dict[str, Any], dispatch: MotorDispatch) -> PrefillResult | None:
        result_data = body.pop(MOTOR_PREFILL_RESULT_KEY, None)
        if result_data is None:
            return None
        try:
            prefill_result = PrefillResult.model_validate(result_data)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid {MOTOR_PREFILL_RESULT_KEY}: {e}",
            ) from e
        if dispatch.role != "decode":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{MOTOR_PREFILL_RESULT_KEY} is only valid for decode dispatch.",
            )
        if not prefill_result.matches_dispatch(dispatch):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{MOTOR_PREFILL_RESULT_KEY} does not match {MOTOR_DISPATCH_KEY}.",
            )
        return prefill_result
