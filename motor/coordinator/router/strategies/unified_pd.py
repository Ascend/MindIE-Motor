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
from typing import Any, AsyncGenerator

import anyio
from fastapi.responses import JSONResponse, StreamingResponse

from motor.common.resources.dispatch import (
    DispatchPlan,
    DispatchStopReason,
    MOTOR_DISPATCH_KEY,
    MOTOR_PREFILL_RESULT_KEY,
    PrefillResult,
    PrefillResultStatus,
)
from motor.common.resources.endpoint import WorkloadAction
from motor.common.resources.instance import PDRole
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.domain import ScheduledResource, SchedulingFacade, UpdateWorkloadParams
from motor.coordinator.domain.request_manager import RequestManager
from motor.coordinator.models.request import RequestInfo, ReqState
from motor.coordinator.router.dispatch_session import (
    AttemptContext,
    AttemptState,
    PDDispatchSession,
)
from motor.coordinator.router.dispatch_capability import select_dispatch_plan_for_pair
from motor.coordinator.router.stop_client import DispatchStopClient
from motor.coordinator.router.strategies.base import BaseRouter, check_cancel_error
from motor.coordinator.router.rescheduler.rescheduler import Rescheduler
from motor.coordinator.router.workload import WorkloadActionHandler
from motor.coordinator.router.precision_sample.request import inject_logprobs
from motor.coordinator.router.precision_sample import response as sampling_resp
from motor.coordinator.router.adapters.stream import (
    parse_stream_chunk_json,
    encode_stream_chunk_bytes,
    update_token_id_cache,
)


class UnifiedPDRouter(BaseRouter):
    """Unified P/D pair router behind feature flag.

    Coordinator owns lifecycle orchestration; EngineServer dispatch adapters own
    engine-specific request and response normalization.
    """

    _DISPATCH_MODE = "pd_pair"

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
            req_info, config, scheduler, request_manager, workload_action_handler, sampling_manager=sampling_manager
        )
        self.rescheduler = Rescheduler(
            config.exception_config.recompute_enabled,
            req_info,
            self.logger,
        )

    async def handle_request(self) -> StreamingResponse | JSONResponse:
        await self.do_encode()
        self.is_meta = False
        if self.req_info.req_data.get("stream", False):
            return StreamingResponse(
                self._generate_stream_response(),
                media_type="text/event-stream",
            )
        return await self._generate_response()

    async def _generate_stream_response(self) -> AsyncGenerator[str, None]:
        trace_obj = self.req_info.trace_obj
        with self._trace_span("UnifiedPD_Stream", True):
            max_retry = max(self.config.exception_config.transport_retry_limit, 1)
            session = PDDispatchSession(self.req_info.req_id)

            async with self._manage_request_context():
                for attempt_index in range(max_retry):
                    attempt: AttemptContext | None = None
                    try:
                        attempt = await self._create_attempt(session)
                        attempt.register_canceller()
                        dispatch_plan = self._select_dispatch_plan(attempt)
                        if attempt_index > 0:
                            self.rescheduler.is_rescheduling = True
                            self.rescheduler.retry_count = attempt_index
                            self.logger.warning(
                                f"Rescheduling[{attempt_index}/{max_retry}]: P=[{attempt.prefill_resource.endpoint.ip} "
                                f"{attempt.prefill_resource.instance.job_name}] "
                                f"D=[{attempt.decode_resource.endpoint.ip} {attempt.decode_resource.instance.job_name}]"
                            )
                        attempt.transition(AttemptState.DISPATCHING)
                        async for chunk in self._run_stream_attempt(attempt, dispatch_plan):
                            attempt.transition(AttemptState.FIRST_VISIBLE)
                            yield chunk
                        attempt.unregister_canceller()
                        attempt.transition(AttemptState.DONE)
                        self.logger.info(trace_obj.set_end_and_ttft_tpot())
                        return
                    except (asyncio.CancelledError, Exception) as e:
                        error, retry = await self._process_response_error(attempt, attempt_index, e)
                        if not retry:
                            yield self._generate_streaming_error_chunk(error)
                            return

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
                        dispatch_plan = self._select_dispatch_plan(attempt)
                        if attempt_index > 0:
                            self.rescheduler.retry_count = attempt_index
                            self.logger.warning(
                                f"Rescheduling[{attempt_index}/{max_retry}]: P=[{attempt.prefill_resource.endpoint.ip} "
                                f"{attempt.prefill_resource.instance.job_name}] "
                                f"D=[{attempt.decode_resource.endpoint.ip} {attempt.decode_resource.instance.job_name}]"
                            )
                        attempt.transition(AttemptState.DISPATCHING)
                        body = await self._run_nonstream_attempt(attempt, dispatch_plan)
                        attempt.unregister_canceller()
                        attempt.transition(AttemptState.DONE)
                        return JSONResponse(content=body)
                    except (asyncio.CancelledError, Exception) as e:
                        error, retry = await self._process_response_error(attempt, attempt_index, e)
                        if not retry:
                            raise error

        error_message = "Unified PD request ended without response"
        trace_obj.set_trace_error_message(error_message)
        raise RuntimeError(error_message)

    async def _process_response_error(
        self,
        attempt: AttemptContext | None,
        attempt_index: int,
        error: Exception | asyncio.CancelledError,
    ) -> (Exception, bool):
        trace_obj = self.req_info.trace_obj
        trace_obj.set_trace_exception(error)
        max_retry = max(self.config.exception_config.transport_retry_limit, 1)

        if isinstance(error, asyncio.CancelledError):
            reason = DispatchStopReason.CLIENT_DISCONNECT
            reason_str, retry = check_cancel_error(error)
            retry = retry and (attempt_index < max_retry - 1)
            error = RuntimeError(f"Unified PD cancelled because of {reason}")
            label = f"Unified PD cancelled {attempt_index}/{max_retry}"
        else:
            reason = DispatchStopReason.PEER_FAILED
            reason_str = str(error)
            retry = attempt_index < max_retry - 1
            label = f"Unified PD exception {attempt_index}/{max_retry}"

        if attempt:
            error_msg = str(
                f"{label}: P=[{attempt.prefill_resource.endpoint.ip} "
                f"{attempt.prefill_resource.instance.job_name}] "
                f"D=[{attempt.decode_resource.endpoint.ip} {attempt.decode_resource.instance.job_name}] "
                f"because of {reason_str}, {retry=}"
            )
            trace_obj.set_trace_error_message(error_msg)
            self.logger.warning("%s", error_msg)
            attempt.unregister_canceller()
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

    async def _create_attempt(self, session: PDDispatchSession) -> AttemptContext:
        attempt_seq = session._attempt_seq + 1
        constraint = self.req_info.scheduling_constraint
        if constraint is None:
            select_pair = getattr(self._scheduler, "select_pair_and_allocate", None)
            if select_pair is not None:
                pair = await select_pair(self.req_info)
                if pair is not None:
                    await self._record_attempt_workload(attempt_seq, PDRole.ROLE_P, pair.prefill_workload)
                    self.req_info.update_state(ReqState.P_ALLOCATED)
                    await self._record_attempt_workload(attempt_seq, PDRole.ROLE_D, pair.decode_workload)
                    self.req_info.update_state(ReqState.D_ALLOCATED)
                    return session.new_attempt(pair.prefill, pair.decode, self.config)

        p_resource = await self._prepare_attempt_resource(PDRole.ROLE_P, attempt_seq)
        try:
            d_resource = await self._prepare_attempt_resource(PDRole.ROLE_D, attempt_seq)
        except Exception as e:
            error_message = (
                f"Unified PD D allocation failed after P allocated "
                f"req_id={self.req_info.req_id} attempt={attempt_seq}: {e}"
            )
            self.req_info.trace_obj.set_trace_error_message(error_message)
            self.logger.warning(error_message)
            await self._release_attempt_resource(p_resource, attempt_seq, WorkloadAction.RELEASE_TOKENS)
            await self._release_attempt_resource(p_resource, attempt_seq, WorkloadAction.RELEASE_KV)
            raise
        return session.new_attempt(p_resource, d_resource, self.config)

    async def _run_stream_attempt(
        self, attempt: AttemptContext, dispatch_plan: DispatchPlan
    ) -> AsyncGenerator[str, None]:
        if dispatch_plan == DispatchPlan.PREFILL_HANDOFF_DECODE:
            run_func = self._run_handoff_stream_attempt
        else:
            run_func = self._run_concurrent_stream_attempt
        async for chunk in run_func(attempt):
            yield chunk
        return

    async def _run_concurrent_stream_attempt(self, attempt: AttemptContext) -> AsyncGenerator[str, None]:
        attempt.transition(AttemptState.ACTIVE)
        p_req, p_api = self._request_for_attempt(attempt, PDRole.ROLE_P)
        d_req, d_api = self._request_for_attempt(attempt, PDRole.ROLE_D)
        stream_adapter_state = {}
        sampling_state = self._init_sampling_state()
        async with (
            self._client_for(attempt.prefill_resource) as p_client,
            self._client_for(attempt.decode_resource) as d_client,
        ):

            async def prefill_task():
                await self.forward_request(p_api, p_req, p_client, self.config.exception_config.first_token_timeout)

            p_task = attempt.register_prefill_task(asyncio.create_task(prefill_task()))
            async for chunk in self._run_stream_decode_phase(
                attempt,
                d_client,
                d_api,
                d_req,
                stream_adapter_state,
                sampling_state=sampling_state,
                prefill_task=p_task,
            ):
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
    ) -> AsyncGenerator[str, None]:
        queue = asyncio.Queue()
        self._start_stream_decode_task(
            attempt, queue, d_api, d_req, d_client, stream_adapter_state, sampling_state=sampling_state
        )
        async for chunk in self._iter_stream_decode_queue(
            attempt, queue, sampling_state=sampling_state, prefill_task=prefill_task
        ):
            yield chunk

    def _start_stream_decode_task(
        self,
        attempt: AttemptContext,
        queue: asyncio.Queue,
        d_api: str,
        d_req: dict[str, Any],
        d_client,
        stream_adapter_state: dict,
        *,
        sampling_state: dict | None = None,
    ) -> asyncio.Task:
        async def decode_task() -> None:
            try:
                async for chunk in self.forward_stream_request(
                    d_api, d_req, d_client, self.config.exception_config.infer_timeout
                ):
                    if chunk:
                        attempt.transition(AttemptState.FIRST_VISIBLE)
                        if sampling_state is not None:
                            chunk = self._collect_logprobs_from_stream_chunk(chunk, sampling_state)
                        if self.config.exception_config.recompute_enabled:
                            chunk = self.rescheduler.process_stream_chunk(
                                chunk, stream_adapter_state=stream_adapter_state
                            )
                        queue.put_nowait(("chunk", chunk))
                queue.put_nowait(("done", None))
            except asyncio.CancelledError as e:
                queue.put_nowait(("cancel", e))
            except Exception as e:
                queue.put_nowait(("error", e))

        return attempt.register_decode_task(asyncio.create_task(decode_task()))

    async def _iter_stream_decode_queue(
        self,
        attempt: AttemptContext,
        queue: asyncio.Queue,
        *,
        sampling_state: dict | None = None,
        prefill_task: asyncio.Task | None = None,
    ) -> AsyncGenerator[str, None]:
        try:
            while True:
                key, value = await queue.get()
                if key == "chunk":
                    yield value
                elif key == "done":
                    if prefill_task is not None:
                        await prefill_task
                    self.req_info.update_state(ReqState.DECODE_END)
                    if sampling_state is not None:
                        await self._maybe_submit_sample(attempt, sampling_state)
                    return
                elif key in {"cancel", "error"}:
                    await attempt.cancel(repr(value))
                    raise value
        finally:
            await self._release_attempt(attempt)

    async def _run_nonstream_attempt(self, attempt: AttemptContext, dispatch_plan: DispatchPlan) -> dict[str, Any]:
        if dispatch_plan == DispatchPlan.PREFILL_HANDOFF_DECODE:
            return await self._run_handoff_nonstream_attempt(attempt)
        else:
            return await self._run_concurrent_nonstream_attempt(attempt)

    async def _run_concurrent_nonstream_attempt(self, attempt: AttemptContext) -> dict[str, Any]:
        attempt.transition(AttemptState.ACTIVE)
        p_req, p_api = self._request_for_attempt(attempt, PDRole.ROLE_P)
        d_req, d_api = self._request_for_attempt(attempt, PDRole.ROLE_D)
        sampling_state = self._init_sampling_state()
        async with (
            self._client_for(attempt.prefill_resource) as p_client,
            self._client_for(attempt.decode_resource) as d_client,
        ):

            async def prefill_task():
                await self.forward_request(p_api, p_req, p_client, self.config.exception_config.first_token_timeout)

            p_task = attempt.register_prefill_task(asyncio.create_task(prefill_task()))
            return await self._await_nonstream_decode(
                attempt, d_api, d_req, d_client, sampling_state=sampling_state, prefill_task=p_task
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
        async def decode_task() -> tuple[Any, Any]:
            try:
                response = await self.forward_request(
                    d_api, d_req, d_client, self.config.exception_config.infer_timeout
                )
                return response.json(), None
            except (asyncio.CancelledError, Exception) as e:
                return None, e

        d_task = attempt.register_decode_task(asyncio.create_task(decode_task()))
        response, error = await d_task
        if error:
            await attempt.cancel(repr(error))
            await self._release_attempt(attempt)
            raise error
        try:
            if prefill_task is not None:
                await prefill_task
            self.req_info.update_state(ReqState.DECODE_END)
            if sampling_state is not None:
                response = self._collect_logprobs_from_nonstream_body(response, sampling_state)
                await self._maybe_submit_sample(attempt, sampling_state)
                self._strip_logprobs_for_client(response, sampling_state)
            await self._release_attempt(attempt)
            return response
        except (asyncio.CancelledError, Exception) as e:
            self.logger.warning(
                "Unified PD non-stream decode failed req_id=%s attempt=%s: %s",
                self.req_info.req_id,
                attempt.attempt_seq,
                e,
            )
            await attempt.cancel(repr(e))
            await self._release_attempt(attempt)
            raise

    async def _run_handoff_stream_attempt(self, attempt: AttemptContext) -> AsyncGenerator[str, None]:
        attempt.transition(AttemptState.ACTIVE)
        stream_adapter_state = {}
        sampling_state = self._init_sampling_state()
        async with (
            self._client_for(attempt.prefill_resource) as p_client,
            self._client_for(attempt.decode_resource) as d_client,
        ):
            prefill_result = await self._await_handoff_prefill(attempt, p_client)
            d_req, d_api = self._request_for_attempt(attempt, PDRole.ROLE_D, prefill_result=prefill_result)
            async for chunk in self._run_stream_decode_phase(
                attempt, d_client, d_api, d_req, stream_adapter_state, sampling_state=sampling_state
            ):
                yield chunk

    async def _run_handoff_nonstream_attempt(self, attempt: AttemptContext) -> dict[str, Any]:
        attempt.transition(AttemptState.ACTIVE)
        sampling_state = self._init_sampling_state()
        async with (
            self._client_for(attempt.prefill_resource) as p_client,
            self._client_for(attempt.decode_resource) as d_client,
        ):
            prefill_result = await self._await_handoff_prefill(attempt, p_client)
            d_req, d_api = self._request_for_attempt(attempt, PDRole.ROLE_D, prefill_result=prefill_result)
            return await self._await_nonstream_decode(attempt, d_api, d_req, d_client, sampling_state=sampling_state)

    async def _await_handoff_prefill(self, attempt: AttemptContext, p_client) -> PrefillResult:
        async def prefill_task():
            result = await self._request_prefill_result(attempt, p_client)
            attempt.unregister_prefill_canceller()
            return result

        p_task = attempt.register_prefill_task(asyncio.create_task(prefill_task()))
        return await p_task

    def _request_for_attempt(
        self, attempt: AttemptContext, role: PDRole, *, prefill_result: PrefillResult | None = None
    ) -> (dict[str, Any], str):
        api = self.req_info.entry_api
        req = self.req_info.req_data.copy()
        stream = self.req_info.req_data.get('stream', False)
        req["request_id"] = f"{attempt.root_request_id}#a{attempt.attempt_seq}"
        if role == PDRole.ROLE_P:
            req["stream"] = False
            req = self._apply_prefill_params(req, set_min_tokens=False)
        if stream and self.config.exception_config.recompute_enabled:
            req["return_token_ids"] = True
            if self.rescheduler.is_rescheduling:
                req, api = self.rescheduler.prepare_retry_request(req)
        if (
            role == PDRole.ROLE_D
            and self.config.token_sampling_config.precision_check_enabled
            and self._sampling_manager is not None
        ):
            inject_logprobs(req, self.config.token_sampling_config, req_id=self.req_info.req_id)
        req[MOTOR_DISPATCH_KEY] = attempt.dispatch_for(role, self._DISPATCH_MODE).model_dump(mode="json")
        if prefill_result is not None:
            req[MOTOR_PREFILL_RESULT_KEY] = prefill_result.model_dump(mode="json")
        return (req, api)

    async def _request_prefill_result(self, attempt: AttemptContext, p_client) -> PrefillResult:
        p_req, p_api = self._request_for_attempt(attempt, PDRole.ROLE_P)
        response = await self.forward_request(p_api, p_req, p_client, self.config.exception_config.first_token_timeout)
        prefill_result = PrefillResult.model_validate(response.json())
        self._validate_prefill_result(attempt, prefill_result, expected_status=PrefillResultStatus.COMPLETED)
        return prefill_result

    @staticmethod
    def _validate_prefill_result(
        attempt: AttemptContext,
        prefill_result: PrefillResult,
        *,
        expected_status: PrefillResultStatus,
    ) -> None:
        if (
            prefill_result.root_request_id != attempt.root_request_id
            or prefill_result.pair_id != attempt.pair_id
            or prefill_result.attempt_seq != attempt.attempt_seq
        ):
            raise RuntimeError("PrefillResult does not match current dispatch attempt")
        if prefill_result.status != expected_status.value:
            raise RuntimeError(f"Unexpected PrefillResult status: {prefill_result.status}")

    def _select_dispatch_plan(self, attempt: AttemptContext) -> DispatchPlan:
        return select_dispatch_plan_for_pair(
            prefill=attempt.prefill_resource,
            decode=attempt.decode_resource,
        )

    async def _prepare_attempt_resource(self, role: PDRole, attempt_seq: int) -> ScheduledResource:
        self.req_info.update_state(ReqState.P_SCHEDULING if role == PDRole.ROLE_P else ReqState.D_SCHEDULING)
        target_instance_id = None
        constraint = self.req_info.scheduling_constraint
        if constraint is not None:
            target_instance_id = constraint.target_for_role(role)
        result = await self._scheduler.select_and_allocate(
            role,
            self.req_info,
            target_instance_id=target_instance_id,
        )
        if result is None:
            error_message = f"No instance available for role {role}"
            self.req_info.trace_obj.set_trace_error_message(error_message)
            raise RuntimeError(error_message)
        ins, endpoint, workload = result
        await self._record_attempt_workload(attempt_seq, role, workload)
        self.req_info.update_state(ReqState.P_ALLOCATED if role == PDRole.ROLE_P else ReqState.D_ALLOCATED)
        return ScheduledResource(instance=ins, endpoint=endpoint)

    async def _record_attempt_workload(self, attempt_seq: int, role: PDRole, workload) -> None:
        if not await self._request_manager.add_req_attempt_workload(self.req_info.req_id, attempt_seq, role, workload):
            raise RuntimeError(
                f"Request {self.req_info.req_id} already allocated for attempt {attempt_seq} role {role}"
            )

    async def _release_attempt(self, attempt: AttemptContext) -> None:
        if attempt.prefill_resource:
            await self._release_attempt_resource(
                attempt.prefill_resource, attempt.attempt_seq, WorkloadAction.RELEASE_TOKENS, attempt
            )
            await self._release_attempt_resource(
                attempt.prefill_resource, attempt.attempt_seq, WorkloadAction.RELEASE_KV, attempt
            )
        if attempt.decode_resource:
            await self._release_attempt_resource(
                attempt.decode_resource, attempt.attempt_seq, WorkloadAction.RELEASE_TOKENS, attempt
            )

    async def _release_attempt_resource(
        self,
        resource: ScheduledResource,
        attempt_seq: int,
        action: WorkloadAction,
        attempt: AttemptContext | None = None,
    ) -> bool:
        if attempt is not None and self._release_already_marked(attempt, resource, action):
            return False
        workload_change, role = await self._workload_action_handler.compute_and_update(
            resource,
            self.req_info.req_id,
            action,
            self.req_info,
            attempt_seq=attempt_seq,
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
        with anyio.CancelScope(shield=True):
            ok = await self._scheduler.update_workload(params)
        if attempt is not None:
            self._mark_released(attempt, resource, action)
        return ok

    async def _stop_attempt(self, attempt: AttemptContext | None, reason: DispatchStopReason) -> None:
        if attempt is None:
            return
        attempt.stop()
        client = DispatchStopClient(self.config)
        tasks = []
        if attempt.prefill_resource:
            tasks.append(client.stop(attempt.prefill_resource, attempt, reason))
        if attempt.decode_resource:
            tasks.append(client.stop(attempt.decode_resource, attempt, reason))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._release_attempt(attempt)
        attempt.transition(AttemptState.STOPPED)

    def _client_for(self, resource: ScheduledResource):
        if resource is None:
            raise RuntimeError("Scheduled resource is missing")
        return self._manage_client_context(resource)

    @staticmethod
    def _release_already_marked(attempt: AttemptContext, resource: ScheduledResource, action: WorkloadAction) -> bool:
        role = PDRole(resource.instance.role)
        flags = attempt.release_flags
        if role == PDRole.ROLE_P and action == WorkloadAction.RELEASE_TOKENS:
            return flags.prefill_tokens
        if role == PDRole.ROLE_P and action == WorkloadAction.RELEASE_KV:
            return flags.prefill_kv
        if role == PDRole.ROLE_D and action == WorkloadAction.RELEASE_TOKENS:
            return flags.decode_tokens
        if role == PDRole.ROLE_D and action == WorkloadAction.RELEASE_KV:
            return flags.decode_kv
        return False

    @staticmethod
    def _mark_released(attempt: AttemptContext, resource: ScheduledResource, action: WorkloadAction) -> None:
        role = PDRole(resource.instance.role)
        flags = attempt.release_flags
        if role == PDRole.ROLE_P and action == WorkloadAction.RELEASE_TOKENS:
            flags.prefill_tokens = True
        elif role == PDRole.ROLE_P and action == WorkloadAction.RELEASE_KV:
            flags.prefill_kv = True
        elif role == PDRole.ROLE_D and action == WorkloadAction.RELEASE_TOKENS:
            flags.decode_tokens = True
        elif role == PDRole.ROLE_D and action == WorkloadAction.RELEASE_KV:
            flags.decode_kv = True

    # ------------------------------------------------------------------
    # Precision sampling helpers
    # ------------------------------------------------------------------

    def _init_sampling_state(self) -> dict:
        return {
            "enabled": self.config.token_sampling_config.precision_check_enabled,
            "client_logprobs": bool(self.req_info.req_data.get("logprobs")),
            "lp_count": self.config.token_sampling_config.logprobs_count,
            "info": {},
        }

    def _collect_logprobs_from_stream_chunk(self, chunk: bytes, sampling_state: dict) -> bytes:
        if not sampling_state["enabled"] or not chunk:
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
        sampling_resp.strip_logprobs_for_client(
            chunk_json,
            client_requested_logprobs=sampling_state["client_logprobs"],
        )
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

    # ------------------------------------------------------------------
    # Metaserver forward entry point (CDP mode: D-side prefill → P instance)
    # ------------------------------------------------------------------

    async def handle_metaserver_request(self) -> dict[str, Any]:
        self.is_meta = True
        schedule_resource: ScheduledResource = None
        try:
            schedule_resource = await self.prepare_resource(PDRole.ROLE_P)
            req_data = self.req_info.req_data.copy()
            req_data["stream"] = False
            async with self._client_for(schedule_resource) as client:
                response = await self.forward_request(
                    self.req_info.api,
                    req_data,
                    client,
                    self.config.exception_config.first_token_timeout,
                )
            resp_json = response.json()
            self.logger.debug("Prefill response received")
            usage = resp_json.get("usage", {})
            if usage and "prompt_tokens_details" in usage:
                details = usage["prompt_tokens_details"]
                if details is None:
                    details = {"cached_tokens": 0}
                self.req_info.update_prompt_tokens_details(details)
            self.req_info.update_state(ReqState.PREFILL_END)
            if hasattr(self.req_info, "p_instance_id"):
                self.req_info.p_instance_id = schedule_resource.instance.id
            return resp_json
        except asyncio.CancelledError:
            self.logger.info("Metaserver request was cancelled")
            self.req_info.cancel_scope()
            raise
        except Exception:
            self.req_info.cancel_scope()
            self.req_info.update_state(ReqState.EXCEPTION)
            raise
        finally:
            if schedule_resource and self.req_info.state != ReqState.PREFILL_END:
                if not await self.release_all(schedule_resource):
                    self.logger.debug(
                        "release_all(prefill) returned False instance_id=%s",
                        schedule_resource.instance.id,
                    )

    @staticmethod
    async def _cancel_task_quietly(task: asyncio.Task | None) -> None:
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
