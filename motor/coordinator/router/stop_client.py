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
    DispatchStopState,
)
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.domain import ScheduledResource
from motor.coordinator.router.dispatch_session import AttemptContext
from motor.coordinator.router.sglang_native_dispatch import is_sglang_resource

logger = get_logger(__name__)


def _engine_request_id(attempt: AttemptContext) -> str:
    return f"{attempt.root_request_id}#a{attempt.attempt_seq}"


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

        if is_sglang_resource(resource):
            return await self._stop_sglang_native(resource, attempt, reason, timeout)
        return await self._stop_motor_dispatch(resource, attempt, reason, timeout)

    async def _stop_sglang_native(
        self,
        resource: ScheduledResource,
        attempt: AttemptContext,
        reason: DispatchStopReason,
        timeout: float,
    ) -> DispatchStopResponse | None:
        """Abort via stock SGLang ``POST /abort_request`` (no InferEndpoint stop API)."""
        endpoint = resource.endpoint
        engine_request_id = _engine_request_id(attempt)
        try:
            client = await HTTPClientPool().get_client(
                ip=endpoint.ip,
                port=endpoint.business_port,
                tls_config=self._config.infer_tls_config,
            )
            response = await client.post(
                "/abort_request",
                json={"rid": engine_request_id},
                timeout=timeout,
            )
            response.raise_for_status()
            logger.info(
                "SGLang abort_request accepted root_request_id=%s attempt_seq=%s endpoint=%s:%s rid=%s reason=%s",
                attempt.root_request_id,
                attempt.attempt_seq,
                endpoint.ip,
                endpoint.business_port,
                engine_request_id,
                reason.value,
            )
            return DispatchStopResponse(
                root_request_id=attempt.root_request_id,
                attempt_seq=attempt.attempt_seq,
                accepted=True,
                state=DispatchStopState.STOPPED,
                message="sglang:/abort_request",
            )
        except httpx.HTTPError as e:
            logger.warning(
                "SGLang abort_request failed root_request_id=%s attempt_seq=%s endpoint=%s:%s rid=%s error=%s",
                attempt.root_request_id,
                attempt.attempt_seq,
                endpoint.ip,
                endpoint.business_port,
                engine_request_id,
                e,
            )
        except Exception as e:
            logger.warning(
                "SGLang abort_request unexpected error root_request_id=%s attempt_seq=%s error=%s",
                attempt.root_request_id,
                attempt.attempt_seq,
                e,
            )
        return None

    async def _stop_motor_dispatch(
        self,
        resource: ScheduledResource,
        attempt: AttemptContext,
        reason: DispatchStopReason,
        timeout: float,
    ) -> DispatchStopResponse | None:
        endpoint = resource.endpoint
        request = DispatchStopRequest(
            root_request_id=attempt.root_request_id,
            engine_request_id=_engine_request_id(attempt),
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
