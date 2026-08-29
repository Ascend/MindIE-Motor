# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
#
# MindIE is licensed under both the Mulan PSL v2 and the Apache License, Version 2.0.
# You may choose to use this software under the terms of either license.
#
# ---------------------------------------------------------------------------
# Mulan PSL v2:
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
#
# Apache License, Version 2.0:
# You may obtain a copy of the License at:
#         http://www.apache.org/licenses/LICENSE-2.0
# ---------------------------------------------------------------------------
#
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the respective licenses for more details.

import asyncio
import string
import uuid
from functools import wraps

import httpx
from fastapi import HTTPException, Request, status
from fastapi.responses import Response

from motor.config.coordinator import CoordinatorConfig
from motor.common.resources.instance import PDRole
from motor.coordinator.models.constants import OpenAIField
from motor.coordinator.models.request import RequestInfo
from motor.coordinator.tracer.tracing import TracerManager
from motor.coordinator.domain.request_manager import RequestManager
from motor.coordinator.domain.agent_hint import (
    apply_session_control_autofill,
    parse_agent_hint,
    ensure_minimum_messages_for_session_edits,
)
from motor.coordinator.router.adapters.pd_protocol import trim_vllm_engine_request_id
from motor.coordinator.router.dispatch_session import AttemptContext
from motor.coordinator.router.strategies.base import BaseRouter
from motor.coordinator.router.strategies.pd_hybrid import PDHybridRouter
from motor.coordinator.router.strategies.unified_pd import UnifiedPDRouter
from motor.coordinator.scheduler.policy.kv_cache_affinity import adapt_context_budget
from motor.coordinator.router.upstream_error import (
    UpstreamHTTPError,
    render_transport_error,
    render_upstream_error,
)
from motor.common.http.security_utils import (
    sanitize_error_message,
    filter_sensitive_headers,
    build_safe_body_structure,
    validate_and_sanitize_path,
)
from motor.common.logger import get_logger
import motor.common.utils.error as cancel_error
from motor.common.utils.error import RequestCancelledError

logger = get_logger(__name__)

_TRACEPARENT_TRACE_ID_INDEX = 1
_TRACE_ID_LENGTH = 32
_ZERO_TRACE_ID = "0" * _TRACE_ID_LENGTH


def _is_valid_trace_id(trace_id: str) -> bool:
    return (
        len(trace_id) == _TRACE_ID_LENGTH
        and trace_id != _ZERO_TRACE_ID
        and all(char in string.hexdigits for char in trace_id)
    )


def _extract_trace_id_from_traceparent(traceparent: str | None) -> str:
    if not traceparent:
        return ""
    parts = traceparent.strip().split("-")
    if len(parts) < 4:
        logger.warning("Invalid traceparent header format, fallback to request id generation")
        return ""
    trace_id = parts[_TRACEPARENT_TRACE_ID_INDEX].lower()
    if not _is_valid_trace_id(trace_id):
        logger.warning("Invalid trace id in traceparent header, fallback to request id generation")
        return ""
    return trace_id


def _append_unique_request_id_suffix(upstream_id: str) -> str:
    # Upstream ids are useful for correlation, but they can be reused across requests
    # or supplied by untrusted clients. Keep them as a prefix and append a short
    # local suffix so Motor's internal request id remains unique.
    return f"{upstream_id}-{uuid.uuid4().hex[:8]}"


# Resolve request id from traceparent or x-request-id header, or generate a new one
# so we can ensure that the request id is unique and can be used to track the request
async def _resolve_request_id(raw_request: Request, request_manager: RequestManager) -> str:
    trace_id = _extract_trace_id_from_traceparent(raw_request.headers.get("traceparent"))
    if trace_id:
        return _append_unique_request_id_suffix(trace_id)

    request_id = (raw_request.headers.get("x-request-id") or "").strip()
    if request_id:
        return _append_unique_request_id_suffix(request_id)

    return await request_manager.generate_request_id()


async def listen_for_disconnect(request: Request) -> None:
    """Returns if a disconnect message is received"""
    while True:
        message = await request.receive()
        if isinstance(message, dict) and message.get("type") == "http.disconnect":
            break


async def _cancel_tasks_and_wait(*tasks: asyncio.Task, reason: str = "") -> None:
    """Cancel given tasks and await them to avoid pending-task warnings."""
    for t in tasks:
        if not t.done():
            t.cancel(msg=reason)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def with_cancellation(handler_func):
    """
    Decorator: cancel the handler when the client disconnects.

    Runs the handler and listen_for_disconnect(request) concurrently; when one
    finishes, the other is cancelled. If the handler finishes first, its return
    value is returned; if the client disconnects first, raises HTTP 499.
    """

    @wraps(handler_func)
    async def wrapper(*args, **kwargs):
        request = args[0] if args else kwargs["raw_request"]
        handler_task = asyncio.create_task(handler_func(*args, **kwargs))
        disconnect_task = asyncio.create_task(listen_for_disconnect(request))

        try:
            done, pending = await asyncio.wait(
                [handler_task, disconnect_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            if handler_task in done:
                await _cancel_tasks_and_wait(*pending)
                return handler_task.result()
            else:
                await _cancel_tasks_and_wait(*pending, reason=cancel_error.CLIENT_DISCONNECT)
                logger.info("Client disconnected; cancelling in-flight request with HTTP 499")
                raise HTTPException(
                    status_code=499,
                    detail=sanitize_error_message(cancel_error.CLIENT_DISCONNECT),
                )
        except HTTPException:
            raise
        except (Exception, asyncio.CancelledError):
            await _cancel_tasks_and_wait(handler_task, disconnect_task, reason=cancel_error.DISPATCH_ABORT)
            raise

    return wrapper


def _is_pd_hybrid_deploy(config: CoordinatorConfig | None) -> bool:
    deploy_config = getattr(config, "deploy_config", None)
    return getattr(deploy_config, "hybrid_instances_num", None) is not None


def _is_pd_separation_fallback_to_hybrid_enabled(config: CoordinatorConfig | None) -> bool:
    scheduler_config = getattr(config, "scheduler_config", None)
    return bool(getattr(scheduler_config, "enable_pd_separation_fallback_to_hybrid", True))


async def select_router_class(
    scheduler,
    req_info: RequestInfo | None = None,
    config: CoordinatorConfig | None = None,
) -> type["BaseRouter"]:
    """Select the router implementation from the live instance topology.

    Routing is derived from the live roles and circuit-breaker state. Native protocol
    selection happens inside UnifiedPDRouter from the selected instance engine_type.
    Shared by user traffic and the internal precision probe so both route identically.

    Raises HTTPException(503) when no routable topology is available.
    """
    roles = await scheduler.get_available_instance_roles()
    has_pd_roles = PDRole.ROLE_P in roles and PDRole.ROLE_D in roles
    has_routable_pd_pair = has_pd_roles
    if has_pd_roles:
        get_unblocked = getattr(scheduler, "get_unblocked_instances", None)
        if get_unblocked is not None:
            unblocked_p = await get_unblocked(PDRole.ROLE_P)
            unblocked_d = await get_unblocked(PDRole.ROLE_D)
            has_routable_pd_pair = bool(unblocked_p and unblocked_d)

    if has_routable_pd_pair:
        return UnifiedPDRouter

    # Degrade to hybrid mode if any unblocked instance is available
    get_unblocked = getattr(scheduler, "get_unblocked_instances", None)
    has_unblocked = False
    if get_unblocked is not None:
        for role in (PDRole.ROLE_U, PDRole.ROLE_P, PDRole.ROLE_D):
            if await get_unblocked(role):
                has_unblocked = True
                break
    else:
        has_unblocked = PDRole.ROLE_U in roles or PDRole.ROLE_P in roles or PDRole.ROLE_D in roles

    if not has_unblocked:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No routable inference topology is currently available: all instances are circuit-broken or absent",
        )

    fallback_enabled = _is_pd_separation_fallback_to_hybrid_enabled(config)
    is_hybrid_deploy = _is_pd_hybrid_deploy(config)
    if not fallback_enabled and not is_hybrid_deploy:
        if has_pd_roles:
            message = "PD separate service has no circuit-breaker-available P/D pair and fallback is disabled"
        else:
            message = "PD separate service is unavailable and fallback to hybrid is disabled"
        if req_info is not None:
            req_info.trace_obj.set_trace_error_message(message)
        logger.warning("PD separate service cannot route request because hybrid fallback is disabled: %s", message)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=message)

    if PDRole.ROLE_U in roles or PDRole.ROLE_P in roles:
        if has_pd_roles and not has_routable_pd_pair and PDRole.ROLE_U in roles:
            message = "P/D instances are unavailable; falling back to PDHybridRouter via union instances"
            if req_info is not None:
                req_info.trace_obj.set_trace_error_message(message)
            logger.warning(message)
        elif has_pd_roles and not has_routable_pd_pair and req_info is not None:
            error_message = "PD separate service degraded to hybrid: P or D instances are circuit-broken"
            req_info.trace_obj.set_trace_error_message(error_message)
            logger.warning(error_message)
        elif req_info is not None and PDRole.ROLE_U not in roles:
            error_message = "PD separate service degraded to hybrid: only prefill instances available"
            req_info.trace_obj.set_trace_error_message(error_message)
            logger.warning(error_message)
        return PDHybridRouter
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="No routable inference topology is currently available",
    )


@with_cancellation
async def handle_request(
    raw_request: Request,
    config: CoordinatorConfig,
    scheduler=None,
    *,
    request_manager: RequestManager,
    request_json: dict | None = None,
) -> Response:
    """Handle incoming requests and route them to appropriate router implementation

    Args:
        raw_request: The incoming FastAPI request object
        request_manager: RequestManager instance (required, injected by InferenceServer)
        request_json: Body already parsed by the API ingress, when available

    Returns:
        Response: The response from the selected router implementation (stream, non-stream, or error)

    Raises:
        HTTPException: If request body is empty or request fail
    """

    req_info = await __create_request_info(raw_request, request_manager, request_json=request_json)

    if TracerManager().contains_trace_headers(raw_request.headers):
        req_info.trace_obj.parent_context = TracerManager().extract_trace_context(raw_request.headers)

    if scheduler is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Scheduler (SchedulingFacade) is required and must be injected by the server",
        )

    tokenization_service = getattr(raw_request.app.state, "tokenization_service", None)
    tokenized_requests = None
    if tokenization_service is not None:
        tokenized_requests = await tokenization_service.tokenize(
            req_info.req_id,
            req_info.api,
            req_info.req_data,
        )
        if tokenized_requests is not None:
            req_info.tokenized_requests = list(tokenized_requests)
            longest_prompt = max(tokenized_requests, key=lambda item: len(item.prompt_token_ids))
            req_info.token_ids = list(longest_prompt.prompt_token_ids)

    adapt_context_budget(req_info, config)
    if tokenized_requests is not None:
        tokenization_service.sync_sampling_params(req_info.req_data, tokenized_requests)

    router_impl_class = await select_router_class(scheduler, req_info=req_info, config=config)

    sampling_manager = getattr(raw_request.app.state, "sampling_manager", None)
    router_impl = router_impl_class(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=request_manager,
        sampling_manager=sampling_manager,
    )
    set_render_client = getattr(router_impl, "set_render_client", None)
    if callable(set_render_client):
        set_render_client(tokenization_service.render_client if tokenization_service is not None else None)

    try:
        return await router_impl.handle_request()
    except UpstreamHTTPError as e:
        req_info.trace_obj.set_trace_error_message(f"Proxy endpoint {req_info.api} failed: {e}")
        logger.warning(
            "Upstream inference request failed api=%s status_code=%s phase=%s",
            req_info.api,
            e.status_code,
            e.phase,
        )
        return render_upstream_error(e)
    except httpx.RequestError as e:
        req_info.trace_obj.set_trace_error_message(f"Proxy endpoint {req_info.api} failed: {e}")
        logger.warning("Upstream inference transport failed api=%s error=%s", req_info.api, e)
        return render_transport_error(e)
    except RequestCancelledError as e:
        req_info.trace_obj.set_trace_error_message(str(e))
        logger.debug(
            "Request cancelled api=%s req_id=%s reason=%s",
            req_info.api,
            req_info.req_id,
            e.reason,
        )
        safe_error_msg = sanitize_error_message(str(e))
        # Client disconnect is not a server fault (nginx-style 499).
        if e.reason == cancel_error.CLIENT_DISCONNECT:
            raise HTTPException(status_code=499, detail=safe_error_msg) from e
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=safe_error_msg) from e
    except Exception as e:
        req_info.trace_obj.set_trace_error_message(f"Proxy endpoint {req_info.api} failed: {e}")
        logger.error(
            f"Error occurred in proxy server endpoint: {req_info.api}, error: {str(e)}",
            exc_info=True,
        )
        if isinstance(e, HTTPException):
            raise e
        safe_error_msg = sanitize_error_message(str(e))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=safe_error_msg) from e


def _parse_trigger_attempt_seq(raw_request: Request) -> int:
    raw_value = raw_request.query_params.get("attempt")
    if raw_value is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing attempt query parameter")
    try:
        return int(raw_value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid attempt query parameter",
        ) from exc


@with_cancellation
async def handle_metaserver_request(
    raw_request: Request,
    config: CoordinatorConfig,
    scheduler=None,
    *,
    request_manager: RequestManager,
) -> dict:
    """Handle Decode-side layerwise callback and forward Prefill to a scheduled P instance."""
    try:
        body = await raw_request.json()
    except Exception as e:
        logger.warning("Metaserver JSON parse failed: %s", e)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON format") from e
    if not isinstance(body, dict) or not body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty request json")

    request_id = trim_vllm_engine_request_id(str(body.get("request_id") or ""))
    batch_index = None
    batch_marker = request_id.rpartition("#p")
    if batch_marker[0] and batch_marker[2].isdigit():
        request_id = batch_marker[0]
        batch_index = int(batch_marker[2])
    if not request_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing request_id")
    req_info = await request_manager.get_req_info(request_id)
    if req_info is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Request ID {request_id} not found",
        )

    attempt_seq = _parse_trigger_attempt_seq(raw_request)
    attempt = req_info._trigger_attempt
    if not isinstance(attempt, AttemptContext):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trigger attempt not found")
    if attempt.attempt_seq != attempt_seq:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Stale trigger attempt callback")
    if batch_index != req_info._trigger_batch_index:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Stale trigger batch callback")

    if scheduler is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Scheduler (SchedulingFacade) is required and must be injected by the server",
        )

    router_impl = UnifiedPDRouter(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=request_manager,
    )
    try:
        return await router_impl.handle_metaserver_request(body)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error occurred in metaserver endpoint: %s", e, exc_info=True)
        safe_error_msg = sanitize_error_message(str(e))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=safe_error_msg) from e


async def __create_request_info(
    raw_request: Request,
    request_manager: RequestManager,
    request_json: dict | None = None,
) -> RequestInfo:
    request_body = await raw_request.body()
    if not request_body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty request body")

    if request_json is None:
        try:
            request_json = await raw_request.json()
        except Exception as e:
            logger.warning("JSON parse failed: %s", e)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON format") from e

    if not request_json:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty request json")
    filtered_headers = filter_sensitive_headers(raw_request.headers)
    filtered_body = build_safe_body_structure(request_json)
    logger.debug("Got request headers: %s, body: %s", filtered_headers, filtered_body)
    req_id = await _resolve_request_id(raw_request, request_manager)
    req_len = len(request_body)
    api = validate_and_sanitize_path(raw_request.url.path)

    req_data = request_json.copy()
    client_expects_token_ids = bool(request_json.get("return_token_ids", False))

    apply_session_control_autofill(request_json)
    ensure_minimum_messages_for_session_edits(request_json, req_data)
    agent_hint_info = parse_agent_hint(
        request_json,
        headers=dict(raw_request.headers),
    )
    return RequestInfo(
        req_id=req_id,
        req_data=req_data,
        api=api,
        req_len=req_len,
        entry_api=api,
        client_expects_token_ids=client_expects_token_ids,
        client_expects_chat_shape=(OpenAIField.MESSAGES in request_json),
        agent_hint_info=agent_hint_info,
    )
