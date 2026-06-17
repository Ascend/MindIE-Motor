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

"""Result of scheduler-global precision streak update."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PrecisionStreakResult:
    """Response from Scheduler.record_precision_result (via ZMQ)."""

    skip: bool = False
    threshold_hit: bool = False
    consecutive: int = 0
    action_token: str | None = None
