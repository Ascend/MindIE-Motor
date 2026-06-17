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

from motor.common.http.http_client import HTTPClientPool
from motor.common.logger import get_logger
from motor.common.resources.dispatch import (
    DispatchStopReason,
    DispatchStopRequest,
    DispatchStopResponse,
)
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.domain import ScheduledResource
from motor.coordinator.router.dispatch_session import AttemptContext

logger = get_logger(__name__)


class DispatchStopClient:
    def __init__(self, config: CoordinatorConfig) -> None:
        self._config = config

    async def stop(
        self,
        resource: ScheduledResource,
        attempt: AttemptContext,
        reason: DispatchStopReason,
        timeout: float = 1.0,
    ) -> DispatchStopResponse | None:
        if not resource or not resource.endpoint:
            return None

        endpoint = resource.endpoint
        request = DispatchStopRequest(
            root_request_id=attempt.root_request_id,
            engine_request_id=f"{attempt.root_request_id}#a{attempt.attempt_seq}",
            attempt_seq=attempt.attempt_seq,
            pair_id=attempt.pair_id,
            reason=reason.value,
            sent_at_ms=int(time.time() * 1000),
        )
        try:
            client = await HTTPClientPool().get_client(
                ip=endpoint.ip,
                port=endpoint.business_port,
                tls_config=self._config.infer_tls_config,
            )
            response = await client.post(
                "/v1/dispatch/stop",
                json=request.model_dump(mode="json"),
                timeout=timeout,
            )
            response.raise_for_status()
            return DispatchStopResponse.model_validate(response.json())
        except httpx.HTTPError as e:
            logger.warning(
                "Dispatch stop failed root_request_id=%s attempt_seq=%s endpoint=%s:%s error=%s",
                attempt.root_request_id,
                attempt.attempt_seq,
                endpoint.ip,
                endpoint.business_port,
                e,
            )
        except Exception as e:
            logger.warning(
                "Dispatch stop response invalid root_request_id=%s attempt_seq=%s error=%s",
                attempt.root_request_id,
                attempt.attempt_seq,
                e,
            )
        return None
