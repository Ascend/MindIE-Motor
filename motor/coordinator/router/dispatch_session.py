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
import uuid
from dataclasses import dataclass, field
from enum import Enum

from motor.common.http import HTTPClientPool
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


class AttemptStopReason(str, Enum):
    CLIENT_DISCONNECT = "client_disconnect"
    PEER_FAILED = "peer_failed"
    TIMEOUT = "timeout"
    OTHER = "other"


@dataclass
class AttemptReleaseFlags:
    prefill_tokens: bool = False
    decode_tokens: bool = False


@dataclass
class AttemptContext:
    root_request_id: str
    attempt_seq: int
    pair_id: str
    prefill_resource: ScheduledResource | None = None
    decode_resource: ScheduledResource | None = None
    state: AttemptState = AttemptState.CREATED
    release_flags: AttemptReleaseFlags = field(default_factory=AttemptReleaseFlags)
    prefill_task: asyncio.Task | None = None
    decode_task: asyncio.Task | None = None
    prefill_dispatched: bool = False
    prefill_completed: bool = False
    decode_dispatched: bool = False
    decode_completed: bool = False
    trigger_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    stop_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    config: CoordinatorConfig | None = None

    def transition(self, state: AttemptState) -> bool:
        if self.state == AttemptState.STOPPED:
            return state == AttemptState.STOPPED
        if self.state == AttemptState.STOPPING:
            if state == AttemptState.STOPPED:
                self.state = state
                return True
            return state == AttemptState.STOPPING
        if self.state == AttemptState.DONE:
            return state == AttemptState.DONE
        self.state = state
        return True

    def stop(self) -> None:
        if self.state not in (AttemptState.DONE, AttemptState.STOPPED):
            self.state = AttemptState.STOPPING

    def register_prefill_task(self, task: asyncio.Task) -> asyncio.Task:
        self.prefill_task = task
        return task

    def register_decode_task(self, task: asyncio.Task) -> asyncio.Task:
        self.decode_task = task
        return task

    def mark_dispatched(self, role: str) -> None:
        if role == "prefill":
            self.prefill_dispatched = True
        else:
            self.decode_dispatched = True

    def mark_completed(self, role: str) -> None:
        if role == "prefill":
            self.prefill_completed = True
        else:
            self.decode_completed = True

    def needs_abort(self, role: str) -> bool:
        if role == "prefill":
            return self.prefill_dispatched and not self.prefill_completed
        return self.decode_dispatched and not self.decode_completed

    async def cancel(self, reason: str = ""):
        tasks = []
        current_task = asyncio.current_task()
        if (
            self.prefill_task
            and self.prefill_task is not current_task
            and not self.prefill_task.done()
            and not self.prefill_task.cancelled()
        ):
            logger.info(
                "Cancelling prefill task: %s %s because %s",
                self.prefill_resource.endpoint.ip if self.prefill_resource else "pending",
                self.prefill_resource.instance.job_name if self.prefill_resource else "pending",
                reason,
            )
            self.prefill_task.cancel(msg=reason)
            tasks.append(self.prefill_task)
        if (
            self.decode_task
            and self.decode_task is not current_task
            and not self.decode_task.done()
            and not self.decode_task.cancelled()
        ):
            logger.info(
                "Cancelling decode task: %s %s because %s",
                self.decode_resource.endpoint.ip if self.decode_resource else "pending",
                self.decode_resource.instance.job_name if self.decode_resource else "pending",
                reason,
            )
            self.decode_task.cancel(msg=reason)
            tasks.append(self.decode_task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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
            logger.error("Unregister error: %s", e)

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
            logger.error("Unregister error: %s", e)

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
    def __init__(self, root_request_id: str) -> None:
        self.root_request_id = root_request_id
        self._attempt_seq = 0

    def new_attempt(
        self,
        prefill_resource: ScheduledResource | None,
        decode_resource: ScheduledResource | None,
        config: CoordinatorConfig,
    ) -> AttemptContext:
        self._attempt_seq += 1
        attempt = AttemptContext(
            root_request_id=self.root_request_id,
            attempt_seq=self._attempt_seq,
            pair_id=uuid.uuid4().hex,
            prefill_resource=prefill_resource,
            decode_resource=decode_resource,
            config=config,
        )
        return attempt
