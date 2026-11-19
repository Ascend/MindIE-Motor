# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Coordinator-unified profiling trigger.

Resolves user-selected targets (service / instance / DP rank / P-D role) from the
live inference topology and fans the vLLM-Ascend PyTorch Profiler start/stop
requests out to the matching native engine endpoints.
"""

from motor.coordinator.profiling.models import (
    ProfilingAction,
    ProfilingResponse,
    ProfilingStartRequest,
    ProfilingTarget,
    ProfilingTargetResult,
    ProfilingTargetSelector,
    ProfilingTargetStatus,
)
from motor.coordinator.profiling.service import (
    ProfilingError,
    ProfilingService,
    ProfilingUnavailableError,
)

__all__ = [
    "ProfilingAction",
    "ProfilingError",
    "ProfilingService",
    "ProfilingResponse",
    "ProfilingStartRequest",
    "ProfilingTarget",
    "ProfilingTargetResult",
    "ProfilingTargetSelector",
    "ProfilingTargetStatus",
    "ProfilingUnavailableError",
]
