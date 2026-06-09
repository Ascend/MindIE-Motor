#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from motor.coordinator.main import main
from motor.coordinator.daemon.coordinator_daemon import CoordinatorDaemon
from motor.coordinator.process.constants import (
    PROCESS_KEY_INFERENCE,
    PROCESS_KEY_MGMT,
    PROCESS_KEY_SCHEDULER,
)


@patch('motor.coordinator.daemon.coordinator_daemon.create_shared_socket')
def test_daemon_stop_all_processes_exclude_mgmt(mock_create_socket):
    """Test _stop_all_processes with exclude_processes skips Mgmt"""
    mock_create_socket.return_value = None  # No socket, so no Infer manager

    mock_config = MagicMock()
    mock_config.standby_config.enable_master_standby = False
    mock_config.api_config.coordinator_api_host = "0.0.0.0"
    mock_config.api_config.coordinator_api_infer_port = 8000
    mock_config.inference_workers_config.num_workers = 1

    mock_scheduler = MagicMock()
    mock_mgmt = MagicMock()
    mock_infer = MagicMock()

    daemon = CoordinatorDaemon(mock_config)
    daemon._process_managers = {
        PROCESS_KEY_SCHEDULER: mock_scheduler,
        PROCESS_KEY_MGMT: mock_mgmt,
        PROCESS_KEY_INFERENCE: mock_infer,
    }

    daemon._stop_all_processes(exclude_processes={PROCESS_KEY_MGMT})

    mock_infer.stop.assert_called_once()
    mock_mgmt.stop.assert_not_called()
    mock_scheduler.stop.assert_called_once()


@patch('motor.coordinator.daemon.coordinator_daemon.create_shared_socket')
def test_daemon_stop_all_processes_no_exclude(mock_create_socket):
    """Test _stop_all_processes without exclude stops all"""
    mock_create_socket.return_value = None

    mock_config = MagicMock()
    mock_config.standby_config.enable_master_standby = False
    mock_config.api_config.coordinator_api_host = "0.0.0.0"
    mock_config.api_config.coordinator_api_infer_port = 8000
    mock_config.inference_workers_config.num_workers = 1

    mock_scheduler = MagicMock()
    mock_mgmt = MagicMock()
    mock_infer = MagicMock()

    daemon = CoordinatorDaemon(mock_config)
    daemon._process_managers = {
        PROCESS_KEY_SCHEDULER: mock_scheduler,
        PROCESS_KEY_MGMT: mock_mgmt,
        PROCESS_KEY_INFERENCE: mock_infer,
    }

    daemon._stop_all_processes()

    mock_infer.stop.assert_called_once()
    mock_mgmt.stop.assert_called_once()
    mock_scheduler.stop.assert_called_once()


@patch('motor.coordinator.daemon.coordinator_daemon.create_shared_socket')
def test_stop_inference_only_stops_inference_only(mock_create_socket):
    """_on_become_standby stops only Inference; Mgmt and Scheduler are not stopped."""
    mock_create_socket.return_value = None

    mock_config = MagicMock()
    mock_config.standby_config.enable_master_standby = False
    mock_config.api_config.coordinator_api_host = "0.0.0.0"
    mock_config.api_config.coordinator_api_infer_port = 8000
    mock_config.inference_workers_config.num_workers = 1

    mock_scheduler = MagicMock()
    mock_mgmt = MagicMock()
    mock_infer = MagicMock()

    daemon = CoordinatorDaemon(mock_config)
    daemon._process_managers = {
        PROCESS_KEY_SCHEDULER: mock_scheduler,
        PROCESS_KEY_MGMT: mock_mgmt,
        PROCESS_KEY_INFERENCE: mock_infer,
    }

    daemon._on_become_standby()

    mock_infer.stop.assert_called_once()
    mock_mgmt.stop.assert_not_called()
    mock_scheduler.stop.assert_not_called()


@patch('motor.coordinator.daemon.coordinator_daemon.create_shared_socket')
def test_start_inference_only_starts_inference_only(mock_create_socket):
    """_on_become_master starts only Inference; Scheduler and Mgmt are not started."""
    mock_create_socket.return_value = None

    mock_config = MagicMock()
    mock_config.standby_config.enable_master_standby = False
    mock_config.api_config.coordinator_api_host = "0.0.0.0"
    mock_config.api_config.coordinator_api_infer_port = 8000
    mock_config.inference_workers_config.num_workers = 1

    mock_scheduler = MagicMock()
    mock_mgmt = MagicMock()
    mock_infer = MagicMock()
    mock_infer.start.return_value = True

    daemon = CoordinatorDaemon(mock_config)
    daemon._process_managers = {
        PROCESS_KEY_SCHEDULER: mock_scheduler,
        PROCESS_KEY_MGMT: mock_mgmt,
        PROCESS_KEY_INFERENCE: mock_infer,
    }

    daemon._on_become_master()

    mock_infer.start.assert_called_once()
    mock_scheduler.start.assert_not_called()
    mock_mgmt.start.assert_not_called()


@patch('motor.coordinator.daemon.coordinator_daemon.create_shared_socket')
def test_start_processes_scheduler_then_inference_order(mock_create_socket):
    """Non-standby flow: _start_processes([SCHEDULER]) then [INFERENCE] calls sleep(2) after Scheduler."""
    mock_create_socket.return_value = None

    mock_config = MagicMock()
    mock_config.standby_config.enable_master_standby = False
    mock_config.api_config.coordinator_api_host = "0.0.0.0"
    mock_config.api_config.coordinator_api_infer_port = 8000
    mock_config.inference_workers_config.num_workers = 1

    mock_scheduler = MagicMock()
    mock_scheduler.start.return_value = True
    mock_infer = MagicMock()
    mock_infer.start.return_value = True

    daemon = CoordinatorDaemon(mock_config)
    daemon._process_managers = {
        PROCESS_KEY_SCHEDULER: mock_scheduler,
        PROCESS_KEY_MGMT: MagicMock(),
        PROCESS_KEY_INFERENCE: mock_infer,
    }

    with patch('motor.coordinator.daemon.coordinator_daemon.time.sleep') as mock_sleep:
        daemon._start_processes([PROCESS_KEY_SCHEDULER])
        daemon._start_processes([PROCESS_KEY_INFERENCE])

    mock_scheduler.start.assert_called_once()
    mock_sleep.assert_called_once_with(2)
    mock_infer.start.assert_called_once()


@pytest.mark.asyncio
async def test_main_daemon_flow():
    """Test main() creates CoordinatorDaemon and runs it"""
    daemon_run = AsyncMock()

    with (
        patch.dict('os.environ', {'USER_CONFIG_PATH': '/fake/config.json'}),
        patch('motor.config.coordinator.CoordinatorConfig.from_json') as mock_from_json,
        patch('motor.coordinator.main.run_port_setup_or_exit') as mock_port_setup,
        patch('motor.coordinator.main.CoordinatorDaemon') as mock_daemon_class,
        patch('motor.coordinator.main.logger') as mock_logger,
    ):
        mock_config = MagicMock()
        mock_config.config_path = '/fake/config.json'
        mock_config.get_config_summary.return_value = "Config summary"
        mock_config.logging_config.log_level = "INFO"
        mock_config.logging_config.log_file = None
        mock_from_json.return_value = mock_config

        mock_daemon_instance = MagicMock()
        mock_daemon_instance.run = daemon_run
        mock_daemon_class.return_value = mock_daemon_instance

        await main()

        mock_port_setup.assert_called_once()
        mock_logger.info.assert_any_call("Starting Motor Coordinator Daemon...")
        mock_daemon_class.assert_called_once_with(mock_config)
        daemon_run.assert_called_once()
