# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Process management infrastructure: base class, constants, mgmt/scheduler/inference process managers.
"""

__all__ = [
    "BaseProcessManager",
    "InferenceProcessManager",
    "MgmtProcessManager",
    "ObsProcessManager",
    "SchedulerProcessManager",
    "create_shared_socket",
    "run_inference_worker_proc",
    "run_obs_server_proc",
    "run_scheduler_server_proc",
    "PROCESS_KEY_INFERENCE",
    "PROCESS_KEY_MGMT",
    "PROCESS_KEY_OBS",
    "PROCESS_KEY_SCHEDULER",
    "START_ORDER",
    "STOP_ORDER",
]

from motor.coordinator.process.base import BaseProcessManager
from motor.coordinator.process.constants import (
    PROCESS_KEY_INFERENCE,
    PROCESS_KEY_MGMT,
    PROCESS_KEY_OBS,
    PROCESS_KEY_SCHEDULER,
    START_ORDER,
    STOP_ORDER,
)
from motor.coordinator.process.inference_manager import (
    InferenceProcessManager,
    create_shared_socket,
    run_inference_worker_proc,
)
from motor.coordinator.process.mgmt_manager import MgmtProcessManager
from motor.coordinator.process.obs_manager import ObsProcessManager, run_obs_server_proc
from motor.coordinator.process.scheduler_manager import (
    SchedulerProcessManager,
    run_scheduler_server_proc,
)
