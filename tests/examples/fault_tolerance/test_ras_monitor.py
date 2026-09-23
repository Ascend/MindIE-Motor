# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


_MODULE_PATH = (
    Path(__file__).parents[3] / "examples" / "features" / "fault_tolerance" / "ras_monitor" / "ras_monitor.py"
)
_SPEC = importlib.util.spec_from_file_location("ras_monitor", _MODULE_PATH)
ras_monitor = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules[_SPEC.name] = ras_monitor
_SPEC.loader.exec_module(ras_monitor)


def _params(api_key: str = ""):
    return ras_monitor.CheckParams(
        with_cert=False,
        model_name="test",
        input_content="test",
        coordinator_port="1025",
        coordinator_obs_port="1027",
        namespace="test",
        coordinator_ip="127.0.0.1",
        management_api_key=api_key,
    )


def test_instances_are_all_unhealthy_requires_non_empty_strict_booleans():
    assert ras_monitor.instances_are_all_unhealthy({"instances": [{"healthy": False}]}) is True
    assert ras_monitor.instances_are_all_unhealthy({"instances": []}) is False
    assert ras_monitor.instances_are_all_unhealthy({"instances": [{"healthy": True}]}) is False
    assert ras_monitor.instances_are_all_unhealthy({"instances": [{"id": 1}]}) is False
    assert ras_monitor.instances_are_all_unhealthy({"instances": [{"healthy": 0}]}) is False


def test_query_all_instances_unhealthy_uses_observability_port_and_api_key():
    response = MagicMock(status=200, data=b'{"instances":[{"healthy":false}]}')
    pool = MagicMock()
    pool.request.return_value = response

    assert ras_monitor.query_all_instances_unhealthy(pool, _params("secret")) is True
    pool.request.assert_called_once_with(
        "GET",
        "http://127.0.0.1:1027/instances",
        headers={"X-Motor-Management-Key": "secret"},
    )


def test_query_failure_does_not_trigger_restart():
    pool = MagicMock()
    pool.request.side_effect = RuntimeError("network down")

    assert ras_monitor.query_all_instances_unhealthy(pool, _params()) is False


def test_two_consecutive_all_unhealthy_results_trigger_restart():
    with (
        patch.object(ras_monitor, "query_all_instances_unhealthy", return_value=True) as query,
        patch.object(ras_monitor.time, "sleep") as sleep,
    ):
        assert ras_monitor.wait_for_all_instances_unhealthy(MagicMock(), _params(), 300) is True

    assert query.call_count == 2
    sleep.assert_called_once_with(ras_monitor.INSTANCE_STATUS_POLL_INTERVAL)


def test_single_all_unhealthy_result_does_not_trigger_restart():
    with (
        patch.object(ras_monitor, "query_all_instances_unhealthy", side_effect=[True, False]) as query,
        patch.object(ras_monitor.time, "monotonic", side_effect=[0, 0, 10]),
        patch.object(ras_monitor.time, "sleep") as sleep,
    ):
        assert ras_monitor.wait_for_all_instances_unhealthy(MagicMock(), _params(), 10) is False

    assert query.call_count == 2
    sleep.assert_called_once_with(ras_monitor.INSTANCE_STATUS_POLL_INTERVAL)


def test_healthy_result_resets_consecutive_all_unhealthy_count():
    with (
        patch.object(ras_monitor, "query_all_instances_unhealthy", side_effect=[True, False, True, True]) as query,
        patch.object(ras_monitor.time, "monotonic", side_effect=[0, 0, 10, 20]),
        patch.object(ras_monitor.time, "sleep") as sleep,
    ):
        assert ras_monitor.wait_for_all_instances_unhealthy(MagicMock(), _params(), 40) is True

    assert query.call_count == 4
    assert sleep.call_count == 3
