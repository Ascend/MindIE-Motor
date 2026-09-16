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
from motor.coordinator.models.response import ErrorResponse
from motor.coordinator.domain import ScheduledResource
from motor.coordinator.models.request import RequestInfo, ReqState
from motor.coordinator.domain import SchedulingFacade, UpdateWorkloadParams
from motor.coordinator.router.precision_sample.sample_builder import (
    build_decode_sample,
    _log_sample_submission,
)
from motor.coordinator.router.precision_sample import response as sampling_resp
from motor.coordinator.router.adapters.stream import (
    parse_stream_chunk_json,
    encode_stream_chunk_bytes,
    stream_chunk_needs_sampling_parse,
    update_token_id_cache,
)
from motor.common.resources.instance import Instance
from motor.common.resources.endpoint import Endpoint, Workload
from motor.coordinator.domain.request_manager import RequestManager
from motor.coordinator.router.workload import WorkloadActionHandler
from motor.coordinator.router.upstream_error import UpstreamHTTPError
from motor.coordinator.tracer.tracing import TracerManager
from motor.coordinator.domain.scheduling import InstanceReadiness
from motor.coordinator.scheduler.runtime.scheduler_client import resolve_program_id

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
        if reason in {cancel_error.CLIENT_DISCONNECT, cancel_error.DISPATCH_ABORT, cancel_error.INFER_TIMEOUT}:
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
    """Per-request recompute counters and flags for P/D routers."""

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
        self._forward_resource: ScheduledResource | None = None
        self._sched_to_p_logged = False
        self._program_target_instance_id: int | None = None
        self._program_target_endpoint_id: int | None = None

    def _stream_overall_timeout(self) -> float:
        """Remaining infer_timeout budget for the streaming response, counted from request arrival.

        Streaming responses are served by uvicorn after the handler returns, so the
        ``timeout_handler`` decorator cannot bound them; the budget is passed to
        CommitAwareStreamingResponse and enforced as an overall wall-clock deadline.
        """
        infer_timeout = self.config.exception_config.infer_timeout
        elapsed = time.time() - self.req_info.status.get(ReqState.ARRIVE, time.time())
        return max(infer_timeout - elapsed, 0.0)

    def _log_sched_to_p_if_needed(self) -> None:
        """Full-INFO T_sched→P end mark, immediately before the first HTTP POST to P/U."""
        resource = self._forward_resource
        if self._sched_to_p_logged or resource is None or resource.instance is None:
            return
        role = resource.instance.role
        if role not in (PDRole.ROLE_P, PDRole.ROLE_U):
            return
        self._sched_to_p_logged = True
        now = time.time()
        arrive = self.req_info.status.get(ReqState.ARRIVE, now)
        self.logger.info(
            "Scheduling metric stage=dispatch_to_p req_id=%s unix_ts=%.6f elapsed_ms=%.2f role=%s",
            self.req_info.req_id,
            now,
            (now - arrive) * 1000.0,
            getattr(role, "value", role),
        )

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
        for field in ("request_id", "rid"):
            engine_request_id = req_data.get(field)
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
            route_degradation = getattr(trace_obj, "route_degradation", "")
            if route_degradation:
                trace_obj.set_trace_attribute("routing.degradation", route_degradation)
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
            with CancelScope(shield=True):
                await self._reclaim_residual_workloads()
                await self._request_manager.del_req_info(self.req_info.req_id)
            self._log_request_details()

    async def _drain_pending_releases(self) -> None:
        """Hook: routers that run background release tasks drain them before residual reclaim."""
        return

    async def _reclaim_residual_workloads(self) -> None:
        """Release scheduler-ledger entries whose local records survived the whole request.

        Must run after all in-flight releases settle (see _drain_pending_releases), because a
        record is only removed by finalize_release after the ledger ACKed the release; popping
        a record while its release RPC is still in flight would double-subtract the ledger.
        """
        try:
            await self._drain_pending_releases()
        except Exception as e:
            self.logger.error(
                "Draining pending releases failed; skip residual workload reclaim to avoid "
                "double release req_id=%s error=%r",
                self.req_info.req_id,
                e,
            )
            return
        residuals = await self._request_manager.pop_residual_workloads(self.req_info.req_id)
        for key, workload, owner in residuals:
            if owner is None:
                self.logger.error(
                    "Orphan workload record has no owner; cannot reclaim ledger tokens "
                    "req_id=%s key=%s active_tokens=%s",
                    self.req_info.req_id,
                    key,
                    workload.active_tokens,
                )
                continue
            instance_id, endpoint_id = owner
            self.logger.error(
                "Reclaiming orphan workload allocation req_id=%s key=%s instance_id=%s endpoint_id=%s active_tokens=%s",
                self.req_info.req_id,
                key,
                instance_id,
                endpoint_id,
                workload.active_tokens,
            )
            params = UpdateWorkloadParams(
                instance_id=instance_id,
                endpoint_id=endpoint_id,
                role=key[-1],
                req_id=self.req_info.req_id,
                workload_action=WorkloadAction.RELEASE_TOKENS,
                workload_change=Workload(active_tokens=-workload.active_tokens),
            )
            try:
                ok = await self._scheduler.update_workload(params)
            except Exception as e:
                self.logger.error(
                    "Reclaim release RPC raised req_id=%s instance_id=%s endpoint_id=%s error=%r",
                    self.req_info.req_id,
                    instance_id,
                    endpoint_id,
                    e,
                )
                continue
            if not ok:
                self.logger.error(
                    "Reclaim release rejected by scheduler req_id=%s instance_id=%s endpoint_id=%s",
                    self.req_info.req_id,
                    instance_id,
                    endpoint_id,
                )

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
        previous = self._forward_resource
        self._forward_resource = resource
        try:
            yield client
        finally:
            self._forward_resource = previous

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

    async def prepare_resource(
        self,
        role: PDRole,
        *,
        target_instance_id: int | None = None,
        required_engine_type: str | None = None,
        required_dispatch_capability: str | None = None,
    ) -> ScheduledResource:
        """Select instance + allocate workload (one RPC), record in RequestManager, retry on failure."""
        self.req_info.update_state(_scheduling_state_for_role(role))

        program_admission = await self._admit_program_if_needed(role)
        if program_admission == "rejected":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Program admission queue timeout",
            )
        program_admitted = program_admission == "admitted"
        program_target_instance_id = getattr(self, "_program_target_instance_id", None)
        program_target_endpoint_id = getattr(self, "_program_target_endpoint_id", None)
        constraint = self.req_info.scheduling_constraint
        if target_instance_id is None and constraint is not None:
            target_instance_id = constraint.target_for_role(role)
        elif target_instance_id is None and program_target_instance_id is not None:
            target_instance_id = program_target_instance_id

        last_exception = None
        t0_prepare = time.perf_counter()
        for attempt in range(self.config.exception_config.max_retry):
            try:
                t0_select = time.perf_counter()
                scheduler_kwargs = {"target_instance_id": target_instance_id}
                if constraint is None and program_target_endpoint_id is not None:
                    scheduler_kwargs["target_endpoint_id"] = program_target_endpoint_id
                if required_engine_type is not None:
                    scheduler_kwargs["required_engine_type"] = required_engine_type
                if required_dispatch_capability is not None:
                    scheduler_kwargs["required_dispatch_capability"] = required_dispatch_capability
                result = await self._scheduler.select_and_allocate(role, self.req_info, **scheduler_kwargs)
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

                try:
                    recorded = await self._request_manager.add_req_workload(
                        self.req_info.req_id,
                        role,
                        allocate_workload,
                        instance_id=ins.id,
                        endpoint_id=endpoint.id,
                    )
                except BaseException as e:
                    # The ledger commit inside select_and_allocate already succeeded; if local
                    # bookkeeping is interrupted (incl. CancelledError, which is not an
                    # Exception subclass), roll the commit back before propagating.
                    self.logger.error(
                        "Workload bookkeeping interrupted after allocation; rolling back "
                        "req_id=%s role=%s instance_id=%s endpoint_id=%s error=%r",
                        self.req_info.req_id,
                        role,
                        ins.id,
                        endpoint.id,
                        e,
                    )
                    await self._rollback_allocated_workload(ins, endpoint, role, allocate_workload)
                    raise
                if not recorded:
                    await self._rollback_allocated_workload(ins, endpoint, role, allocate_workload)
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
        if program_admitted:
            await self._cancel_program_admission()
        error_detail = f"Scheduling failed after {self.config.exception_config.max_retry} attempts, role: {role}"
        if last_exception:
            error_detail += f", last error: {str(last_exception)}"
        trace_obj = self.req_info.trace_obj
        trace_obj.set_trace_error_message(error_detail, is_meta=self.is_meta)

        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=error_detail)

    def _program_identity(self) -> tuple[str | None, str | None]:
        """Resolve RFC-style task/agent identity with session fallback."""
        hint = getattr(self.req_info, "agent_hint_info", None)
        session_id = resolve_program_id(self.req_info)
        parent_id = getattr(hint, "parent_program_id", None) or getattr(hint, "parent_session_id", None)
        return session_id, parent_id

    async def _admit_program_if_needed(self, role: PDRole) -> str:
        """Gate P/Union forwarding while preserving fail-open compatibility."""
        self._program_target_instance_id = None
        self._program_target_endpoint_id = None
        # CPCD/Handoff owns decode-side KV transfer.  Coordinator admission is
        # intentionally scoped to the P/Union leg; D is allocated at handoff.
        if role not in (PDRole.ROLE_P, PDRole.ROLE_U):
            return "bypass"
        progress_ttl = getattr(self.config.scheduler_config, "progress_ttl", None)
        if not progress_ttl or not getattr(progress_ttl, "enabled", False):
            return "bypass"
        program_id, parent_program_id = self._program_identity()
        admit = getattr(self._scheduler, "admit_program", None)
        poll = getattr(self._scheduler, "poll_program", None)
        if not program_id or not callable(admit) or not callable(poll):
            return "bypass"
        token_ids = getattr(self.req_info, "token_ids", None)
        prompt_tokens = len(token_ids) if isinstance(token_ids, list) else 0
        req_data = self.req_info.req_data or {}
        max_output_tokens = int(req_data.get("max_completion_tokens", req_data.get("max_tokens", 0)) or 0)
        shared_prefix_tokens = int(req_data.get("cached_tokens", 0) or 0)
        capacity_total = int(getattr(progress_ttl, "fallback_total_kv_tokens", 0))
        native_used = 0
        native_waiting = 0
        program_target_instance_id = None
        program_target_endpoint_id = None
        capacity_probe = getattr(self._scheduler, "get_program_capacity", None)
        if callable(capacity_probe):
            capacity_result = await capacity_probe(role, self.req_info)
            if len(capacity_result) == 5:
                capacity_total, native_used, native_waiting, program_target_instance_id, program_target_endpoint_id = (
                    capacity_result
                )
            elif len(capacity_result) == 4:
                capacity_total, native_used, native_waiting, program_target_instance_id = capacity_result
            elif len(capacity_result) == 3:
                capacity_total, native_used, native_waiting = capacity_result
            else:
                capacity_total, native_used = capacity_result
        self._program_target_instance_id = program_target_instance_id
        self._program_target_endpoint_id = program_target_endpoint_id
        wait_started = time.monotonic()
        wait_deadline = wait_started + max(0.0, self.config.exception_config.first_token_timeout - 1.0)
        last_poll_log = wait_started
        self.logger.info(
            "ProgressTTL request_start request_id=%s program_id=%s parent_program_id=%s hint_session_id=%s hint_parent_session_id=%s role=%s prompt_tokens=%d max_output_tokens=%d shared_prefix_tokens=%d capacity_kv=%d native_used=%d native_waiting=%d target=%s-%s",
            self.req_info.req_id,
            program_id,
            parent_program_id,
            getattr(self.req_info.agent_hint_info, "session_id", None),
            getattr(self.req_info.agent_hint_info, "parent_session_id", None),
            role,
            prompt_tokens,
            max_output_tokens,
            shared_prefix_tokens,
            capacity_total,
            native_used,
            native_waiting,
            program_target_instance_id,
            program_target_endpoint_id,
        )
        try:
            response = await admit(
                request_id=self.req_info.req_id,
                program_id=program_id,
                parent_program_id=parent_program_id,
                prompt_tokens=prompt_tokens,
                max_output_tokens=max_output_tokens,
                shared_prefix_tokens=max(0, shared_prefix_tokens),
                capacity_total_kv_tokens=capacity_total,
                native_used_kv_tokens=native_used,
                native_waiting_kv_tokens=native_waiting,
                target_instance_id=program_target_instance_id,
                target_endpoint_id=program_target_endpoint_id,
            )
            while response.get("disposition") == "queued":
                if time.monotonic() >= wait_deadline:
                    await self._cancel_program_admission()
                    self.logger.info(
                        "ProgressTTL request_timeout request_id=%s program_id=%s waited_ms=%.2f disposition=queued",
                        self.req_info.req_id,
                        program_id,
                        (time.monotonic() - wait_started) * 1000,
                    )
                    return "rejected"
                await asyncio.sleep(0.1)
                if callable(capacity_probe):
                    capacity_result = await capacity_probe(role, self.req_info)
                    if len(capacity_result) == 5:
                        (
                            capacity_total,
                            native_used,
                            native_waiting,
                            program_target_instance_id,
                            program_target_endpoint_id,
                        ) = capacity_result
                    elif len(capacity_result) == 4:
                        capacity_total, native_used, native_waiting, program_target_instance_id = capacity_result
                    elif len(capacity_result) == 3:
                        capacity_total, native_used, native_waiting = capacity_result
                    elif len(capacity_result) == 2:
                        capacity_total, native_used = capacity_result
                self._program_target_instance_id = program_target_instance_id
                self._program_target_endpoint_id = program_target_endpoint_id
                response = await poll(
                    request_id=self.req_info.req_id,
                    capacity_total_kv_tokens=capacity_total,
                    native_used_kv_tokens=native_used,
                    native_waiting_kv_tokens=native_waiting,
                    target_instance_id=program_target_instance_id,
                    target_endpoint_id=program_target_endpoint_id,
                )
                now = time.monotonic()
                if response.get("disposition") != "queued" or now - last_poll_log >= 1.0:
                    self.logger.info(
                        "ProgressTTL request_poll request_id=%s program_id=%s disposition=%s waited_ms=%.2f capacity_kv=%d native_used=%d native_waiting=%d target=%s-%s",
                        self.req_info.req_id,
                        program_id,
                        response.get("disposition"),
                        (now - wait_started) * 1000,
                        capacity_total,
                        native_used,
                        native_waiting,
                        program_target_instance_id,
                        program_target_endpoint_id,
                    )
                    last_poll_log = now
        except asyncio.CancelledError:
            await self._cancel_program_admission()
            raise
        if response.get("disposition") == "admitted":
            self.logger.info(
                "ProgressTTL request_admitted request_id=%s program_id=%s", self.req_info.req_id, program_id
            )
            return "admitted"
        if response.get("disposition") == "bypass":
            return "bypass"
        return "rejected"

    async def _cancel_program_admission(self) -> None:
        """Best-effort cleanup when endpoint allocation cannot be completed."""
        cancel = getattr(self._scheduler, "cancel_program", None)
        if callable(cancel):
            with CancelScope(shield=True):
                await cancel(self.req_info.req_id)

    async def _complete_program(self) -> None:
        """Publish response completion after the endpoint workload is released."""
        complete = getattr(self._scheduler, "complete_program", None)
        program_id, _ = self._program_identity()
        if not program_id or not callable(complete):
            return
        token_ids = getattr(self.req_info, "cached_token_ids", None)
        prompt_ids = getattr(self.req_info, "prompt_token_ids", None)
        usage_prompt = getattr(self.req_info, "usage_prompt_tokens", None)
        usage_completion = getattr(self.req_info, "usage_completion_tokens", None)
        prompt_tokens = usage_prompt if usage_prompt is not None else len(prompt_ids or [])
        completion_tokens = usage_completion if usage_completion is not None else len(token_ids or [])
        accounting_origin = "usage" if usage_prompt is not None or usage_completion is not None else "token_ids"
        if completion_tokens == 0 and getattr(self.req_info.trace_obj, "count_token", 0) > 0:
            completion_tokens = self.req_info.trace_obj.count_token
            accounting_origin = "other"
        total_tokens = prompt_tokens + completion_tokens
        self.logger.info(
            "ProgressTTL completion_usage request_id=%s prompt_tokens=%d completion_tokens=%d total_tokens=%d source=%s usage_prompt=%s usage_completion=%s token_ids_prompt=%d token_ids_completion=%d",
            self.req_info.req_id,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            accounting_origin,
            usage_prompt,
            usage_completion,
            len(prompt_ids or []),
            len(token_ids or []),
        )
        with CancelScope(shield=True):
            await complete(
                self.req_info.req_id,
                total_tokens,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

    async def _rollback_allocated_workload(
        self,
        instance: Instance,
        endpoint: Endpoint,
        role: PDRole,
        allocate_workload: Workload,
    ) -> bool:
        """Undo a scheduler allocation if local request workload bookkeeping fails."""
        rollback_workload = Workload(
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

    def _build_request_timeout(self, timeout: int) -> httpx.Timeout:
        """Bound the TCP connect phase without touching the overall request timeout.

        ``timeout`` (first_token_timeout / infer_timeout) keeps governing
        read/write/pool exactly as before; only the connect phase gets an
        independent bounded limit. Without this, a blackholed engine address
        (pod deleted, IP released) fails only after the kernel SYN budget
        (~63s): the circuit breaker re-trips and the hybrid fallback engages
        far beyond the client timeout, so requests die instead of degrading.
        ``connect_timeout <= 0`` keeps the historical single-value behavior.
        """
        connect_timeout = self.config.exception_config.connect_timeout
        if connect_timeout > 0:
            return httpx.Timeout(timeout=timeout, connect=connect_timeout)
        return httpx.Timeout(timeout=timeout)

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
        self._log_sched_to_p_if_needed()
        t0_forward = time.perf_counter()
        async with client.stream(
            "POST",
            f"/{api}",
            json=req_data,
            headers=headers,
            timeout=self._build_request_timeout(timeout),
        ) as response:
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
                    self._capture_usage_from_chunk(frame)
                    yield frame
            if pending:
                # Keep backward compatibility for non-SSE upstream responses.
                self._capture_usage_from_chunk(pending)
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
        self._log_sched_to_p_if_needed()
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
        # Non-streaming responses carry usage in the completed JSON body.  Capture
        # it here while the response is still available so Progress-TTL completion
        # accounting does not depend on precision-sampling token-id fields.
        try:
            body = response.json()
        except (ValueError, TypeError, json.JSONDecodeError):
            body = None
        if isinstance(body, dict):
            self.req_info.update_usage(body)
        elapsed_forward_ms = (time.perf_counter() - t0_forward) * 1000
        if _should_log_scheduling_sample(self.req_info.req_id):
            self.logger.info(
                "Scheduling latency stage=forward_to_engine elapsed_ms=%.2f api=%s",
                elapsed_forward_ms,
                api,
            )
        return response

    def _capture_usage_from_chunk(self, chunk: bytes) -> None:
        """Capture usage fields from a response frame without requiring precision sampling."""
        if not chunk or b"usage" not in chunk:
            return
        parsed = parse_stream_chunk_json(chunk, self.logger)
        # Anthropic SSE frames include an ``event:`` line before ``data:``;
        # parse_stream_chunk_json intentionally handles the OpenAI single-line
        # form, so extract the data payload for the multi-line form as well.
        if parsed is None:
            for line in chunk.splitlines():
                if line.startswith(b"data:"):
                    parsed = parse_stream_chunk_json(line, self.logger)
                    if parsed is not None:
                        break
        if isinstance(parsed, dict):
            self.req_info.update_usage(parsed)

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
            async with client.stream(
                "POST",
                url,
                json=req_data,
                headers=headers,
                timeout=self._build_request_timeout(timeout),
            ) as response:
                yield response, True
            return

        response = await client.post(
            url,
            json=req_data,
            headers=headers,
            timeout=self._build_request_timeout(timeout),
        )
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
        """Release compute ledger (active_tokens)."""
        result = await self._update_workload(resource, WorkloadAction.RELEASE_TOKENS)
        if result and resource.instance.role in (PDRole.ROLE_P, PDRole.ROLE_U, "prefill", "union", "both"):
            await self._complete_program()
        return result

    async def release_tokens(self, resource: ScheduledResource):
        return await self._update_workload(resource, WorkloadAction.RELEASE_TOKENS)

    async def do_encode(self):
        if not await self._check_can_encode():
            return
        trace_obj = self.req_info.trace_obj
        headers = trace_obj.get_trace_headers_dict(self.is_meta)
        trace_context = TracerManager().extract_trace_context(headers)
        with TracerManager().tracer.start_as_current_span("PD_Encode", context=trace_context) as span:
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
        # Shield covers finalize_release too: it is the only gate against re-sending this release,
        # so it must not be interrupted by the same cancellation that shields update_workload.
        with CancelScope(shield=True):
            ok = await self._scheduler.update_workload(params)
            if ok and action == WorkloadAction.RELEASE_TOKENS:
                try:
                    await self._workload_action_handler.finalize_release(self.req_info.req_id, role)
                except Exception as exc:
                    # Scheduler already applied the release; keep the ACK as success regardless.
                    self.logger.warning(
                        "finalize_release failed after scheduler ACK req_id=%s action=%s: %s",
                        self.req_info.req_id,
                        action.value,
                        exc,
                    )
        return ok

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

    def _init_sampling_state(self) -> dict:
        return {
            "enabled": self.config.precision_detection_config.precision_check_enabled,
            "client_logprobs": bool(self.req_info.req_data.get("logprobs")),
            "lp_count": self.config.precision_detection_config.logprobs_count,
            "info": {},
        }

    def _collect_logprobs_from_stream_chunk(self, chunk: bytes, sampling_state: dict) -> bytes:
        if not sampling_state["enabled"] or not chunk:
            return chunk
        if not stream_chunk_needs_sampling_parse(chunk):
            return chunk
        chunk_json = parse_stream_chunk_json(chunk, self.logger)
        if chunk_json is None:
            return chunk
        update_token_id_cache(sampling_state["info"], chunk_json)
        sampling_resp.update_logprob_cache(
            sampling_state["info"],
            chunk_json,
            logprobs_count=sampling_state["lp_count"],
        )
        has_logprobs_field = any(isinstance(ch, dict) and "logprobs" in ch for ch in chunk_json.get("choices") or [])
        sampling_resp.strip_logprobs_for_client(
            chunk_json,
            client_requested_logprobs=sampling_state["client_logprobs"],
        )
        if not sampling_state["client_logprobs"] and not has_logprobs_field:
            return chunk
        if sampling_state["client_logprobs"]:
            return chunk
        return encode_stream_chunk_bytes(chunk, chunk_json)

    def _collect_logprobs_from_nonstream_body(self, body: dict, sampling_state: dict) -> dict:
        if not sampling_state["enabled"]:
            return body
        info = sampling_state["info"]
        update_token_id_cache(info, body)
        sampling_resp.update_logprob_cache(info, body, logprobs_count=sampling_state["lp_count"])
        return body

    def _strip_logprobs_for_client(self, body: dict, sampling_state: dict) -> None:
        if not sampling_state["enabled"]:
            return
        sampling_resp.strip_logprobs_for_client(
            body,
            client_requested_logprobs=sampling_state["client_logprobs"],
        )

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
