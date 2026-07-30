# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import threading
import time

from motor.node_manager.core import api_ready_event


def setup_function():
    api_ready_event.clear_api_ready()


def test_wait_until_api_ready_returns_false_on_timeout():
    assert api_ready_event.wait_until_api_ready(timeout=0.01) is False


def test_mark_api_ready_allows_wait_to_succeed():
    api_ready_event.mark_api_ready()
    assert api_ready_event.wait_until_api_ready(timeout=0.1) is True


def test_clear_api_ready_resets_ready_state():
    api_ready_event.mark_api_ready()
    api_ready_event.clear_api_ready()
    assert api_ready_event.wait_until_api_ready(timeout=0.01) is False


def test_wait_until_api_ready_unblocks_after_mark():
    result = {"ready": False}

    def waiter():
        result["ready"] = api_ready_event.wait_until_api_ready(timeout=1.0)

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.05)
    api_ready_event.mark_api_ready()
    thread.join(timeout=1.0)

    assert result["ready"] is True
