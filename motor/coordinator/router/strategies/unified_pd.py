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
from motor.coordinator.domain import ScheduledResource, UpdateWorkloadParams
from motor.coordinator.models.request import ReqState
from motor.coordinator.router.dispatch_session import (
    AttemptContext,
    AttemptState,
    PDDispatchSession,
)
from motor.coordinator.router.dispatch_capability import select_dispatch_plan_for_pair
from motor.coordinator.router.stop_client import DispatchStopClient
from motor.coordinator.router.strategies.base import BaseRouter


class UnifiedPDRouter(BaseRouter):
    """Unified P/D pair router behind feature flag.

    Coordinator owns lifecycle orchestration; EngineServer dispatch adapters own
    engine-specific request and response normalization.
    """

    _DISPATCH_MODE = "pd_pair"

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
        max_retry = self.config.exception_config.transport_retry_limit
        session = PDDispatchSession(self.req_info.req_id)
        last_error: Exception | None = None

        for attempt_index in range(max_retry):
            attempt = await self._create_attempt(session)
            try:
                dispatch_plan = self._select_dispatch_plan(attempt)
                async with self._manage_request_context():
                    attempt.transition(AttemptState.DISPATCHING)
                    async for chunk in self._run_stream_attempt(attempt, dispatch_plan):
                        attempt.transition(AttemptState.FIRST_VISIBLE)
                        yield chunk
                    attempt.transition(AttemptState.DONE)
                    return
            except asyncio.CancelledError:
                await self._stop_attempt(attempt, DispatchStopReason.CLIENT_DISCONNECT)
                raise
            except Exception as e:
                last_error = e
                await self._stop_attempt(attempt, DispatchStopReason.PEER_FAILED)
                if attempt.first_visible_sent or attempt_index == max_retry - 1:
                    self.req_info.update_state(ReqState.EXCEPTION)
                    yield self._generate_streaming_error_chunk(e)
                    return
                await asyncio.sleep(self.config.exception_config.retry_delay * (2**attempt_index))

        if last_error is not None:
            yield self._generate_streaming_error_chunk(last_error)

    async def _generate_response(self) -> JSONResponse:
        max_retry = self.config.exception_config.transport_retry_limit
        session = PDDispatchSession(self.req_info.req_id)
        last_error: Exception | None = None

        for attempt_index in range(max_retry):
            attempt = await self._create_attempt(session)
            try:
                dispatch_plan = self._select_dispatch_plan(attempt)
                async with self._manage_request_context():
                    attempt.transition(AttemptState.DISPATCHING)
                    body = await self._run_nonstream_attempt(attempt, dispatch_plan)
                    attempt.transition(AttemptState.DONE)
                    return JSONResponse(content=body)
            except asyncio.CancelledError:
                await self._stop_attempt(attempt, DispatchStopReason.CLIENT_DISCONNECT)
                raise
            except Exception as e:
                last_error = e
                await self._stop_attempt(attempt, DispatchStopReason.PEER_FAILED)
                if attempt_index < max_retry - 1:
                    await asyncio.sleep(self.config.exception_config.retry_delay * (2**attempt_index))
                    continue
                self.req_info.update_state(ReqState.EXCEPTION)
                raise

        if last_error is not None:
            raise last_error
        raise RuntimeError("Unified PD request ended without response")

    async def _create_attempt(self, session: PDDispatchSession) -> AttemptContext:
        attempt_seq = session._attempt_seq + 1
        pair = None
        select_pair = getattr(self._scheduler, "select_pair_and_allocate", None)
        if select_pair is not None:
            pair = await select_pair(self.req_info)
        if pair is not None:
            await self._record_attempt_workload(attempt_seq, PDRole.ROLE_P, pair.prefill_workload)
            self.req_info.update_state(ReqState.P_ALLOCATED)
            await self._record_attempt_workload(attempt_seq, PDRole.ROLE_D, pair.decode_workload)
            self.req_info.update_state(ReqState.D_ALLOCATED)
            return session.new_attempt(pair.prefill, pair.decode)

        p_resource = await self._prepare_attempt_resource(PDRole.ROLE_P, attempt_seq)
        try:
            d_resource = await self._prepare_attempt_resource(PDRole.ROLE_D, attempt_seq)
        except Exception:
            await self._release_attempt_resource(p_resource, attempt_seq, WorkloadAction.RELEASE_TOKENS)
            await self._release_attempt_resource(p_resource, attempt_seq, WorkloadAction.RELEASE_KV)
            raise
        return session.new_attempt(p_resource, d_resource)

    async def _run_stream_attempt(
        self, attempt: AttemptContext, dispatch_plan: DispatchPlan
    ) -> AsyncGenerator[str, None]:
        if dispatch_plan == DispatchPlan.PREFILL_HANDOFF_DECODE:
            async for chunk in self._run_handoff_stream_attempt(attempt):
                yield chunk
            return

        attempt.transition(AttemptState.ACTIVE)
        p_req = self._request_for_attempt(attempt, PDRole.ROLE_P, stream=False)
        d_req = self._request_for_attempt(attempt, PDRole.ROLE_D, stream=True)
        async with (
            self._client_for(attempt.prefill_resource) as p_client,
            self._client_for(attempt.decode_resource) as d_client,
        ):
            p_task = asyncio.create_task(
                self.forward_request(p_req, p_client, self.config.exception_config.first_token_timeout)
            )
            attempt.prefill_task = p_task
            try:
                async for chunk in self.forward_stream_request(
                    d_req, d_client, self.config.exception_config.infer_timeout
                ):
                    if chunk:
                        attempt.transition(AttemptState.FIRST_VISIBLE)
                        yield chunk
                await p_task
                self.req_info.update_state(ReqState.DECODE_END)
            finally:
                await self._cancel_task_quietly(p_task)
                await self._release_attempt(attempt)

    async def _run_nonstream_attempt(self, attempt: AttemptContext, dispatch_plan: DispatchPlan) -> dict[str, Any]:
        if dispatch_plan == DispatchPlan.PREFILL_HANDOFF_DECODE:
            return await self._run_handoff_nonstream_attempt(attempt)

        attempt.transition(AttemptState.ACTIVE)
        p_req = self._request_for_attempt(attempt, PDRole.ROLE_P, stream=False)
        d_req = self._request_for_attempt(attempt, PDRole.ROLE_D, stream=False)
        async with (
            self._client_for(attempt.prefill_resource) as p_client,
            self._client_for(attempt.decode_resource) as d_client,
        ):
            p_task = asyncio.create_task(
                self.forward_request(p_req, p_client, self.config.exception_config.first_token_timeout)
            )
            attempt.prefill_task = p_task
            try:
                response = await self.forward_request(d_req, d_client, self.config.exception_config.infer_timeout)
                await p_task
                body = response.json()
                self.req_info.update_state(ReqState.DECODE_END)
                return body
            finally:
                await self._cancel_task_quietly(p_task)
                await self._release_attempt(attempt)

    async def _run_handoff_stream_attempt(self, attempt: AttemptContext) -> AsyncGenerator[str, None]:
        attempt.transition(AttemptState.ACTIVE)
        async with (
            self._client_for(attempt.prefill_resource) as p_client,
            self._client_for(attempt.decode_resource) as d_client,
        ):
            prefill_result = await self._request_prefill_result(attempt, p_client)
            d_req = self._request_for_attempt(
                attempt,
                PDRole.ROLE_D,
                stream=True,
                prefill_result=prefill_result,
            )
            try:
                async for chunk in self.forward_stream_request(
                    d_req, d_client, self.config.exception_config.infer_timeout
                ):
                    if chunk:
                        attempt.transition(AttemptState.FIRST_VISIBLE)
                        yield chunk
                self.req_info.update_state(ReqState.DECODE_END)
            finally:
                await self._release_attempt(attempt)

    async def _run_handoff_nonstream_attempt(self, attempt: AttemptContext) -> dict[str, Any]:
        attempt.transition(AttemptState.ACTIVE)
        async with (
            self._client_for(attempt.prefill_resource) as p_client,
            self._client_for(attempt.decode_resource) as d_client,
        ):
            prefill_result = await self._request_prefill_result(attempt, p_client)
            d_req = self._request_for_attempt(
                attempt,
                PDRole.ROLE_D,
                stream=False,
                prefill_result=prefill_result,
            )
            try:
                response = await self.forward_request(d_req, d_client, self.config.exception_config.infer_timeout)
                body = response.json()
                self.req_info.update_state(ReqState.DECODE_END)
                return body
            finally:
                await self._release_attempt(attempt)

    def _request_for_attempt(
        self,
        attempt: AttemptContext,
        role: PDRole,
        *,
        stream: bool,
        prefill_result: PrefillResult | None = None,
    ) -> dict[str, Any]:
        req = self.req_info.req_data.copy()
        req["request_id"] = f"{attempt.root_request_id}#a{attempt.attempt_seq}"
        req["stream"] = stream
        if role == PDRole.ROLE_P:
            req = self._apply_prefill_params(req, set_min_tokens=False)
        req[MOTOR_DISPATCH_KEY] = attempt.dispatch_for(role, self._DISPATCH_MODE).model_dump(mode="json")
        if prefill_result is not None:
            req[MOTOR_PREFILL_RESULT_KEY] = prefill_result.model_dump(mode="json")
        return req

    async def _request_prefill_result(self, attempt: AttemptContext, p_client) -> PrefillResult:
        p_req = self._request_for_attempt(attempt, PDRole.ROLE_P, stream=False)
        response = await self.forward_request(p_req, p_client, self.config.exception_config.first_token_timeout)
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
            deploy_mode=self.config.scheduler_config.deploy_mode.value,
            prefill=attempt.prefill_resource,
            decode=attempt.decode_resource,
        )

    async def _prepare_attempt_resource(self, role: PDRole, attempt_seq: int) -> ScheduledResource:
        self.req_info.update_state(ReqState.P_SCHEDULING if role == PDRole.ROLE_P else ReqState.D_SCHEDULING)
        result = await self._scheduler.select_and_allocate(role, self.req_info)
        if result is None:
            raise RuntimeError(f"No instance available for role {role}")
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

    async def _stop_attempt(self, attempt: AttemptContext, reason: DispatchStopReason) -> None:
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
    async def _cancel_task_quietly(task: asyncio.Task | None) -> None:
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

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
