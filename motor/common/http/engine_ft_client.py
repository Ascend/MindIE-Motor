# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Engine FaultTolerance HTTP client for status and recovery instructions.

Single source of truth for talking to the vLLM FT API on the engine's
business port; protocol constants live in ``motor.common.constants``.
"""

import concurrent.futures
from collections.abc import Callable
from functools import partial
from typing import Any, TypeVar

from motor.common.constants import (
    ENGINE_FT_TIMEOUT,
    FT_APPLY_PATH,
    FT_STATUS_PATH,
)
from motor.common.http.http_client import SafeHTTPSClient
from motor.common.resources.endpoint import Endpoint
from motor.common.utils.net import format_address


_Result = TypeVar("_Result")


def _run_engine_requests(endpoints: list[Endpoint], request: Callable[[Endpoint], _Result]) -> dict[int, _Result]:
    if not endpoints:
        return {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(endpoints)) as executor:
        futures = [executor.submit(request, endpoint) for endpoint in endpoints]
        return {endpoint.id: future.result() for endpoint, future in zip(endpoints, futures)}


class EngineFtApplyError(RuntimeError):
    """HTTP rejection from the versioned engine FT apply endpoint."""

    def __init__(self, engine_id: int, status_code: int) -> None:
        self.engine_id = engine_id
        self.status_code = status_code
        message = "engine %d FT apply expected HTTP 202, got %d"
        super().__init__(message % (engine_id, status_code))


def query_engine_ft_status(ep: Endpoint, timeout: float = ENGINE_FT_TIMEOUT) -> dict:
    """GET one engine's FT status payload; raises ValueError on a non-dict body."""
    address = format_address(ep.ip, ep.business_port)
    with SafeHTTPSClient(address=address, tls_config=None, timeout=timeout) as client:
        payload = client.get(FT_STATUS_PATH)
    if not isinstance(payload, dict):
        raise ValueError("unexpected FT status payload type for engine %d: %s" % (ep.id, type(payload).__name__))
    return payload


def query_engine_ft_entry(ep: Endpoint, timeout: float = ENGINE_FT_TIMEOUT) -> dict[str, Any]:
    """Return the status entry matching one endpoint rank."""
    engines = query_engine_ft_status(ep, timeout).get("engines")
    if not isinstance(engines, list) or not engines:
        raise ValueError("engine %d FT status contains no engines" % ep.id)
    for status in engines:
        if isinstance(status, dict) and status.get("id") == ep.id:
            return status
    raise ValueError("engine %d FT status has no matching rank" % ep.id)


def query_engine_ft_entries(endpoints: list[Endpoint], timeout: float = ENGINE_FT_TIMEOUT) -> dict[int, dict[str, Any]]:
    """Query status entries concurrently for a group of engine endpoints."""
    return _run_engine_requests(endpoints, partial(query_engine_ft_entry, timeout=timeout))


def apply_engine_ft_instruction(
    ep: Endpoint,
    instruction: str,
    params: dict | None = None,
    request_id: str = "",
    timeout: float = ENGINE_FT_TIMEOUT,
) -> dict:
    """Submit one asynchronous FT instruction to an engine.

    vLLM returns HTTP 202 as soon as the instruction is accepted. The
    response is therefore an acknowledgement only; callers must poll
    :func:`query_engine_ft_status` for the terminal outcome.
    """
    if instruction not in {"retry", "scale_down"}:
        raise ValueError("unsupported engine FT instruction: %s" % instruction)
    if not isinstance(params, dict) and params is not None:
        raise TypeError("engine FT params must be a dict")
    if not isinstance(request_id, str):
        raise TypeError("engine FT request_id must be a string")

    address = format_address(ep.ip, ep.business_port)
    body = {
        "instruction": instruction,
        "params": params or {},
        "request_id": request_id,
    }
    with SafeHTTPSClient(address=address, tls_config=None, timeout=timeout) as client:
        response = client.do_post(FT_APPLY_PATH, data=body)
    if response.status_code != 202:
        raise EngineFtApplyError(ep.id, response.status_code)
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("unexpected FT apply payload type for engine %d: %s" % (ep.id, type(payload).__name__))
    return payload


def apply_engine_ft_instructions(
    endpoints: list[Endpoint],
    instruction: str,
    params: dict | None = None,
    request_id: str = "",
    timeout: float = ENGINE_FT_TIMEOUT,
) -> None:
    """Submit the same FT instruction once to every selected engine."""
    _run_engine_requests(
        endpoints,
        partial(
            apply_engine_ft_instruction,
            instruction=instruction,
            params=params,
            request_id=request_id,
            timeout=timeout,
        ),
    )
