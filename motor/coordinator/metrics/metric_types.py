# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Lightweight metric types with zero dependencies.

Extracted from metrics_collector.py to avoid circular imports when
aggregation_engine.py needs to construct Metric objects.
"""

from dataclasses import dataclass, field
from enum import Enum


class AggregationScope(Enum):
    """
    Merge context for _aggregate_metrics (not a 1:1 copy of HTTP metrics type).

    role_scope filtering is enabled only for SERVICE today; NODE is reserved for
    future node-level filtering. DP views do not merge and do not use a scope.
    """

    INSTANCE = "instance"  # per PD instance (endpoints within one instance)
    ROLE = "role"  # same role across instances (get_metrics type=role)
    NODE = "node"  # same (pod_ip, role) across endpoints (type=node)
    SERVICE = "service"  # cluster-wide full metrics (type=full)


@dataclass(frozen=True)
class AggregationContext:
    scope: AggregationScope = AggregationScope.INSTANCE
    instance_roles: dict[int, str] | None = None
    instance_engine_types: dict[int, str] | None = None
    ins_ids: list[int] | None = None


class MetricType(Enum):
    GAUGE = "gauge"
    COUNTER = "counter"
    HISTOGRAM = "histogram"
    SUMMARY = "summary"
    NONE = ""

    def __str__(self):
        return self.value

    @classmethod
    def from_string(cls, type_string):
        return cls[type_string.upper()]


@dataclass
class Metric:
    name: str = ""
    help: str = ""
    type: MetricType = MetricType.NONE
    label: list[str] = field(default_factory=list)
    value: list[float] = field(default_factory=list)

    def copy(self) -> "Metric":
        return Metric(
            name=self.name,
            help=self.help,
            type=self.type,
            label=list(self.label),
            value=list(self.value),
        )


# Motor metric family exposed on /metrics from the scheduler metrics snapshot
# (cnt/fresh_load/a_tokens/total_cnt) plus the collector-side kv usage samples (stat="kv_usage"),
# P-instance per-node KV hit rate samples (stat="kv_hit_tokens"/"kv_input_tokens"/"kv_hit_rate")
# and per-endpoint running/waiting request samples (stat="running"/"waiting").
MOTOR_ENDPOINT_STATE_METRIC = "motor:endpoint_state"

# HELP 文本在 collector 与 scheduler 侧多处构建同一指标族时共用，避免重复定义。
MOTOR_ENDPOINT_STATE_HELP = (
    "Per-endpoint scheduling state (request_count / fresh_load / active_tokens / "
    "total_cnt / kv_usage / kv_hit_tokens / kv_input_tokens / kv_hit_rate / running / waiting)"
)
