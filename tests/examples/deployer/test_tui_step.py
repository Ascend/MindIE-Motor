# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import pytest

from lib.tui.step import PROGRESS_TOTAL, parse_log_line


@pytest.mark.parametrize("previous_status", ["INITIAL", "WAIT2START"])
def test_normal_endpoint_transition_completes_startup_progress(previous_status: str) -> None:
    """Normal and snapshot startup paths must both complete the progress bar."""
    line = f"Native engine rank 0, status change from EndpointStatus.{previous_status} to EndpointStatus.NORMAL"

    assert parse_log_line(line) == PROGRESS_TOTAL
