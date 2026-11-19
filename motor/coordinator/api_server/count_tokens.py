# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Forward Anthropic token counts to a native vLLM endpoint in the worker's service pool."""

from itertools import count

import httpx
from fastapi import HTTPException, status
from fastapi.responses import Response
from starlette.datastructures import Headers

from motor.common.http.http_client import HTTPClientPool
from motor.common.logger import get_logger
from motor.common.resources.endpoint import Endpoint, EndpointStatus
from motor.common.resources.instance import InsStatus, PDRole
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.domain.scheduling import SchedulingFacade
from motor.coordinator.router.upstream_error import (
    UpstreamHTTPError,
    read_bounded_response_body,
    render_transport_error,
    render_upstream_error,
)
from motor.coordinator.tracer.tracing import TracerManager

logger = get_logger(__name__)

COUNT_TOKENS_PATH = "/v1/messages/count_tokens"
COUNT_TOKENS_RESPONSE_MAX_BYTES = 64 * 1024


class CountTokensProxy:
    """Keep admission and round robin on a single inference worker's event loop."""

    def __init__(self) -> None:
        self._next_endpoint = count()
        self._inflight = 0

    async def forward(
        self, scheduler: SchedulingFacade | None, config: CoordinatorConfig, model: str, body: bytes, headers: Headers
    ) -> Response:
        if scheduler is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Scheduler is unavailable")
        settings = config.count_tokens_config
        # Check and increment without yielding to other requests on this event loop.
        if self._inflight >= settings.max_inflight:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many concurrent token count requests")
        self._inflight += 1
        try:
            endpoint = await self._select_endpoint(scheduler, model)
            tracer = TracerManager()
            parent = tracer.extract_trace_context(headers) if tracer.contains_trace_headers(headers) else None
            try:
                with tracer.tracer.start_as_current_span("Count_Tokens", context=parent):
                    return await self._forward(endpoint, config, body, headers)
            except httpx.RequestError as exc:
                logger.warning(
                    "Count Tokens upstream transport failure endpoint=%s:%s error=%s",
                    endpoint.ip,
                    endpoint.business_port,
                    type(exc).__name__,
                )
                return render_transport_error(exc)
        finally:
            self._inflight -= 1

    async def _select_endpoint(self, scheduler: SchedulingFacade, model: str) -> Endpoint:
        get_roles = getattr(scheduler, "get_available_instance_roles", None)
        get_local = getattr(scheduler, "get_local_instances", None)
        get_unblocked = getattr(scheduler, "get_unblocked_instances", None)
        if not all(map(callable, (get_roles, get_local, get_unblocked))):
            logger.warning("Count Tokens scheduler is missing instance or circuit-breaker views")
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Scheduler is unavailable")
        model_key = model.casefold()  # standalone registration compares served model IDs case-insensitively
        roles = await get_roles()
        for role in (PDRole.ROLE_P, PDRole.ROLE_U):
            if role not in roles:
                continue
            instances = await get_local(role)
            matching = [
                ins
                for ins in instances.values()
                if (ins.engine_type or "").strip().lower() == "vllm" and ins.model_name.casefold() == model_key
            ]
            if not matching:
                continue
            unblocked = set(await get_unblocked(role))
            endpoints = [
                ep
                for ins in matching
                if ins.id in unblocked and ins.status == InsStatus.ACTIVE and not ins.recovery_paused
                for ep in ins.get_all_endpoints()
                if ep.status == EndpointStatus.NORMAL
            ]
            if endpoints:
                return endpoints[next(self._next_endpoint) % len(endpoints)]
            # A registered P pool owns the model. Do not silently switch tokenizer profiles
            # when all its endpoints are temporarily unavailable.
            logger.warning("Count Tokens matching pool has no available endpoint role=%s model=%r", role, model[:128])
            break
        else:
            logger.warning("Count Tokens has no matching registered vLLM pool model=%r", model[:128])
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "No available vLLM token count endpoint for model")

    async def _forward(self, endpoint: Endpoint, config: CoordinatorConfig, body: bytes, headers: Headers) -> Response:
        client = await HTTPClientPool().get_client(
            ip=endpoint.ip,
            port=endpoint.business_port,
            tls_config=config.infer_tls_config,
            keepalive_expiry=config.timeout_config.engine_client_keepalive_expiry,
        )
        upstream_headers = {"content-type": "application/json", "accept-encoding": "identity"}
        for name in ("anthropic-version", "anthropic-beta", "x-request-id"):
            if name in headers:
                upstream_headers[name] = headers[name]
        upstream_headers.update(TracerManager().inject_trace_context())
        timeout = config.count_tokens_config.timeout_seconds
        connect = config.exception_config.connect_timeout
        async with client.stream(
            "POST",
            COUNT_TOKENS_PATH,
            content=body,
            headers=upstream_headers,
            timeout=httpx.Timeout(timeout, connect=min(connect, timeout) if connect > 0 else timeout),
        ) as upstream:
            logger.debug(
                "Count Tokens upstream endpoint=%s:%s status=%d request_bytes=%d",
                endpoint.ip,
                endpoint.business_port,
                upstream.status_code,
                len(body),
            )
            if not upstream.is_success:
                error_body, truncated = await read_bounded_response_body(
                    upstream, config.exception_config.upstream_error_body_max_bytes
                )
                return render_upstream_error(
                    UpstreamHTTPError.from_response(
                        upstream,
                        body=error_body,
                        truncated=truncated,
                        phase="count_tokens",
                    )
                )
            # Bound memory before HTTPX can decompress a response that ignored Accept-Encoding.
            if upstream.headers.get("content-encoding", "identity").strip().lower() != "identity":
                logger.warning(
                    "Count Tokens upstream ignored identity encoding endpoint=%s:%s",
                    endpoint.ip,
                    endpoint.business_port,
                )
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY, "Unexpected encoding for upstream token count response"
                )
            response_body, truncated = await read_bounded_response_body(upstream, COUNT_TOKENS_RESPONSE_MAX_BYTES)
            if truncated:
                logger.warning(
                    "Count Tokens upstream response exceeds %d bytes endpoint=%s:%s",
                    COUNT_TOKENS_RESPONSE_MAX_BYTES,
                    endpoint.ip,
                    endpoint.business_port,
                )
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Upstream token count response is too large")
            return Response(
                content=response_body,
                status_code=upstream.status_code,
                headers={"content-type": upstream.headers.get("content-type", "application/json")},
            )
