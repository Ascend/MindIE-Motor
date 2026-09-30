# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import math
import re
import time

import httpx

from motor.common.logger import get_logger
from motor.common.resources.dispatch import DispatchProfile
from motor.common.resources.instance import PDRole
from motor.common.utils.net import format_address
from motor.node_manager.core.services.native_engine.virtual_inference.spec import VirtualInferenceSpec

logger = get_logger(__name__)

VIRTUAL_REQUEST_ID_MARKER = "_virtual"
_WAITING_BY_REASON_METRIC = "vllm:num_requests_waiting_by_reason"
_REASON_LABEL_PATTERN = re.compile(r'(?:^|,)\s*reason="capacity"\s*(?:,|$)')


def generate_request_id() -> str:
    """Generate a virtual request ID carrying the ``_virtual`` marker."""
    current_timestamp = int(time.time() * 1000000)
    request_id = f"{current_timestamp}_virtual"
    logger.debug("Generated virtual request ID: %s", request_id)
    return request_id


def parse_capacity_waiting_requests(metrics_text: str) -> float | None:
    """Return the sum of vLLM capacity-waiting requests, or None when unavailable."""
    capacity_waiting = 0.0
    found = False
    for raw_line in metrics_text.splitlines():
        line = raw_line.strip()
        if not line.startswith(f"{_WAITING_BY_REASON_METRIC}{{"):
            continue
        labels_end = line.find("}")
        if labels_end < 0 or not _REASON_LABEL_PATTERN.search(line[len(_WAITING_BY_REASON_METRIC) + 1 : labels_end]):
            continue
        sample_parts = line[labels_end + 1 :].strip().split()
        if not sample_parts:
            return None
        try:
            value = float(sample_parts[0])
        except ValueError:
            return None
        if not math.isfinite(value) or value < 0:
            return None
        capacity_waiting += value
        found = True
    return capacity_waiting if found else None


class VllmCompletionsRequester:
    """vLLM: lightweight ``POST /v1/completions``, PD-aware for layerwise decode."""

    def __init__(self, spec: VirtualInferenceSpec) -> None:
        self._spec = spec

    async def send(self, client: httpx.AsyncClient, timeout: httpx.Timeout) -> None:
        virtual_request = {"model": self._spec.model_name, "prompt": "1", "max_tokens": 1}
        if self._spec.role == PDRole.ROLE_D and self._spec.dispatch_profile == DispatchProfile.TRIGGER:
            logger.debug("Make virtual request for layerwise decode (endpoint %s)", self._spec.endpoint_id)
            virtual_request["kv_transfer_params"] = {
                "do_remote_decode": False,
                "do_remote_prefill": True,
                "do_virtual": True,
            }

        logger.debug(
            "Sending virtual health check request %s to %s/v1/completions",
            virtual_request,
            format_address(self._spec.host, self._spec.port),
        )
        request_id = generate_request_id()
        response = await client.post(
            "/v1/completions",
            json=virtual_request,
            headers={"Content-Type": "application/json", "X-Request-Id": request_id},
            timeout=timeout,
        )
        response.raise_for_status()


class VllmMetricsRequester:
    """Read vLLM scheduler capacity pressure from the native ``/metrics`` endpoint."""

    @staticmethod
    async def get_capacity_waiting_requests(client: httpx.AsyncClient, timeout: httpx.Timeout) -> float | None:
        response = await client.get("/metrics", timeout=timeout)
        response.raise_for_status()
        return parse_capacity_waiting_requests(response.text)
