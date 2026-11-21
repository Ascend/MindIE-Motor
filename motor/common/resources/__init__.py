# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Common resources module - contains shared data models and specifications.
"""

__all__ = [
    # From http_msg_spec
    "RegisterMsg",
    "StartCmdMsg",
    "ReregisterMsg",
    "HeartbeatMsg",
    "TerminateInstanceMsg",
    "InsEventMsg",
    "EventType",
    # From instance
    "Instance",
    "PDRole",
    "ReadOnlyInstance",
    "InsStatus",
    "InsConditionEvent",
    "NodeManagerInfo",
    "ParallelConfig",
    # From endpoint
    "Endpoint",
    "Workload",
    "DeviceInfo",
    "EndpointStatus",
]

# Import from http_msg_spec
from .http_msg_spec import (
    RegisterMsg,
    StartCmdMsg,
    ReregisterMsg,
    HeartbeatMsg,
    TerminateInstanceMsg,
    InsEventMsg,
    EventType,
)

# Import from instance
from .instance import (
    Instance,
    PDRole,
    ReadOnlyInstance,
    InsStatus,
    InsConditionEvent,
    NodeManagerInfo,
    ParallelConfig,
)

# Import from endpoint
from .endpoint import (
    Endpoint,
    Workload,
    DeviceInfo,
    EndpointStatus,
)
