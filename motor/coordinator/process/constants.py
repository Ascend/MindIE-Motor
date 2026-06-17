# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Process orchestration constants: keys and start/stop order for CoordinatorDaemon.
"""

# Process keys for Daemon _process_managers dict
PROCESS_KEY_SCHEDULER = "SchedulerProcess"
PROCESS_KEY_MGMT = "MgmtProcess"
PROCESS_KEY_OBS = "ObsProcess"
PROCESS_KEY_INFERENCE = "InferenceWorkers"

# Start order: Scheduler -> Mgmt -> Obs -> Infer
START_ORDER = [PROCESS_KEY_SCHEDULER, PROCESS_KEY_MGMT, PROCESS_KEY_OBS, PROCESS_KEY_INFERENCE]
# Stop order (reverse): Infer -> Obs -> Mgmt -> Scheduler
STOP_ORDER = [PROCESS_KEY_INFERENCE, PROCESS_KEY_OBS, PROCESS_KEY_MGMT, PROCESS_KEY_SCHEDULER]
