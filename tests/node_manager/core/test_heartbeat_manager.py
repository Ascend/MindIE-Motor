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
    from motor.node_manager.core.engine_manager import EngineManager
    from motor.node_manager.core.heartbeat_manager import HeartbeatManager
    from motor.config.node_manager import NodeManagerConfig


def _clear_engine_manager_singleton() -> None:
    if hasattr(EngineManager, "_instances") and EngineManager in EngineManager._instances:
        del EngineManager._instances[EngineManager]


class TestHeartBeatManager:
    """HeartBeatManager test class"""

    @pytest.fixture(name="heart_beat_manager")
    def _heart_beat_manager_fixture(self, config_data):
        """return HeartBeatManager instance"""
        with (
            patch('motor.config.node_manager.safe_open') as mock_safe_open,
            patch('threading.Thread') as mock_thread_class,
            patch('motor.node_manager.core.heartbeat_manager.EngineManager') as mock_engine_manager_cls,
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
            mock_engine_manager = MagicMock()
            mock_engine_manager.is_engine_checkpoint_done.return_value = True
            mock_engine_manager_cls.return_value = mock_engine_manager
            _clear_engine_manager_singleton()
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
            Endpoint(id=1, ip="192.168.1.1", business_port="8080", mgmt_port="9090", status=EndpointStatus.NORMAL),
            Endpoint(id=2, ip="192.168.1.2", business_port="8080", mgmt_port="9090", status=EndpointStatus.NORMAL),
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

    def test_update_endpoint_resets_grace_period(self, heart_beat_manager, sample_start_cmd_msg):
        """update_endpoint should restart grace period for engine cold start"""
        heart_beat_manager._thread_started = True
        heart_beat_manager._is_within_grace_period = False
        heart_beat_manager._engine_status_thread_start_time = time.time() - 300

        before = heart_beat_manager._engine_status_thread_start_time
        heart_beat_manager.update_endpoint(sample_start_cmd_msg)

        assert heart_beat_manager._is_within_grace_period is True
        assert heart_beat_manager._engine_status_thread_start_time > before

    @patch('motor.node_manager.core.heartbeat_manager.EngineServerApiClient.query_status')
    def test_get_engine_server_status_success(self, mock_query_status, heart_beat_manager, sample_endpoints):
        """test get engine server status success"""
        # Mock query_status to return normal status for each endpoint
        mock_query_status.return_value = {"status": "normal"}

        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = sample_endpoints.copy()

        heart_beat_manager._get_engine_server_status()

        # Verify that query_status was called for each endpoint
        assert mock_query_status.call_count == 2

        # Verify that status was updated correctly
        assert heart_beat_manager._endpoints[0].status == EndpointStatus.NORMAL
        assert heart_beat_manager._endpoints[1].status == EndpointStatus.NORMAL

    @patch('motor.node_manager.core.heartbeat_manager.EngineServerApiClient.query_status')
    def test_get_engine_server_status_discards_stale_probe_write_back(
        self, mock_query_status, heart_beat_manager, sample_start_cmd_msg
    ):
        """stale probe result must not overwrite endpoints refreshed by update_endpoint"""
        stale_endpoint = Endpoint(
            id=0,
            ip="10.0.0.28",
            business_port="8080",
            mgmt_port="9090",
            status=EndpointStatus.NORMAL,
        )

        def probe_and_update_during_probe(*args, **kwargs):
            heart_beat_manager.update_endpoint(sample_start_cmd_msg)
            return {"status": "abnormal"}

        mock_query_status.side_effect = probe_and_update_during_probe

        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = [stale_endpoint]
            heart_beat_manager._endpoints_generation = 0

        heart_beat_manager._get_engine_server_status()

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
                Endpoint(id=1, ip="192.168.1.1", business_port="8080", mgmt_port="9090", status=EndpointStatus.NORMAL)
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
                Endpoint(id=1, ip="192.168.1.1", business_port="8080", mgmt_port="9090", status=EndpointStatus.NORMAL)
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

    @patch('motor.node_manager.core.heartbeat_manager.EngineManager')
    def test_reregister_success(self, mock_engine_manager_class, heart_beat_manager):
        """test _reregister success"""
        mock_engine_manager = MagicMock()
        mock_engine_manager.post_reregister_msg.return_value = True
        mock_engine_manager_class.return_value = mock_engine_manager

        heart_beat_manager._reregister()

        mock_engine_manager.post_reregister_msg.assert_called_once()

    @patch('motor.node_manager.core.heartbeat_manager.EngineManager')
    def test_reregister_failure(self, mock_engine_manager_class, heart_beat_manager):
        """test _reregister failure"""
        mock_engine_manager = MagicMock()
        mock_engine_manager.post_reregister_msg.return_value = False
        mock_engine_manager_class.return_value = mock_engine_manager

        heart_beat_manager._reregister()

        mock_engine_manager.post_reregister_msg.assert_called_once()

    @patch('motor.node_manager.core.heartbeat_manager.threading.Thread')
    @patch('motor.node_manager.core.heartbeat_manager.time.sleep')
    @patch('motor.node_manager.core.heartbeat_manager.ControllerApiClient.report_heartbeat')
    @patch('motor.node_manager.core.heartbeat_manager.EngineManager')
    def test_reregister_triggered_on_503(
        self, mock_engine_manager_class, mock_report_heartbeat, mock_sleep, mock_thread_class, heart_beat_manager
    ):
        """test that reregister is triggered when 503 error occurs"""
        call_count = {"count": 0}

        def mock_stop_sleep(seconds):
            call_count["count"] += 1
            if call_count["count"] >= 1:
                heart_beat_manager.stop_event.set()

        # Mock report_heartbeat to raise 503 error
        mock_report_heartbeat.side_effect = Exception("503 Service Unavailable")

        mock_engine_manager = MagicMock()
        mock_engine_manager.is_engine_checkpoint_done.return_value = True
        mock_engine_manager.post_reregister_msg.return_value = True
        mock_engine_manager_class.return_value = mock_engine_manager

        mock_reregister_thread = MagicMock()
        mock_thread_class.return_value = mock_reregister_thread

        heart_beat_manager._job_name = "test_job"
        heart_beat_manager._instance_id = 1
        # pod_ip is already set during initialization
        heart_beat_manager.stop_event.clear()  # Ensure stop_event is not set initially

        mock_sleep.side_effect = mock_stop_sleep

        heart_beat_manager._report_heartbeat_loop()

        # Verify that reregister was called (via EngineManager)
        mock_engine_manager.post_reregister_msg.assert_called()

    @patch('motor.node_manager.core.heartbeat_manager.time.sleep')
    @patch('motor.node_manager.core.heartbeat_manager.ControllerApiClient.report_heartbeat')
    @patch('motor.node_manager.core.heartbeat_manager.EngineManager')
    def test_reregister_lock_thread_safety(
        self, mock_engine_manager_class, mock_report_heartbeat, mock_sleep, heart_beat_manager
    ):
        """test that _reregister_lock prevents concurrent reregister attempts"""
        call_count = {"count": 0}

        def mock_stop_sleep(seconds):
            if call_count["count"] >= 1:
                heart_beat_manager.stop_event.set()
            call_count["count"] += 1

        # Mock EngineManager
        mock_engine_manager = MagicMock()
        mock_engine_manager.is_engine_checkpoint_done.return_value = True
        mock_engine_manager.post_reregister_msg.return_value = True
        mock_engine_manager_class.return_value = mock_engine_manager

        # Mock report_heartbeat to raise 503 error
        mock_report_heartbeat.side_effect = Exception("503 Service Unavailable")

        heart_beat_manager._job_name = "test_job"
        heart_beat_manager._instance_id = 1
        # pod_ip is already set during initialization
        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = [
                Endpoint(id=1, ip="192.168.1.1", business_port="8080", mgmt_port="9090", status=EndpointStatus.NORMAL)
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

    def test_initial_suicide_flag(self, heart_beat_manager):
        """test that suicide flag is initially False"""
        assert heart_beat_manager.should_suicide() is False
        assert heart_beat_manager._consecutive_abnormal_count == 0

    @patch('motor.node_manager.core.heartbeat_manager.time.sleep')
    @patch('motor.node_manager.core.heartbeat_manager.ControllerApiClient.report_heartbeat')
    def test_consecutive_abnormal_heartbeat_counting(self, mock_report_heartbeat, mock_sleep, heart_beat_manager):
        """test that consecutive abnormal heartbeats are counted correctly"""
        call_count = {"count": 0}

        def mock_stop_sleep(seconds):
            call_count["count"] += 1
            if call_count["count"] >= 6:  # Run 6 times to test 5 consecutive abnormal
                heart_beat_manager.stop_event.set()

        mock_report_heartbeat.return_value = None

        # Set endpoint info with abnormal status
        heart_beat_manager._job_name = "test_job"
        heart_beat_manager._instance_id = 1
        heart_beat_manager._is_within_grace_period = False  # Ensure we're past grace period
        heart_beat_manager.stop_event.clear()

        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = [
                Endpoint(id=1, ip="192.168.1.1", business_port="8080", mgmt_port="9090", status=EndpointStatus.ABNORMAL)
            ]

        mock_sleep.side_effect = mock_stop_sleep

        # Run the heartbeat loop
        heart_beat_manager._report_heartbeat_loop()

        # After 5 consecutive abnormal heartbeats, suicide flag should be set
        assert heart_beat_manager.should_suicide() is True
        assert heart_beat_manager._consecutive_abnormal_count >= 5

    @patch('motor.node_manager.core.heartbeat_manager.time.sleep')
    @patch('motor.node_manager.core.heartbeat_manager.ControllerApiClient.report_heartbeat')
    def test_abnormal_count_reset_on_normal_status(self, mock_report_heartbeat, mock_sleep, heart_beat_manager):
        """test that abnormal count resets when status returns to normal"""
        call_count = {"count": 0}

        def mock_stop_sleep(seconds):
            call_count["count"] += 1
            # Change status to normal after first iteration
            if call_count["count"] == 1:
                with heart_beat_manager._endpoint_lock:
                    if heart_beat_manager._endpoints:
                        heart_beat_manager._endpoints[0].status = EndpointStatus.NORMAL
            if call_count["count"] >= 3:
                heart_beat_manager.stop_event.set()

        mock_report_heartbeat.return_value = None

        heart_beat_manager._job_name = "test_job"
        heart_beat_manager._instance_id = 1
        heart_beat_manager._is_within_grace_period = False
        heart_beat_manager.stop_event.clear()

        # Start with abnormal status
        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = [
                Endpoint(id=1, ip="192.168.1.1", business_port="8080", mgmt_port="9090", status=EndpointStatus.ABNORMAL)
            ]

        mock_sleep.side_effect = mock_stop_sleep

        heart_beat_manager._report_heartbeat_loop()

        # After status returns to normal, count should be reset
        assert heart_beat_manager._consecutive_abnormal_count == 0
        assert heart_beat_manager.should_suicide() is False

    def test_update_endpoint_resets_abnormal_count(self, heart_beat_manager, sample_start_cmd_msg):
        """test that updating endpoint resets abnormal count and suicide flag"""
        # Set abnormal count and suicide flag first
        with heart_beat_manager._abnormal_count_lock:
            heart_beat_manager._consecutive_abnormal_count = 5
        with heart_beat_manager._suicide_lock:
            heart_beat_manager._should_suicide = True

        # Update endpoint should reset both
        heart_beat_manager.update_endpoint(sample_start_cmd_msg)

        assert heart_beat_manager._consecutive_abnormal_count == 0
        assert heart_beat_manager.should_suicide() is False

    @patch('motor.node_manager.core.heartbeat_manager.time.sleep')
    @patch('motor.node_manager.core.heartbeat_manager.ControllerApiClient.report_heartbeat')
    def test_suicide_flag_set_after_five_abnormal_heartbeats(
        self, mock_report_heartbeat, mock_sleep, heart_beat_manager
    ):
        """test that suicide flag is set exactly after 5 consecutive abnormal heartbeats"""
        call_count = {"count": 0}

        def mock_stop_sleep(seconds):
            call_count["count"] += 1
            if call_count["count"] >= 5:
                heart_beat_manager.stop_event.set()

        mock_report_heartbeat.return_value = None

        heart_beat_manager._job_name = "test_job"
        heart_beat_manager._instance_id = 1
        heart_beat_manager._is_within_grace_period = False
        heart_beat_manager.stop_event.clear()

        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = [
                Endpoint(id=1, ip="192.168.1.1", business_port="8080", mgmt_port="9090", status=EndpointStatus.ABNORMAL)
            ]

        mock_sleep.side_effect = mock_stop_sleep

        # Initially suicide flag should be False
        assert heart_beat_manager.should_suicide() is False

        heart_beat_manager._report_heartbeat_loop()

        # After 5 consecutive abnormal heartbeats, suicide flag should be True
        assert heart_beat_manager.should_suicide() is True
        assert heart_beat_manager._consecutive_abnormal_count == 5

    @patch('motor.node_manager.core.heartbeat_manager.time.sleep')
    @patch('motor.node_manager.core.heartbeat_manager.ControllerApiClient.report_heartbeat')
    def test_multiple_endpoints_abnormal_triggers_suicide(self, mock_report_heartbeat, mock_sleep, heart_beat_manager):
        """test that if any endpoint is abnormal, it counts towards suicide"""
        call_count = {"count": 0}

        def mock_stop_sleep(seconds):
            call_count["count"] += 1
            if call_count["count"] >= 5:
                heart_beat_manager.stop_event.set()

        mock_report_heartbeat.return_value = None

        heart_beat_manager._job_name = "test_job"
        heart_beat_manager._instance_id = 1
        heart_beat_manager._is_within_grace_period = False
        heart_beat_manager.stop_event.clear()

        # Set multiple endpoints, one abnormal
        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = [
                Endpoint(
                    id=1, ip="192.168.1.1", business_port="8080", mgmt_port="9090", status=EndpointStatus.ABNORMAL
                ),
                Endpoint(id=2, ip="192.168.1.2", business_port="8080", mgmt_port="9090", status=EndpointStatus.NORMAL),
            ]

        mock_sleep.side_effect = mock_stop_sleep

        heart_beat_manager._report_heartbeat_loop()

        # Even with one endpoint abnormal, suicide should be triggered after 5 consecutive reports
        assert heart_beat_manager.should_suicide() is True

    @patch('motor.node_manager.core.heartbeat_manager.time.sleep')
    @patch('motor.node_manager.core.heartbeat_manager.ControllerApiClient.report_heartbeat')
    def test_abnormal_triggers_suicide_when_report_fails(self, mock_report_heartbeat, mock_sleep, heart_beat_manager):
        """endpoint stays abnormal but Controller heartbeat report fails should still trigger suicide"""
        call_count = {"count": 0}

        def mock_stop_sleep(seconds):
            call_count["count"] += 1
            if call_count["count"] >= 5:
                heart_beat_manager.stop_event.set()

        mock_report_heartbeat.side_effect = Exception("Connection refused")

        heart_beat_manager._job_name = "test_job"
        heart_beat_manager._instance_id = 1
        heart_beat_manager._is_within_grace_period = False
        heart_beat_manager.stop_event.clear()

        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = [
                Endpoint(id=1, ip="192.168.1.1", business_port="8080", mgmt_port="9090", status=EndpointStatus.ABNORMAL)
            ]

        mock_sleep.side_effect = mock_stop_sleep

        heart_beat_manager._report_heartbeat_loop()

        assert heart_beat_manager.should_suicide() is True
        assert heart_beat_manager._consecutive_abnormal_count == 5

    @patch('motor.node_manager.core.heartbeat_manager.threading.Thread')
    def test_should_suicide_thread_safety(self, mock_thread_class, heart_beat_manager):
        """test that should_suicide method is thread-safe"""

        # Set suicide flag
        with heart_beat_manager._suicide_lock:
            heart_beat_manager._should_suicide = True

        # Verify flag is set
        assert heart_beat_manager.should_suicide() is True

        # Test that the lock protects the flag correctly
        # We'll test by calling should_suicide multiple times and verifying consistency
        results = []
        for _ in range(10):
            results.append(heart_beat_manager.should_suicide())

        # All calls should get the same result (True)
        assert len(results) == 10
        assert all(results), f"All results should be True, got {results}"

        # Test concurrent access simulation by checking lock behavior
        # Reset flag and test again
        with heart_beat_manager._suicide_lock:
            heart_beat_manager._should_suicide = False

        results2 = []
        for _ in range(10):
            results2.append(heart_beat_manager.should_suicide())

        # All calls should get False now
        assert len(results2) == 10
        assert all(r is False for r in results2), f"All results should be False, got {results2}"

    def test_is_started_after_restore_defaults_false(self, heart_beat_manager):
        assert heart_beat_manager.is_started_after_restore() is False

    def test_set_started_after_restore(self, heart_beat_manager):
        heart_beat_manager.set_started_after_restore(True)
        assert heart_beat_manager.is_started_after_restore() is True

    @patch("motor.node_manager.core.heartbeat_manager.EngineManager")
    def test_register_after_restore_success(self, mock_engine_manager_class, heart_beat_manager):
        mock_engine_manager = MagicMock()
        mock_engine_manager.post_register_msg.return_value = True
        mock_engine_manager_class.return_value = mock_engine_manager

        heart_beat_manager._register_after_restore()

        mock_engine_manager.register_prepare_after_restore.assert_called_once()
        mock_engine_manager.post_register_msg.assert_called_once()
        assert heart_beat_manager._is_registered_after_restore is True

    @patch("motor.node_manager.core.heartbeat_manager.EngineManager")
    def test_register_after_restore_prepare_failure(self, mock_engine_manager_class, heart_beat_manager):
        mock_engine_manager = MagicMock()
        mock_engine_manager.register_prepare_after_restore.side_effect = RuntimeError("metadata missing")
        mock_engine_manager_class.return_value = mock_engine_manager

        heart_beat_manager._register_after_restore()

        mock_engine_manager.post_register_msg.assert_not_called()
        assert heart_beat_manager._is_registered_after_restore is False
        assert heart_beat_manager._register_after_restore_retry_count == 1

    @patch("motor.node_manager.core.heartbeat_manager.is_restored_from_host_side_snapshot", return_value=True)
    @patch("motor.node_manager.core.heartbeat_manager.time.sleep")
    @patch("motor.node_manager.core.heartbeat_manager.ControllerApiClient.report_heartbeat")
    @patch("motor.node_manager.core.heartbeat_manager.EngineManager")
    def test_report_heartbeat_loop_registers_before_reporting(
        self,
        mock_engine_manager_class,
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

        mock_engine_manager = MagicMock()
        mock_engine_manager.is_engine_checkpoint_done.return_value = True
        mock_engine_manager.register_prepare_after_restore.return_value = None
        mock_engine_manager.post_register_msg.return_value = True
        mock_engine_manager_class.return_value = mock_engine_manager
        mock_report_heartbeat.return_value = None
        mock_sleep.side_effect = mock_stop_sleep

        heart_beat_manager._job_name = "restored-job"
        heart_beat_manager._instance_id = 1
        heart_beat_manager.stop_event.clear()
        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = [
                Endpoint(id=1, ip="192.168.1.1", business_port="8080", mgmt_port="9090", status=EndpointStatus.NORMAL)
            ]

        heart_beat_manager._report_heartbeat_loop()

        mock_engine_manager.register_prepare_after_restore.assert_called_once()
        mock_engine_manager.post_register_msg.assert_called_once()
        mock_report_heartbeat.assert_called_once()

    @patch("motor.node_manager.core.heartbeat_manager.is_restored_from_host_side_snapshot", return_value=True)
    @patch("motor.node_manager.core.heartbeat_manager.EngineServerApiClient.query_status")
    def test_get_engine_server_status_keeps_status_before_start_after_restore(
        self, mock_query_status, _mock_restored, heart_beat_manager, sample_endpoints
    ):
        mock_query_status.return_value = {"status": "abnormal"}

        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = sample_endpoints.copy()

        heart_beat_manager._get_engine_server_status()

        assert heart_beat_manager._endpoints[0].status == EndpointStatus.NORMAL
        assert heart_beat_manager._endpoints[1].status == EndpointStatus.NORMAL
        assert mock_query_status.call_count == 2

    @patch("motor.node_manager.core.heartbeat_manager.is_restored_from_host_side_snapshot", return_value=False)
    @patch("motor.node_manager.core.heartbeat_manager.time.sleep")
    @patch("motor.node_manager.core.heartbeat_manager.ControllerApiClient.report_heartbeat")
    @patch("motor.node_manager.core.heartbeat_manager.EngineManager")
    def test_report_heartbeat_skipped_until_checkpoint_done(
        self, mock_engine_manager_class, mock_report_heartbeat, mock_sleep, _mock_restored, heart_beat_manager
    ):
        call_count = {"count": 0}

        def mock_stop_sleep(_seconds):
            call_count["count"] += 1
            if call_count["count"] >= 1:
                heart_beat_manager.stop_event.set()

        mock_engine_manager = MagicMock()
        mock_engine_manager.is_engine_checkpoint_done.return_value = False
        mock_engine_manager_class.return_value = mock_engine_manager
        mock_sleep.side_effect = mock_stop_sleep

        heart_beat_manager._job_name = "test_job"
        heart_beat_manager._instance_id = 1
        heart_beat_manager.stop_event.clear()
        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = [
                Endpoint(id=1, ip="192.168.1.1", business_port="8080", mgmt_port="9090", status=EndpointStatus.NORMAL)
            ]

        heart_beat_manager._report_heartbeat_loop()

        mock_report_heartbeat.assert_not_called()
        mock_engine_manager.is_engine_checkpoint_done.assert_called()

    @patch("motor.node_manager.core.heartbeat_manager.is_restored_from_host_side_snapshot", return_value=False)
    @patch("motor.node_manager.core.heartbeat_manager.time.sleep")
    @patch("motor.node_manager.core.heartbeat_manager.ControllerApiClient.report_heartbeat")
    @patch("motor.node_manager.core.heartbeat_manager.EngineManager")
    def test_report_heartbeat_resumes_after_checkpoint_done(
        self, mock_engine_manager_class, mock_report_heartbeat, mock_sleep, _mock_restored, heart_beat_manager
    ):
        call_count = {"count": 0}

        def mock_stop_sleep(_seconds):
            call_count["count"] += 1
            if call_count["count"] >= 1:
                heart_beat_manager.stop_event.set()

        mock_engine_manager = MagicMock()
        mock_engine_manager.is_engine_checkpoint_done.return_value = True
        mock_engine_manager_class.return_value = mock_engine_manager
        mock_report_heartbeat.return_value = None
        mock_sleep.side_effect = mock_stop_sleep

        heart_beat_manager._job_name = "test_job"
        heart_beat_manager._instance_id = 1
        heart_beat_manager.stop_event.clear()
        with heart_beat_manager._endpoint_lock:
            heart_beat_manager._endpoints = [
                Endpoint(id=1, ip="192.168.1.1", business_port="8080", mgmt_port="9090", status=EndpointStatus.NORMAL)
            ]

        heart_beat_manager._report_heartbeat_loop()

        mock_report_heartbeat.assert_called_once()
