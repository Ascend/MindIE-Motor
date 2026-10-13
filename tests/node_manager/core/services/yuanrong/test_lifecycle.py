# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Key lifecycle tests for the NodeManager-managed Yuanrong Worker."""

import subprocess
import time
from unittest.mock import MagicMock, patch

import pytest

from motor.config.node_manager import KVCacheStoreConfig
from motor.node_manager.core.services.yuanrong.lifecycle import YuanrongService


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for key in ("DS_WORKER_ADDR", "DATASYSTEM_CLIENT_LOG_DIR", "ETCD_ADDRESS", "HOST_IP"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("POD_IP", "127.0.0.1")


def _service() -> YuanrongService:
    config = KVCacheStoreConfig(
        enable=True,
        backend="yuanrong",
        etcd_address="etcd-svc:2379",
        worker_port=18481,
        shared_memory_size_mb=51200,
    )
    config.worker_launcher.start_args = [{"interleave": "0-7"}, {"d": "/home/yuanrong"}, {"t": 120}]
    config.worker_launcher.worker_args = [{"node_timeout_s": 20}, {"log_only_write_info_file": False}]
    return YuanrongService(config)


def test_build_cmd_preserves_arguments_around_worker_marker():
    assert _service()._build_cmd() == [
        "dscli",
        "start",
        "--interleave",
        "0-7",
        "-d",
        "/home/yuanrong",
        "-t",
        "120",
        "-w",
        "--worker_address",
        "127.0.0.1:18481",
        "--etcd_address",
        "etcd-svc:2379",
        "--shared_memory_size_mb",
        "51200",
        "--node_timeout_s",
        "20",
        "--log_only_write_info_file",
        "false",
    ]


def test_health_check_waits_for_worker_during_startup_grace_period():
    service = _service()
    service._started = True
    service._ready_deadline = time.monotonic() + 30
    service._restart_failures = 2

    with (
        patch.object(service, "_worker_ready", return_value=False),
        patch.object(service, "_worker_process_exists", return_value=True),
        patch.object(service, "_stop_worker_locked") as stop_worker,
        patch.object(service, "_pull_locked") as pull_worker,
    ):
        service.health_check()

    stop_worker.assert_not_called()
    pull_worker.assert_not_called()
    assert service._restart_failures == 2


def test_health_check_stops_stuck_worker_before_restart():
    service = _service()
    service._started = True
    service._ready_deadline = time.monotonic() - 1
    actions = []

    with (
        patch.object(service, "_worker_ready", return_value=False),
        patch.object(service, "_worker_process_exists", side_effect=[True, False]),
        patch.object(service, "_stop_worker_locked", side_effect=lambda: actions.append("stop")),
        patch.object(service, "_pull_locked", side_effect=lambda: actions.append("start")),
    ):
        service.health_check()

    assert actions == ["stop", "start"]


def test_health_check_does_not_duplicate_worker_when_stop_fails():
    service = _service()
    service._started = True
    service._ready_deadline = time.monotonic() - 1

    with (
        patch.object(service, "_worker_ready", return_value=False),
        patch.object(service, "_worker_process_exists", return_value=True),
        patch.object(
            service, "_stop_worker_locked", side_effect=RuntimeError("Worker is still running")
        ) as stop_worker,
        patch.object(service, "_pull_locked") as pull_worker,
    ):
        service.health_check()

    stop_worker.assert_called_once()
    pull_worker.assert_not_called()
    assert service._started is True
    assert service._restart_failures == 1


@pytest.mark.parametrize(
    "inspection",
    [OSError("pgrep unavailable"), subprocess.CompletedProcess([], 2, stdout="", stderr="probe failed")],
)
def test_pull_does_not_spawn_when_worker_inspection_fails(inspection):
    service = _service()

    with (
        patch.object(service, "_is_port_open", return_value=False),
        patch("motor.node_manager.core.services.yuanrong.lifecycle.subprocess.run") as run,
        patch("motor.node_manager.core.services.yuanrong.lifecycle.subprocess.Popen") as popen,
    ):
        if isinstance(inspection, Exception):
            run.side_effect = inspection
        else:
            run.return_value = inspection
        with pytest.raises(RuntimeError, match="Failed to inspect Yuanrong Worker processes"):
            service.pull()

    popen.assert_not_called()


def test_pull_keeps_spawned_worker_monitored_when_followup_probe_fails():
    service = _service()
    launcher = MagicMock()
    launcher.poll.return_value = 0

    with (
        patch.object(service, "_is_port_open", return_value=True),
        patch.object(service, "_worker_process_exists", side_effect=[False, RuntimeError("probe failed")]),
        patch("motor.node_manager.core.services.yuanrong.lifecycle.subprocess.Popen", return_value=launcher),
        patch("motor.node_manager.core.services.yuanrong.lifecycle.os.makedirs"),
        patch("motor.node_manager.core.services.yuanrong.lifecycle.time.sleep"),
        pytest.raises(RuntimeError, match="probe failed"),
    ):
        service.pull()

    assert service._started is True
    assert service._proc is launcher
    assert service._ready_deadline is not None
