# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from dataclasses import dataclass

from motor.common.resources.dispatch import DispatchProfile
from motor.common.resources.instance import PDRole
from motor.config.tls_config import TLSConfig


@dataclass(frozen=True)
class TargetIdentity:
    """Immutable target endpoint identity; any field change requires monitor replacement."""

    instance_id: int
    endpoint_id: int
    host: str
    port: int
    engine_type: str


@dataclass(frozen=True)
class VirtualInferenceSpec:
    """Immutable vLLM virtual inference config for one DP0 target (from deploy health_check_config)."""

    instance_id: int
    endpoint_id: int
    host: str
    port: int
    role: PDRole
    engine_type: str
    model_name: str
    dispatch_profile: DispatchProfile
    tls_config: TLSConfig | None
    enabled: bool
    npu_usage_threshold: int
    max_failure_count: int
    # vLLM per-request timeout (health_check_config.virtual_inference_timeout).
    request_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if isinstance(self.request_timeout_seconds, bool) or not isinstance(self.request_timeout_seconds, (int, float)):
            raise ValueError("request_timeout_seconds must be a number")
        if self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be > 0")

    @property
    def identity(self) -> TargetIdentity:
        """Return target identity for monitor reconciliation."""
        return TargetIdentity(
            instance_id=self.instance_id,
            endpoint_id=self.endpoint_id,
            host=self.host,
            port=self.port,
            engine_type=self.engine_type,
        )
