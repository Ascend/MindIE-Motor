# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Engine FaultTolerance HTTP client: poll the engine's FT status endpoint.

Single source of truth for talking to the vLLM FT API on the engine's
business port; protocol constants live in ``motor.common.constants``.
"""

from motor.common.constants import ENGINE_FT_TIMEOUT, FT_STATUS_PATH
from motor.common.http.http_client import SafeHTTPSClient
from motor.common.logger import get_logger
from motor.common.resources.endpoint import Endpoint
from motor.common.utils.net import format_address

logger = get_logger(__name__)


def query_engine_ft_status(ep: Endpoint, timeout: float = ENGINE_FT_TIMEOUT) -> dict:
    """GET one engine's FT status payload; raises ValueError on a non-dict body."""
    address = format_address(ep.ip, ep.business_port)
    with SafeHTTPSClient(address=address, tls_config=None, timeout=timeout) as client:
        payload = client.get(FT_STATUS_PATH)
    if not isinstance(payload, dict):
        raise ValueError("unexpected FT status payload type for engine %d: %s" % (ep.id, type(payload).__name__))
    return payload
