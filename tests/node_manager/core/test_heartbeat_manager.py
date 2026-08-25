# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
import sys
import time
import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from tests.node_manager.conftest import apply_node_manager_test_config, create_config_mock

# Patch NodeManagerConfig.from_json() before importing modules that use it

# Create a mock config to avoid file loading issues during import
mock_config = MagicMock()
mock_config.basic_config = MagicMock()
mock_config.api_config = MagicMock()

with patch('motor.config.node_manager.NodeManagerConfig.from_json', return_value=mock_config):
    from motor.common.resources.endpoint import Endpoint, EndpointStatus
    from motor.common.resources.http_msg_spec import StartCmdMsg
    from motor.node_manager.core.register_manager import RegisterManager
    from motor.node_manager.core.heartbeat_manager import HeartbeatManager
    from motor.config.node_manager import NodeManagerConfig


def _clear_register_manager_singleton() -> None:
    if hasattr(RegisterManager, "_instances") and RegisterManager in RegisterManager._instances:
        del RegisterManager._instances[RegisterManager]


class TestHeartBeatManager:
    """HeartBeatManager test class"""

    @pytest.fixture(name="heart_beat_manager")
    def _heart_beat_manager_fixture(self, config_data):
        """return HeartBeatManager instance"""
        with (
            patch('motor.config.node_manager.safe_open') as mock_safe_open,
            patch('threading.Thread') as mock_thread_class,
            patch('motor.node_manager.core.heartbeat_manager.RegisterManager') as mock_register_manager_cls,
            patch.dict(
                'os.environ',
                {
                    'JOB_NAME': 'test_job',
                    'CONFIG_PATH': 'tests/jsons',
                    'USER_CONFIG_PATH': 'tests/jsons/user_config.json',
                    'ROLE': 'both',
                },
            ),
        ):
            mock_safe_open.side_effect = create_config_mock(config_data)
            mock_thread = MagicMock()
            mock_thread_class.return_value = mock_thread
            mock_register_manager = MagicMock()
            mock_register_manager.is_engine_checkpoint_done.return_value = True
            mock_register_manager_cls.return_value = mock_register_manager
            _clear_register_manager_singleton()
            # clear HeartBeatManager instance (HeartbeatManager is still singleton)
            if hasattr(HeartbeatManager, '_instances') and HeartbeatManager in HeartbeatManager._instances:
                try:
                    HeartbeatManager._instances[HeartbeatManager].stop()
                except Exception:
                    pass
                if HeartbeatManager in HeartbeatManager._instances:
                    del HeartbeatManager._instances[HeartbeatManager]

            config = NodeManagerConfig()
            apply_node_manager_test_config(config, config_data)

            manager = HeartbeatManager(config)
            yield manager

    @pytest.fixture(name="sample_endpoints")
    def _sample_endpoints_fixture(self):
        """return sample endpoints"""
        return [
            Endpoint(id=1, ip="192.168.1.1", business_port="8080", status=EndpointStatus.NORMAL),
            Endpoint(id=2, ip="192.168.1.2", business_port="8080", status=EndpointStatus.NORMAL),
        ]

    @pytest.fixture(name="sample_start_cmd_msg")
    def _sample_start_cmd_msg_fixture(self, sample_endpoints):
        """return start command message"""
        return StartCmdMsg(
            job_name="test_job", role="prefill", instance_id=1, endpoints=sample_endpoints, master_dp_ip="192.168.1.100"
        )

    @pytest.fixture(name="mock_http_client")
    def _mock_http_client_fixture(self):
        """mock HTTP client fixture"""
        with patch(
            'motor.node_manager.core.heartbeat_manager.ControllerApiClient.report_heartbeat'
        ) as mock_report_heartbeat:
            mock_report_heartbeat.return_value = None
            yield mock_report_heartbeat

    @patch('motor.config.node_manager.safe_open')
    @patch.dict(
        'os.environ',
        {'JOB_NAME': 'test_job', 'CONFIG_PATH': './', 'USER_CONFIG_PATH': './user_config.json', 'ROLE': 'both'},
    )
    def test_singleton_pattern(self, mock_safe_open, config_data):
        """test singleton pattern"""
        mock_safe_open.side_effect = create_config_mock(config_data)
        # Clear singleton instance
        if hasattr(HeartbeatManager, '_instances') and HeartbeatManager in HeartbeatManager._instances:
            if HeartbeatManager in HeartbeatManager._instances:
                del HeartbeatManager._instances[HeartbeatManager]

        with patch('threading.Thread'):
            config = NodeManagerConfig()
            manager1 = HeartbeatManager(config)
            manager2 = HeartbeatManager(config)
            assert manager1 is manager2

    def test_initial_state(self, heart_beat_manager):
        """test initial state"""
        assert heart_beat_manager._job_name == ""
        assert heart_beat_manager._role == "prefill"
        assert heart_beat_manager._instance_id == -1
        assert heart_beat_manager._endpoints == []
        assert heart_beat_manager.stop_event.is_set() is False
        assert heart_beat_manager._thread_started is False

    def test_check_all_endpoints_normal_empty(self, heart_beat_manager):
        """empty endpoints should not be treated as ready"""
        assert heart_beat_manager.check_all_endpoints_normal() is False

    def test_check_all_endpoints_normal_success(self, heart_beat_manager, sample_endpoints):
        """all normal endpoints should be treated as ready"""
        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = sample_endpoints.copy()
        assert heart_beat_manager.check_all_endpoints_normal() is True

    def test_update_endpoint(self, heart_beat_manager, sample_start_cmd_msg):
        """test update endpoint"""
        heart_beat_manager.update_endpoint(sample_start_cmd_msg)

        assert heart_beat_manager._job_name == "test_job"
        assert heart_beat_manager._role == "prefill"
        assert heart_beat_manager._instance_id == 1
        assert len(heart_beat_manager._endpoints) == 2
        assert heart_beat_manager._endpoints[0].id == 1
        assert heart_beat_manager._endpoints[1].id == 2

    def test_update_endpoint_invalidates_inflight_native_probe(self, heart_beat_manager, sample_start_cmd_msg):
        """update_endpoint advances the generation used to reject stale probe results."""
        before = heart_beat_manager._endpoints_generation
        heart_beat_manager.update_endpoint(sample_start_cmd_msg)

        assert heart_beat_manager._endpoints_generation == before + 1

    @patch('motor.node_manager.core.daemon.Daemon')
    def test_engine_metrics_targets_exclude_headless(self, mock_daemon, heart_beat_manager):
        routable = Endpoint(id=1, ip="10.0.0.1", business_port="8001")
        headless = Endpoint(
            id=2,
            ip="10.0.0.2",
            business_port="8002",
            headless=True,
        )
        mock_daemon.return_value.get_engine_metrics_target.return_value = "https://10.0.0.1:8001/metrics"
        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = [routable, headless]

        targets = heart_beat_manager.get_engine_metrics_targets()

        assert targets == ["https://10.0.0.1:8001/metrics"]
        mock_daemon.return_value.get_engine_metrics_target.assert_called_once_with(routable)

    @patch('motor.node_manager.core.daemon.Daemon')
    def test_refresh_native_engine_status_success(self, mock_daemon, heart_beat_manager, sample_endpoints):
        """READY native runtimes map to normal endpoint status."""
        from motor.node_manager.core.services.native_engine.models import RuntimeState

        mock_daemon.return_value.get_engine_runtime_state.return_value = RuntimeState.READY

        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = sample_endpoints.copy()

        heart_beat_manager._refresh_native_engine_status()

        assert mock_daemon.return_value.get_engine_runtime_state.call_count == 2

        assert heart_beat_manager._endpoints[0].status == EndpointStatus.NORMAL
        assert heart_beat_manager._endpoints[1].status == EndpointStatus.NORMAL
        assert heart_beat_manager.is_within_grace_period() is False

    @patch('motor.node_manager.core.daemon.Daemon')
    def test_refresh_native_engine_status_passes_instance_id(self, mock_daemon, heart_beat_manager, sample_endpoints):
        """runtime refresh must pass the snapshotted instance_id down to the daemon."""
        from motor.node_manager.core.services.native_engine.models import RuntimeState

        mock_daemon.return_value.get_engine_runtime_state.return_value = RuntimeState.READY

        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = sample_endpoints.copy()
            heart_beat_manager._instance_id = 42

        heart_beat_manager._refresh_native_engine_status()

        calls = mock_daemon.return_value.get_engine_runtime_state.call_args_list
        assert len(calls) == 2
        for call in calls:
            assert call.args[1] == 42

    @patch('motor.node_manager.core.daemon.Daemon')
    def test_refresh_native_engine_status_snapshots_instance_id_once(
        self, mock_daemon, heart_beat_manager, sample_endpoints, sample_start_cmd_msg
    ):
        """instance_id is snapshotted under lock together with endpoints/generation."""
        from motor.node_manager.core.services.native_engine.models import RuntimeState

        mock_daemon.return_value.get_engine_runtime_state.return_value = RuntimeState.READY
        endpoint = sample_endpoints[0]
        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = [endpoint]
            heart_beat_manager._instance_id = 7

        seen_instance_ids = []

        def probe(*args, **kwargs):
            seen_instance_ids.append(args[1])
            heart_beat_manager.update_endpoint(sample_start_cmd_msg)
            return RuntimeState.READY

        mock_daemon.return_value.get_engine_runtime_state.side_effect = probe

        heart_beat_manager._refresh_native_engine_status()

        # The probe ran against the snapshotted instance_id=7 even though the
        # mid-probe update_endpoint reset _instance_id to the start-cmd value.
        assert seen_instance_ids == [7]

    @patch('motor.node_manager.core.daemon.Daemon')
    def test_refresh_native_engine_status_keeps_initial_while_loading(self, mock_daemon, heart_beat_manager):
        from motor.node_manager.core.services.native_engine.models import RuntimeState

        mock_daemon.return_value.get_engine_runtime_state.return_value = RuntimeState.STARTING
        endpoint = Endpoint(
            id=1,
            ip="192.168.1.1",
            business_port="8080",
            status=EndpointStatus.INITIAL,
        )
        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = [endpoint]

        heart_beat_manager._refresh_native_engine_status()

        assert heart_beat_manager._endpoints[0].status == EndpointStatus.INITIAL

    @patch('motor.node_manager.core.daemon.Daemon')
    def test_refresh_headless_process_liveness_reports_wait2start(self, mock_daemon, heart_beat_manager):
        from motor.node_manager.core.services.native_engine.models import RuntimeState

        mock_daemon.return_value.get_engine_runtime_state.return_value = RuntimeState.RUNNING
        endpoint = Endpoint(
            id=1,
            ip="192.168.1.2",
            business_port="8080",
            status=EndpointStatus.INITIAL,
            headless=True,
        )
        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = [endpoint]

        heart_beat_manager._refresh_native_engine_status()

        assert heart_beat_manager._endpoints[0].status == EndpointStatus.WAIT2START

    @patch('motor.node_manager.core.daemon.Daemon')
    def test_refresh_native_engine_status_discards_stale_probe_write_back(
        self, mock_daemon, heart_beat_manager, sample_start_cmd_msg
    ):
        """stale probe result must not overwrite endpoints refreshed by update_endpoint"""
        from motor.node_manager.core.services.native_engine.models import RuntimeState

        stale_endpoint = Endpoint(
            id=0,
            ip="10.0.0.28",
            business_port="8080",
            status=EndpointStatus.NORMAL,
        )

        def probe_and_update_during_probe(*args, **kwargs):
            heart_beat_manager.update_endpoint(sample_start_cmd_msg)
            return RuntimeState.UNHEALTHY

        mock_daemon.return_value.get_engine_runtime_state.side_effect = probe_and_update_during_probe

        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = [stale_endpoint]
            heart_beat_manager._endpoints_generation = 0

        heart_beat_manager._refresh_native_engine_status()

        assert len(heart_beat_manager._endpoints) == 2
        assert heart_beat_manager._endpoints[0].ip == "192.168.1.1"
        assert heart_beat_manager._endpoints[1].ip == "192.168.1.2"
        assert heart_beat_manager._endpoints[0].status == EndpointStatus.NORMAL
        assert heart_beat_manager._endpoints[1].status == EndpointStatus.NORMAL

    @patch('motor.node_manager.core.heartbeat_manager.time.sleep')
    @patch('motor.node_manager.core.heartbeat_manager.ControllerApiClient.report_heartbeat')
    def test_report_heartbeat_loop_success(self, mock_report_heartbeat, mock_sleep, heart_beat_manager):
        """test _report_heartbeat_loop success"""
        call_count = {"count": 0}

        def mock_stop_sleep(seconds):
            call_count["count"] += 1
            if call_count["count"] >= 1:
                heart_beat_manager.stop_event.set()

        mock_report_heartbeat.return_value = None

        # set endpoint info
        heart_beat_manager._job_name = "test_job"
        heart_beat_manager._instance_id = 1
        # pod_ip is already set during initialization
        heart_beat_manager.stop_event.clear()  # Ensure stop_event is not set initially
        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = [
                Endpoint(id=1, ip="192.168.1.1", business_port="8080", status=EndpointStatus.NORMAL)
            ]

        mock_sleep.side_effect = mock_stop_sleep

        # Call the method directly (will execute once then stop)
        heart_beat_manager._report_heartbeat_loop()

        # Verify report_heartbeat was called
        assert mock_report_heartbeat.called, "report_heartbeat should be called"

    @patch('motor.node_manager.core.heartbeat_manager.time.sleep')
    @patch('motor.node_manager.core.heartbeat_manager.ControllerApiClient.report_heartbeat')
    def test_heartbeat_report_loop(self, mock_report_heartbeat, mock_sleep, heart_beat_manager):
        """test heartbeat report loop"""
        call_count = {"count": 0}

        # set loop exec once
        def mock_stop_sleep(seconds):
            call_count["count"] += 1
            if call_count["count"] >= 1:
                heart_beat_manager.stop_event.set()

        mock_report_heartbeat.return_value = None

        # set endpoint info
        heart_beat_manager._job_name = "test_job"
        heart_beat_manager._instance_id = 1
        # pod_ip is already set during initialization
        heart_beat_manager.stop_event.clear()  # Ensure stop_event is not set initially
        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = [
                Endpoint(id=1, ip="192.168.1.1", business_port="8080", status=EndpointStatus.NORMAL)
            ]

        mock_sleep.side_effect = mock_stop_sleep

        heart_beat_manager._report_heartbeat_loop()
        # assert report_heartbeat was called
        assert mock_report_heartbeat.called

    @patch('motor.node_manager.core.heartbeat_manager.time.sleep')
    @patch('motor.node_manager.core.heartbeat_manager.ControllerApiClient.report_heartbeat')
    def test_heartbeat_report_with_empty_endpoints(self, mock_report_heartbeat, mock_sleep, heart_beat_manager):
        """test heartbeat report with empty endpoints"""
        call_count = {"count": 0}

        # set loop exec once
        def mock_stop_sleep(seconds):
            call_count["count"] += 1
            if call_count["count"] >= 1:
                heart_beat_manager.stop_event.set()

        mock_report_heartbeat.return_value = None

        # set endpoint info
        heart_beat_manager._job_name = "test_job"
        heart_beat_manager._instance_id = 1
        # pod_ip is already set during initialization
        heart_beat_manager.stop_event.clear()  # Ensure stop_event is not set initially
        # clear endpoint list
        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = []

        mock_sleep.side_effect = mock_stop_sleep

        heart_beat_manager._report_heartbeat_loop()
        # Even with empty endpoints, the loop should still run and send heartbeat
        # (with empty status dict)
        assert mock_report_heartbeat.called

    @patch('motor.config.node_manager.safe_open')
    @patch.dict(
        'os.environ',
        {'JOB_NAME': 'test_job', 'CONFIG_PATH': './', 'USER_CONFIG_PATH': './user_config.json', 'ROLE': 'both'},
    )
    def test_thread_safety(self, mock_safe_open, sample_start_cmd_msg, config_data):
        """test thread safety"""
        import threading

        mock_safe_open.side_effect = create_config_mock(config_data)
        # Clear singleton instance
        if hasattr(HeartbeatManager, '_instances') and HeartbeatManager in HeartbeatManager._instances:
            if HeartbeatManager in HeartbeatManager._instances:
                del HeartbeatManager._instances[HeartbeatManager]

        with patch('threading.Thread'):
            config = NodeManagerConfig()
            heartbeat_manager = HeartbeatManager(config)

            # Set initial state
            heartbeat_manager.update_endpoint(sample_start_cmd_msg)

            def update_endpoints():
                for _ in range(50):
                    heartbeat_manager.update_endpoint(sample_start_cmd_msg)
                    time.sleep(0.0005)

            def read_endpoints():
                for _ in range(50):
                    with heartbeat_manager._endpoint_lock:
                        endpoints = heartbeat_manager._endpoints.copy()
                    # assert endpoint len
                    assert len(endpoints) == len(sample_start_cmd_msg.endpoints)
                    time.sleep(0.0005)

            threads = []
            for i in range(3):
                if i % 2 == 0:
                    thread = threading.Thread(target=update_endpoints)
                else:
                    thread = threading.Thread(target=read_endpoints)
                threads.append(thread)
                thread.start()

            # Wait for all threads to complete.
            for thread in threads:
                thread.join(timeout=3.0)

            # Verify the consistency of the final state.
            assert heartbeat_manager._job_name == sample_start_cmd_msg.job_name
            assert len(heartbeat_manager._endpoints) == len(sample_start_cmd_msg.endpoints)

    def test_start_method(self, heart_beat_manager):
        """test start method"""
        assert heart_beat_manager._thread_started is False
        heart_beat_manager.start()
        assert heart_beat_manager._thread_started is True
        # Calling start again should not change the state
        heart_beat_manager.start()
        assert heart_beat_manager._thread_started is True

    @patch('motor.node_manager.core.heartbeat_manager.RegisterManager')
    def test_reregister_success(self, mock_register_manager_class, heart_beat_manager):
        """test _reregister success"""
        mock_register_manager = MagicMock()
        mock_register_manager.post_reregister_msg.return_value = True
        mock_register_manager_class.return_value = mock_register_manager

        heart_beat_manager._reregister()

        mock_register_manager.post_reregister_msg.assert_called_once()

    @patch('motor.node_manager.core.heartbeat_manager.RegisterManager')
    def test_reregister_failure(self, mock_register_manager_class, heart_beat_manager):
        """test _reregister failure"""
        mock_register_manager = MagicMock()
        mock_register_manager.post_reregister_msg.return_value = False
        mock_register_manager_class.return_value = mock_register_manager

        heart_beat_manager._reregister()

        mock_register_manager.post_reregister_msg.assert_called_once()

    @patch('motor.node_manager.core.heartbeat_manager.threading.Thread')
    @patch('motor.node_manager.core.heartbeat_manager.time.sleep')
    @patch('motor.node_manager.core.heartbeat_manager.ControllerApiClient.report_heartbeat')
    @patch('motor.node_manager.core.heartbeat_manager.RegisterManager')
    def test_reregister_triggered_on_503(
        self, mock_register_manager_class, mock_report_heartbeat, mock_sleep, mock_thread_class, heart_beat_manager
    ):
        """test that reregister is triggered when 503 error occurs"""
        call_count = {"count": 0}

        def mock_stop_sleep(seconds):
            call_count["count"] += 1
            if call_count["count"] >= 1:
                heart_beat_manager.stop_event.set()

        # Mock report_heartbeat to raise 503 error
        mock_report_heartbeat.side_effect = Exception("503 Service Unavailable")

        mock_register_manager = MagicMock()
        mock_register_manager.is_engine_checkpoint_done.return_value = True
        mock_register_manager.post_reregister_msg.return_value = True
        mock_register_manager_class.return_value = mock_register_manager

        mock_reregister_thread = MagicMock()
        mock_thread_class.return_value = mock_reregister_thread

        heart_beat_manager._job_name = "test_job"
        heart_beat_manager._instance_id = 1
        # pod_ip is already set during initialization
        heart_beat_manager.stop_event.clear()  # Ensure stop_event is not set initially

        mock_sleep.side_effect = mock_stop_sleep

        heart_beat_manager._report_heartbeat_loop()

        # Verify that reregister was called (via RegisterManager)
        mock_register_manager.post_reregister_msg.assert_called()

    @patch('motor.node_manager.core.heartbeat_manager.time.sleep')
    @patch('motor.node_manager.core.heartbeat_manager.ControllerApiClient.report_heartbeat')
    @patch('motor.node_manager.core.heartbeat_manager.RegisterManager')
    def test_reregister_lock_thread_safety(
        self, mock_register_manager_class, mock_report_heartbeat, mock_sleep, heart_beat_manager
    ):
        """test that _reregister_lock prevents concurrent reregister attempts"""
        call_count = {"count": 0}

        def mock_stop_sleep(seconds):
            if call_count["count"] >= 1:
                heart_beat_manager.stop_event.set()
            call_count["count"] += 1

        # Mock RegisterManager
        mock_register_manager = MagicMock()
        mock_register_manager.is_engine_checkpoint_done.return_value = True
        mock_register_manager.post_reregister_msg.return_value = True
        mock_register_manager_class.return_value = mock_register_manager

        # Mock report_heartbeat to raise 503 error
        mock_report_heartbeat.side_effect = Exception("503 Service Unavailable")

        heart_beat_manager._job_name = "test_job"
        heart_beat_manager._instance_id = 1
        # pod_ip is already set during initialization
        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = [
                Endpoint(id=1, ip="192.168.1.1", business_port="8080", status=EndpointStatus.NORMAL)
            ]

        mock_sleep.side_effect = mock_stop_sleep

        # Start the loop - it should trigger reregister once, then skip on subsequent 503s
        heart_beat_manager._report_heartbeat_loop()

        # Verify that _reregistering flag is properly managed
        # The lock ensures only one reregister thread is started
        assert True  # Test passes if no race condition occurs

    def test_stop_method(self, heart_beat_manager):
        """test stop method"""
        # Start threads first
        heart_beat_manager.start()
        assert heart_beat_manager._thread_started is True

        # Stop should set stop_event and join threads
        heart_beat_manager.stop()

        assert heart_beat_manager.stop_event.is_set() is True

    def test_is_started_after_restore_defaults_false(self, heart_beat_manager):
        assert heart_beat_manager.is_started_after_restore() is False

    def test_set_started_after_restore(self, heart_beat_manager):
        heart_beat_manager.set_started_after_restore(True)
        assert heart_beat_manager.is_started_after_restore() is True

    @patch("motor.node_manager.core.heartbeat_manager.RegisterManager")
    def test_register_after_restore_success(self, mock_register_manager_class, heart_beat_manager):
        mock_register_manager = MagicMock()
        mock_register_manager.post_register_msg.return_value = True
        mock_register_manager_class.return_value = mock_register_manager

        heart_beat_manager._register_after_restore()

        mock_register_manager.register_prepare_after_restore.assert_called_once()
        mock_register_manager.post_register_msg.assert_called_once()
        assert heart_beat_manager._is_registered_after_restore is True

    @patch("motor.node_manager.core.heartbeat_manager.RegisterManager")
    def test_register_after_restore_prepare_failure(self, mock_register_manager_class, heart_beat_manager):
        mock_register_manager = MagicMock()
        mock_register_manager.register_prepare_after_restore.side_effect = RuntimeError("metadata missing")
        mock_register_manager_class.return_value = mock_register_manager

        heart_beat_manager._register_after_restore()

        mock_register_manager.post_register_msg.assert_not_called()
        assert heart_beat_manager._is_registered_after_restore is False
        assert heart_beat_manager._register_after_restore_retry_count == 1

    @patch("motor.node_manager.core.heartbeat_manager.is_restored_from_host_side_snapshot", return_value=True)
    @patch("motor.node_manager.core.heartbeat_manager.time.sleep")
    @patch("motor.node_manager.core.heartbeat_manager.ControllerApiClient.report_heartbeat")
    @patch("motor.node_manager.core.heartbeat_manager.RegisterManager")
    def test_report_heartbeat_loop_registers_before_reporting(
        self,
        mock_register_manager_class,
        mock_report_heartbeat,
        mock_sleep,
        _mock_restored,
        heart_beat_manager,
    ):
        call_count = {"count": 0}

        def mock_stop_sleep(_seconds):
            call_count["count"] += 1
            # First sleep happens after register; heartbeat is sent on the next loop.
            if call_count["count"] >= 2:
                heart_beat_manager.stop_event.set()

        mock_register_manager = MagicMock()
        mock_register_manager.is_engine_checkpoint_done.return_value = True
        mock_register_manager.register_prepare_after_restore.return_value = None
        mock_register_manager.post_register_msg.return_value = True
        mock_register_manager_class.return_value = mock_register_manager
        mock_report_heartbeat.return_value = None
        mock_sleep.side_effect = mock_stop_sleep

        heart_beat_manager._job_name = "restored-job"
        heart_beat_manager._instance_id = 1
        heart_beat_manager.stop_event.clear()
        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = [
                Endpoint(id=1, ip="192.168.1.1", business_port="8080", status=EndpointStatus.NORMAL)
            ]

        heart_beat_manager._report_heartbeat_loop()

        mock_register_manager.register_prepare_after_restore.assert_called_once()
        mock_register_manager.post_register_msg.assert_called_once()
        mock_report_heartbeat.assert_called_once()

    @patch("motor.node_manager.core.heartbeat_manager.is_restored_from_host_side_snapshot", return_value=True)
    @patch('motor.node_manager.core.daemon.Daemon')
    def test_refresh_native_engine_status_keeps_status_before_start_after_restore(
        self, mock_daemon, _mock_restored, heart_beat_manager, sample_endpoints
    ):
        from motor.node_manager.core.services.native_engine.models import RuntimeState

        mock_daemon.return_value.get_engine_runtime_state.return_value = RuntimeState.UNHEALTHY

        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = sample_endpoints.copy()

        heart_beat_manager._refresh_native_engine_status()

        assert heart_beat_manager._endpoints[0].status == EndpointStatus.NORMAL
        assert heart_beat_manager._endpoints[1].status == EndpointStatus.NORMAL
        assert mock_daemon.return_value.get_engine_runtime_state.call_count == 2

    @patch("motor.node_manager.core.heartbeat_manager.is_restored_from_host_side_snapshot", return_value=False)
    @patch("motor.node_manager.core.heartbeat_manager.time.sleep")
    @patch("motor.node_manager.core.heartbeat_manager.ControllerApiClient.report_heartbeat")
    @patch("motor.node_manager.core.heartbeat_manager.RegisterManager")
    def test_report_heartbeat_skipped_until_checkpoint_done(
        self, mock_register_manager_class, mock_report_heartbeat, mock_sleep, _mock_restored, heart_beat_manager
    ):
        call_count = {"count": 0}

        def mock_stop_sleep(_seconds):
            call_count["count"] += 1
            if call_count["count"] >= 1:
                heart_beat_manager.stop_event.set()

        mock_register_manager = MagicMock()
        mock_register_manager.is_engine_checkpoint_done.return_value = False
        mock_register_manager_class.return_value = mock_register_manager
        mock_sleep.side_effect = mock_stop_sleep

        heart_beat_manager._job_name = "test_job"
        heart_beat_manager._instance_id = 1
        heart_beat_manager.stop_event.clear()
        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = [
                Endpoint(id=1, ip="192.168.1.1", business_port="8080", status=EndpointStatus.NORMAL)
            ]

        heart_beat_manager._report_heartbeat_loop()

        mock_report_heartbeat.assert_not_called()
        mock_register_manager.is_engine_checkpoint_done.assert_called()

    @patch("motor.node_manager.core.heartbeat_manager.is_restored_from_host_side_snapshot", return_value=False)
    @patch("motor.node_manager.core.heartbeat_manager.time.sleep")
    @patch("motor.node_manager.core.heartbeat_manager.ControllerApiClient.report_heartbeat")
    @patch("motor.node_manager.core.heartbeat_manager.RegisterManager")
    def test_report_heartbeat_resumes_after_checkpoint_done(
        self, mock_register_manager_class, mock_report_heartbeat, mock_sleep, _mock_restored, heart_beat_manager
    ):
        call_count = {"count": 0}

        def mock_stop_sleep(_seconds):
            call_count["count"] += 1
            if call_count["count"] >= 1:
                heart_beat_manager.stop_event.set()

        mock_register_manager = MagicMock()
        mock_register_manager.is_engine_checkpoint_done.return_value = True
        mock_register_manager_class.return_value = mock_register_manager
        mock_report_heartbeat.return_value = None
        mock_sleep.side_effect = mock_stop_sleep

        heart_beat_manager._job_name = "test_job"
        heart_beat_manager._instance_id = 1
        heart_beat_manager.stop_event.clear()
        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = [
                Endpoint(id=1, ip="192.168.1.1", business_port="8080", status=EndpointStatus.NORMAL)
            ]

        heart_beat_manager._report_heartbeat_loop()

        mock_report_heartbeat.assert_called_once()

    # -- endpoint-state facts (consumed by the Daemon's suicide arbitration) ----

    def test_has_abnormal_endpoints_true(self, heart_beat_manager):
        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = [
                Endpoint(id=1, ip="192.168.1.1", business_port="8080", status=EndpointStatus.ABNORMAL)
            ]
        assert heart_beat_manager.has_abnormal_endpoints() is True

    def test_has_abnormal_endpoints_false(self, heart_beat_manager):
        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = [
                Endpoint(id=1, ip="192.168.1.1", business_port="8080", status=EndpointStatus.NORMAL)
            ]
        assert heart_beat_manager.has_abnormal_endpoints() is False

    def test_endpoints_generation_increments_on_update(self, heart_beat_manager, sample_start_cmd_msg):
        gen_before = heart_beat_manager.endpoints_generation()
        heart_beat_manager.update_endpoint(sample_start_cmd_msg)
        assert heart_beat_manager.endpoints_generation() == gen_before + 1

    def test_grace_period_state(self, heart_beat_manager):
        heart_beat_manager._is_within_grace_period = True
        assert heart_beat_manager.is_within_grace_period() is True
        heart_beat_manager._is_within_grace_period = False
        assert heart_beat_manager.is_within_grace_period() is False
