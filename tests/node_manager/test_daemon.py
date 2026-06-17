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
import json
import pytest
from unittest.mock import patch, MagicMock, mock_open

from motor.node_manager.core.daemon import Daemon
from motor.config.node_manager import NodeManagerConfig
from motor.common.resources.endpoint import Endpoint
from motor.common.resources.instance import PDRole, ParallelConfig


def create_config_mock(config_data):
    def mock_side_effect(file_path, mode):
        file_path_str = str(file_path)
        if "user_config.json" in file_path_str:
            return mock_open(read_data=json.dumps(config_data)).return_value
        return mock_open().return_value

    return mock_side_effect


@pytest.fixture
def config_data():
    return {
        "parallel_config": {"tp_size": 2, "pp_size": 1},
        "role": "both",
        "controller_api_dns": "localhost",
        "controller_api_port": 8080,
        "node_manager_port": 8080,
        "model_name": "vllm",
    }


@pytest.fixture
def daemon(config_data):
    # Clear singleton instance (Daemon is still singleton)
    if hasattr(Daemon, '_instances') and Daemon in Daemon._instances:
        if Daemon in Daemon._instances:
            del Daemon._instances[Daemon]

    config_path = os.path.join(os.path.dirname(__file__), '..', 'jsons', 'user_config.json')
    with patch.dict('os.environ', {'JOB_NAME': 'test_job', 'USER_CONFIG_PATH': config_path, 'ROLE': 'both'}):
        config = NodeManagerConfig()
        # Manually set the configuration data
        config.basic_config.parallel_config = ParallelConfig(
            tp_size=config_data["parallel_config"]["tp_size"], pp_size=config_data["parallel_config"]["pp_size"]
        )
        config.basic_config.job_name = config_data.get("model_name", "test_job")
        config.basic_config.role = PDRole(config_data.get("role", "both"))
        config.api_config.node_manager_port = config_data.get("node_manager_port", 8080)

        # Set device_num for testing (simulating visible devices)
        config.basic_config.device_num = 8  # 8 devices for testing

        daemon_instance = Daemon(config)
        yield daemon_instance


@pytest.fixture
def endpoints():
    return [
        Endpoint(id=i, ip=f"192.168.1.{100 + i}", business_port=str(8000 + i * 2), mgmt_port=str(9000 + i * 2))
        for i in range(3)
    ]


class TestDaemon:
    @patch('subprocess.Popen')
    def test_pull_engine_success(self, mock_popen, daemon, endpoints):
        mock_process = MagicMock(pid=12345)
        mock_process.poll.return_value = None  # Process is still running
        mock_popen.return_value = mock_process
        instance_id = 1
        master_dp_ip = "192.168.1.100"
        daemon.pull_engine(PDRole.ROLE_P, endpoints, instance_id, master_dp_ip)
        # Verify that process was added to engine_pids
        assert len(daemon.engine_pids) > 0
        assert 12345 in daemon.engine_pids

    @pytest.mark.parametrize(
        "invalid_endpoint,error_msg",
        [
            (Endpoint(id=0, ip="invalid_ip", business_port="8000", mgmt_port="9090"), "Failed to pull engine"),
            (Endpoint(id=0, ip="192.168.1.1", business_port="999999", mgmt_port="9090"), "Failed to pull engine"),
        ],
    )
    def test_pull_engine_invalid_params(self, daemon, invalid_endpoint, error_msg):
        with pytest.raises(RuntimeError, match=error_msg):
            daemon.pull_engine(PDRole.ROLE_U, [invalid_endpoint], instance_id=1, master_dp_ip="192.168.1.100")

    @pytest.mark.parametrize(
        "exception,should_not_raise",
        [
            (None, True),
            (ProcessLookupError("No such process"), True),
            (PermissionError("Permission denied"), True),
            (Exception("Unexpected error"), True),
        ],
    )
    @patch('os.kill')
    def test_exit_daemon(self, mock_kill, daemon, exception, should_not_raise):
        # Mock SIGKILL for Windows compatibility
        with patch('motor.node_manager.core.daemon.signal.SIGKILL', 9, create=True):
            daemon.engine_pids = [1001, 1002]
            if exception:
                mock_kill.side_effect = exception
            daemon.stop()  # Method is called 'stop', not 'exit_daemon'
            assert mock_kill.call_count == len([1001, 1002])

    @pytest.mark.parametrize(
        "ip,port,expected",
        [
            ("192.168.1.100", "8080", True),
            ("2001:db8::1", "8080", True),
            ("invalid_ip", "8080", False),
            ("192.168.1.100", "not_number", False),
            ("192.168.1.100", "0", False),
            ("192.168.1.100", "99999", False),
            ("192.168.1.100", "1", False),
            ("192.168.1.100", "65535", True),
        ],
    )
    def test_check_params(self, daemon, ip, port, expected):
        endpoint = Endpoint(id=1, ip=ip, business_port=port, mgmt_port="9090")
        assert daemon._check_params(endpoint) == expected

    @patch('subprocess.Popen')
    @patch('motor.node_manager.core.daemon.logger')
    def test_command_format(self, mock_logger, mock_popen, daemon):
        mock_process = MagicMock(pid=12345)
        mock_process.poll.return_value = None  # Process is still running
        mock_popen.return_value = mock_process

        endpoint = Endpoint(id=5, ip="10.0.0.1", business_port="9000", mgmt_port="9090")
        instance_id = 1
        master_dp_ip = "192.168.1.100"
        daemon.pull_engine(PDRole.ROLE_P, [endpoint], instance_id, master_dp_ip)

        # Verify that process was added to engine_pids
        assert len(daemon.engine_pids) > 0
        assert 12345 in daemon.engine_pids
        # Verify Popen was called
        mock_popen.assert_called_once()

    @patch('subprocess.Popen')
    def test_hybrid_role_starts_union_engine(self, mock_popen, daemon):
        mock_process = MagicMock(pid=12345)
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        endpoint = Endpoint(id=0, ip="10.0.0.1", business_port="9000", mgmt_port="9090")
        daemon.pull_engine(PDRole.ROLE_U, [endpoint], instance_id=1, master_dp_ip="192.168.1.100")

        cmd = mock_popen.call_args.args[0]
        role_arg_index = cmd.index("--role") + 1
        assert cmd[role_arg_index] == "union"

    # ===== D2D Weight Transfer Tests =====

    @patch('subprocess.Popen')
    def test_pull_engine_with_d2d_peer_ips(self, mock_popen, daemon):
        """pull_engine adds --d2d-peer-ips CLI arg when d2d_peer_ips is provided."""
        mock_process = MagicMock(pid=12345)
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        endpoint = Endpoint(id=0, ip="10.0.0.1", business_port="9000", mgmt_port="9090")
        d2d_peer_ips = ["0:192.168.1.10", "0:192.168.1.11"]

        daemon.pull_engine(
            PDRole.ROLE_P,
            [endpoint],
            instance_id=1,
            master_dp_ip="192.168.1.100",
            d2d_peer_ips=d2d_peer_ips,
        )

        mock_popen.assert_called_once()
        cmd = mock_popen.call_args.args[0]
        assert '--d2d-peer-ips' in cmd
        idx = cmd.index('--d2d-peer-ips')
        assert cmd[idx + 1] == "192.168.1.10,192.168.1.11"

    @patch('subprocess.Popen')
    def test_pull_engine_without_d2d_peer_ips(self, mock_popen, daemon):
        """pull_engine does NOT add --d2d-peer-ips when d2d_peer_ips is None."""
        mock_process = MagicMock(pid=12345)
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        endpoint = Endpoint(id=0, ip="10.0.0.1", business_port="9000", mgmt_port="9090")

        daemon.pull_engine(
            PDRole.ROLE_P,
            [endpoint],
            instance_id=1,
            master_dp_ip="192.168.1.100",
        )

        mock_popen.assert_called_once()
        cmd = mock_popen.call_args.args[0]
        assert '--d2d-peer-ips' not in cmd

    @patch('subprocess.Popen')
    def test_pull_engine_with_empty_d2d_peer_ips(self, mock_popen, daemon):
        """pull_engine does NOT add --d2d-peer-ips when d2d_peer_ips is empty list
        (no peers means no D2D transfer needed; upstream returns None, not []).
        """
        mock_process = MagicMock(pid=12345)
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        endpoint = Endpoint(id=0, ip="10.0.0.1", business_port="9000", mgmt_port="9090")

        daemon.pull_engine(
            PDRole.ROLE_P,
            [endpoint],
            instance_id=1,
            master_dp_ip="192.168.1.100",
            d2d_peer_ips=[],
        )

        mock_popen.assert_called_once()
        cmd = mock_popen.call_args.args[0]
        assert '--d2d-peer-ips' not in cmd

    @patch('subprocess.Popen')
    def test_pull_engine_with_d2d_peer_ips_rank_encoded(self, mock_popen, daemon):
        """pull_engine routes rank-encoded d2d_peer_ips to matching endpoint.id engines."""
        mock_process = MagicMock(pid=12345)
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        endpoints = [
            Endpoint(id=0, ip="10.0.0.1", business_port="9000", mgmt_port="9090"),
            Endpoint(id=1, ip="10.0.0.1", business_port="9001", mgmt_port="9091"),
        ]
        d2d_peer_ips = ["0:192.168.1.10", "1:192.168.1.11"]

        daemon.pull_engine(
            PDRole.ROLE_P,
            endpoints,
            instance_id=1,
            master_dp_ip="192.168.1.100",
            d2d_peer_ips=d2d_peer_ips,
        )

        assert mock_popen.call_count == 2
        first_cmd = mock_popen.call_args_list[0].args[0]
        second_cmd = mock_popen.call_args_list[1].args[0]
        assert first_cmd[first_cmd.index('--d2d-peer-ips') + 1] == "192.168.1.10"
        assert second_cmd[second_cmd.index('--d2d-peer-ips') + 1] == "192.168.1.11"

    @patch('subprocess.Popen')
    def test_pull_engine_d2d_peer_ips_no_match_for_endpoint(self, mock_popen, daemon):
        """pull_engine does NOT add --d2d-peer-ips when d2d_peer_ips has no entries for this endpoint.id."""
        mock_process = MagicMock(pid=12345)
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        endpoint = Endpoint(id=1, ip="10.0.0.1", business_port="9000", mgmt_port="9090")
        d2d_peer_ips = ["0:192.168.1.10"]

        daemon.pull_engine(
            PDRole.ROLE_P,
            [endpoint],
            instance_id=1,
            master_dp_ip="192.168.1.100",
            d2d_peer_ips=d2d_peer_ips,
        )

        mock_popen.assert_called_once()
        cmd = mock_popen.call_args.args[0]
        assert '--d2d-peer-ips' not in cmd

    @patch('subprocess.Popen')
    def test_pull_engine_includes_node_rank(self, mock_popen, daemon):
        """Test that --node-rank is included in the engine_server CLI with default value"""
        mock_process = MagicMock(pid=12345)
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        endpoint = Endpoint(id=0, ip="10.0.0.1", business_port="9000", mgmt_port="9090")
        daemon.pull_engine(PDRole.ROLE_P, [endpoint], instance_id=1, master_dp_ip="192.168.1.100")

        cmd = mock_popen.call_args.args[0]
        assert "--node-rank" in cmd
        node_rank_index = cmd.index("--node-rank")
        assert cmd[node_rank_index + 1] == "0"

    @patch('subprocess.Popen')
    def test_pull_engine_custom_node_rank(self, mock_popen, daemon):
        """Test that --node-rank value matches the node_rank parameter"""
        mock_process = MagicMock(pid=12345)
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        endpoint = Endpoint(id=1, ip="10.0.0.1", business_port="9000", mgmt_port="9090")
        daemon.pull_engine(PDRole.ROLE_P, [endpoint], instance_id=1, master_dp_ip="192.168.1.100", node_rank=2)

        cmd = mock_popen.call_args.args[0]
        node_rank_index = cmd.index("--node-rank")
        assert cmd[node_rank_index + 1] == "2"
