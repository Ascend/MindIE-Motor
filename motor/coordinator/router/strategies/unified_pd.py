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
import time
from contextlib import aclosing
from dataclasses import dataclass, replace
from typing import Any, AsyncGenerator

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, Response

import motor.common.utils.error as cancel_error
from motor.common.utils.error import RequestCancelledError
from motor.common.utils.env import Env
from motor.common.utils.net import format_address, is_unspecified_host
from motor.common.resources.endpoint import WorkloadAction
from motor.common.resources.dispatch import DispatchPlan
from motor.common.resources.instance import PDRole
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.domain import (
    ScheduledResource,
    SchedulingFacade,
    UpdateWorkloadParams,
)
from motor.coordinator.domain.request_manager import RequestManager
from motor.coordinator.models.request import RequestInfo, ReqState
from motor.coordinator.router.dispatch_session import (
    AttemptContext,
    AttemptState,
    AttemptStopReason,
    PDDispatchSession,
)
from motor.coordinator.router.strategies.base import BaseRouter, check_cancel_error
from motor.coordinator.router.strategies.pd_hybrid import PDHybridRouter
from motor.coordinator.router.rescheduler.rescheduler import (
    Rescheduler,
    RetryRequestPlan,
)
from motor.coordinator.router.workload import WorkloadActionHandler
from motor.coordinator.router.precision_sample.request import inject_logprobs
from motor.coordinator.router.adapters.completion_to_chat import (
    adapt_completion_nonstream_to_chat,
    is_completion_like_body,
)
from motor.coordinator.router.adapters.pd_protocol import (
    ADAPTERS,
    CoordinationMode,
    EngineEndpointMetadata,
    EngineProtocolError,
    EngineRequest,
    LegContext,
    PDProtocolAdapter,
    PrefillMetadata,
    VllmProtocolAdapter,
)
from motor.coordinator.router.adapters.stream import (
    parse_stream_chunk_json,
    encode_stream_chunk_bytes,
    strip_nonstream_response_body_for_client,
    strip_stream_chunk_bytes_for_client,
)
from motor.coordinator.router.stream_response import (
    CommitAwareStreamingResponse,
    StreamCommitController,
)
from motor.coordinator.router.upstream_error import (
    UpstreamHTTPError,
    is_cb_reportable_failure,
    is_retryable_upstream_error,
)

_SGLANG_PROTOCOL_ADAPTER = ADAPTERS["sglang"]
_TRIGGER_CAPABILITY = DispatchPlan.CONCURRENT_ENGINE_SYNC.value
_HANDOFF_CAPABILITY = DispatchPlan.PREFILL_HANDOFF_DECODE.value


@dataclass(frozen=True)
class ReleaseWorkItem:
    stage: str
    params: UpdateWorkloadParams
    attempt_seq: int
    role: PDRole
    action: WorkloadAction
    attempt: AttemptContext | None = None


@dataclass(frozen=True)
class ReleaseTaskContext:
    stage: str
    req_id: str
    attempt_seq: int
    instance_id: int
    endpoint_id: int
    role: PDRole
    action: WorkloadAction


ReleaseKey = tuple[str, int, PDRole, WorkloadAction]


@dataclass
class _ReleaseTaskRecord:
    """Bookkeeping for a single in-flight background release task.

    Consolidates what used to be four parallel dicts keyed by the task: the dedup
    key, the logging context, and the work item (populated once the release payload
    has been computed inside the task).
    """

    key: ReleaseKey
    context: ReleaseTaskContext
    item: ReleaseWorkItem | None = None


class UnifiedPDRouter(BaseRouter):
    """Native-engine P/D router.

    Coordinator owns lifecycle orchestration and uses stateless protocol adapters
    for native-engine request mapping.
    """

    # Background workload-release RPC retry policy.
    _RELEASE_RPC_ATTEMPTS = 3
    _RELEASE_RPC_BACKOFF_BASE_S = 0.05

    def __init__(
        self,
        req_info: RequestInfo,
        config: CoordinatorConfig,
        scheduler: SchedulingFacade,
        request_manager: RequestManager,
        workload_action_handler: WorkloadActionHandler | None = None,
        sampling_manager=None,
    ):
        super().__init__(
            req_info,
            config,
            scheduler,
            request_manager,
            workload_action_handler,
            sampling_manager=sampling_manager,
        )
        self.rescheduler = Rescheduler(
            config.exception_config.reschedule_enabled,
            req_info,
            self.logger,
        )
        self._stream_commit_controller: StreamCommitController | None = None
        self._stream_body_sent = False
        self._active_retry_plan: RetryRequestPlan | None = None
        self._hybrid_stream_fallback_attempted = False  # A request is only allowed to fallback once.
        self._pd_uses_trigger: bool | None = None
        # Task -> its bookkeeping record (dedup key, logging context, computed work item).
        self._release_records: dict[asyncio.Task[bool], _ReleaseTaskRecord] = {}
        # Dedup reverse index: release key -> the single in-flight task for that key.
        self._release_inflight: dict[ReleaseKey, asyncio.Task[bool]] = {}

    def _capture_prompt_tokens_details(self, body: dict[str, Any]) -> None:
        candidates = [body]
        payload = body.get("payload")
        if isinstance(payload, dict):
            candidates.append(payload)

        for candidate in candidates:
            usage = candidate.get("usage")
            if not isinstance(usage, dict) or "prompt_tokens_details" not in usage:
                continue
            details = usage["prompt_tokens_details"]
            if details is None:
                details = {"cached_tokens": 0}
            self.req_info.update_prompt_tokens_details(details)
            return

    def _record_prefill_complete(self, body: dict[str, Any]) -> None:
        self._capture_prompt_tokens_details(body)
        self.req_info.update_state(ReqState.PREFILL_END)

    def _record_bootstrap_prefill_response(
        self,
        attempt: AttemptContext,
        response: httpx.Response,
    ) -> None:
        if not self._is_native_sglang_attempt(attempt):
            self._record_prefill_complete(response.json())
            return

        try:
            body = response.json()
        except ValueError:
            # SGLang's native gateway forwards the client's stream flag to both
            # legs. A streaming P response is internal: consume its SSE frames,
            # retain usage metadata when present, and never forward P chunks.
            for line in response.content.splitlines():
                frame_body = parse_stream_chunk_json(line, self.logger)
                if frame_body is not None:
                    self._capture_prompt_tokens_details(frame_body)
            self.req_info.update_state(ReqState.PREFILL_END)
            return

        self._record_prefill_complete(body)

    def _merge_prompt_tokens_details(self, body: dict[str, Any]) -> bool:
        details = self.req_info.prompt_tokens_details
        usage = body.get("usage")
        if not details or not isinstance(usage, dict) or not usage:
            return False
        if usage.get("prompt_tokens_details") == details:
            return False
        usage["prompt_tokens_details"] = details
        return True

    def _merge_prompt_tokens_details_into_stream_chunk(self, chunk: bytes) -> bytes:
        if not self.req_info.prompt_tokens_details:
            return chunk
        # Only usage-bearing chunks (typically just the final include_usage chunk) can carry
        # prompt_tokens_details; skip the JSON parse for the vast majority that have no usage.
        if b"usage" not in chunk:
            return chunk
        chunk_json = parse_stream_chunk_json(chunk, self.logger)
        if chunk_json is None or not self._merge_prompt_tokens_details(chunk_json):
            return chunk
        return encode_stream_chunk_bytes(chunk, chunk_json)

    @staticmethod
    def _is_native_sglang_attempt(attempt: AttemptContext | None) -> bool:
        return UnifiedPDRouter._adapter_for_attempt(attempt) is _SGLANG_PROTOCOL_ADAPTER

    @staticmethod
    def _adapter_for_attempt(attempt: AttemptContext | None) -> PDProtocolAdapter | None:
        if attempt is None:
            return None
        resource = attempt.prefill_resource or attempt.decode_resource
        if resource is None:
            return None
        engine_type = getattr(resource.instance, "engine_type", None)
        if not isinstance(engine_type, str):
            return None
        return ADAPTERS.get(engine_type.strip().lower())

    @staticmethod
    def _require_adapter_for_attempt(attempt: AttemptContext) -> PDProtocolAdapter:
        adapter = UnifiedPDRouter._adapter_for_attempt(attempt)
        if adapter is not None:
            return adapter
        resource = attempt.prefill_resource or attempt.decode_resource
        engine_type = getattr(resource.instance, "engine_type", None) if resource is not None else None
        raise RuntimeError(f"Unsupported native engine type for P/D coordination: {engine_type!r}")

    @staticmethod
    def _strip_native_internal_fields_from_body(body: dict[str, Any], attempt: AttemptContext) -> None:
        adapter = UnifiedPDRouter._adapter_for_attempt(attempt)
        if adapter is None:
            return
        for field in adapter.internal_response_fields:
            body.pop(field, None)

    def _strip_native_internal_fields_from_stream_chunk(
        self,
        chunk: bytes,
        attempt: AttemptContext,
    ) -> bytes:
        adapter = self._adapter_for_attempt(attempt)
        if adapter is None:
            return chunk
        fields = adapter.internal_response_fields
        if not any(f'"{field}"'.encode() in chunk for field in fields):
            return chunk
        chunk_json = parse_stream_chunk_json(chunk, self.logger)
        if chunk_json is None:
            self.logger.warning("Dropping invalid native stream chunk containing internal protocol fields")
            return b""
        mutated = False
        for field in fields:
            if field in chunk_json:
                chunk_json.pop(field)
                mutated = True
        return encode_stream_chunk_bytes(chunk, chunk_json) if mutated else chunk

    async def handle_request(self) -> Response:
        await self.do_encode()
        self.is_meta = False
        uses_trigger = await self._pd_cluster_uses_trigger()
        if uses_trigger:
            self._ensure_trigger_metaserver()
        if self.req_info.req_data.get("stream", False):
            commit_parts = {"decode"} if uses_trigger else {"prefill", "decode"}
            self._stream_commit_controller = StreamCommitController.requiring(commit_parts)
            return CommitAwareStreamingResponse(
                self._generate_stream_response(),
                self._stream_commit_controller,
                on_first_body_sent=self._mark_stream_body_sent,
            )
        return await self._generate_response()

    def _mark_stream_body_sent(self) -> None:
        self._stream_body_sent = True

    def _stream_retry_allowed(self) -> bool:
        if not self._stream_body_sent:
            return True
        return self.rescheduler.can_resume_after_visible_output(self.req_info.req_data)

    def _build_hybrid_fallback_router(self) -> PDHybridRouter:
        return PDHybridRouter(
            self.req_info,
            self.config,
            scheduler=self._scheduler,
            request_manager=self._request_manager,
            workload_action_handler=self._workload_action_handler,
            sampling_manager=self._sampling_manager,
        )

    async def _hybrid_fallback_feasible(self) -> bool:
        """True when decode pool is exhausted (no unblocked D) but a hybrid candidate exists.

        Shared by the non-stream / stream-restart / stream-resume fallback gates so the
        ``get_unblocked_instances`` availability probe stays in one place.
        """
        if not self.config.scheduler_config.enable_pd_separation_fallback_to_hybrid:
            return False
        get_unblocked = getattr(self._scheduler, "get_unblocked_instances", None)
        if get_unblocked is None:
            return False
        if await get_unblocked(PDRole.ROLE_D):
            return False
        unblocked_u = await get_unblocked(PDRole.ROLE_U)
        unblocked_p = await get_unblocked(PDRole.ROLE_P)
        return bool(unblocked_u or unblocked_p)

    async def _should_use_hybrid_fallback_nonstream(self) -> bool:
        if self.req_info.req_data.get("stream", False):
            return False
        return await self._hybrid_fallback_feasible()

    async def _should_use_hybrid_fallback_stream_restart(self) -> bool:
        if not self.req_info.req_data.get("stream", False):
            return False
        if self._hybrid_stream_fallback_attempted:
            return False
        if self._stream_body_sent:
            return False
        if self._stream_commit_controller is None or self._stream_commit_controller.commit_sealed:
            return False
        return await self._hybrid_fallback_feasible()

    async def _should_use_hybrid_fallback_stream_resume(self) -> bool:
        """Post-commit continuation gate: visible output already sent, decode pool gone.

        Requires a replayable token cache (prompt + generated ids) so a single hybrid
        instance can continue generation on the same SSE stream without repeating output.
        """
        if not self.req_info.req_data.get("stream", False):
            return False
        if self._hybrid_stream_fallback_attempted:
            return False
        if not self._stream_body_sent:
            return False
        if not self.rescheduler.can_resume_after_visible_output(self.req_info.req_data):
            return False
        return await self._hybrid_fallback_feasible()

    async def _run_hybrid_fallback_nonstream(self) -> JSONResponse:
        self.logger.warning("UnifiedPD fallback to PDHybrid for non-stream request: decode unavailable")
        self.req_info.trace_obj.set_trace_error_message(
            "UnifiedPD fallback to PDHybrid(non-stream): decode unavailable"
        )
        hybrid = self._build_hybrid_fallback_router()
        response = await hybrid.handle_request(manage_request_context=False)
        if not isinstance(response, JSONResponse):
            raise RuntimeError("Hybrid fallback expected non-stream JSON response")
        return response

    async def _run_hybrid_fallback_stream_restart(
        self,
        attempt: AttemptContext | None,
        attempt_index: int,
    ) -> AsyncGenerator[str, None]:
        if self._stream_commit_controller is None:
            raise RuntimeError("Stream fallback requires stream commit controller")
        fallback_attempt_id = (attempt.attempt_seq + 1) if attempt is not None else (attempt_index + 1)
        self._hybrid_stream_fallback_attempted = True
        self._stream_commit_controller.begin_attempt(fallback_attempt_id)
        self.logger.warning(
            "UnifiedPD stream fallback to PDHybrid before commit, fallback_attempt=%d",
            fallback_attempt_id,
        )
        self.req_info.trace_obj.set_trace_error_message(
            "UnifiedPD fallback to PDHybrid(stream restart): decode unavailable"
        )

        def _mark_unified_ready() -> None:
            self._stream_commit_controller.mark_ready("prefill", fallback_attempt_id)
            self._stream_commit_controller.mark_ready("decode", fallback_attempt_id)

        hybrid = self._build_hybrid_fallback_router()
        async with aclosing(
            hybrid.stream_fallback_from_existing_context(
                req_data=self.req_info.req_data.copy(),
                attempt_id=fallback_attempt_id,
                mark_unified_ready=_mark_unified_ready,
            )
        ) as fallback_stream:
            async for chunk in fallback_stream:
                yield chunk

    async def _run_hybrid_fallback_stream_resume(
        self,
        attempt: AttemptContext | None,
        attempt_index: int,
    ) -> AsyncGenerator[str, None]:
        """Continue a committed stream on one hybrid instance via token replay.

        The HTTP response is already committed (tokens were sent), so the commit
        controller is left untouched; the replay body carries prompt + generated
        token ids so the hybrid leg resumes generation instead of repeating it.
        """
        fallback_attempt_id = (attempt.attempt_seq + 1) if attempt is not None else (attempt_index + 1)
        self._hybrid_stream_fallback_attempted = True
        self.rescheduler.retry_count = max(attempt_index, 1)
        retry_plan = self.rescheduler.build_retry_plan(self.req_info.req_data)
        if retry_plan is None:
            raise RuntimeError("Hybrid stream resume requires cached token ids for replay")
        replay_req, replay_api = self.rescheduler.apply_retry_plan(
            self.req_info.req_data.copy(),
            retry_plan,
        )
        self.logger.warning(
            "UnifiedPD stream fallback to PDHybrid after commit (token replay), "
            "fallback_attempt=%d replay_len=%d api=%s",
            fallback_attempt_id,
            len(retry_plan.prompt_token_ids),
            replay_api,
        )
        self.req_info.trace_obj.set_trace_error_message(
            "UnifiedPD fallback to PDHybrid(stream resume): decode unavailable"
        )
        hybrid = self._build_hybrid_fallback_router()
        async with aclosing(
            hybrid.stream_fallback_from_existing_context(
                req_data=replay_req,
                attempt_id=fallback_attempt_id,
                api=replay_api,
                is_resume=True,
            )
        ) as fallback_stream:
            async for chunk in fallback_stream:
                yield chunk

    async def _generate_stream_response(self) -> AsyncGenerator[str, None]:
        trace_obj = self.req_info.trace_obj
        with self._trace_span("UnifiedPD_Stream", True):
            max_retry = max(self.config.exception_config.transport_retry_limit, 1)
            session = PDDispatchSession(self.req_info.req_id)

            async with self._manage_request_context():
                for attempt_index in range(max_retry):
                    attempt: AttemptContext | None = None
                    cleanup_reason = AttemptStopReason.OTHER
                    try:
                        self._active_retry_plan = None
                        if attempt_index > 0:
                            self.rescheduler.is_rescheduling = True
                            self.rescheduler.retry_count = attempt_index
                            self._active_retry_plan = self.rescheduler.build_retry_plan(self.req_info.req_data)

                        attempt = await self._create_attempt(session)
                        if not self._stream_commit_controller.commit_sealed:
                            self._stream_commit_controller.begin_attempt(attempt.attempt_seq)
                        attempt.register_canceller()
                        coordination_mode = self._select_coordination_mode(attempt)
                        if attempt_index > 0:
                            self.logger.warning(
                                "Rescheduling %d/%d: P=[%s], D=[%s]",
                                attempt_index,
                                max_retry,
                                self._resource_label(attempt.prefill_resource),
                                self._resource_label(attempt.decode_resource),
                            )
                        attempt.transition(AttemptState.DISPATCHING)
                        async with aclosing(self._run_stream_attempt(attempt, coordination_mode)) as attempt_stream:
                            async for chunk in attempt_stream:
                                attempt.transition(AttemptState.FIRST_VISIBLE)
                                yield chunk
                        await self._release_attempt(attempt, wait=False)
                        try:
                            await self._drain_release_tasks()
                        except asyncio.CancelledError:
                            self.logger.warning(
                                "Unified PD stream cancelled while draining release tasks req_id=%s attempt=%s",
                                self.req_info.req_id,
                                attempt.attempt_seq,
                            )
                        attempt.transition(AttemptState.DONE)
                        self.logger.info(trace_obj.set_end_and_ttft_tpot())
                        return
                    except GeneratorExit:
                        cleanup_reason = AttemptStopReason.CLIENT_DISCONNECT
                        raise
                    except (asyncio.CancelledError, Exception) as e:
                        error, retry = await self._process_response_error(
                            attempt,
                            attempt_index,
                            e,
                            allow_retry=self._stream_retry_allowed(),
                        )
                        if await self._should_use_hybrid_fallback_stream_restart():
                            async with aclosing(
                                self._run_hybrid_fallback_stream_restart(attempt, attempt_index)
                            ) as fallback_stream:
                                async for chunk in fallback_stream:
                                    yield chunk
                            return
                        if await self._should_use_hybrid_fallback_stream_resume():
                            async with aclosing(
                                self._run_hybrid_fallback_stream_resume(attempt, attempt_index)
                            ) as fallback_stream:
                                async for chunk in fallback_stream:
                                    yield chunk
                            self.logger.info(trace_obj.set_end_and_ttft_tpot())
                            return
                        if not retry:
                            raise error
                    finally:
                        if attempt is not None:
                            attempt.unregister_canceller()
                            if attempt.state not in (
                                AttemptState.DONE,
                                AttemptState.STOPPED,
                            ):
                                await self._stop_attempt(attempt, cleanup_reason)

    async def _generate_response(self) -> JSONResponse:
        trace_obj = self.req_info.trace_obj
        with self._trace_span("UnifiedPD", False):
            max_retry = max(self.config.exception_config.transport_retry_limit, 1)
            session = PDDispatchSession(self.req_info.req_id)

            async with self._manage_request_context():
                for attempt_index in range(max_retry):
                    attempt: AttemptContext | None = None
                    try:
                        attempt = await self._create_attempt(session)
                        attempt.register_canceller()
                        coordination_mode = self._select_coordination_mode(attempt)
                        if attempt_index > 0:
                            self.rescheduler.retry_count = attempt_index
                            self.logger.warning(
                                "Rescheduling %d/%d: P=[%s], D=[%s]",
                                attempt_index,
                                max_retry,
                                self._resource_label(attempt.prefill_resource),
                                self._resource_label(attempt.decode_resource),
                            )
                        attempt.transition(AttemptState.DISPATCHING)
                        body = await self._run_nonstream_attempt(attempt, coordination_mode)
                        attempt.unregister_canceller()
                        attempt.transition(AttemptState.DONE)
                        self._merge_prompt_tokens_details(body)
                        if "chat" in self.req_info.effective_entry_api() and is_completion_like_body(body):
                            adapt_completion_nonstream_to_chat(body, req_id=self.req_info.req_id)
                        self._strip_native_internal_fields_from_body(body, attempt)
                        strip_nonstream_response_body_for_client(
                            body,
                            client_return_token_ids=self.req_info.client_expects_token_ids,
                        )
                        return JSONResponse(content=body)
                    except (asyncio.CancelledError, Exception) as e:
                        error, retry = await self._process_response_error(attempt, attempt_index, e)
                        if await self._should_use_hybrid_fallback_nonstream():
                            return await self._run_hybrid_fallback_nonstream()
                        if not retry:
                            raise error

        error_message = "Unified PD request ended without response"
        trace_obj.set_trace_prompt(self.req_info.req_data)
        trace_obj.set_trace_error_message(error_message)
        raise RuntimeError(error_message)

    async def _process_response_error(
        self,
        attempt: AttemptContext | None,
        attempt_index: int,
        error: Exception | asyncio.CancelledError,
        *,
        allow_retry: bool = True,
    ) -> (Exception, bool):
        trace_obj = self.req_info.trace_obj
        trace_obj.set_trace_prompt(self.req_info.req_data)
        trace_obj.set_trace_exception(error)
        max_retry = max(self.config.exception_config.transport_retry_limit, 1)

        if isinstance(error, asyncio.CancelledError):
            reason_str, retry = check_cancel_error(error)
            reason = self._cancel_stop_reason(reason_str)
            retry = retry and allow_retry and (attempt_index < max_retry - 1)
            error = RequestCancelledError(reason_str)
            label = f"Unified PD cancelled {attempt_index}/{max_retry}"
        else:
            reason = AttemptStopReason.PEER_FAILED
            reason_str = str(error)
            retry = allow_retry and attempt_index < max_retry - 1
            if isinstance(error, HTTPException):
                retry = False
            elif isinstance(error, (UpstreamHTTPError, httpx.RequestError)):
                retry = retry and is_retryable_upstream_error(error)
            label = f"Unified PD exception {attempt_index}/{max_retry}"

        if attempt:
            error_msg = str(
                f"{label}: P=[{self._resource_label(attempt.prefill_resource)}] "
                f"D=[{self._resource_label(attempt.decode_resource)}] because of "
                f"{reason_str}, {retry=}"
            )
            trace_obj.set_trace_error_message(error_msg)
            self.logger.warning("%s", error_msg)
            await self._stop_attempt(attempt, reason)
        else:
            error_msg = str(f"{label}, because of {reason_str}, {retry=}")
            trace_obj.set_trace_error_message(error_msg)
            self.logger.warning("%s", error_msg)

        if retry:
            await asyncio.sleep(self.config.exception_config.retry_delay * (2**attempt_index))
        else:
            self.req_info.update_state(ReqState.EXCEPTION)
        return error, retry

    @staticmethod
    def _cancel_stop_reason(reason: str) -> AttemptStopReason:
        if reason.startswith(cancel_error.NODE_FAULT):
            return AttemptStopReason.PEER_FAILED
        if reason == cancel_error.CLIENT_DISCONNECT:
            return AttemptStopReason.CLIENT_DISCONNECT
        return AttemptStopReason.OTHER

    @staticmethod
    def _resource_label(resource: ScheduledResource | None) -> str:
        if resource is None:
            return "pending"
        return f"{resource.endpoint.ip} {resource.instance.job_name}"

    async def _create_attempt(self, session: PDDispatchSession) -> AttemptContext:
        attempt_seq = session._attempt_seq + 1
        if await self._pd_cluster_uses_trigger():
            self._ensure_trigger_metaserver()
            d_resource = await self._prepare_attempt_resource(PDRole.ROLE_D, attempt_seq)
            return session.new_attempt(None, d_resource, self.config)

        p_resource = await self._prepare_attempt_resource(PDRole.ROLE_P, attempt_seq)

        # Handoff connectors (CPCD-style) do not need a concrete decode endpoint while prefill runs.
        # Allocate D after prefill completes so long prompts do not reserve stale decode workload for
        # the entire prefill window. Concurrent connectors still allocate both legs up front.
        if self._should_defer_decode_allocation(p_resource):
            return session.new_attempt(p_resource, None, self.config)

        try:
            d_resource = await self._prepare_attempt_resource(
                PDRole.ROLE_D,
                attempt_seq,
                required_engine_type=str(p_resource.instance.engine_type),
            )
        except Exception as e:
            error_message = (
                f"Unified PD D allocation failed after P allocated "
                f"req_id={self.req_info.req_id} attempt={attempt_seq}: {e}"
            )
            self.req_info.trace_obj.set_trace_error_message(error_message)
            self.logger.warning(error_message)
            await self._release_attempt_resource(p_resource, attempt_seq, WorkloadAction.RELEASE_TOKENS)
            raise
        return session.new_attempt(p_resource, d_resource, self.config)

    @staticmethod
    def _should_defer_decode_allocation(
        prefill_resource: ScheduledResource | None,
    ) -> bool:
        if prefill_resource is None:
            return False
        engine_type = getattr(prefill_resource.instance, "engine_type", None)
        if not isinstance(engine_type, str):
            return False
        adapter = ADAPTERS.get(engine_type.strip().lower())
        return adapter is not None and adapter.coordination_mode == CoordinationMode.HANDOFF

    async def _pd_cluster_uses_trigger(self) -> bool:
        if self._pd_uses_trigger is not None:
            return self._pd_uses_trigger
        self._pd_uses_trigger = await self._detect_vllm_trigger_cluster()
        return self._pd_uses_trigger

    async def _detect_vllm_trigger_cluster(self) -> bool:
        p_plans = await self._vllm_dispatch_plans(PDRole.ROLE_P)
        d_plans = await self._vllm_dispatch_plans(PDRole.ROLE_D)
        combined = p_plans | d_plans
        if not combined:
            return False
        if _TRIGGER_CAPABILITY in combined and _HANDOFF_CAPABILITY in combined:
            raise HTTPException(
                status_code=503,
                detail="Mixed vLLM handoff and layerwise/trigger P/D instances are not supported",
            )
        return combined == {_TRIGGER_CAPABILITY}

    async def _vllm_dispatch_plans(self, role: PDRole) -> set[str]:
        get_instances = getattr(self._scheduler, "get_local_instances", None)
        if get_instances is None:
            get_instances = getattr(self._scheduler, "get_available_instances", None)
        if get_instances is None:
            return set()
        get_unblocked = getattr(self._scheduler, "get_unblocked_instances", None)
        unblocked_ids = set(await get_unblocked(role)) if get_unblocked is not None else None
        instances = await get_instances(role)
        plans: set[str] = set()
        for inst in instances.values():
            if unblocked_ids is not None and inst.id not in unblocked_ids:
                continue
            if str(getattr(inst, "engine_type", "") or "").strip().lower() != "vllm":
                continue
            caps = list(getattr(inst, "dispatch_capabilities", None) or [])
            if _TRIGGER_CAPABILITY in caps:
                plans.add(_TRIGGER_CAPABILITY)
            else:
                plans.add(_HANDOFF_CAPABILITY)
        return plans

    def _ensure_trigger_metaserver(self) -> None:
        port = getattr(self.config, "worker_metaserver_port", None)
        if not isinstance(port, int) or isinstance(port, bool) or port <= 0:
            raise HTTPException(
                status_code=503,
                detail=("layerwise/trigger PD requires inference_workers_config.worker_metaserver_base_port > 0"),
            )

    def _trigger_metaserver_url(self, attempt: AttemptContext) -> str:
        host = Env.pod_ip or self.config.api_config.coordinator_api_host
        if is_unspecified_host(host):
            self.logger.error(
                "Trigger metaserver callback host is unreachable: advertised_host=%s. "
                "Wildcard listen addresses (0.0.0.0/::) cannot be used as Decode callback targets; "
                "set POD_IP or a concrete coordinator_api_host",
                host,
            )
            raise HTTPException(
                status_code=503,
                detail="Trigger metaserver requires POD_IP or a concrete coordinator_api_host",
            )
        port = self.config.worker_metaserver_port
        return f"http://{format_address(host, port)}/v1/metaserver?attempt={attempt.attempt_seq}"

    def _bind_trigger_attempt(self, attempt: AttemptContext) -> None:
        self.req_info._trigger_attempt = attempt

    def _trigger_decode_request_for_attempt(self, attempt: AttemptContext) -> tuple[dict[str, Any], str]:
        adapter = self._require_adapter_for_attempt(attempt)
        if not isinstance(adapter, VllmProtocolAdapter):
            raise RuntimeError(f"Trigger decode requires vLLM adapter, got {adapter.engine_type}")
        req, api = self._base_request_for_attempt(PDRole.ROLE_D)
        context = replace(
            self._native_leg_context(attempt, PDRole.ROLE_D, api),
            engine_request_id=self.req_info.req_id,
        )
        engine_request = adapter.build_trigger_decode_request(req, context, self._trigger_metaserver_url(attempt))
        return engine_request.body, engine_request.api

    async def _run_stream_attempt(
        self, attempt: AttemptContext, coordination_mode: CoordinationMode
    ) -> AsyncGenerator[str, None]:
        if coordination_mode == CoordinationMode.HANDOFF:
            run_func = self._run_handoff_stream_attempt
        elif coordination_mode == CoordinationMode.TRIGGER:
            run_func = self._run_trigger_stream_attempt
        else:
            run_func = self._run_bootstrap_stream_attempt
        async with aclosing(run_func(attempt)) as attempt_stream:
            async for chunk in attempt_stream:
                yield chunk
        return

    async def _run_bootstrap_stream_attempt(self, attempt: AttemptContext) -> AsyncGenerator[str, None]:
        attempt.transition(AttemptState.ACTIVE)
        p_req, p_api = self._request_for_attempt(attempt, PDRole.ROLE_P)
        d_req, d_api = self._request_for_attempt(attempt, PDRole.ROLE_D)
        stream_adapter_state = {}
        sampling_state = self._init_sampling_state()
        async with (
            self._client_for(attempt.prefill_resource) as p_client,
            self._client_for(attempt.decode_resource) as d_client,
        ):
            p_instance_id = attempt.prefill_resource.instance.id

            async def prefill_task():
                try:
                    attempt.mark_dispatched(PDRole.ROLE_P.value)
                    response = await self.forward_request(
                        p_api, p_req, p_client, self.config.exception_config.first_token_timeout
                    )
                    attempt.mark_completed(PDRole.ROLE_P.value)
                    self._record_bootstrap_prefill_response(attempt, response)
                    self._stream_commit_controller.mark_ready("prefill", attempt.attempt_seq)
                    await self._scheduler.report_cb_event(p_instance_id, "success")
                except asyncio.CancelledError:  # pylint: disable=try-except-raise
                    raise
                except Exception as e:
                    if isinstance(e, UpstreamHTTPError):
                        attempt.mark_completed(PDRole.ROLE_P.value)
                    if is_cb_reportable_failure(e):
                        await self._scheduler.report_cb_event(p_instance_id, "failure")
                    raise

            p_task = attempt.register_prefill_task(asyncio.create_task(prefill_task()))
            async with aclosing(
                self._run_stream_decode_phase(
                    attempt,
                    d_client,
                    d_api,
                    d_req,
                    stream_adapter_state,
                    sampling_state=sampling_state,
                    prefill_task=p_task,
                    release_prefill_on_stream=True,
                )
            ) as decode_stream:
                async for chunk in decode_stream:
                    yield chunk

    async def _run_stream_decode_phase(
        self,
        attempt: AttemptContext,
        d_client,
        d_api: str,
        d_req: dict[str, Any],
        stream_adapter_state: dict,
        *,
        sampling_state: dict | None = None,
        prefill_task: asyncio.Task | None = None,
        release_prefill_on_stream: bool = False,
    ) -> AsyncGenerator[str, None]:
        queue = asyncio.Queue(maxsize=1)
        terminal = asyncio.get_running_loop().create_future()
        self._start_stream_decode_task(
            attempt,
            queue,
            terminal,
            d_api,
            d_req,
            d_client,
        )
        async with aclosing(
            self._iter_stream_decode_queue(
                attempt,
                queue,
                terminal,
                stream_adapter_state,
                sampling_state=sampling_state,
                prefill_task=prefill_task,
                release_prefill_on_stream=release_prefill_on_stream,
            )
        ) as queue_stream:
            async for chunk in queue_stream:
                yield chunk

    def _start_stream_decode_task(
        self,
        attempt: AttemptContext,
        queue: asyncio.Queue,
        terminal: asyncio.Future,
        d_api: str,
        d_req: dict[str, Any],
        d_client,
    ) -> asyncio.Task:
        d_instance_id = attempt.decode_resource.instance.id

        async def decode_task() -> None:
            try:
                attempt.mark_dispatched(PDRole.ROLE_D.value)
                async for chunk in self.forward_stream_request(
                    d_api,
                    d_req,
                    d_client,
                    self.config.exception_config.infer_timeout,
                    on_response_ready=lambda: self._stream_commit_controller.mark_ready("decode", attempt.attempt_seq),
                ):
                    if chunk:
                        await queue.put(chunk)
                attempt.mark_completed(PDRole.ROLE_D.value)
                if not terminal.done():
                    terminal.set_result(("done", None))
                await self._scheduler.report_cb_event(d_instance_id, "success")
            except asyncio.CancelledError as e:
                if not terminal.done():
                    terminal.set_result(("cancel", e))
            except Exception as e:
                if isinstance(e, UpstreamHTTPError):
                    attempt.mark_completed(PDRole.ROLE_D.value)
                if is_cb_reportable_failure(e):
                    await self._scheduler.report_cb_event(d_instance_id, "failure")
                if not terminal.done():
                    terminal.set_result(("error", e))

        return attempt.register_decode_task(asyncio.create_task(decode_task()))

    async def _iter_stream_decode_queue(
        self,
        attempt: AttemptContext,
        queue: asyncio.Queue,
        terminal: asyncio.Future,
        stream_adapter_state: dict,
        *,
        sampling_state: dict | None = None,
        prefill_task: asyncio.Task | None = None,
        release_prefill_on_stream: bool = False,
    ) -> AsyncGenerator[str, None]:
        queue_task: asyncio.Task | None = None
        try:
            while True:
                # Atomic non-blocking drain: get_nowait() either returns a queued chunk or raises,
                # so there is no empty()-then-get window. Decode chunks put on the queue are always
                # non-None, so None unambiguously means "queue was empty".
                try:
                    queued_chunk = queue.get_nowait()
                except asyncio.QueueEmpty:
                    queued_chunk = None
                if queued_chunk is not None:
                    key, value = "chunk", queued_chunk
                elif terminal.done():
                    key, value = terminal.result()
                else:
                    queue_task = asyncio.create_task(
                        queue.get(),
                        name=f"unified-pd-queue-{self.req_info.req_id}-a{attempt.attempt_seq}",
                    )
                    waitables = [queue_task, terminal]
                    if prefill_task is not None:
                        waitables.append(prefill_task)
                    done, _ = await asyncio.wait(waitables, return_when=asyncio.FIRST_COMPLETED)

                    if prefill_task is not None and prefill_task in done:
                        # Prefill HTTP may only mean "prepared" in trigger mode; do not release
                        # P load here. Wait for the first decode chunk (below).
                        try:
                            await prefill_task
                        except BaseException as error:
                            queue_task.cancel()
                            await asyncio.gather(queue_task, return_exceptions=True)
                            await attempt.cancel(repr(error))
                            raise
                        prefill_task = None

                    if queue_task in done:
                        key, value = "chunk", queue_task.result()
                    else:
                        queue_task.cancel()
                        await asyncio.gather(queue_task, return_exceptions=True)
                        if not queue.empty():
                            continue
                        if terminal in done:
                            key, value = terminal.result()
                        else:
                            continue
                if key == "chunk":
                    if prefill_task is not None:
                        try:
                            await prefill_task
                        except BaseException as error:
                            await attempt.cancel(repr(error))
                            raise
                        prefill_task = None
                    # Concurrent/trigger: release P only after decode proves progress (first chunk).
                    # Earlier P HTTP completion can be a prepared ACK before real prefill/metaserver work.
                    if release_prefill_on_stream:
                        self._submit_prefill_release_background(attempt, WorkloadAction.RELEASE_TOKENS)
                        release_prefill_on_stream = False
                    if sampling_state is not None:
                        value = self._collect_logprobs_from_stream_chunk(value, sampling_state)
                    if self.config.exception_config.reschedule_enabled:
                        # process_stream_chunk already merges prompt_tokens_details into any usage
                        # block while it parses, so the standalone merge would only re-parse every
                        # chunk to find the work already done. Keep the two paths mutually exclusive.
                        value = self.rescheduler.process_stream_chunk(
                            value,
                            stream_adapter_state=stream_adapter_state,
                        )
                    else:
                        value = self._merge_prompt_tokens_details_into_stream_chunk(value)
                        value = strip_stream_chunk_bytes_for_client(
                            value,
                            client_return_token_ids=self.req_info.client_expects_token_ids,
                        )
                    value = self._strip_native_internal_fields_from_stream_chunk(value, attempt)
                    yield value
                elif key == "done":
                    if prefill_task is not None:
                        # Mirror the "chunk" branch: a prefill failure surfacing here (e.g. the P
                        # leg returned a non-JSON body) must abort the decode leg before it
                        # propagates, so both legs are torn down symmetrically.
                        try:
                            await prefill_task
                        except BaseException as error:
                            await attempt.cancel(repr(error))
                            raise
                        prefill_task = None
                    # No decode chunk arrived: do not early-release P here; _release_attempt cleans up.
                    self.req_info.update_state(ReqState.DECODE_END)
                    if sampling_state is not None:
                        await self._maybe_submit_sample(attempt, sampling_state)
                    return
                elif key in {"cancel", "error"}:
                    await attempt.cancel(repr(value))
                    raise value
        finally:
            if queue_task is not None and not queue_task.done():
                queue_task.cancel()
                await asyncio.gather(queue_task, return_exceptions=True)
            if not terminal.done():
                terminal.cancel()

    async def _run_nonstream_attempt(
        self, attempt: AttemptContext, coordination_mode: CoordinationMode
    ) -> dict[str, Any]:
        if coordination_mode == CoordinationMode.HANDOFF:
            return await self._run_handoff_nonstream_attempt(attempt)
        if coordination_mode == CoordinationMode.TRIGGER:
            return await self._run_trigger_nonstream_attempt(attempt)
        return await self._run_bootstrap_nonstream_attempt(attempt)

    async def _run_trigger_stream_attempt(self, attempt: AttemptContext) -> AsyncGenerator[str, None]:
        self._bind_trigger_attempt(attempt)
        attempt.transition(AttemptState.ACTIVE)
        d_req, d_api = self._trigger_decode_request_for_attempt(attempt)
        stream_adapter_state = {}
        sampling_state = self._init_sampling_state()
        async with self._client_for(attempt.decode_resource) as d_client:
            async with aclosing(
                self._run_stream_decode_phase(
                    attempt,
                    d_client,
                    d_api,
                    d_req,
                    stream_adapter_state,
                    sampling_state=sampling_state,
                )
            ) as decode_stream:
                async for chunk in decode_stream:
                    yield chunk

    async def _run_trigger_nonstream_attempt(self, attempt: AttemptContext) -> dict[str, Any]:
        self._bind_trigger_attempt(attempt)
        attempt.transition(AttemptState.ACTIVE)
        d_req, d_api = self._trigger_decode_request_for_attempt(attempt)
        sampling_state = self._init_sampling_state()
        async with self._client_for(attempt.decode_resource) as d_client:
            return await self._await_nonstream_decode(
                attempt,
                d_api,
                d_req,
                d_client,
                sampling_state=sampling_state,
            )

    async def handle_metaserver_request(self, kv_transfer_params: dict[str, Any]) -> dict[str, Any]:
        """Forward Decode's layerwise metaserver callback to a scheduled Prefill instance."""
        t0_metaserver = time.perf_counter()
        attempt = self.req_info._trigger_attempt
        if not isinstance(attempt, AttemptContext):
            raise HTTPException(status_code=404, detail="Trigger attempt not found")
        async with attempt.trigger_lock:
            if self.req_info.is_cancelled:
                raise HTTPException(status_code=409, detail="Request already cancelled")
            if attempt.state in (AttemptState.STOPPING, AttemptState.STOPPED, AttemptState.DONE):
                raise HTTPException(status_code=409, detail="Trigger attempt is no longer active")
            if attempt.decode_resource is None:
                raise HTTPException(status_code=409, detail="Trigger decode resource is missing")
            if attempt.prefill_completed:
                return {}
            if attempt.prefill_dispatched:
                raise HTTPException(status_code=409, detail="Prefill dispatch did not complete")

            adapter = self._require_adapter_for_attempt(attempt)
            if not isinstance(adapter, VllmProtocolAdapter):
                raise HTTPException(status_code=500, detail="Trigger metaserver requires vLLM")

            current_task = asyncio.current_task()
            if current_task is None:
                raise RuntimeError("Metaserver callback must run in an asyncio task")
            attempt.register_prefill_task(current_task)
            p_instance_id = None
            try:
                if attempt.prefill_resource is None:
                    attempt.prefill_resource = await self._prepare_attempt_resource(
                        PDRole.ROLE_P,
                        attempt.attempt_seq,
                        required_engine_type=str(attempt.decode_resource.instance.engine_type),
                    )
                if self.req_info.is_cancelled or attempt.state in (
                    AttemptState.STOPPING,
                    AttemptState.STOPPED,
                    AttemptState.DONE,
                ):
                    raise HTTPException(status_code=409, detail="Trigger attempt is no longer active")

                req, api = self._base_request_for_attempt(PDRole.ROLE_P)
                context = replace(
                    self._native_leg_context(attempt, PDRole.ROLE_P, api),
                    engine_request_id=self.req_info.req_id,
                )
                engine_request = adapter.build_trigger_prefill_request(req, context, kv_transfer_params)
                p_instance_id = attempt.prefill_resource.instance.id
                async with self._client_for(attempt.prefill_resource) as p_client:
                    attempt.register_canceller()
                    attempt.mark_dispatched(PDRole.ROLE_P.value)
                    response = await self.forward_request(
                        engine_request.api,
                        engine_request.body,
                        p_client,
                        self.config.exception_config.first_token_timeout,
                    )
                    attempt.mark_completed(PDRole.ROLE_P.value)
                    body = response.json()
                    self._record_prefill_complete(body)
                    await self._scheduler.report_cb_event(p_instance_id, "success")
                    self._submit_prefill_release_background(attempt, WorkloadAction.RELEASE_TOKENS)
                    elapsed_ms = (time.perf_counter() - t0_metaserver) * 1000
                    self.logger.info(
                        "Scheduling latency stage=metaserver_request_total elapsed_ms=%.2f role=ROLE_P req_id=%s",
                        elapsed_ms,
                        self.req_info.req_id,
                    )
                    return body
            except asyncio.CancelledError:
                self.logger.info("Metaserver request was cancelled req_id=%s", self.req_info.req_id)
                if attempt.state not in (AttemptState.STOPPING, AttemptState.STOPPED, AttemptState.DONE):
                    await self._stop_attempt(attempt, AttemptStopReason.PEER_FAILED)
                raise
            except Exception as e:
                elapsed_ms = (time.perf_counter() - t0_metaserver) * 1000
                self.logger.warning(
                    "Scheduling latency stage=metaserver_request_total elapsed_ms=%.2f error=%s req_id=%s",
                    elapsed_ms,
                    e,
                    self.req_info.req_id,
                )
                if p_instance_id is not None and is_cb_reportable_failure(e):
                    await self._scheduler.report_cb_event(p_instance_id, "failure")
                if isinstance(e, UpstreamHTTPError):
                    attempt.mark_completed(PDRole.ROLE_P.value)
                await self._stop_attempt(attempt, AttemptStopReason.PEER_FAILED)
                raise
            finally:
                if attempt.prefill_task is current_task:
                    attempt.prefill_task = None

    async def _run_bootstrap_nonstream_attempt(self, attempt: AttemptContext) -> dict[str, Any]:
        attempt.transition(AttemptState.ACTIVE)
        p_req, p_api = self._request_for_attempt(attempt, PDRole.ROLE_P)
        d_req, d_api = self._request_for_attempt(attempt, PDRole.ROLE_D)
        sampling_state = self._init_sampling_state()
        async with (
            self._client_for(attempt.prefill_resource) as p_client,
            self._client_for(attempt.decode_resource) as d_client,
        ):
            p_instance_id = attempt.prefill_resource.instance.id

            async def prefill_task():
                try:
                    attempt.mark_dispatched(PDRole.ROLE_P.value)
                    response = await self.forward_request(
                        p_api, p_req, p_client, self.config.exception_config.first_token_timeout
                    )
                    attempt.mark_completed(PDRole.ROLE_P.value)
                    self._record_bootstrap_prefill_response(attempt, response)
                    await self._scheduler.report_cb_event(p_instance_id, "success")
                except asyncio.CancelledError:  # pylint: disable=try-except-raise
                    raise
                except Exception as e:
                    if isinstance(e, UpstreamHTTPError):
                        attempt.mark_completed(PDRole.ROLE_P.value)
                    if is_cb_reportable_failure(e):
                        await self._scheduler.report_cb_event(p_instance_id, "failure")
                    raise

            p_task = attempt.register_prefill_task(asyncio.create_task(prefill_task()))
            return await self._await_nonstream_decode(
                attempt,
                d_api,
                d_req,
                d_client,
                sampling_state=sampling_state,
                prefill_task=p_task,
            )

    async def _await_nonstream_decode(
        self,
        attempt: AttemptContext,
        d_api: str,
        d_req: dict[str, Any],
        d_client,
        *,
        sampling_state: dict | None = None,
        prefill_task: asyncio.Task | None = None,
    ) -> dict[str, Any]:
        d_instance_id = attempt.decode_resource.instance.id

        async def decode_task() -> tuple[Any, Any]:
            try:
                attempt.mark_dispatched(PDRole.ROLE_D.value)
                response = await self.forward_request(
                    d_api, d_req, d_client, self.config.exception_config.infer_timeout
                )
                attempt.mark_completed(PDRole.ROLE_D.value)
                await self._scheduler.report_cb_event(d_instance_id, "success")
                return response.json(), None
            except asyncio.CancelledError:  # pylint: disable=try-except-raise
                raise
            except Exception as e:
                if isinstance(e, UpstreamHTTPError):
                    attempt.mark_completed(PDRole.ROLE_D.value)
                if is_cb_reportable_failure(e):
                    await self._scheduler.report_cb_event(d_instance_id, "failure")
                return None, e

        d_task = attempt.register_decode_task(asyncio.create_task(decode_task()))
        try:
            while True:
                waitables = [d_task]
                if prefill_task is not None:
                    waitables.append(prefill_task)
                done, _ = await asyncio.wait(waitables, return_when=asyncio.FIRST_COMPLETED)
                if prefill_task is not None and prefill_task in done:
                    # Trigger prepared ACK is not real prefill completion; hold P until decode returns.
                    await prefill_task
                    prefill_task = None
                if d_task in done:
                    break

            response, error = await d_task
            if error:
                await attempt.cancel(repr(error))
                await self._release_attempt(attempt, wait=False)
                await self._drain_release_tasks()
                raise error
            if prefill_task is not None:
                await prefill_task
                prefill_task = None
            # Non-stream concurrent: decode HTTP success is the first-progress signal (no chunks).
            self._submit_prefill_release_background(attempt, WorkloadAction.RELEASE_TOKENS)
            self.req_info.update_state(ReqState.DECODE_END)
            if sampling_state is not None:
                response = self._collect_logprobs_from_nonstream_body(response, sampling_state)
                await self._maybe_submit_sample(attempt, sampling_state)
                self._strip_logprobs_for_client(response, sampling_state)
            await self._release_attempt(attempt, wait=False)
            await self._drain_release_tasks()
            return response
        except (asyncio.CancelledError, Exception) as e:
            self.logger.warning(
                "Unified PD non-stream decode failed req_id=%s attempt=%s: %s",
                self.req_info.req_id,
                attempt.attempt_seq,
                e,
            )
            await attempt.cancel(repr(e))
            await self._release_attempt(attempt, wait=False)
            await self._drain_release_tasks()
            raise

    async def _run_handoff_stream_attempt(self, attempt: AttemptContext) -> AsyncGenerator[str, None]:
        attempt.transition(AttemptState.ACTIVE)
        stream_adapter_state = {}
        sampling_state = self._init_sampling_state()
        async with self._client_for(attempt.prefill_resource) as p_client:
            prefill_result = await self._await_handoff_prefill(attempt, p_client)

        self._submit_release_attempt_resource_background(
            attempt.prefill_resource,
            attempt.attempt_seq,
            WorkloadAction.RELEASE_TOKENS,
            attempt,
        )
        late_decode = await self._ensure_handoff_decode_resource(attempt)
        d_req, d_api = self._request_for_attempt(attempt, PDRole.ROLE_D, prefill_result=prefill_result)
        async with self._client_for(attempt.decode_resource) as d_client:
            if late_decode:
                # Client is now in the pool; the canceller can bind to it.
                attempt.register_decode_canceller()
            async with aclosing(
                self._run_stream_decode_phase(
                    attempt,
                    d_client,
                    d_api,
                    d_req,
                    stream_adapter_state,
                    sampling_state=sampling_state,
                )
            ) as decode_stream:
                async for chunk in decode_stream:
                    yield chunk

    async def _run_handoff_nonstream_attempt(self, attempt: AttemptContext) -> dict[str, Any]:
        attempt.transition(AttemptState.ACTIVE)
        sampling_state = self._init_sampling_state()
        async with self._client_for(attempt.prefill_resource) as p_client:
            prefill_result = await self._await_handoff_prefill(attempt, p_client)

        self._submit_release_attempt_resource_background(
            attempt.prefill_resource,
            attempt.attempt_seq,
            WorkloadAction.RELEASE_TOKENS,
            attempt,
        )
        late_decode = await self._ensure_handoff_decode_resource(attempt)
        d_req, d_api = self._request_for_attempt(attempt, PDRole.ROLE_D, prefill_result=prefill_result)
        async with self._client_for(attempt.decode_resource) as d_client:
            if late_decode:
                # Client is now in the pool; the canceller can bind to it.
                attempt.register_decode_canceller()
            return await self._await_nonstream_decode(attempt, d_api, d_req, d_client, sampling_state=sampling_state)

    async def _ensure_handoff_decode_resource(self, attempt: AttemptContext) -> bool:
        """Lazily allocate the decode leg for a handoff attempt.

        Returns True when the decode resource was allocated by this call. In that case the
        caller must register the decode canceller *after* opening the decode client, because
        the canceller can only bind once the client exists in the pool (see the callers).
        """
        if attempt.decode_resource is not None:
            return False
        start = time.perf_counter()
        adapter = self._require_adapter_for_attempt(attempt)
        d_resource = await self._prepare_attempt_resource(
            PDRole.ROLE_D,
            attempt.attempt_seq,
            required_engine_type=adapter.engine_type,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000
        self.logger.info(
            "Scheduling latency stage=late_select_d elapsed_ms=%.2f instance_id=%s endpoint_id=%s req_id=%s",
            elapsed_ms,
            d_resource.instance.id,
            d_resource.endpoint.id,
            self.req_info.req_id,
        )
        attempt.decode_resource = d_resource
        try:
            adapter = self._require_adapter_for_attempt(attempt)
            decode_engine_type = getattr(attempt.decode_resource.instance, "engine_type", None)
            if not isinstance(decode_engine_type, str) or decode_engine_type.strip().lower() != adapter.engine_type:
                raise RuntimeError(f"{adapter.engine_type} handoff requires a matching decode instance")
        except Exception:
            await self._release_attempt_resource(
                d_resource,
                attempt.attempt_seq,
                WorkloadAction.RELEASE_TOKENS,
                attempt,
            )
            attempt.decode_resource = None
            raise
        return True

    async def _await_handoff_prefill(
        self,
        attempt: AttemptContext,
        p_client,
    ) -> PrefillMetadata:
        p_instance_id = attempt.prefill_resource.instance.id

        async def prefill_task():
            try:
                result = await self._request_prefill_result(attempt, p_client)
                await self._scheduler.report_cb_event(p_instance_id, "success")
                attempt.unregister_prefill_canceller()
                return result
            except asyncio.CancelledError:  # pylint: disable=try-except-raise
                raise
            except Exception as e:
                if is_cb_reportable_failure(e):
                    await self._scheduler.report_cb_event(p_instance_id, "failure")
                raise

        p_task = attempt.register_prefill_task(asyncio.create_task(prefill_task()))
        return await p_task

    def _request_for_attempt(
        self,
        attempt: AttemptContext,
        role: PDRole,
        *,
        prefill_result: PrefillMetadata | None = None,
    ) -> (dict[str, Any], str):
        req, api = self._base_request_for_attempt(role)
        adapter = self._require_adapter_for_attempt(attempt)
        return self._native_request_for_attempt(
            attempt,
            role,
            adapter=adapter,
            req=req,
            api=api,
            prefill_metadata=prefill_result,
        )

    def _base_request_for_attempt(self, role: PDRole) -> tuple[dict[str, Any], str]:
        api = self.req_info.entry_api
        req = self.req_info.req_data.copy()
        stream = self.req_info.req_data.get("stream", False)
        if stream and self.config.exception_config.reschedule_enabled:
            req["return_token_ids"] = True
            if self._active_retry_plan is not None:
                req, api = self.rescheduler.apply_retry_plan(
                    req,
                    self._active_retry_plan,
                    prefill=role == PDRole.ROLE_P,
                )
        if (
            role == PDRole.ROLE_D
            and self.config.precision_detection_config.precision_check_enabled
            and self._sampling_manager is not None
        ):
            inject_logprobs(req, self.config.precision_detection_config, req_id=self.req_info.req_id)
        return req, api

    def _native_request_for_attempt(
        self,
        attempt: AttemptContext,
        role: PDRole,
        *,
        adapter: PDProtocolAdapter,
        req: dict[str, Any],
        api: str,
        prefill_metadata: PrefillMetadata | None = None,
    ) -> tuple[dict[str, Any], str]:
        context = self._native_leg_context(attempt, role, api)
        if role == PDRole.ROLE_P:
            engine_request = adapter.build_prefill_request(req, context)
        else:
            engine_request = adapter.build_decode_request(req, context, prefill_metadata)
        return engine_request.body, engine_request.api

    @staticmethod
    def _native_leg_context(attempt: AttemptContext, role: PDRole, api: str) -> LegContext:
        resource = attempt.prefill_resource if role == PDRole.ROLE_P else attempt.decode_resource
        if resource is None:
            raise RuntimeError(f"Missing {role.value} resource for native engine request")
        peer_resource = attempt.decode_resource if role == PDRole.ROLE_P else attempt.prefill_resource
        return LegContext(
            engine_request_id=f"{attempt.root_request_id}#a{attempt.attempt_seq}",
            pair_id=attempt.pair_id,
            attempt_seq=attempt.attempt_seq,
            api=api,
            endpoint=UnifiedPDRouter._native_endpoint_metadata(resource),
            peer_endpoint=(
                UnifiedPDRouter._native_endpoint_metadata(peer_resource) if peer_resource is not None else None
            ),
        )

    @staticmethod
    def _native_endpoint_metadata(resource: ScheduledResource) -> EngineEndpointMetadata:
        endpoint = resource.endpoint
        return EngineEndpointMetadata(
            host=endpoint.ip,
            bootstrap_port=endpoint.bootstrap_port,
        )

    async def _request_prefill_result(
        self,
        attempt: AttemptContext,
        p_client,
    ) -> PrefillMetadata:
        p_req, p_api = self._request_for_attempt(attempt, PDRole.ROLE_P)
        attempt.mark_dispatched(PDRole.ROLE_P.value)
        try:
            response = await self.forward_request(
                p_api,
                p_req,
                p_client,
                self.config.exception_config.first_token_timeout,
            )
        except UpstreamHTTPError:
            attempt.mark_completed(PDRole.ROLE_P.value)
            raise
        attempt.mark_completed(PDRole.ROLE_P.value)
        response_body = response.json()
        adapter = self._require_adapter_for_attempt(attempt)
        try:
            prefill_metadata = adapter.parse_prefill_response(response_body)
        except EngineProtocolError as error:
            raise UpstreamHTTPError(
                status_code=502,
                body=str(error).encode("utf-8"),
                headers={"content-type": "text/plain; charset=utf-8"},
                phase="prefill",
            ) from error
        if prefill_metadata.usage is not None:
            self._capture_prompt_tokens_details({"usage": prefill_metadata.usage})
        self.req_info.update_state(ReqState.PREFILL_END)
        if self._stream_commit_controller is not None:
            self._stream_commit_controller.mark_ready("prefill", attempt.attempt_seq)
        return prefill_metadata

    def _select_coordination_mode(self, attempt: AttemptContext) -> CoordinationMode:
        adapter = self._require_adapter_for_attempt(attempt)
        if adapter.coordination_mode == CoordinationMode.BOOTSTRAP:
            if attempt.decode_resource is None:
                raise RuntimeError(f"{adapter.engine_type} bootstrap requires a decode instance")
            if attempt.prefill_resource is not None:
                decode_engine_type = getattr(attempt.decode_resource.instance, "engine_type", None)
                if not isinstance(decode_engine_type, str) or decode_engine_type.strip().lower() != adapter.engine_type:
                    raise RuntimeError(
                        f"P/D engine types must match: prefill={adapter.engine_type}, decode={decode_engine_type!r}"
                    )
            return CoordinationMode.BOOTSTRAP
        # Prefer cluster detection already done in create_attempt: ALLOCATE_ONLY historically
        # dropped dispatch_capabilities, which would otherwise fall back to adapter HANDOFF.
        if self._pd_uses_trigger:
            return self._require_trigger_decode(attempt)
        resource = attempt.prefill_resource or attempt.decode_resource
        caps = list(getattr(resource.instance, "dispatch_capabilities", None) or []) if resource is not None else []
        if _TRIGGER_CAPABILITY in caps:
            return self._require_trigger_decode(attempt)
        if attempt.decode_resource is not None and attempt.prefill_resource is not None:
            decode_engine_type = getattr(attempt.decode_resource.instance, "engine_type", None)
            if not isinstance(decode_engine_type, str) or decode_engine_type.strip().lower() != adapter.engine_type:
                raise RuntimeError(
                    f"P/D engine types must match: prefill={adapter.engine_type}, decode={decode_engine_type!r}"
                )
        return adapter.coordination_mode

    def _require_trigger_decode(self, attempt: AttemptContext) -> CoordinationMode:
        if attempt.decode_resource is None:
            self.logger.error(
                "Trigger mode selected without a decode resource req_id=%s attempt=%s",
                self.req_info.req_id,
                attempt.attempt_seq,
            )
            raise HTTPException(
                status_code=503,
                detail=(
                    "Trigger P/D requires a decode instance; cluster detection and "
                    "allocated instance capabilities are inconsistent"
                ),
            )
        return CoordinationMode.TRIGGER

    async def _prepare_attempt_resource(
        self,
        role: PDRole,
        attempt_seq: int,
        *,
        required_engine_type: str | None = None,
    ) -> ScheduledResource:
        self.req_info.update_state(ReqState.P_SCHEDULING if role == PDRole.ROLE_P else ReqState.D_SCHEDULING)
        target_instance_id = None
        constraint = self.req_info.scheduling_constraint
        if constraint is not None:
            target_instance_id = constraint.target_for_role(role)
        scheduler_kwargs = {"target_instance_id": target_instance_id}
        if required_engine_type is not None:
            scheduler_kwargs["required_engine_type"] = required_engine_type
        result = await self._scheduler.select_and_allocate(role, self.req_info, **scheduler_kwargs)
        if result is None:
            error_message = f"No instance available for role {role}"
            if required_engine_type is not None:
                error_message += f" with engine_type={required_engine_type}"
            self.req_info.trace_obj.set_trace_error_message(error_message)
            if required_engine_type is not None:
                raise HTTPException(status_code=503, detail=error_message)
            raise RuntimeError(error_message)
        ins, endpoint, workload = result
        if not await self._request_manager.add_req_attempt_workload(
            self.req_info.req_id,
            attempt_seq,
            role,
            workload,
        ):
            await self._rollback_allocated_workload(ins, endpoint, role, workload)
            raise RuntimeError(
                f"Request {self.req_info.req_id} already allocated for attempt {attempt_seq} role {role}"
            )
        self.req_info.update_state(ReqState.P_ALLOCATED if role == PDRole.ROLE_P else ReqState.D_ALLOCATED)
        return ScheduledResource(instance=ins, endpoint=endpoint)

    async def _release_attempt(self, attempt: AttemptContext, *, wait: bool = True) -> None:
        if attempt.prefill_resource:
            await self._release_attempt_resource(
                attempt.prefill_resource,
                attempt.attempt_seq,
                WorkloadAction.RELEASE_TOKENS,
                attempt,
                wait=wait,
            )
        if attempt.decode_resource:
            await self._release_attempt_resource(
                attempt.decode_resource,
                attempt.attempt_seq,
                WorkloadAction.RELEASE_TOKENS,
                attempt,
                wait=wait,
            )

    def _submit_prefill_release_background(self, attempt: AttemptContext, action: WorkloadAction) -> None:
        if attempt.prefill_resource is None:
            return
        self._submit_release_attempt_resource_background(
            attempt.prefill_resource,
            attempt.attempt_seq,
            action,
            attempt,
        )

    async def _release_attempt_resource(
        self,
        resource: ScheduledResource,
        attempt_seq: int,
        action: WorkloadAction,
        attempt: AttemptContext | None = None,
        *,
        wait: bool = True,
    ) -> bool:
        task = self._enqueue_release_attempt_resource(resource, attempt_seq, action, attempt=attempt, wait=wait)
        if task is None:
            return False
        if not wait:
            return True
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._cleanup_release_task(task)

    def _submit_release_attempt_resource_background(
        self,
        resource: ScheduledResource,
        attempt_seq: int,
        action: WorkloadAction,
        attempt: AttemptContext | None = None,
    ) -> None:
        self._enqueue_release_attempt_resource(resource, attempt_seq, action, attempt=attempt, wait=False)

    def _enqueue_release_attempt_resource(
        self,
        resource: ScheduledResource,
        attempt_seq: int,
        action: WorkloadAction,
        *,
        attempt: AttemptContext | None = None,
        wait: bool,
    ) -> asyncio.Task[bool] | None:
        if attempt is not None and self._release_already_marked(attempt, PDRole(resource.instance.role), action):
            return None
        key = self._release_key(resource, attempt_seq, action)
        inflight = self._release_inflight.get(key)
        if inflight is not None:
            self.logger.debug(
                "Release workload already in-flight stage=%s req_id=%s attempt_seq=%s role=%s action=%s wait=%s",
                self._release_stage(resource, action),
                self.req_info.req_id,
                attempt_seq,
                key[2].value,
                action.value,
                wait,
            )
            return inflight
        context = self._release_task_context(resource, attempt_seq, action)
        task = asyncio.create_task(
            self._release_attempt_resource_task(resource, attempt_seq, action, attempt, task_context=context),
            name=(f"unified-pd-release-{self._release_stage(resource, action)}-{self.req_info.req_id}-a{attempt_seq}"),
        )
        self._track_release_task(task, key=key, context=context)
        self.logger.debug(
            "Release workload enqueued stage=%s instance_id=%s endpoint_id=%s role=%s action=%s req_id=%s wait=%s",
            context.stage,
            context.instance_id,
            context.endpoint_id,
            context.role.value,
            context.action.value,
            context.req_id,
            wait,
        )
        return task

    async def _release_attempt_resource_task(
        self,
        resource: ScheduledResource,
        attempt_seq: int,
        action: WorkloadAction,
        attempt: AttemptContext | None = None,
        task_context: ReleaseTaskContext | None = None,
    ) -> bool:
        try:
            if attempt is not None and self._release_already_marked(attempt, PDRole(resource.instance.role), action):
                self.logger.debug(
                    "Release workload task skipped already_released stage=%s req_id=%s attempt_seq=%s action=%s",
                    self._release_stage(resource, action),
                    self.req_info.req_id,
                    attempt_seq,
                    action.value,
                )
                return True
            item = await self._prepare_release_work_item(resource, attempt_seq, action, attempt=attempt)
            if item is None:
                self.logger.debug(
                    "Release workload task skipped no_workload_change stage=%s req_id=%s attempt_seq=%s action=%s",
                    self._release_stage(resource, action),
                    self.req_info.req_id,
                    attempt_seq,
                    action.value,
                )
                return True
            current_task = asyncio.current_task()
            record = self._release_records.get(current_task) if current_task is not None else None
            if record is not None:
                record.item = item
            ok = await self._send_release_work_item(item)
            if ok:
                # Scheduler ACKed the release: drop the worker-side ledger record that
                # compute_and_update intentionally retained for failure recomputation.
                try:
                    await self._workload_action_handler.finalize_release(self.req_info.req_id, item.role, attempt_seq)
                except Exception as exc:
                    # The scheduler already applied the release; a finalize failure only leaves
                    # a stale local record (a later re-release is deduped by operation_id).
                    # Log it but do not turn the ACKed release into a failure/retry.
                    self.logger.warning(
                        "finalize_release failed after scheduler ACK req_id=%s attempt_seq=%s action=%s: %s",
                        self.req_info.req_id,
                        attempt_seq,
                        action.value,
                        exc,
                    )
                else:
                    # Mark released only after the local record is gone: a finalize failure
                    # leaves the release unmarked, so a later re-enqueue can recompute the
                    # delta and finalize again instead of skipping as already-released.
                    if item.attempt is not None:
                        self._mark_released(item.attempt, item.role, item.action)
            return ok
        except Exception as exc:
            self._log_release_task_result_error(None, "raised", exc, context=task_context)
            return False

    async def _prepare_release_work_item(
        self,
        resource: ScheduledResource,
        attempt_seq: int,
        action: WorkloadAction,
        *,
        attempt: AttemptContext | None = None,
    ) -> ReleaseWorkItem | None:
        workload_change, role = await self._workload_action_handler.compute_and_update(
            resource,
            self.req_info.req_id,
            action,
            self.req_info,
            attempt_seq=attempt_seq,
        )
        if workload_change is None or role is None:
            return None
        params = UpdateWorkloadParams(
            instance_id=resource.instance.id,
            endpoint_id=resource.endpoint.id,
            role=resource.instance.role,
            req_id=self.req_info.req_id,
            workload_action=action,
            workload_change=workload_change,
            # Deterministic id keyed on (request, attempt, endpoint, action): stable across the
            # retries in _send_release_work_item, so a release whose ACK was lost is de-duplicated by
            # the scheduler instead of applied twice (which would drive the load ledger negative).
            operation_id=(
                f"{self.req_info.req_id}:a{attempt_seq}:{resource.instance.id}:{resource.endpoint.id}:{action.value}"
            ),
        )
        return ReleaseWorkItem(
            stage=self._release_stage(resource, action),
            params=params,
            attempt_seq=attempt_seq,
            role=role,
            action=action,
            attempt=attempt,
        )

    def _track_release_task(
        self,
        task: asyncio.Task[bool],
        *,
        key: ReleaseKey,
        context: ReleaseTaskContext,
    ) -> None:
        self._release_inflight[key] = task
        self._release_records[task] = _ReleaseTaskRecord(key=key, context=context)

    def _cleanup_release_task(self, task: asyncio.Task[bool]) -> ReleaseWorkItem | None:
        record = self._release_records.pop(task, None)
        key = record.key if record is not None else None
        if key is not None and self._release_inflight.get(key) is task:
            self._release_inflight.pop(key, None)
        return record.item if record is not None else None

    def _release_task_context(
        self,
        resource: ScheduledResource,
        attempt_seq: int,
        action: WorkloadAction,
    ) -> ReleaseTaskContext:
        return ReleaseTaskContext(
            stage=self._release_stage(resource, action),
            req_id=self.req_info.req_id,
            attempt_seq=attempt_seq,
            instance_id=resource.instance.id,
            endpoint_id=resource.endpoint.id,
            role=PDRole(resource.instance.role),
            action=action,
        )

    async def _send_release_work_item(self, item: ReleaseWorkItem) -> bool:
        start = time.perf_counter()
        attempts = self._RELEASE_RPC_ATTEMPTS
        for retry in range(attempts):
            try:
                ok = await self._scheduler.update_workload(item.params)
            except asyncio.CancelledError:
                self.logger.warning(
                    "Release workload task cancelled stage=%s req_id=%s role=%s action=%s retry=%d",
                    item.stage,
                    item.params.req_id,
                    item.role.value,
                    item.action.value,
                    retry,
                )
                raise
            except Exception as exc:
                ok = False
                self.logger.warning(
                    "Release workload RPC raised stage=%s req_id=%s role=%s action=%s retry=%d error=%s",
                    item.stage,
                    item.params.req_id,
                    item.role.value,
                    item.action.value,
                    retry,
                    exc,
                )
            if ok:
                # NOTE: the release is NOT marked here. The mark happens in the caller only after
                # finalize_release removed the local ledger record, so a finalize failure leaves
                # the release unmarked and a later re-enqueue can recompute and finalize it.
                elapsed_ms = (time.perf_counter() - start) * 1000
                self.logger.info(
                    "Release workload succeeded stage=%s elapsed_ms=%.2f instance_id=%s endpoint_id=%s role=%s action=%s retry=%d",
                    item.stage,
                    elapsed_ms,
                    item.params.instance_id,
                    item.params.endpoint_id,
                    item.role.value,
                    item.action.value,
                    retry,
                )
                return True
            if retry < attempts - 1:
                await asyncio.sleep(self._RELEASE_RPC_BACKOFF_BASE_S * (retry + 1))
        elapsed_ms = (time.perf_counter() - start) * 1000
        self.logger.error(
            "Release workload failed stage=%s elapsed_ms=%.2f instance_id=%s endpoint_id=%s role=%s action=%s req_id=%s",
            item.stage,
            elapsed_ms,
            item.params.instance_id,
            item.params.endpoint_id,
            item.role.value,
            item.action.value,
            item.params.req_id,
        )
        return False

    async def _drain_release_tasks(self) -> None:
        while self._release_records:
            tasks = list(self._release_records)
            gather_task = asyncio.gather(*tasks, return_exceptions=True)
            try:
                results = await asyncio.shield(gather_task)
            except asyncio.CancelledError:
                # The shielded gather keeps running. Finalize it from a callback so a repeated
                # cancellation of this drain coroutine cannot cancel the gather or leak records.
                self._finalize_release_gather_when_done(tasks, gather_task)
                raise
            self._handle_release_task_results(tasks, results)

    def _finalize_release_gather_when_done(
        self,
        tasks: list[asyncio.Task[bool]],
        gather_task: asyncio.Future,
    ) -> None:
        if gather_task.done():
            self._finalize_release_gather(tasks, gather_task)
            return
        gather_task.add_done_callback(
            lambda done_task, release_tasks=tasks: self._finalize_release_gather(release_tasks, done_task)
        )

    def _finalize_release_gather(self, tasks: list[asyncio.Task[bool]], gather_task: asyncio.Future) -> None:
        try:
            results = gather_task.result()
        except asyncio.CancelledError as exc:
            self.logger.warning(
                "Release drain gather cancelled before background tasks settled req_id=%s pending=%d",
                self.req_info.req_id,
                len(tasks),
            )
            self._handle_release_task_results(tasks, [exc for _ in tasks])
        except BaseException as exc:
            self.logger.error(
                "Release drain gather failed req_id=%s pending=%d error=%s",
                self.req_info.req_id,
                len(tasks),
                exc,
            )
            self._handle_release_task_results(tasks, [exc for _ in tasks])
        else:
            self._handle_release_task_results(tasks, results)

    def _handle_release_task_results(self, tasks: list[asyncio.Task[bool]], results: list[Any]) -> None:
        for task, result in zip(tasks, results, strict=False):
            record = self._release_records.get(task)
            if record is None:
                continue
            item = record.item if record is not None else None
            context = record.context if record is not None else None
            if isinstance(result, asyncio.CancelledError):
                self._log_release_task_result_error(item, "cancelled", result, context=context)
            elif isinstance(result, BaseException):
                self._log_release_task_result_error(item, "raised", result, context=context)
            elif result is False:
                self._log_release_task_result_error(item, "failed", context=context)
            self._cleanup_release_task(task)

    def _log_release_task_result_error(
        self,
        item: ReleaseWorkItem | None,
        status: str,
        error: BaseException | None = None,
        *,
        context: ReleaseTaskContext | None = None,
    ) -> None:
        if item is None:
            if context is not None:
                self.logger.error(
                    "Release workload background task %s stage=%s req_id=%s attempt_seq=%s instance_id=%s "
                    "endpoint_id=%s role=%s action=%s error=%s",
                    status,
                    context.stage,
                    context.req_id,
                    context.attempt_seq,
                    context.instance_id,
                    context.endpoint_id,
                    context.role.value,
                    context.action.value,
                    error,
                )
                return
            self.logger.error(
                "Release workload background task %s req_id=%s error=%s",
                status,
                self.req_info.req_id,
                error,
            )
            return
        self.logger.error(
            "Release workload background task %s stage=%s req_id=%s attempt_seq=%s instance_id=%s endpoint_id=%s "
            "role=%s action=%s error=%s",
            status,
            item.stage,
            item.params.req_id,
            item.attempt_seq,
            item.params.instance_id,
            item.params.endpoint_id,
            item.role.value,
            item.action.value,
            error,
        )

    def _release_key(self, resource: ScheduledResource, attempt_seq: int, action: WorkloadAction) -> ReleaseKey:
        return (
            self.req_info.req_id,
            attempt_seq,
            PDRole(resource.instance.role),
            action,
        )

    @staticmethod
    def _release_stage(resource: ScheduledResource, action: WorkloadAction) -> str:
        role = PDRole(resource.instance.role)
        if role == PDRole.ROLE_P and action == WorkloadAction.RELEASE_TOKENS:
            return "release_p_tokens"
        if role == PDRole.ROLE_D and action == WorkloadAction.RELEASE_TOKENS:
            return "release_d_tokens"
        return f"release_{role.value}_{action.value.lower()}"

    async def _stop_attempt(self, attempt: AttemptContext | None, reason: AttemptStopReason) -> None:
        if attempt is None:
            return
        async with attempt.stop_lock:
            attempt.unregister_canceller()
            if attempt.state in (AttemptState.DONE, AttemptState.STOPPED):
                return

            attempt.stop()
            await self._abort_native_attempt(attempt)
            await attempt.cancel(reason.value)
            await self._release_attempt(attempt, wait=False)
            try:
                await self._drain_release_tasks()
            except asyncio.CancelledError:
                self.logger.warning(
                    "Unified PD stop cancelled while draining release tasks req_id=%s attempt=%s reason=%s",
                    self.req_info.req_id,
                    attempt.attempt_seq,
                    reason.value,
                )
            attempt.transition(AttemptState.STOPPED)

    async def _abort_native_attempt(self, attempt: AttemptContext) -> None:
        """Best-effort native cancellation; failures never mask the original stop reason."""
        try:
            adapter = self._require_adapter_for_attempt(attempt)
        except Exception:
            return
        aborts = []
        for role, resource in (
            (PDRole.ROLE_P, attempt.prefill_resource),
            (PDRole.ROLE_D, attempt.decode_resource),
        ):
            if resource is None or not attempt.needs_abort(role.value):
                continue
            context = self._native_leg_context(attempt, role, self.req_info.entry_api)
            abort_request = adapter.build_abort_request(context)
            if abort_request is not None:
                aborts.append(self._send_native_abort(resource, abort_request))
        if aborts:
            await asyncio.gather(*aborts)

    async def _send_native_abort(self, resource: ScheduledResource, request: EngineRequest) -> None:
        try:
            async with self._client_for(resource) as client:
                timeout = min(float(self.config.exception_config.first_token_timeout), 1.0)
                response = await asyncio.wait_for(
                    client.post(
                        f"/{request.api}",
                        json=request.body,
                        timeout=timeout,
                    ),
                    timeout=timeout,
                )
                if response.status_code >= 400:
                    self.logger.warning(
                        "Native abort rejected engine=%s instance_id=%s endpoint_id=%s status=%s",
                        resource.instance.engine_type,
                        resource.instance.id,
                        resource.endpoint.id,
                        response.status_code,
                    )
        except Exception as err:
            self.logger.warning(
                "Native abort failed engine=%s instance_id=%s endpoint_id=%s error=%s",
                resource.instance.engine_type,
                resource.instance.id,
                resource.endpoint.id,
                err,
            )

    def _client_for(self, resource: ScheduledResource):
        if resource is None:
            raise RuntimeError("Scheduled resource is missing")
        return self._manage_client_context(resource)

    @staticmethod
    def _release_already_marked(attempt: AttemptContext, role: PDRole, action: WorkloadAction) -> bool:
        flags = attempt.release_flags
        if role == PDRole.ROLE_P and action == WorkloadAction.RELEASE_TOKENS:
            return flags.prefill_tokens
        if role == PDRole.ROLE_D and action == WorkloadAction.RELEASE_TOKENS:
            return flags.decode_tokens
        return False

    @staticmethod
    def _mark_released(attempt: AttemptContext, role: PDRole, action: WorkloadAction) -> None:
        flags = attempt.release_flags
        if role == PDRole.ROLE_P and action == WorkloadAction.RELEASE_TOKENS:
            flags.prefill_tokens = True
        elif role == PDRole.ROLE_D and action == WorkloadAction.RELEASE_TOKENS:
            flags.decode_tokens = True

    # ------------------------------------------------------------------
    # Precision sampling helpers
    # ------------------------------------------------------------------

    async def _maybe_submit_sample(self, attempt: AttemptContext, sampling_state: dict) -> None:
        self.logger.debug(
            "_maybe_submit_sample entry: enabled=%s mgr_ok=%s",
            sampling_state["enabled"],
            self._sampling_manager is not None,
        )
        if not sampling_state["enabled"] or self._sampling_manager is None:
            return
        if not attempt.prefill_resource or not attempt.decode_resource:
            return
        info = sampling_state["info"]
        info.setdefault("cached_output_token_ids", [])
        info.setdefault("cached_prompt_token_ids", self.req_info.token_ids)
        p_id = attempt.prefill_resource.instance.id
        d_id = attempt.decode_resource.instance.id
        if await self._sampling_manager.confirm_sample((p_id, d_id), time.time()):
            await self._submit_token_sample(p_id, d_id, info, attempt.decode_resource)

    @staticmethod
    async def _cancel_task_quietly(task: asyncio.Task | None) -> None:
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Circuit breaker reporting helpers
    # ------------------------------------------------------------------
