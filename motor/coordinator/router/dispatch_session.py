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

from motor.common.resources.dispatch import DispatchEndpoint, DispatchEndpoints, MotorDispatch
from motor.common.resources.instance import PDRole
from motor.coordinator.domain import ScheduledResource


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
            endpoints=DispatchEndpoints(
                prefill=_dispatch_endpoint(self.prefill_resource),
                decode=_dispatch_endpoint(self.decode_resource),
            ),
        )


class PDDispatchSession:
    def __init__(self, root_request_id: str) -> None:
        self.root_request_id = root_request_id
        self._attempt_seq = 0
        self.attempts: dict[int, AttemptContext] = {}

    def new_attempt(
        self,
        prefill_resource: ScheduledResource | None,
        decode_resource: ScheduledResource | None,
    ) -> AttemptContext:
        self._attempt_seq += 1
        attempt = AttemptContext(
            root_request_id=self.root_request_id,
            attempt_seq=self._attempt_seq,
            pair_id=uuid.uuid4().hex,
            prefill_resource=prefill_resource,
            decode_resource=decode_resource,
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
        url=f"http://{endpoint.ip}:{endpoint.business_port}",
    )
