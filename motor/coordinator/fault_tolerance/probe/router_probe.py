# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
#
# MindIE is licensed under Mulan PSL v2.
# You may use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Precision probe via full Router + Scheduler pipeline (pinned PD group)."""

from __future__ import annotations

import json

from fastapi import HTTPException, status
from fastapi.responses import JSONResponse

from motor.common.logger import get_logger
from motor.config.coordinator import CoordinatorConfig, DeployMode
from motor.coordinator.domain import InstanceReadiness, SchedulingFacade
from motor.coordinator.domain.request_manager import RequestManager
from motor.coordinator.domain.scheduling_constraint import SchedulingConstraint
from motor.coordinator.fault_tolerance.probe.chat_probe import (
    EXPECTED_ANSWER_SUBSTRING,
    PROBE_USER_QUESTION,
    ChatProbe,
    ProbeOutcome,
    _extract_completion_text,
)
from motor.coordinator.models.request import RequestInfo
from motor.coordinator.router.dispatch import _ROUTER_MAP

logger = get_logger(__name__)


def _resolve_deploy_mode(config: CoordinatorConfig, readiness: InstanceReadiness) -> DeployMode:
    config_mode = config.scheduler_config.deploy_mode
    if (
        config_mode
        in (
            DeployMode.PD_SEPARATE,
            DeployMode.CDP_SEPARATE,
            DeployMode.CPCD_SEPARATE,
            DeployMode.PD_DISAGGREGATION_SINGLE_CONTAINER,
        )
        and readiness == InstanceReadiness.ONLY_PREFILL
    ):
        return DeployMode.SINGLE_NODE
    return config_mode


class InternalRouterProbe(ChatProbe):
    """
    Run fixed QA chat through the same Router path as user traffic, pinning the
    PD group that triggered the precision alarm. sampling_manager is always None.
    """

    def __init__(
        self,
        config: CoordinatorConfig,
        scheduler: SchedulingFacade,
        request_manager: RequestManager,
    ) -> None:
        self._config = config
        self._scheduler = scheduler
        self._request_manager = request_manager

    async def run(
        self,
        *,
        p_instance_id: int | None,
        d_instance_id: int,
        model: str,
        max_attempts: int,
        timeout_seconds: float,
    ) -> ProbeOutcome:
        del timeout_seconds  # Router uses exception_config timeouts

        failures = 0
        details: list[str] = []
        readiness = await self._scheduler.has_required_instances()
        deploy_mode = _resolve_deploy_mode(self._config, readiness)
        router_cls = _ROUTER_MAP.get(deploy_mode)
        if router_cls is None:
            logger.warning("InternalRouterProbe: unknown deploy_mode=%s", deploy_mode)
            return ProbeOutcome(
                failures=max_attempts,
                details=[f"unknown deploy_mode: {deploy_mode}"],
            )

        req_data = {
            "model": (model or "").strip() or "default",
            "messages": [{"role": "user", "content": PROBE_USER_QUESTION}],
            "max_tokens": 64,
            "stream": False,
        }
        body_bytes = json.dumps(req_data).encode("utf-8")
        constraint = SchedulingConstraint.for_precision_probe(
            p_instance_id=p_instance_id,
            d_instance_id=d_instance_id,
        )

        for attempt in range(max_attempts):
            req_id = await self._request_manager.generate_request_id()
            req_info = RequestInfo(
                req_id=req_id,
                req_data=req_data.copy(),
                api="v1/chat/completions",
                req_len=len(body_bytes),
                entry_api="v1/chat/completions",
                client_expects_chat_shape=True,
                scheduling_constraint=constraint,
            )
            router = router_cls(
                req_info,
                self._config,
                scheduler=self._scheduler,
                request_manager=self._request_manager,
                sampling_manager=None,
            )
            try:
                response = await router.handle_request()
            except HTTPException as e:
                logger.warning(
                    "InternalRouterProbe: attempt %d/%d HTTP %s: %s",
                    attempt + 1,
                    max_attempts,
                    e.status_code,
                    e.detail,
                )
                failures += 1
                details.append(f"attempt {attempt + 1}: HTTP {e.status_code}: {e.detail}")
                continue
            except Exception as e:
                logger.warning(
                    "InternalRouterProbe: attempt %d/%d error: %s",
                    attempt + 1,
                    max_attempts,
                    e,
                )
                failures += 1
                details.append(f"attempt {attempt + 1}: {e}")
                continue

            if not isinstance(response, JSONResponse):
                logger.warning(
                    "InternalRouterProbe: attempt %d/%d unexpected response type %s",
                    attempt + 1,
                    max_attempts,
                    type(response).__name__,
                )
                failures += 1
                details.append(f"attempt {attempt + 1}: response.type={type(response).__name__}")
                continue

            if response.status_code != status.HTTP_200_OK:
                logger.warning(
                    "InternalRouterProbe: attempt %d/%d status=%s",
                    attempt + 1,
                    max_attempts,
                    response.status_code,
                )
                failures += 1
                details.append(f"attempt {attempt + 1}: status {response.status_code}")
                continue

            try:
                raw = response.body
                data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            except Exception as e:
                failures += 1
                details.append(f"attempt {attempt + 1}: invalid JSON body: {e}")
                continue

            text = _extract_completion_text(data) if isinstance(data, dict) else ""
            if EXPECTED_ANSWER_SUBSTRING in text:
                logger.info(
                    "InternalRouterProbe: attempt %d/%d ok pd_group=(%s,%s)",
                    attempt + 1,
                    max_attempts,
                    p_instance_id,
                    d_instance_id,
                )
            else:
                logger.warning(
                    "InternalRouterProbe: attempt %d/%d substring mismatch preview=%r",
                    attempt + 1,
                    max_attempts,
                    text[:200],
                )
                failures += 1
                details.append(f"attempt {attempt + 1}: substring mismatch")

        return ProbeOutcome(failures=failures, details=details)
