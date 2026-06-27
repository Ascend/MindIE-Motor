# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
from typing import Dict, AsyncGenerator, Any, Iterator
import asyncio
import contextlib
from contextlib import aclosing
import httpx
from fastapi.responses import JSONResponse, Response
from fastapi import HTTPException

from motor.common.http.http_client import HTTPClientPool
from motor.coordinator.domain import ScheduledResource
from motor.coordinator.models.request import ReqState
from motor.coordinator.router.strategies.base import BaseRouter, check_cancel_error
from motor.coordinator.router.rescheduler.rescheduler import Rescheduler
import motor.coordinator.router.adapters as adapters
from motor.coordinator.router.adapters.completion_to_chat import adapt_completion_nonstream_to_chat
from motor.common.resources.instance import PDRole
from motor.coordinator.tracer.tracing import TracerManager
from motor.coordinator.router.upstream_error import (
    UpstreamHTTPError,
    is_retryable_upstream_error,
)
from motor.coordinator.router.stream_response import (
    CommitAwareStreamingResponse,
    StreamCommitController,
)


class PDHybridRouter(BaseRouter):
    """Handle request with a single PD hybrid instance"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._resolved_roles: tuple[PDRole, ...] | None = None
        self._stream_commit_controller: StreamCommitController | None = None
        self._stream_body_sent = False
        self._scheduled_resource: ScheduledResource | None = None
        self.rescheduler = Rescheduler(
            self.config.exception_config.reschedule_enabled,
            self.req_info,
            self.logger,
        )

    async def _resolve_candidate_roles(self) -> tuple[PDRole, ...]:
        """Pick a single scheduling role from the scheduler's local topology view."""
        if self._resolved_roles is not None:
            return self._resolved_roles

        roles = await self._scheduler.get_available_instance_roles()
        if PDRole.ROLE_U in roles:
            self._resolved_roles = (PDRole.ROLE_U,)
            return self._resolved_roles

        if PDRole.ROLE_P in roles:
            error_message = "No union instances available, using prefill instances for single-node scheduling"
            self.logger.info(error_message)
            self.req_info.trace_obj.set_trace_error_message(error_message, is_meta=True)
            self._resolved_roles = (PDRole.ROLE_P,)
            return self._resolved_roles

        self._resolved_roles = ()
        return self._resolved_roles

    @contextlib.contextmanager
    def _inference_span(self) -> Iterator[Any]:
        trace_obj = self.req_info.trace_obj
        headers = trace_obj.get_trace_headers_dict(is_meta=False)
        trace_context = TracerManager().extract_trace_context(headers)
        with TracerManager().tracer.start_as_current_span("PDHybrid_Inference", context=trace_context) as span:
            trace_obj.meta_span = span
            trace_obj.meta_trace_headers = TracerManager().inject_trace_context()
            trace_obj.set_trace_attribute("requestId", self.req_info.req_id, is_meta=True)
            if trace_obj.meta_error_message:
                trace_obj.set_trace_error_message(trace_obj.meta_error_message, is_meta=True)
            yield span

    @contextlib.asynccontextmanager
    async def _manage_hybrid_resource_context(self, attempt: int, max_retry: int):
        """Schedule using the role resolved from instance pool pre-check."""
        candidate_roles = await self._resolve_candidate_roles()
        if not candidate_roles:
            error_message = "No available instance for hybrid scheduling"
            self.req_info.trace_obj.set_trace_error_message(error_message, is_meta=True)
            raise HTTPException(status_code=503, detail=error_message)

        role = candidate_roles[0]
        async with self._manage_resource_context(role, self.release_all) as resource:
            self._scheduled_resource = resource
            yield resource

    def _instance_label(self) -> str:
        """Serving-instance log label, mirroring unified PD's P=[...] D=[...] style."""
        resource = self._scheduled_resource
        if resource is None:
            return "U=[unscheduled]"
        return f"U=[{resource.endpoint.ip} {resource.instance.job_name}]"

    @contextlib.asynccontextmanager
    async def _manage_canceller_context(self, resource: ScheduledResource):
        """Register a node-fault canceller on the endpoint HTTP client for this attempt.

        When the instance is removed from the pool (node fault), ``cancel_all`` cancels
        the in-flight request task with a ``NODE_FAULT`` reason so the retry loop can
        reschedule instead of hanging until transport timeout.
        """
        pool = HTTPClientPool()
        pool_key = pool._get_pool_key(
            resource.endpoint.ip,
            resource.endpoint.business_port,
            self.config.infer_tls_config,
        )
        task = asyncio.current_task()

        async def _cancel_inflight_attempt(reason: str = ""):
            if task is not None and not task.done():
                task.cancel(msg=reason)

        pool.register_canceller(pool_key, self.req_info.req_id, _cancel_inflight_attempt)
        try:
            yield
        finally:
            pool.unregister_canceller(pool_key, self.req_info.req_id)

    @staticmethod
    def _uncancel_current_task():
        """Clear pending cancellation state after a retryable node-fault cancel."""
        task = asyncio.current_task()
        while task is not None and task.cancelling():
            task.uncancel()

    @contextlib.asynccontextmanager
    async def _inference_lifecycle(  # pylint: disable=contextmanager-generator-missing-cleanup
        self, attempt: int, max_retry: int
    ):
        """Tracer span + request lifecycle + hybrid scheduling + HTTP client."""
        with self._inference_span():
            async with (
                self._manage_request_context(),
                self._manage_hybrid_resource_context(attempt, max_retry) as resource,
                self._manage_client_context(resource) as client,
                self._manage_canceller_context(resource),
            ):
                yield client

    async def handle_request(self) -> Response:
        req_data = self.req_info.req_data.copy()

        if self.req_info.req_data.get("stream", False):
            self._stream_commit_controller = StreamCommitController.requiring({"engine"})
            return CommitAwareStreamingResponse(
                self._generate_stream(req_data),
                self._stream_commit_controller,
                on_first_body_sent=self._mark_stream_body_sent,
            )
        return await self._generate_post(req_data)

    def _mark_stream_body_sent(self) -> None:
        self._stream_body_sent = True

    async def _stream_inference_attempt(  # pylint: disable=contextmanager-generator-missing-cleanup
        self,
        req_data: Dict[str, Any],
        api: str,
        attempt: int,
        max_retry: int,
        stream_adapter_state: Dict[str, Any],
    ) -> AsyncGenerator[str, None]:
        trace_obj = self.req_info.trace_obj
        reschedule_enabled = self.config.exception_config.reschedule_enabled
        async with self._inference_lifecycle(attempt, max_retry) as client:
            async for chunk in self.forward_stream_request(
                api,
                req_data,
                client,
                self.config.exception_config.first_token_timeout,
                on_response_ready=lambda: self._stream_commit_controller.mark_ready("engine", attempt + 1),
            ):
                if reschedule_enabled:
                    # Cache prompt/output token ids so a node-fault reschedule can
                    # continue generation from where the failed leg stopped.
                    yield self.rescheduler.process_stream_chunk(chunk, stream_adapter_state=stream_adapter_state)
                else:
                    yield adapters.strip_stream_chunk_bytes_for_client(
                        chunk, client_return_token_ids=self.req_info.client_expects_token_ids
                    )

            self.req_info.update_state(ReqState.DECODE_END)
            self.logger.info(trace_obj.set_end_and_ttft_tpot())

    async def _generate_stream(self, req_data: Dict[str, Any]) -> AsyncGenerator[str, None]:
        """
        Handling hybrid streaming requests
        """
        trace_obj = self.req_info.trace_obj
        with self._trace_span("PDHybrid_Stream", True):
            await self.do_encode()
            self.is_meta = False
            self.logger.debug("Handling hybrid streaming request")
            max_retry = max(self.config.exception_config.transport_retry_limit, 1)
            reschedule_enabled = self.config.exception_config.reschedule_enabled
            api = self.req_info.api
            if reschedule_enabled:
                req_data["return_token_ids"] = True

            for attempt in range(max_retry):
                stream_adapter_state: Dict[str, Any] = {}
                if not self._stream_commit_controller.commit_sealed:
                    self._stream_commit_controller.begin_attempt(attempt + 1)
                try:
                    if attempt > 0:
                        self.rescheduler.is_rescheduling = True
                        self.rescheduler.retry_count = attempt
                        if reschedule_enabled:
                            req_data, api = self.rescheduler.prepare_retry_request(req_data)
                        self.logger.warning("Rescheduling stream[%d/%d] to a new hybrid instance", attempt, max_retry)
                    async with aclosing(
                        self._stream_inference_attempt(req_data, api, attempt, max_retry, stream_adapter_state)
                    ) as attempt_stream:
                        async for chunk in attempt_stream:
                            yield chunk
                    return
                except asyncio.CancelledError as e:
                    reason, cancel_retryable = check_cancel_error(e)
                    retry = (
                        cancel_retryable
                        and attempt < max_retry - 1
                        and (not self._stream_body_sent or reschedule_enabled)
                    )
                    self.logger.warning(
                        "Cancelled stream[%d/%d]: %s because of %s, retry=%s",
                        attempt,
                        max_retry,
                        self._instance_label(),
                        reason,
                        retry,
                    )
                    if not retry:
                        if not cancel_retryable:
                            # Client disconnect or dispatch abort: propagate cancellation;
                            # the engine aborts via upstream connection closure.
                            raise
                        # Node-fault cancel with retries exhausted: clear the pending
                        # cancellation, then raise (don't yield) so the commit-aware response
                        # renders the error itself -- a proper HTTP error before commit, or an
                        # SSE error chunk after. Yielding here would stall _pump_stream on
                        # wait_committed() when the failure happens before HTTP 200 was sent.
                        self._uncancel_current_task()
                        error = RuntimeError(f"Cancelled because of {reason}")
                        trace_obj.set_trace_error_message(str(error))
                        trace_obj.set_trace_error_message(str(error), is_meta=True)
                        self.req_info.update_state(ReqState.EXCEPTION)
                        raise error
                    self._uncancel_current_task()
                except Exception as e:
                    if isinstance(e, HTTPException):
                        transport_retryable = False
                    elif isinstance(e, (UpstreamHTTPError, httpx.RequestError)):
                        transport_retryable = is_retryable_upstream_error(e)
                    else:
                        transport_retryable = True
                    retry = (
                        attempt < max_retry - 1
                        and (not self._stream_body_sent or reschedule_enabled)
                        and transport_retryable
                    )
                    self.logger.error(
                        "Error in streaming (attempt %d/%d): %s", attempt + 1, max_retry, str(e), exc_info=True
                    )
                    if not retry:
                        trace_obj.set_trace_error_message(f"Streaming request failed: {e}")
                        trace_obj.set_trace_error_message(f"Streaming request failed: {e}", is_meta=True)
                        trace_obj.set_trace_status(e)
                        trace_obj.set_trace_exception(e, is_meta=True)
                        self.req_info.update_state(ReqState.EXCEPTION)
                        raise

                wait_time = self.config.exception_config.retry_delay * (2**attempt)
                self.logger.info("Retrying streaming request in %.2f seconds...", wait_time)
                await asyncio.sleep(wait_time)

    async def _generate_post(self, req_data: Dict[str, Any]) -> JSONResponse:
        """
        Handling hybrid non-streaming requests
        """
        trace_obj = self.req_info.trace_obj
        with self._trace_span("PDHybrid", False):
            await self.do_encode()
            self.is_meta = False
            self.logger.debug("Handling hybrid non-streaming request")
            max_retries = max(self.config.exception_config.transport_retry_limit, 1)

            for attempt in range(max_retries):
                try:
                    async with self._inference_lifecycle(attempt, max_retries) as client:
                        response = await self.forward_request(
                            self.req_info.api,
                            req_data,
                            client,
                            self.config.exception_config.infer_timeout,
                        )

                        self.req_info.update_state(ReqState.DECODE_END)
                        body = response.json()
                        if "chat" in self.req_info.effective_entry_api() and body.get("object") == "text_completion":
                            adapt_completion_nonstream_to_chat(body, req_id=self.req_info.req_id)
                        adapters.strip_nonstream_response_body_for_client(
                            body, client_return_token_ids=self.req_info.client_expects_token_ids
                        )
                        return JSONResponse(content=body)

                except asyncio.CancelledError as e:
                    reason, retryable = check_cancel_error(e)
                    retry = retryable and attempt < max_retries - 1
                    self.logger.warning(
                        "Cancelled nonstream[%d/%d]: %s because of %s, retry=%s",
                        attempt,
                        max_retries,
                        self._instance_label(),
                        reason,
                        retry,
                    )
                    if not retry:
                        if not retryable:
                            # Client disconnect or dispatch abort: propagate cancellation;
                            # the engine aborts via upstream connection closure.
                            raise
                        trace_obj.set_trace_error_message(f"Non-streaming request cancelled: {reason}")
                        trace_obj.set_trace_error_message(f"Non-streaming request cancelled: {reason}", is_meta=True)
                        self.req_info.update_state(ReqState.EXCEPTION)
                        raise e
                    self._uncancel_current_task()
                except Exception as e:
                    self.logger.error(
                        "Error in post (attempt %d/%d): %s",
                        attempt + 1,
                        max_retries,
                        str(e),
                    )

                    trace_obj.set_trace_exception(e)
                    trace_obj.set_trace_exception(e, is_meta=True)
                    trace_obj.set_trace_error_message(f"Non-streaming request failed: {e}")
                    trace_obj.set_trace_error_message(f"Non-streaming request failed: {e}", is_meta=True)
                    if isinstance(e, (UpstreamHTTPError, httpx.RequestError)) and not is_retryable_upstream_error(e):
                        self.req_info.update_state(ReqState.EXCEPTION)
                        raise
                    if attempt >= max_retries - 1:
                        self.logger.error("All retries failed for non-streaming decode request.")
                        self.req_info.update_state(ReqState.EXCEPTION)
                        raise e

                wait_time = self.config.exception_config.retry_delay * (2**attempt)
                self.logger.info("Retrying non-streaming request in %.2f seconds...", wait_time)
                await asyncio.sleep(wait_time)
