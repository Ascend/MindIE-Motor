# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import asyncio
import contextlib
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Callable, Iterator

import httpx
from anyio import CancelScope
from fastapi import status, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse

from motor.common.resources.endpoint import WorkloadAction
from motor.common.resources.instance import PDRole
from motor.common.http.http_client import HTTPClientPool
from motor.common.logger import get_logger
from motor.common.http.security_utils import filter_sensitive_headers, build_safe_body_structure
from motor.common.utils.net import format_address
import motor.common.utils.error as cancel_error
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.models.constants import (
    DEFAULT_REQUEST_ID,
    OpenAIField,
    REQUEST_ID_KEY,
)
from motor.common.resources.dispatch import MOTOR_DISPATCH_KEY
from motor.coordinator.models.response import ErrorResponse
from motor.coordinator.domain import ScheduledResource
from motor.coordinator.models.request import RequestInfo, ReqState
from motor.coordinator.domain import SchedulingFacade, UpdateWorkloadParams
from motor.coordinator.router.precision_sample.sample_builder import (
    build_decode_sample,
    _log_sample_submission,
)
from motor.common.resources.instance import Instance
from motor.common.resources.endpoint import Endpoint, Workload
from motor.coordinator.domain.request_manager import RequestManager
from motor.coordinator.router.workload import WorkloadActionHandler
from motor.coordinator.router.upstream_error import UpstreamHTTPError
from motor.coordinator.tracer.tracing import TracerManager
from motor.coordinator.domain.scheduling import InstanceReadiness

logger = get_logger(__name__)

_SCHEDULING_LOG_SAMPLE_RATE = 100  # ~1% sampling at high QPS


def _should_log_scheduling_sample(req_id: str) -> bool:
    return hash(req_id) % _SCHEDULING_LOG_SAMPLE_RATE == 0


def _scheduling_state_for_role(role: PDRole) -> ReqState:
    if role == PDRole.ROLE_E:
        return ReqState.E_SCHEDULING
    if role == PDRole.ROLE_P:
        return ReqState.P_SCHEDULING
    return ReqState.D_SCHEDULING


def _allocated_state_for_role(role: PDRole) -> ReqState:
    if role == PDRole.ROLE_E:
        return ReqState.E_ALLOCATED
    if role == PDRole.ROLE_P:
        return ReqState.P_ALLOCATED
    return ReqState.D_ALLOCATED


def check_cancel_error(error: asyncio.CancelledError) -> (str, bool):
    """Return cancelled reason and if need retry"""
    reason = "Exception"
    if error.args:
        reason = error.args[0]
        if reason in {cancel_error.CLIENT_DISCONNECT, cancel_error.DISPATCH_ABORT}:
            return reason, False
        elif reason.startswith(cancel_error.SCOPE_ABORT):
            return cancel_error.SCOPE_ABORT, False
        elif reason.startswith(cancel_error.NODE_FAULT):
            return reason, True
    return reason, True


class RequestLoggerAdapter(logging.LoggerAdapter):
    def process(self, msg: str, kwargs: Any) -> tuple[str, Any]:
        req_id = self.extra.get(REQUEST_ID_KEY, DEFAULT_REQUEST_ID) if self.extra else DEFAULT_REQUEST_ID
        return f"[{req_id}] {msg}", kwargs


@dataclass
class RecomputeState:
    """Per-request recompute counters and flags (PD/CDP routers)."""

    retry_count: int = 0
    wants_retry: bool = False
    total_generated_token: str = ""


class BaseRouter(ABC):
    """
    Base router; depends on SchedulingFacade injection.
    """

    def __init__(
        self,
        req_info: RequestInfo,
        config: CoordinatorConfig,
        scheduler: SchedulingFacade,
        request_manager: RequestManager,
        workload_action_handler: WorkloadActionHandler | None = None,
        sampling_manager=None,
    ):
        self.config = config
        self.req_info = req_info
        self.first_chunk_sent = False
        self.logger = RequestLoggerAdapter(logger, extra={REQUEST_ID_KEY: req_info.req_id})
        self.is_meta = False
        self._scheduler: SchedulingFacade = scheduler
        self._request_manager = request_manager
        self._workload_action_handler = (
            workload_action_handler
            if workload_action_handler is not None
            else WorkloadActionHandler(self._request_manager)
        )
        self._sampling_manager = sampling_manager

    @staticmethod
    def build_error_response(e: Exception) -> ErrorResponse:
        if isinstance(e, HTTPException):
            return ErrorResponse(
                code=e.status_code,
                type=type(e).__name__,
                message=e.detail,
            )
        if isinstance(e, UpstreamHTTPError):
            return ErrorResponse(
                code=e.status_code,
                type=type(e).__name__,
                message=str(e),
            )
        if isinstance(e, httpx.HTTPStatusError):
            return ErrorResponse(code=e.response.status_code, type=type(e).__name__, message=str(e))
        return ErrorResponse(
            code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            type=type(e).__name__,
            message=str(e),
        )

    @staticmethod
    def _apply_prefill_params(
        req_data: dict,
        *,
        set_min_tokens: bool = True,
    ) -> dict:
        p_req = req_data.copy()
        p_req[OpenAIField.STREAM] = False
        p_req[OpenAIField.MAX_TOKENS] = 1
        if OpenAIField.MAX_COMPLETION_TOKENS in p_req:
            p_req[OpenAIField.MAX_COMPLETION_TOKENS] = 1
        if set_min_tokens:
            p_req[OpenAIField.MIN_TOKENS] = 1
        p_req.pop(OpenAIField.STREAM_OPTIONS, None)
        return p_req

    def _forward_request_id(self, req_data: dict) -> str:
        dispatch_data = req_data.get(MOTOR_DISPATCH_KEY)
        if isinstance(dispatch_data, dict):
            engine_request_id = dispatch_data.get("engine_request_id")
            if isinstance(engine_request_id, str) and engine_request_id:
                return engine_request_id
        return self.req_info.req_id

    @contextlib.contextmanager
    def _trace_span(self, span_name: str, is_stream: bool) -> Iterator[Any]:
        trace_obj = self.req_info.trace_obj
        with TracerManager().tracer.start_as_current_span(span_name, context=trace_obj.parent_context) as span:
            if is_stream:
                trace_obj.set_time_start()
            trace_obj.span = span
            trace_obj.trace_headers = TracerManager().inject_trace_context()
            trace_obj.set_trace_attribute("requestId", self.req_info.req_id)
            trace_obj.set_trace_attribute("stream", is_stream)
            if trace_obj.error_message:
                trace_obj.set_trace_error_message(trace_obj.error_message)
            yield span

    @abstractmethod
    async def handle_request(self) -> StreamingResponse | JSONResponse:
        pass

    @contextlib.asynccontextmanager
    async def _manage_request_context(self):
        """
        Lifecycle management for request in the RequestManager.
        Ensures request info is added and cleaned up.
        """
        await self._request_manager.add_req_info(self.req_info)
        try:
            yield
        finally:
            await self._request_manager.del_req_info(self.req_info.req_id)
            self._log_request_details()

    @contextlib.asynccontextmanager
    async def _manage_client_context(self, resource: ScheduledResource):
        endpoint = resource.endpoint
        t0_client = time.perf_counter()
        client_pool = HTTPClientPool()
        client = await client_pool.get_client(
            ip=endpoint.ip, port=endpoint.business_port, tls_config=self.config.infer_tls_config
        )
        elapsed_client_ms = (time.perf_counter() - t0_client) * 1000
        self.logger.debug(
            "Scheduling latency stage=get_http_client elapsed_ms=%.2f endpoint=%s:%s",
            elapsed_client_ms,
            endpoint.ip,
            endpoint.business_port,
        )
        yield client

    @contextlib.asynccontextmanager
    async def _manage_resource_context(self, role: PDRole, release_func):
        resource: ScheduledResource | None = None
        trace_obj = self.req_info.trace_obj
        try:
            trace_obj.add_trace_event("Begin Scheduled Resource", is_meta=self.is_meta)
            resource = await self.prepare_resource(role)
            attributes = {
                "instance": f"{resource.instance.id}-{resource.instance.role}",
                "endpoint": f"{resource.endpoint.id}-{resource.endpoint.ip}:{resource.endpoint.business_port}",
            }
            trace_obj.add_trace_event("Scheduled Resource ok", attributes=attributes, is_meta=self.is_meta)
            yield resource
        finally:
            if resource:
                if asyncio.iscoroutinefunction(release_func):
                    with CancelScope(shield=True):
                        result = await release_func(resource)
                else:
                    result = release_func(resource)
                if not result:
                    self.logger.debug(
                        "release_func(%s) returned False instance_id=%s endpoint_id=%s state=%s",
                        role.name,
                        resource.instance.id,
                        resource.endpoint.id,
                        self.req_info.state,
                    )

    async def prepare_resource(self, role: PDRole) -> ScheduledResource:
        """Select instance + allocate workload (one RPC), record in RequestManager, retry on failure."""
        self.req_info.update_state(_scheduling_state_for_role(role))

        target_instance_id = None
        constraint = self.req_info.scheduling_constraint
        if constraint is not None:
            target_instance_id = constraint.target_for_role(role)

        last_exception = None
        t0_prepare = time.perf_counter()
        for attempt in range(self.config.exception_config.max_retry):
            try:
                t0_select = time.perf_counter()
                result = await self._scheduler.select_and_allocate(
                    role,
                    self.req_info,
                    target_instance_id=target_instance_id,
                )
                elapsed_select_ms = (time.perf_counter() - t0_select) * 1000
                if _should_log_scheduling_sample(self.req_info.req_id):
                    self.logger.info(
                        "Scheduling latency role=%s stage=select_and_allocate elapsed_ms=%.2f attempt=%d/%d",
                        role,
                        elapsed_select_ms,
                        attempt + 1,
                        self.config.exception_config.max_retry,
                    )

                if result is None:
                    msg = f"No instance available for role {role} or allocate failed"
                    raise ValueError(msg)

                ins, endpoint, allocate_workload = result
                if not ins or not endpoint:
                    msg = f"Invalid scheduler result: {result}"
                    raise ValueError(msg)

                if not await self._request_manager.add_req_workload(self.req_info.req_id, role, allocate_workload):
                    await self._rollback_allocated_workload(
                        ins,
                        endpoint,
                        role,
                        allocate_workload,
                    )
                    msg = f"Request {self.req_info.req_id} already allocated for role {role}"
                    raise RuntimeError(msg)

                self.req_info.update_state(_allocated_state_for_role(role))

                elapsed_prepare_ms = (time.perf_counter() - t0_prepare) * 1000
                if _should_log_scheduling_sample(self.req_info.req_id):
                    self.logger.info(
                        "Scheduling role=%s allocated instance_id=%s endpoint_id=%s "
                        "job=%s endpoint=%s:%s total_ms=%.2f",
                        role,
                        ins.id,
                        endpoint.id,
                        ins.job_name,
                        endpoint.ip,
                        endpoint.business_port,
                        elapsed_prepare_ms,
                    )
                self.logger.debug(
                    "Dispatch api=%s len=%d endpoint_status=%s model=%s",
                    self.req_info.api,
                    self.req_info.req_len,
                    endpoint.status,
                    ins.model_name,
                )
                return ScheduledResource(instance=ins, endpoint=endpoint)

            except Exception as e:
                last_exception = e
                exc_info_flag = attempt == 0
                self.logger.warning(
                    "Scheduling attempt %d/%d failed for role %s: %s",
                    attempt + 1,
                    self.config.exception_config.max_retry,
                    role,
                    e,
                    exc_info=exc_info_flag,
                )

                if attempt < self.config.exception_config.max_retry - 1:
                    await asyncio.sleep(0.1)
                    continue

        self.req_info.update_state(ReqState.EXCEPTION)
        error_detail = f"Scheduling failed after {self.config.exception_config.max_retry} attempts, role: {role}"
        if last_exception:
            error_detail += f", last error: {str(last_exception)}"
        trace_obj = self.req_info.trace_obj
        trace_obj.set_trace_error_message(error_detail, is_meta=self.is_meta)

        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=error_detail)

    async def _rollback_allocated_workload(
        self,
        instance: Instance,
        endpoint: Endpoint,
        role: PDRole,
        allocate_workload: Workload,
    ) -> bool:
        """Undo a scheduler allocation if local request workload bookkeeping fails."""
        rollback_workload = Workload(
            active_kv_cache=-allocate_workload.active_kv_cache,
            active_tokens=-allocate_workload.active_tokens,
        )
        params = UpdateWorkloadParams(
            instance_id=instance.id,
            endpoint_id=endpoint.id,
            role=role,
            req_id=self.req_info.req_id,
            workload_action=WorkloadAction.RELEASE_TOKENS,
            workload_change=rollback_workload,
        )
        with CancelScope(shield=True):
            success = await self._scheduler.update_workload(params)
        if not success:
            self.logger.warning(
                "Failed to rollback allocated workload instance_id=%s endpoint_id=%s role=%s",
                instance.id,
                endpoint.id,
                role,
            )
        return success

    async def forward_stream_request(
        self,
        api: str,
        req_data: dict,
        client: httpx.AsyncClient,
        timeout: int,
        *,
        on_response_ready: Callable[[], None] | None = None,
    ) -> AsyncGenerator[str, None]:
        trace_obj = self.req_info.trace_obj
        headers = {'Content-Type': 'application/json', 'X-Request-Id': self._forward_request_id(req_data)}
        trace_obj.set_trace_attribute("server.path", api, self.is_meta)
        headers.update(trace_obj.get_trace_headers_dict(self.is_meta))

        filtered_headers = filter_sensitive_headers(headers)
        safe_body_structure = build_safe_body_structure(req_data)
        self.logger.debug(
            "Forward stream request base_url: %s, api: %s, headers: %s, body: %s, timeout: %s",
            client.base_url,
            api,
            filtered_headers,
            safe_body_structure,
            timeout,
        )

        self.first_chunk_sent = False
        trace_obj.add_trace_event(f"Begin to stream: {client.base_url}/{api}, {client.timeout}", is_meta=self.is_meta)
        t0_forward = time.perf_counter()
        async with client.stream("POST", f"/{api}", json=req_data, headers=headers, timeout=timeout) as response:
            trace_obj.add_trace_event(f"Stream ok: {response.status_code}", is_meta=self.is_meta)
            elapsed_to_connect_ms = (time.perf_counter() - t0_forward) * 1000
            if _should_log_scheduling_sample(self.req_info.req_id):
                self.logger.info(
                    "Scheduling latency stage=forward_to_engine_connect elapsed_ms=%.2f api=%s",
                    elapsed_to_connect_ms,
                    api,
                )
            if not response.is_success:
                error_body, body_truncated = await self._read_bounded_error_body(response)
                upstream_error = UpstreamHTTPError.from_response(
                    response,
                    body=error_body,
                    phase="stream",
                    truncated=body_truncated,
                )
                trace_obj.set_trace_error_message(str(upstream_error), is_meta=self.is_meta)
                raise upstream_error
            if on_response_ready is not None:
                on_response_ready()
            count_token = 0
            pending = b""
            async for chunk in response.aiter_bytes():
                if not self.first_chunk_sent and chunk:
                    self.first_chunk_sent = True
                    trace_obj.set_time_first_token()
                    elapsed_first_chunk_ms = (time.perf_counter() - t0_forward) * 1000
                    if _should_log_scheduling_sample(self.req_info.req_id):
                        self.logger.info(
                            "Scheduling latency stage=forward_to_engine_first_chunk elapsed_ms=%.2f api=%s",
                            elapsed_first_chunk_ms,
                            self.req_info.api,
                        )
                    self.req_info.update_state(ReqState.FIRST_TOKEN_FINISH)
                else:
                    count_token += 1
                pending += chunk
                while True:
                    split_idx = pending.find(b"\n\n")
                    delim_len = 2
                    split_idx_crlf = pending.find(b"\r\n\r\n")
                    if split_idx_crlf != -1 and (split_idx == -1 or split_idx_crlf < split_idx):
                        split_idx = split_idx_crlf
                        delim_len = 4
                    if split_idx == -1:
                        break
                    frame_end = split_idx + delim_len
                    frame = pending[:frame_end]
                    pending = pending[frame_end:]
                    yield frame
            if pending:
                # Keep backward compatibility for non-SSE upstream responses.
                yield pending
            trace_obj.set_count_token(count_token)

    async def forward_request(
        self, api: str, req_data: dict, client: httpx.AsyncClient, timeout: int
    ) -> httpx.Response:
        """Forward non-streaming request to the given resource

        Args:
            req_data: The request data to forward
            client: The client to scheduled endpoint

        Returns:
            The response from the endpoint
        """
        trace_obj = self.req_info.trace_obj
        headers = {'Content-Type': 'application/json', 'X-Request-Id': self._forward_request_id(req_data)}
        trace_obj.set_trace_attribute("server.path", api, self.is_meta)
        headers.update(trace_obj.get_trace_headers_dict(self.is_meta))

        filtered_headers = filter_sensitive_headers(headers)
        filtered_body = build_safe_body_structure(req_data)
        self.logger.debug(
            "Forward request base_url: %s, api: %s, headers: %s, body: %s, timeout: %s",
            client.base_url,
            api,
            filtered_headers,
            filtered_body,
            timeout,
        )

        trace_obj.add_trace_event(f"Begin to post: {client.base_url}/{api}, {client.timeout}", is_meta=self.is_meta)
        t0_forward = time.perf_counter()
        url = f"/{api}"
        async with self._open_nonstream_response(
            client,
            url,
            req_data=req_data,
            headers=headers,
            timeout=timeout,
        ) as (response, streamed):
            if not response.is_success:
                if streamed:
                    error_body, body_truncated = await self._read_bounded_error_body(response)
                else:
                    # Compatibility for lightweight internal/test clients that only
                    # implement post(). Production httpx clients use the bounded path.
                    limit = max(self.config.exception_config.upstream_error_body_max_bytes, 0)
                    full_body = response.content
                    error_body = full_body[:limit]
                    body_truncated = len(full_body) > limit
                upstream_error = UpstreamHTTPError.from_response(
                    response,
                    body=error_body,
                    phase="non-stream",
                    truncated=body_truncated,
                )
                trace_obj.set_trace_error_message(str(upstream_error), is_meta=self.is_meta)
                raise upstream_error
            # Callers parse the returned response after this context exits, so cache
            # the complete successful body before the connection is closed.
            if streamed:
                await response.aread()

        trace_obj.add_trace_event(f"Post ok: {response.status_code}", is_meta=self.is_meta)
        elapsed_forward_ms = (time.perf_counter() - t0_forward) * 1000
        if _should_log_scheduling_sample(self.req_info.req_id):
            self.logger.info(
                "Scheduling latency stage=forward_to_engine elapsed_ms=%.2f api=%s",
                elapsed_forward_ms,
                api,
            )
        return response

    @contextlib.asynccontextmanager
    async def _open_nonstream_response(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        req_data: dict,
        headers: dict[str, str],
        timeout: int,
    ):
        if isinstance(client, httpx.AsyncClient):
            async with client.stream("POST", url, json=req_data, headers=headers, timeout=timeout) as response:
                yield response, True
            return

        response = await client.post(url, json=req_data, headers=headers, timeout=timeout)
        try:
            yield response, False
        finally:
            await response.aclose()

    async def _read_bounded_error_body(self, response: httpx.Response) -> tuple[bytes, bool]:
        """Read at most ``upstream_error_body_max_bytes`` of a streamed error body.

        Returns ``(body, truncated)``; ``truncated`` is True when the engine's error body
        exceeded the cap, so the caller can avoid forwarding a body cut mid-payload.
        """
        limit = max(self.config.exception_config.upstream_error_body_max_bytes, 0)
        probe_limit = limit + 1
        body = bytearray()
        chunk_size = min(8192, probe_limit)
        if isinstance(response, httpx.Response):
            chunks = response.aiter_bytes(chunk_size=chunk_size)
        else:
            # Compatibility for lightweight response doubles used by router tests.
            chunks = response.aiter_bytes()
        async for chunk in chunks:
            if not chunk:
                continue
            remaining = probe_limit - len(body)
            body.extend(chunk[:remaining])
            if len(body) >= probe_limit:
                return bytes(body[:limit]), True
        return bytes(body), False

    def _infer_base_url_for_resource(self, resource: ScheduledResource) -> str:
        scheme = "https" if self.config.infer_tls_config.enable_tls else "http"
        ep = resource.endpoint
        return f"{scheme}://{format_address(ep.ip, ep.business_port)}"

    async def release_all(self, resource: ScheduledResource):
        """Release tokens and KV cache; returns True only if both succeed."""
        tokens_result = await self._update_workload(resource, WorkloadAction.RELEASE_TOKENS)
        kv_result = await self._update_workload(resource, WorkloadAction.RELEASE_KV)
        return tokens_result and kv_result

    async def release_tokens(self, resource: ScheduledResource):
        return await self._update_workload(resource, WorkloadAction.RELEASE_TOKENS)

    async def release_kv(self, resource: ScheduledResource):
        return await self._update_workload(resource, WorkloadAction.RELEASE_KV)

    async def do_encode(self):
        if not await self._check_can_encode():
            return
        trace_obj = self.req_info.trace_obj
        headers = trace_obj.get_trace_headers_dict(self.is_meta)
        trace_context = TracerManager().extract_trace_context(headers)
        with TracerManager().tracer.start_as_current_span("CDP_Encode", context=trace_context) as span:
            self.is_meta = True
            trace_obj.meta_span = span
            trace_obj.meta_trace_headers = TracerManager().inject_trace_context()
            trace_obj.set_trace_attribute("requestId", self.req_info.req_id, is_meta=True)

            req_data = self.req_info.req_data.copy()
            max_retry = self.config.exception_config.transport_retry_limit
            for attempt in range(max_retry):
                req_data[OpenAIField.STREAM] = False
                req_data[OpenAIField.MAX_TOKENS] = 1
                req_data[OpenAIField.MIN_TOKENS] = 1
                if OpenAIField.MAX_COMPLETION_TOKENS in req_data:
                    req_data[OpenAIField.MAX_COMPLETION_TOKENS] = 1
                if OpenAIField.STREAM_OPTIONS in req_data:
                    del req_data[OpenAIField.STREAM_OPTIONS]

                try:
                    async with (
                        self._manage_resource_context(PDRole.ROLE_E, self.release_tokens) as resource,
                        self._manage_client_context(resource) as client,
                    ):
                        cancel_scope = CancelScope()
                        self.req_info.set_cancel_scope(cancel_scope, PDRole.ROLE_E)
                        with cancel_scope:
                            await self.forward_request(
                                self.req_info.api, req_data, client, self.config.exception_config.infer_timeout
                            )
                            break
                except asyncio.CancelledError:
                    self.logger.info(
                        "The non streaming request was terminated because of infer timeout or client disconnect."
                    )
                    self.req_info.cancel_scope()
                    raise
                except HTTPException:
                    self.req_info.cancel_scope()
                    raise
                except Exception as e:
                    self.logger.error(f"Post Decode error: {e}")
                    self.req_info.cancel_scope()
                    trace_obj.set_trace_error_message(f"Post Decode error: {e}", is_meta=self.is_meta)
                    trace_obj.set_trace_exception(e)

                    if attempt < max_retry - 1:
                        wait_time = self.config.exception_config.retry_delay * (2**attempt)
                        self.logger.info("Retrying non-streaming request in %.2f seconds...", wait_time)
                        await asyncio.sleep(wait_time)
                        continue

                    self.req_info.update_state(ReqState.EXCEPTION)
                    raise e

    async def _check_can_encode(self) -> bool:
        messages = self.req_info.req_data.get("messages")
        if not messages:
            return False
        is_multimodal = False
        for msg in messages:
            if not isinstance(msg.get("content"), list):
                continue

            for content_item in msg["content"]:
                content_type = content_item.get("type")
                if not content_type:
                    continue

                if content_type in {"image_url", "video_url"}:
                    is_multimodal = True
                    break

        if not is_multimodal:
            return False

        instance_readiness = await self._scheduler.has_required_instances()
        if instance_readiness not in {
            InstanceReadiness.REQUIRED_MET_EPD,
            InstanceReadiness.ENCODE_PREFILL,
        }:
            return False

        return True

    async def _update_workload(self, resource: ScheduledResource, action: WorkloadAction):
        """Update the given resource's workload.
        Delegates to WorkloadActionHandler to compute workload_change, update RequestManager, then call Scheduler.
        """
        workload_change, role = await self._workload_action_handler.compute_and_update(
            resource,
            self.req_info.req_id,
            action,
            self.req_info,
        )
        if workload_change is None or role is None:
            return False
        params = UpdateWorkloadParams(
            instance_id=resource.instance.id,
            endpoint_id=resource.endpoint.id,
            role=resource.instance.role,
            req_id=self.req_info.req_id,
            workload_action=action,
            workload_change=workload_change,
        )
        # Release RPC must finish even if the request/stream task is cancelled (e.g. client disconnect).
        with CancelScope(shield=True):
            return await self._scheduler.update_workload(params)

    async def _submit_token_sample(
        self,
        p_instance_id: int | None,
        d_instance_id: int,
        request_info: dict,
        decode_resource: ScheduledResource | None = None,
    ) -> None:
        if self._sampling_manager is None:
            return
        try:
            d_url = ""
            if decode_resource is not None:
                d_url = self._infer_base_url_for_resource(decode_resource)
            req_data = self.req_info.req_data
            request_structure = json.dumps(build_safe_body_structure(req_data), ensure_ascii=False)
            sample = build_decode_sample(
                p_instance_id,
                d_instance_id,
                request_info,
                self.req_info.req_id,
                model=req_data.get("model", "") or "",
                d_infer_base_url=d_url,
                trace_headers=self.req_info.trace_obj.get_trace_headers_dict(),
                request_structure=request_structure,
            )
            _log_sample_submission(sample)
            await self._sampling_manager.submit_sample(sample)
        except Exception as e:
            self.logger.warning("_submit_token_sample failed: %s", e)

    def _log_request_details(self):
        current_time = time.time()
        cost_time = current_time - self.req_info.status[ReqState.ARRIVE]
        self.logger.debug(
            "API: %s, Length: %d, State: %s, Cost Time: %s, All status Time: %s",
            self.req_info.api,
            self.req_info.req_len,
            self.req_info.state,
            cost_time,
            self.req_info.status,
        )
