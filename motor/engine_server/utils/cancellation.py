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
import functools
from typing import Any, Callable

from fastapi import Request

from motor.common.logger import get_logger

logger = get_logger(__name__)


def _request_log_context(request: Request) -> str:
    request_id = request.headers.get("X-Request-Id", "")
    client = request.client.host if request.client else ""
    return (
        f"method={request.method} path={request.url.path} "
        f"request_id={request_id} client={client}"
    )


async def listen_for_disconnect(request: Request) -> None:
    """Return when the ASGI server reports that the client disconnected."""
    while True:
        message = await request.receive()
        if isinstance(message, dict) and message.get("type") == "http.disconnect":
            logger.info(
                "EngineServer upstream client disconnected: %s",
                _request_log_context(request),
            )
            break


async def _cancel_tasks_and_wait(*tasks: asyncio.Task) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _get_raw_request(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Request | None:
    raw_request = kwargs.get("raw_request")
    if isinstance(raw_request, Request):
        return raw_request
    return next((arg for arg in args if isinstance(arg, Request)), None)


def with_cancellation(handler_func: Callable) -> Callable:
    """Cancel a FastAPI handler when the upstream HTTP client disconnects."""

    @functools.wraps(handler_func)
    async def wrapper(*args, **kwargs):
        raw_request = _get_raw_request(args, kwargs)
        if raw_request is None:
            return await handler_func(*args, **kwargs)

        request_log_context = _request_log_context(raw_request)
        logger.debug("EngineServer cancellation monitor started: %s", request_log_context)
        handler_task = asyncio.create_task(handler_func(*args, **kwargs))
        disconnect_task = asyncio.create_task(listen_for_disconnect(raw_request))

        try:
            done, pending = await asyncio.wait(
                [handler_task, disconnect_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            await _cancel_tasks_and_wait(*pending)

            if handler_task in done:
                logger.debug(
                    "EngineServer handler completed before disconnect: %s",
                    request_log_context,
                )
                return handler_task.result()
            logger.info(
                "EngineServer cancelling handler after upstream disconnect: %s",
                request_log_context,
            )
            return None
        except (Exception, asyncio.CancelledError):
            await _cancel_tasks_and_wait(handler_task, disconnect_task)
            raise

    return wrapper
