# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import time

import httpx

from motor.common.logger import get_logger
from motor.common.resources.dispatch import DispatchProfile
from motor.common.resources.instance import PDRole
from motor.common.utils.net import format_address
from motor.node_manager.core.services.native_engine.virtual_inference.spec import VirtualInferenceSpec

logger = get_logger(__name__)

VIRTUAL_REQUEST_ID_MARKER = "_virtual"


def generate_request_id() -> str:
    """Generate a virtual request ID carrying the ``_virtual`` marker."""
    current_timestamp = int(time.time() * 1000000)
    request_id = f"{current_timestamp}_virtual"
    logger.debug("Generated virtual request ID: %s", request_id)
    return request_id


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
