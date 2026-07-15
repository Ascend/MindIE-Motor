# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from motor.common.http import HTTPClientPool
from motor.common.resources.dispatch import (
    DispatchEndpoint,
    DispatchEndpoints,
    MotorDispatch,
    PrefillContextBudget,
)
from motor.common.resources.instance import PDRole
from motor.common.utils.net import format_address
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.domain import ScheduledResource

from motor.common.logger import get_logger

logger = get_logger(__name__)


class AttemptState(str, Enum):
    CREATED = "created"
    DISPATCHING = "dispatching"
    ACTIVE = "active"
    FIRST_VISIBLE = "first_visible"
    DONE = "done"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass
class AttemptReleaseFlags:
    prefill_tokens: bool = False
    prefill_kv: bool = False
    decode_tokens: bool = False
    decode_kv: bool = False


@dataclass
class AttemptContext:
    root_request_id: str
    attempt_seq: int
    pair_id: str
    prefill_context_budget: PrefillContextBudget | None = None
    prefill_resource: ScheduledResource | None = None
    decode_resource: ScheduledResource | None = None
    state: AttemptState = AttemptState.CREATED
    first_visible_sent: bool = False
    stop_sent: bool = False
    release_flags: AttemptReleaseFlags = field(default_factory=AttemptReleaseFlags)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    prefill_task: asyncio.Task | None = None
    decode_task: asyncio.Task | None = None
    stop_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    config: CoordinatorConfig | None = None
    fail_reason: str | None = None

    def transition(self, state: AttemptState) -> bool:
        if self.state == AttemptState.STOPPED:
            return state == AttemptState.STOPPED
        if self.state == AttemptState.STOPPING:
            if state == AttemptState.STOPPED:
                self.state = state
                self.updated_at = time.time()
                return True
            return state == AttemptState.STOPPING
        if self.state == AttemptState.DONE:
            return state == AttemptState.DONE
        self.state = state
        self.updated_at = time.time()
        if state == AttemptState.FIRST_VISIBLE:
            self.first_visible_sent = True
        return True

    def stop(self) -> None:
        if self.state not in (AttemptState.DONE, AttemptState.STOPPED):
            self.state = AttemptState.STOPPING
            self.stop_sent = True
            self.updated_at = time.time()

    def dispatch_for(self, role: PDRole, dispatch_mode: str) -> MotorDispatch:
        return MotorDispatch(
            root_request_id=self.root_request_id,
            engine_request_id=f"{self.root_request_id}#a{self.attempt_seq}",
            pair_id=self.pair_id,
            attempt_seq=self.attempt_seq,
            role="prefill" if role == PDRole.ROLE_P else "decode",
            dispatch_mode=dispatch_mode,
            prefill_context_budget=self.prefill_context_budget,
            endpoints=DispatchEndpoints(
                prefill=_dispatch_endpoint(self.prefill_resource),
                decode=_dispatch_endpoint(self.decode_resource),
            ),
        )

    def register_prefill_task(self, task: asyncio.Task) -> asyncio.Task:
        self.prefill_task = task
        return task

    def register_decode_task(self, task: asyncio.Task) -> asyncio.Task:
        self.decode_task = task
        return task

    async def cancel(self, reason: str = ""):
        self.fail_reason = reason
        task = []
        if self.prefill_task and not self.prefill_task.done() and not self.prefill_task.cancelled():
            logger.info(
                f"Cancelling prefill task: {self.prefill_resource.endpoint.ip} {self.prefill_resource.instance.job_name}"
                f" because {reason}"
            )
            self.prefill_task.cancel(msg=reason)
            task.append(self.prefill_task)
        if self.decode_task and not self.decode_task.done() and not self.decode_task.cancelled():
            logger.info(
                f"Cancelling decode task: {self.decode_resource.endpoint.ip} {self.decode_resource.instance.job_name}"
                f" because {reason}"
            )
            self.decode_task.cancel(msg=reason)
            task.append(self.decode_task)
        if task:
            await asyncio.gather(*task, return_exceptions=True)

    def register_canceller(self):
        pool = HTTPClientPool()
        if self.prefill_resource:
            p_key = pool._get_pool_key(
                self.prefill_resource.endpoint.ip,
                self.prefill_resource.endpoint.business_port,
                self.config.infer_tls_config,
            )
            pool.register_canceller(p_key, self.pair_id, self.cancel)
        if self.decode_resource:
            d_key = pool._get_pool_key(
                self.decode_resource.endpoint.ip,
                self.decode_resource.endpoint.business_port,
                self.config.infer_tls_config,
            )
            pool.register_canceller(d_key, self.pair_id, self.cancel)

    def unregister_canceller(self):
        try:
            pool = HTTPClientPool()
            if self.prefill_resource:
                p_key = pool._get_pool_key(
                    self.prefill_resource.endpoint.ip,
                    self.prefill_resource.endpoint.business_port,
                    self.config.infer_tls_config,
                )
                pool.unregister_canceller(p_key, self.pair_id)
            if self.decode_resource:
                d_key = pool._get_pool_key(
                    self.decode_resource.endpoint.ip,
                    self.decode_resource.endpoint.business_port,
                    self.config.infer_tls_config,
                )
                pool.unregister_canceller(d_key, self.pair_id)
        except Exception as e:
            logger.error(f"Unregister error {e=}")
            pass

    def unregister_prefill_canceller(self):
        try:
            pool = HTTPClientPool()
            if self.prefill_resource:
                p_key = pool._get_pool_key(
                    self.prefill_resource.endpoint.ip,
                    self.prefill_resource.endpoint.business_port,
                    self.config.infer_tls_config,
                )
                pool.unregister_canceller(p_key, self.pair_id)
        except Exception as e:
            logger.error(f"Unregister error {e=}")
            pass

    def register_decode_canceller(self):
        if not self.decode_resource:
            return
        pool = HTTPClientPool()
        d_key = pool._get_pool_key(
            self.decode_resource.endpoint.ip,
            self.decode_resource.endpoint.business_port,
            self.config.infer_tls_config,
        )
        pool.register_canceller(d_key, self.pair_id, self.cancel)


class PDDispatchSession:
    def __init__(
        self,
        root_request_id: str,
        prefill_context_budget: PrefillContextBudget | None = None,
    ) -> None:
        self.root_request_id = root_request_id
        self.prefill_context_budget = prefill_context_budget
        self._attempt_seq = 0
        self.attempts: dict[int, AttemptContext] = {}

    def new_attempt(
        self,
        prefill_resource: ScheduledResource | None,
        decode_resource: ScheduledResource | None,
        config: CoordinatorConfig,
        *,
        consumed_output_tokens: int = 0,
    ) -> AttemptContext:
        self._attempt_seq += 1
        budget = self.prefill_context_budget
        if budget is not None:
            budget = budget.after_output_tokens(consumed_output_tokens)
        attempt = AttemptContext(
            root_request_id=self.root_request_id,
            attempt_seq=self._attempt_seq,
            pair_id=uuid.uuid4().hex,
            prefill_context_budget=budget,
            prefill_resource=prefill_resource,
            decode_resource=decode_resource,
            config=config,
        )
        self.attempts[attempt.attempt_seq] = attempt
        return attempt


def _dispatch_endpoint(resource: ScheduledResource | None) -> DispatchEndpoint | None:
    if not resource or not resource.instance or not resource.endpoint:
        return None
    endpoint = resource.endpoint
    return DispatchEndpoint(
        instance_id=int(resource.instance.id),
        endpoint_id=int(endpoint.id),
        url=f"http://{format_address(endpoint.ip, endpoint.business_port)}",
    )
