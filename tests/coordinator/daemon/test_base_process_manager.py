# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.

"""Unit tests for BaseProcessManager (via concrete subclasses)."""

from unittest.mock import MagicMock

import pytest

from motor.coordinator.process.mgmt_manager import MgmtProcessManager
from motor.config.coordinator import CoordinatorConfig


@pytest.fixture
def mock_config():
    config = MagicMock(spec=CoordinatorConfig)
    config.api_config.coordinator_api_host = "127.0.0.1"
    config.api_config.coordinator_api_infer_port = 18000
    config.inference_workers_config.num_workers = 1
    return config


def test_mgmt_process_manager_get_process_count():
    """MgmtProcessManager manages 1 process."""
    config = MagicMock()
    mgr = MgmtProcessManager(config)
    assert mgr._get_process_count() == 1


def test_mgmt_process_manager_create_process():
    """MgmtProcessManager._create_process returns Process with run_mgmt_server_proc target."""
    config = MagicMock()
    mgr = MgmtProcessManager(config)
    proc = mgr._create_process(0)
    assert proc.name == "MgmtServer"
    assert proc._target.__name__ == "run_mgmt_server_proc"


def test_mgmt_process_manager_start_idempotent():
    """MgmtProcessManager.start is idempotent when already running."""
    config = MagicMock()
    mgr = MgmtProcessManager(config)
    mock_proc = MagicMock()
    mock_proc.is_alive.return_value = True
    mock_proc.pid = 12345
    mgr._processes = [mock_proc]
    mgr._spawn_context = MagicMock()
    mgr._spawn_context.Process.return_value = mock_proc

    result = mgr.start()
    assert result is True
    mgr._spawn_context.Process.assert_not_called()


def test_base_process_manager_terminate_process():
    """_terminate_process calls terminate, join. No kill when process exits."""
    config = MagicMock()
    mgr = MgmtProcessManager(config)
    mock_proc = MagicMock()
    mock_proc.is_alive.side_effect = [True, False]  # alive at start, dead after join

    mgr._terminate_process(mock_proc, timeout=0.01)
    mock_proc.terminate.assert_called_once()
    mock_proc.join.assert_called_once()
    mock_proc.kill.assert_not_called()


def test_base_process_manager_terminate_process_kill_on_timeout():
    """_terminate_process calls kill when join times out."""
    config = MagicMock()
    mgr = MgmtProcessManager(config)
    mock_proc = MagicMock()
    mock_proc.is_alive.return_value = True  # Still alive after join

    mgr._terminate_process(mock_proc, timeout=0.01)
    mock_proc.terminate.assert_called_once()
    mock_proc.join.assert_called_once()
    mock_proc.kill.assert_called_once()
