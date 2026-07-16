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
from motor.coordinator.router.strategies.base import BaseRouter
from motor.coordinator.router.strategies.pd_hybrid import PDHybridRouter
from motor.coordinator.router.strategies.unified_pd import UnifiedPDRouter
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

logger = get_logger(__name__)


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
    value is returned; if the client disconnects first, returns None.
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
                return None
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

    Routing is derived from the roles currently present plus whether a P/D pair shares a
    dispatch capability — no deploy_mode. Shared by user traffic (handle_request) and the
    internal precision probe so both route identically.

    Raises HTTPException(503) when no routable topology is available.
    """
    roles = await scheduler.get_available_instance_roles()
    has_pd_roles = PDRole.ROLE_P in roles and PDRole.ROLE_D in roles
    has_compatible_pair = False
    if has_pd_roles:
        compatibility_check = getattr(scheduler, "has_compatible_pd_pair", None)
        has_compatible_pair = await compatibility_check() if compatibility_check is not None else True
        if has_compatible_pair:
            # Check circuit breaker: only treat as compatible if there are
            # non-blocked instances for BOTH roles
            get_unblocked = getattr(scheduler, "get_unblocked_instances", None)
            if get_unblocked is not None:
                unblocked_p = await get_unblocked(PDRole.ROLE_P)
                unblocked_d = await get_unblocked(PDRole.ROLE_D)
                if not unblocked_p or not unblocked_d:
                    has_compatible_pair = False

    if has_compatible_pair:
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
            message = "PD separate service has no compatible P/D pair and fallback to hybrid is disabled"
        else:
            message = "PD separate service is unavailable and fallback to hybrid is disabled"
        if req_info is not None:
            req_info.trace_obj.set_trace_error_message(message)
        logger.warning("PD separate service cannot route request because hybrid fallback is disabled: %s", message)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=message)

    if PDRole.ROLE_U in roles or PDRole.ROLE_P in roles:
        if has_pd_roles and not has_compatible_pair and PDRole.ROLE_U in roles:
            message = (
                "P/D instances are online but advertise no shared dispatch capability; "
                "falling back to PDHybridRouter via union instances. "
                "Check the engine kv_connector is recognized or set dispatch_profile explicitly."
            )
            if req_info is not None:
                req_info.trace_obj.set_trace_error_message(message)
            logger.warning(message)
        elif has_pd_roles and not has_compatible_pair and req_info is not None:
            error_message = "PD separate service degraded to hybrid: P or D instances circuit-broken or incompatible"
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
) -> Response:
    """Handle incoming requests and route them to appropriate router implementation

    Args:
        raw_request: The incoming FastAPI request object
        request_manager: RequestManager instance (required, injected by InferenceServer)

    Returns:
        Response: The response from the selected router implementation (stream, non-stream, or error)

    Raises:
        HTTPException: If request body is empty or request fail
    """

    req_info = await __create_request_info(raw_request, request_manager)

    if TracerManager().contains_trace_headers(raw_request.headers):
        req_info.trace_obj.parent_context = TracerManager().extract_trace_context(raw_request.headers)

    if scheduler is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Scheduler (SchedulingFacade) is required and must be injected by the server",
        )

    router_impl_class = await select_router_class(scheduler, req_info=req_info, config=config)

    sampling_manager = getattr(raw_request.app.state, "sampling_manager", None)
    router_impl = router_impl_class(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=request_manager,
        sampling_manager=sampling_manager,
    )

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


async def __create_request_info(
    raw_request: Request,
    request_manager: RequestManager,
) -> RequestInfo:
    request_body = await raw_request.body()
    if not request_body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty request body")

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
    req_id = await request_manager.generate_request_id()
    req_len = len(request_body)
    api = validate_and_sanitize_path(raw_request.url.path)

    req_data = request_json.copy()
    client_expects_token_ids = bool(request_json.get("return_token_ids", False))

    return RequestInfo(
        req_id=req_id,
        req_data=req_data,
        api=api,
        req_len=req_len,
        entry_api=api,
        client_expects_token_ids=client_expects_token_ids,
        client_expects_chat_shape=(OpenAIField.MESSAGES in request_json),
    )
