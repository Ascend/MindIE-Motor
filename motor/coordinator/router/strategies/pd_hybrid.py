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
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi import HTTPException

from motor.coordinator.models.request import ReqState
from motor.coordinator.router.strategies.base import BaseRouter
import motor.coordinator.router.adapters as recompute_common
from motor.coordinator.router.adapters.completion_to_chat import adapt_completion_nonstream_to_chat
from motor.common.resources.instance import PDRole
from motor.coordinator.tracer.tracing import TracerManager


class PDHybridRouter(BaseRouter):
    """Handle request with a single PD hybrid instance"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._resolved_roles: tuple[PDRole, ...] | None = None

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
            yield resource

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
            ):
                yield client

    async def handle_request(self) -> StreamingResponse | JSONResponse:
        req_data = self.req_info.req_data.copy()

        if self.req_info.req_data.get("stream", False):
            return StreamingResponse(self._generate_stream(req_data), media_type="text/event-stream")
        return await self._generate_post(req_data)

    async def _stream_inference_attempt(  # pylint: disable=contextmanager-generator-missing-cleanup
        self,
        req_data: Dict[str, Any],
        attempt: int,
        max_retry: int,
    ) -> AsyncGenerator[str, None]:
        trace_obj = self.req_info.trace_obj
        async with self._inference_lifecycle(attempt, max_retry) as client:
            async for chunk in self.forward_stream_request(
                self.req_info.api,
                req_data,
                client,
                self.config.exception_config.first_token_timeout,
            ):
                yield recompute_common.strip_stream_chunk_bytes_for_client(
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
            max_retry = self.config.exception_config.transport_retry_limit

            for attempt in range(max_retry):
                try:
                    async with aclosing(self._stream_inference_attempt(req_data, attempt, max_retry)) as attempt_stream:
                        async for chunk in attempt_stream:
                            yield chunk
                    return
                except asyncio.CancelledError:
                    self.logger.debug("Stream request was cancelled")
                    raise
                except Exception as e:
                    self.logger.error(
                        "Error in streaming (attempt %d/%d): %s", attempt + 1, max_retry, str(e), exc_info=True
                    )

                    # If chunk was already sent, cannot retry the HTTP stream.
                    # Send error chunk and terminate.
                    if self.first_chunk_sent or attempt == max_retry - 1:
                        trace_obj.set_trace_error_message(f"Streaming request failed: {e}")
                        trace_obj.set_trace_error_message(f"Streaming request failed: {e}", is_meta=True)
                        trace_obj.set_trace_status(e)
                        trace_obj.set_trace_exception(e, is_meta=True)
                        self.req_info.update_state(ReqState.EXCEPTION)
                        yield self._generate_streaming_error_chunk(e)
                        return

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
            max_retries = self.config.exception_config.transport_retry_limit

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
                        recompute_common.strip_nonstream_response_body_for_client(
                            body, client_return_token_ids=self.req_info.client_expects_token_ids
                        )
                        return JSONResponse(content=body)

                except asyncio.CancelledError:
                    self.logger.debug("Post request was cancelled")
                    raise
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
                    if attempt < max_retries - 1:
                        wait_time = self.config.exception_config.retry_delay * (2**attempt)
                        self.logger.info("Retrying non-streaming request in %.2f seconds...", wait_time)
                        await asyncio.sleep(wait_time)
                        continue

                    self.logger.error("All retries failed for non-streaming decode request.")
                    self.req_info.update_state(ReqState.EXCEPTION)
                    raise e
