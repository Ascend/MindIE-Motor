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

os.environ["USER_CONFIG_PATH"] = os.path.join(os.path.dirname(__file__), "..", "jsons", "user_config.json")

from motor.node_manager.core.daemon import Daemon
from motor.node_manager.core.services.registry import SERVICE_ENGINE
from motor.config.node_manager import NodeManagerConfig
from motor.common.resources.endpoint import Endpoint
from motor.common.resources.instance import PDRole, ParallelConfig
from motor.node_manager.core.services.native_engine.models import CommandSpec, LaunchSpec, ProbeSpec, RuntimeState


def _recording_backend():
    backend = MagicMock()

    def prepare(context):
        return LaunchSpec(
            command=CommandSpec(
                argv=(
                    "vllm",
                    "serve",
                    "--host",
                    context.host,
                    "--port",
                    str(context.business_port),
                ),
                env=context.environment,
            ),
            probe=ProbeSpec(path="/health", timeout_seconds=5, startup_timeout_seconds=1800),
        )

    backend.prepare.side_effect = prepare
    return backend


def _last_launch_context(daemon):
    backend = daemon._services[SERVICE_ENGINE].backend
    return backend.prepare.call_args.args[0]


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
        config.basic_config.model_name = config_data.get("model_name", "test-model")
        config.basic_config.engine_type = "vllm"
        config.basic_config.role = PDRole(config_data.get("role", "both"))
        config.api_config.node_manager_port = config_data.get("node_manager_port", 8080)

        # Set device_num for testing (simulating visible devices)
        config.basic_config.device_num = 8  # 8 devices for testing

        backend = _recording_backend()
        with patch("motor.node_manager.core.services.native_engine.service.get_backend", return_value=backend):
            daemon_instance = Daemon(config)
            yield daemon_instance


@pytest.fixture
def endpoints():
    return [Endpoint(id=i, ip=f"192.168.1.{100 + i}", business_port=str(8000 + i * 2)) for i in range(3)]


class TestDaemon:
    @pytest.mark.parametrize(
        ("launch_env", "expected_host_ip", "expected_mc_ipv6"),
        [
            (
                {"POD_IP": "10.0.0.8", "MOONCAKE_ASCEND_IPV6_EXPERIMENT": "1"},
                "10.0.0.8",
                "1",
            ),
            (
                {
                    "POD_IP": "10.0.0.8",
                    "VLLM_HOST_IP": "10.0.0.9",
                    "MOONCAKE_ASCEND_IPV6_EXPERIMENT": "1",
                    "MC_USE_IPV6": "0",
                },
                "10.0.0.9",
                "0",
            ),
        ],
    )
    @patch('subprocess.Popen')
    def test_engine_launch_environment_contract(
        self,
        mock_popen,
        daemon,
        launch_env,
        expected_host_ip,
        expected_mc_ipv6,
    ):
        mock_process = MagicMock(pid=12345)
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process
        endpoint = Endpoint(id=0, ip="10.0.0.1", business_port="9000")

        with patch.dict(os.environ, launch_env, clear=True):
            daemon.pull_engine(PDRole.ROLE_U, [endpoint], instance_id=1, master_dp_ip="192.168.1.100")

        child_env = mock_popen.call_args.kwargs["env"]
        assert child_env["VLLM_HOST_IP"] == expected_host_ip
        assert child_env["MC_USE_IPV6"] == expected_mc_ipv6
        assert child_env["ASCEND_RT_VISIBLE_DEVICES"] == "0,1"

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

    def test_pull_engine_failure_rolls_back_only_new_endpoints(self, daemon, endpoints):
        engine = daemon._services[SERVICE_ENGINE]
        with (
            patch.object(engine.supervisor, "start", side_effect=[False, True, RuntimeError("start failed")]),
            patch.object(engine.supervisor, "stop") as stop,
        ):
            with pytest.raises(RuntimeError, match="start failed"):
                daemon.pull_engine(PDRole.ROLE_P, endpoints, instance_id=1, master_dp_ip="192.168.1.100")

        stop.assert_called_once_with(endpoints[1].id)

    def test_get_engine_runtime_state_passes_instance_id(self, daemon):
        engine = daemon._services[SERVICE_ENGINE]
        endpoint = Endpoint(id=0, ip="10.0.0.1", business_port="9000")
        with patch.object(engine, "runtime_state", return_value=RuntimeState.READY) as runtime_state:
            result = daemon.get_engine_runtime_state(endpoint, instance_id=42)

        assert result == RuntimeState.READY
        runtime_state.assert_called_once_with(endpoint, 42)

    def test_get_engine_runtime_state_no_engine_returns_stopped(self, daemon):
        endpoint = Endpoint(id=0, ip="10.0.0.1", business_port="9000")
        with patch.object(daemon, "_services", {}):
            assert daemon.get_engine_runtime_state(endpoint, instance_id=1) == RuntimeState.STOPPED

    @patch('subprocess.Popen')
    def test_native_metrics_target_uses_business_port(self, mock_popen, daemon):
        process = MagicMock(pid=12345)
        process.poll.return_value = None
        mock_popen.return_value = process
        endpoint = Endpoint(id=0, ip="2001:db8::8", business_port="8000")
        daemon.pull_engine(PDRole.ROLE_U, [endpoint], instance_id=1, master_dp_ip="192.168.1.100")

        assert daemon.get_engine_metrics_target(endpoint) == "http://[2001:db8::8]:8000/metrics"

        endpoint.headless = True
        assert daemon.get_engine_metrics_target(endpoint) is None

    @patch('subprocess.Popen')
    @patch('motor.node_manager.core.daemon.logger')
    def test_command_format(self, mock_logger, mock_popen, daemon):
        mock_process = MagicMock(pid=12345)
        mock_process.poll.return_value = None  # Process is still running
        mock_popen.return_value = mock_process

        endpoint = Endpoint(id=5, ip="10.0.0.1", business_port="9000")
        instance_id = 1
        master_dp_ip = "192.168.1.100"
        daemon.pull_engine(PDRole.ROLE_P, [endpoint], instance_id, master_dp_ip)

        # Verify that process was added to engine_pids
        assert len(daemon.engine_pids) > 0
        assert 12345 in daemon.engine_pids
        # Verify Popen was called
        mock_popen.assert_called_once()
        assert mock_popen.call_args.args[0][:2] == ["vllm", "serve"]
        assert "engine_server" not in mock_popen.call_args.args[0]

    @patch('subprocess.Popen')
    def test_hybrid_role_starts_union_engine(self, mock_popen, daemon):
        mock_process = MagicMock(pid=12345)
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        endpoint = Endpoint(id=0, ip="10.0.0.1", business_port="9000")
        daemon.pull_engine(PDRole.ROLE_U, [endpoint], instance_id=1, master_dp_ip="192.168.1.100")

        context = _last_launch_context(daemon)
        assert context.role == PDRole.ROLE_U

    @patch('subprocess.Popen')
    def test_single_container_hybrid_omits_kv_port_when_unset(self, mock_popen, config_data):
        if hasattr(Daemon, '_instances') and Daemon in Daemon._instances:
            del Daemon._instances[Daemon]

        config_path = os.path.join(os.path.dirname(__file__), '..', 'jsons', 'user_config.json')
        with patch.dict('os.environ', {'JOB_NAME': 'test_job', 'USER_CONFIG_PATH': config_path, 'ROLE': 'both'}):
            config = NodeManagerConfig()
            config.basic_config.parallel_config = ParallelConfig(
                tp_size=config_data["parallel_config"]["tp_size"],
                pp_size=config_data["parallel_config"]["pp_size"],
            )
            config.basic_config.device_num = 8
            config.basic_config.enable_multi_endpoints = False
            config.basic_config.engine_type = "vllm"
            config.single_container_config.single_container_flag = True
            config.single_container_config.device_offset = 0
            config.single_container_config.kv_port = None
            config.single_container_config.dp_rpc_port = 9000
            backend = _recording_backend()
            with patch("motor.node_manager.core.services.native_engine.service.get_backend", return_value=backend):
                daemon = Daemon(config)

        mock_process = MagicMock(pid=12345)
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        endpoint = Endpoint(id=0, ip="10.0.0.1", business_port="9000")
        daemon.pull_engine(PDRole.ROLE_U, [endpoint], instance_id=1, master_dp_ip="192.168.1.100")

        context = _last_launch_context(daemon)
        assert context.kv_port is None
        assert context.dp_rpc_port == 9000

    # ===== D2D Weight Transfer Tests =====

    @patch('subprocess.Popen')
    def test_pull_engine_with_d2d_peer_ips(self, mock_popen, daemon):
        """pull_engine adds --d2d-peer-ips CLI arg when d2d_peer_ips is provided."""
        mock_process = MagicMock(pid=12345)
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        endpoint = Endpoint(id=0, ip="10.0.0.1", business_port="9000")
        d2d_peer_ips = ["0:192.168.1.10", "0:192.168.1.11"]

        daemon.pull_engine(
            PDRole.ROLE_P,
            [endpoint],
            instance_id=1,
            master_dp_ip="192.168.1.100",
            d2d_peer_ips=d2d_peer_ips,
        )

        mock_popen.assert_called_once()
        context = _last_launch_context(daemon)
        assert context.d2d_peer_ips == ("192.168.1.10", "192.168.1.11")

    @patch('subprocess.Popen')
    def test_pull_engine_without_d2d_peer_ips(self, mock_popen, daemon):
        """pull_engine does NOT add --d2d-peer-ips when d2d_peer_ips is None."""
        mock_process = MagicMock(pid=12345)
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        endpoint = Endpoint(id=0, ip="10.0.0.1", business_port="9000")

        daemon.pull_engine(
            PDRole.ROLE_P,
            [endpoint],
            instance_id=1,
            master_dp_ip="192.168.1.100",
        )

        mock_popen.assert_called_once()
        assert _last_launch_context(daemon).d2d_peer_ips == ()

    @patch('subprocess.Popen')
    def test_pull_engine_with_empty_d2d_peer_ips(self, mock_popen, daemon):
        """pull_engine does NOT add --d2d-peer-ips when d2d_peer_ips is empty list
        (no peers means no D2D transfer needed; upstream returns None, not []).
        """
        mock_process = MagicMock(pid=12345)
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        endpoint = Endpoint(id=0, ip="10.0.0.1", business_port="9000")

        daemon.pull_engine(
            PDRole.ROLE_P,
            [endpoint],
            instance_id=1,
            master_dp_ip="192.168.1.100",
            d2d_peer_ips=[],
        )

        mock_popen.assert_called_once()
        assert _last_launch_context(daemon).d2d_peer_ips == ()

    @patch('subprocess.Popen')
    def test_pull_engine_with_d2d_peer_ips_rank_encoded(self, mock_popen, daemon):
        """pull_engine routes rank-encoded d2d_peer_ips to matching endpoint.id engines."""
        mock_process = MagicMock(pid=12345)
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        endpoints = [
            Endpoint(id=0, ip="10.0.0.1", business_port="9000"),
            Endpoint(id=1, ip="10.0.0.1", business_port="9001"),
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
        contexts = [call.args[0] for call in daemon._services[SERVICE_ENGINE].backend.prepare.call_args_list]
        assert contexts[0].d2d_peer_ips == ("192.168.1.10",)
        assert contexts[1].d2d_peer_ips == ("192.168.1.11",)

    @patch('subprocess.Popen')
    def test_pull_engine_d2d_peer_ips_no_match_for_endpoint(self, mock_popen, daemon):
        """pull_engine does NOT add --d2d-peer-ips when d2d_peer_ips has no entries for this endpoint.id."""
        mock_process = MagicMock(pid=12345)
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        endpoint = Endpoint(id=1, ip="10.0.0.1", business_port="9000")
        d2d_peer_ips = ["0:192.168.1.10"]

        daemon.pull_engine(
            PDRole.ROLE_P,
            [endpoint],
            instance_id=1,
            master_dp_ip="192.168.1.100",
            d2d_peer_ips=d2d_peer_ips,
        )

        mock_popen.assert_called_once()
        assert _last_launch_context(daemon).d2d_peer_ips == ()

    @patch('subprocess.Popen')
    def test_pull_engine_includes_node_rank(self, mock_popen, daemon):
        """Test that the default node rank is passed to the backend."""
        mock_process = MagicMock(pid=12345)
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        endpoint = Endpoint(id=0, ip="10.0.0.1", business_port="9000")
        daemon.pull_engine(PDRole.ROLE_P, [endpoint], instance_id=1, master_dp_ip="192.168.1.100")

        assert _last_launch_context(daemon).node_rank == 0

    @patch('subprocess.Popen')
    def test_pull_engine_custom_node_rank(self, mock_popen, daemon):
        """Test that the configured node rank is passed to the backend."""
        mock_process = MagicMock(pid=12345)
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        endpoint = Endpoint(id=1, ip="10.0.0.1", business_port="9000")
        daemon.pull_engine(PDRole.ROLE_P, [endpoint], instance_id=1, master_dp_ip="192.168.1.100", node_rank=2)

        assert _last_launch_context(daemon).node_rank == 2


def _patch_restart_params(restart_params):
    """Patch the RegisterManager the Daemon resolves relaunch params from."""
    return patch(
        "motor.node_manager.core.register_manager.RegisterManager",
        return_value=MagicMock(get_restart_params=MagicMock(return_value=restart_params)),
    )


def _restart_params():
    return {
        "role": "prefill",
        "endpoints": [Endpoint(id=i, ip=f"192.168.1.{100 + i}", business_port=str(8000 + i * 2)) for i in range(2)],
        "instance_id": 1,
        "master_dp_ip": "192.168.1.100",
        "d2d_peer_ips": None,
        "node_rank": 0,
    }


@patch("subprocess.Popen")
def test_restart_engine_relaunches_engines_in_place(mock_popen, daemon, endpoints, capsys):
    """restart_engine only touches the engine service: KV store and monitor
    thread stay alive, the FaultReporter is paused/resumed for the window.
    """
    mock_process = MagicMock(pid=12345)
    mock_process.poll.return_value = None
    mock_popen.return_value = mock_process

    kv_service = MagicMock()
    daemon._services["kv_store"] = kv_service
    daemon.pull_engine(PDRole.ROLE_P, endpoints, 1, "192.168.1.100")
    assert 12345 in daemon._services["engine"].pid_list()

    fault_reporter = MagicMock()
    daemon._fault_reporter = fault_reporter
    with _patch_restart_params(_restart_params()):
        daemon.restart_engine(1)

    kv_service.stop.assert_not_called()
    assert daemon._monitor_thread is not None and daemon._monitor_thread.is_alive()
    assert len(daemon._services["engine"].pid_list()) == len(_restart_params()["endpoints"])
    fault_reporter.pause.assert_called_once()
    fault_reporter.resume.assert_called_once()
    assert not daemon.is_engine_restart_in_progress()
    # The relaunch separator (with per-container count) hits the container
    # stdout stream so log review can delimit relaunch #N.
    engine_service = daemon._services["engine"]
    assert engine_service._restart_count == 1
    assert "[ENGINE RELAUNCH #1] instance_id=1" in capsys.readouterr().out


@pytest.mark.parametrize(
    "prepare,error",
    [
        (lambda d: setattr(d, "_engine_restart_in_progress", True), "EngineRestartInProgressError"),
        (lambda d: None, "EngineRestartParamError"),
    ],
    ids=["already_in_progress", "no_start_recorded"],
)
def test_restart_engine_rejects_invalid_requests(daemon, prepare, error):
    """Overlapping relaunches and missing launch params are rejected, not raced."""
    from motor.node_manager.core.daemon import EngineRestartInProgressError, EngineRestartParamError

    expected = EngineRestartInProgressError if error == "EngineRestartInProgressError" else EngineRestartParamError
    prepare(daemon)
    with _patch_restart_params(None if error == "EngineRestartParamError" else _restart_params()):
        with pytest.raises(expected):
            daemon.restart_engine(1)


def test_suicide_after_threshold_abnormal_observations(daemon):
    """Continuous abnormal endpoint observations reach the threshold."""
    with (
        patch("motor.node_manager.core.daemon.HeartbeatManager") as mock_hb_cls,
        patch("motor.node_manager.core.daemon.time.monotonic", return_value=1000.0),
    ):
        mock_hb = mock_hb_cls.return_value
        mock_hb.endpoints_generation.return_value = 0
        mock_hb.is_within_grace_period.return_value = False
        mock_hb.has_abnormal_endpoints.return_value = True

        daemon._last_endpoints_generation = 0
        for _ in range(daemon._suicide_threshold):
            daemon._check_suicide_condition()

    assert daemon.should_suicide() is True


def test_suicide_counter_reset_conditions(daemon):
    """Healthy endpoints, the grace period or a generation change reset the counter."""
    with (
        patch("motor.node_manager.core.daemon.HeartbeatManager") as mock_hb_cls,
        patch("motor.node_manager.core.daemon.time.monotonic", return_value=1000.0),
    ):
        mock_hb = mock_hb_cls.return_value
        mock_hb.endpoints_generation.return_value = 0
        mock_hb.is_within_grace_period.return_value = False
        mock_hb.has_abnormal_endpoints.return_value = True

        daemon._last_endpoints_generation = 0
        daemon._suicide_abnormal_count = 4
        mock_hb.has_abnormal_endpoints.return_value = False
        daemon._check_suicide_condition()
        assert daemon._suicide_abnormal_count == 0

        daemon._suicide_abnormal_count = 4
        mock_hb.is_within_grace_period.return_value = True
        mock_hb.has_abnormal_endpoints.return_value = True
        daemon._check_suicide_condition()
        assert daemon._suicide_abnormal_count == 0

        daemon._suicide_abnormal_count = 4
        mock_hb.endpoints_generation.return_value = 1
        daemon._check_suicide_condition()
        assert daemon._suicide_abnormal_count == 0

    assert not daemon.should_suicide()


def test_freeze_suspends_counting_and_expires(daemon):
    """A freeze window suspends counting; deadline expiry or abort resumes it."""
    with (
        patch("motor.node_manager.core.daemon.HeartbeatManager") as mock_hb_cls,
        patch("motor.node_manager.core.daemon.time.monotonic", return_value=1000.0),
    ):
        mock_hb = mock_hb_cls.return_value
        mock_hb.endpoints_generation.return_value = 0
        mock_hb.is_within_grace_period.return_value = False
        mock_hb.has_abnormal_endpoints.return_value = True

        daemon._last_endpoints_generation = 0
        daemon.freeze_suicide(60)
        daemon._suicide_abnormal_count = 4
        daemon._check_suicide_condition()
        assert daemon._suicide_abnormal_count == 0
        assert not daemon.should_suicide()

    with patch("motor.node_manager.core.daemon.time.monotonic", return_value=1100.0):
        assert not daemon.is_suicide_frozen()

    daemon.unfreeze_suicide()
    assert not daemon.is_suicide_frozen()


def test_engine_death_reported_and_deduped_by_pid(daemon):
    """A dead engine PID freezes suicide and reports once; a fresh PID reports again."""
    with (
        patch("motor.node_manager.core.daemon.ControllerApiClient") as mock_client_cls,
        patch("motor.node_manager.core.daemon.time.monotonic", return_value=1000.0),
    ):
        mock_client_cls.report_software_fault.return_value = True
        daemon._config.fault_tolerance_config.engine_restart_wait_timeout_sec = 60.0
        daemon._config.api_config.pod_ip = "10.0.0.1"

        daemon._handle_engine_deaths([(12345, 0)])
        daemon._handle_engine_deaths([(12345, 0)])
        daemon._handle_engine_deaths([(54321, 0)])

        assert daemon.is_suicide_frozen()
        assert mock_client_cls.report_software_fault.call_count == 2
        fault = mock_client_cls.report_software_fault.call_args_list[0][0][0]
        assert fault["engine_id"] == 0
        assert fault["engine_status"] == 1
        assert fault["exception_type"] == "EngineDeadError"
        assert fault["pod_ip"] == "10.0.0.1"


def test_engine_death_report_failure_retried_without_freeze(daemon):
    """A failed report is neither deduped nor frozen: the next round retries and
    the container-restart fallback stays live.
    """
    with (
        patch("motor.node_manager.core.daemon.ControllerApiClient") as mock_client_cls,
        patch("motor.node_manager.core.daemon.time.monotonic", return_value=1000.0),
    ):
        mock_client_cls.report_software_fault.side_effect = RuntimeError("controller down")
        daemon._handle_engine_deaths([(12345, 0)])
        assert 12345 not in daemon._reported_dead_pids
        assert not daemon.is_suicide_frozen()

        mock_client_cls.report_software_fault.side_effect = None
        mock_client_cls.report_software_fault.return_value = True
        daemon._handle_engine_deaths([(12345, 0)])
        assert 12345 in daemon._reported_dead_pids
        assert daemon.is_suicide_frozen()
        assert mock_client_cls.report_software_fault.call_count == 2


def test_engine_death_reported_without_freeze_when_relaunch_disabled(daemon):
    """With enable_engine_relaunch off the death is still reported, but the
    suicide arbitration is not frozen — the container-restart fallback (k8s)
    stays live instead of waiting out the freeze window.
    """
    with (
        patch("motor.node_manager.core.daemon.ControllerApiClient") as mock_client_cls,
        patch("motor.node_manager.core.daemon.time.monotonic", return_value=1000.0),
    ):
        mock_client_cls.report_software_fault.return_value = True
        daemon._config.fault_tolerance_config.enable_engine_relaunch = False
        daemon._config.api_config.pod_ip = "10.0.0.1"

        daemon._handle_engine_deaths([(12345, 0)])

        assert mock_client_cls.report_software_fault.call_count == 1
        assert 12345 in daemon._reported_dead_pids
        assert not daemon.is_suicide_frozen()


def test_abnormal_cold_start_endpoint_not_reported(daemon):
    """An ABNORMAL endpoint that was never NORMAL is still loading — no death report."""
    with (
        patch("motor.node_manager.core.daemon.HeartbeatManager") as mock_hb_cls,
        patch("motor.node_manager.core.daemon.ControllerApiClient") as mock_client_cls,
        patch("motor.node_manager.core.daemon.time.monotonic", return_value=1000.0),
    ):
        mock_hb = mock_hb_cls.return_value
        mock_hb.endpoints_generation.return_value = 0
        mock_hb.is_within_grace_period.return_value = False
        mock_hb.has_abnormal_endpoints.return_value = True
        mock_hb.abnormal_endpoint_ids.return_value = [0, 1]
        mock_hb.normal_endpoint_ids.return_value = []
        mock_client_cls.report_software_fault.return_value = True

        daemon._last_endpoints_generation = 0
        for _ in range(5):
            daemon._check_suicide_condition()

        assert mock_client_cls.report_software_fault.call_count == 0


def test_abnormal_after_normal_reported_until_recovery(daemon):
    """Once an endpoint turned NORMAL, its ABNORMAL is reported — once, until recovery."""
    with (
        patch("motor.node_manager.core.daemon.HeartbeatManager") as mock_hb_cls,
        patch("motor.node_manager.core.daemon.ControllerApiClient") as mock_client_cls,
        patch("motor.node_manager.core.daemon.time.monotonic", return_value=1000.0),
    ):
        mock_hb = mock_hb_cls.return_value
        mock_hb.endpoints_generation.return_value = 0
        mock_hb.is_within_grace_period.return_value = False
        mock_client_cls.report_software_fault.return_value = True

        mock_hb.has_abnormal_endpoints.return_value = False
        mock_hb.normal_endpoint_ids.return_value = [0]
        daemon._last_endpoints_generation = 0
        daemon._check_suicide_condition()

        mock_hb.has_abnormal_endpoints.return_value = True
        mock_hb.abnormal_endpoint_ids.return_value = [0]
        mock_hb.normal_endpoint_ids.return_value = []
        daemon._check_suicide_condition()
        daemon._check_suicide_condition()
        assert mock_client_cls.report_software_fault.call_count == 1
        assert daemon.is_suicide_frozen()

        daemon.unfreeze_suicide()
        mock_hb.has_abnormal_endpoints.return_value = False
        daemon._check_suicide_condition()
        assert daemon._reported_abnormal_ep_ids == set()
        mock_hb.has_abnormal_endpoints.return_value = True
        daemon._check_suicide_condition()
        assert mock_client_cls.report_software_fault.call_count == 2
