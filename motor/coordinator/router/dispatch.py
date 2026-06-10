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

from fastapi import HTTPException, Request, status
from fastapi.responses import StreamingResponse, JSONResponse

from motor.config.coordinator import CoordinatorConfig, DeployMode
from motor.coordinator.models.constants import OpenAIField
from motor.coordinator.models.request import RequestInfo
from motor.coordinator.domain import InstanceReadiness
from motor.coordinator.tracer.tracing import TracerManager
from motor.coordinator.domain.request_manager import RequestManager
from motor.coordinator.router.strategies.base import BaseRouter
from motor.coordinator.router.strategies.pd_hybrid import PDHybridRouter
from motor.coordinator.router.strategies.unified_pd import UnifiedPDRouter
from motor.common.http.security_utils import (
    sanitize_error_message,
    filter_sensitive_headers,
    filter_sensitive_body,
    validate_and_sanitize_path,
)
from motor.common.logger import get_logger

logger = get_logger(__name__)

_ROUTER_MAP: dict[DeployMode, type["BaseRouter"]] = {
    DeployMode.CDP_SEPARATE: UnifiedPDRouter,
    DeployMode.PD_SEPARATE: UnifiedPDRouter,
    DeployMode.CPCD_SEPARATE: UnifiedPDRouter,
    DeployMode.SINGLE_NODE: PDHybridRouter,
    DeployMode.PD_DISAGGREGATION_SINGLE_CONTAINER: UnifiedPDRouter,
    DeployMode.PD_DUAL_DISPATCH: UnifiedPDRouter,
}


async def listen_for_disconnect(request: Request) -> None:
    """Returns if a disconnect message is received"""
    while True:
        message = await request.receive()
        if isinstance(message, dict) and message.get("type") == "http.disconnect":
            break


async def _cancel_tasks_and_wait(*tasks: asyncio.Task) -> None:
    """Cancel given tasks and await them to avoid pending-task warnings."""
    for t in tasks:
        if not t.done():
            t.cancel()
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
            await _cancel_tasks_and_wait(*pending)

            if handler_task in done:
                return handler_task.result()
            return None
        except (Exception, asyncio.CancelledError):
            await _cancel_tasks_and_wait(handler_task, disconnect_task)
            raise

    return wrapper


@with_cancellation
async def handle_request(
    raw_request: Request,
    config: CoordinatorConfig,
    scheduler=None,
    *,
    request_manager: RequestManager,
) -> StreamingResponse | JSONResponse:
    """Handle incoming requests and route them to appropriate router implementation

    Args:
        raw_request: The incoming FastAPI request object
        request_manager: RequestManager instance (required, injected by InferenceServer)

    Returns:
        StreamingResponse: The stream response from the selected router implementation
        JSONResponse: The nonstream response from the selected router implementation

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

    config_mode = config.scheduler_config.deploy_mode
    readiness = await scheduler.has_required_instances()
    if config_mode in (
        DeployMode.PD_SEPARATE,
        DeployMode.CDP_SEPARATE,
        DeployMode.CPCD_SEPARATE,
        DeployMode.PD_DISAGGREGATION_SINGLE_CONTAINER,
    ) and readiness in {InstanceReadiness.ONLY_PREFILL, InstanceReadiness.ENCODE_PREFILL}:
        deploy_mode = DeployMode.SINGLE_NODE  # fallback only when has P but no D
        error_message = f"PD separate service fallback to hybrid mode: config_mode={config_mode}, readiness={readiness}"
        req_info.trace_obj.set_trace_error_message(error_message)
        logger.warning(error_message)
    else:
        deploy_mode = config_mode

    router_impl_class = _ROUTER_MAP.get(deploy_mode)
    if not router_impl_class:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unknown deploy mode: {deploy_mode}",
        )

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
    filtered_body = filter_sensitive_body(request_json)
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
