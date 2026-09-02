# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import threading
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

import pytest

from motor.common.resources.dispatch import DispatchProfile
from motor.common.resources.endpoint import Endpoint
from motor.common.resources.instance import PDRole
from motor.config.node_manager import VLLMStartupAccelerationConfig
from motor.node_manager.core.services.native_engine import service as service_module
from motor.node_manager.core.services.native_engine.models import CommandSpec, LaunchSpec, ProbeSpec, RuntimeState
from motor.node_manager.core.services.native_engine.service import NativeEngineService
from motor.node_manager.core.services.native_engine.virtual_inference import VirtualInferenceSpec


def _fake_deploy_config(
    *,
    enable_virtual_inference: bool = True,
    npu_usage_threshold: int = 3,
    max_failure_count: int = 6,
    infer_tls=None,
    virtual_inference_timeout: float = 5.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        model_config=SimpleNamespace(model_name="test-model"),
        engine_config=SimpleNamespace(configs={}),
        dispatch_profile=None,
        infer_tls_config=infer_tls,
        health_check_config=SimpleNamespace(
            enable_virtual_inference=enable_virtual_inference,
            npu_usage_threshold=npu_usage_threshold,
            max_failure_count=max_failure_count,
            virtual_inference_timeout=virtual_inference_timeout,
        ),
    )


def _make_endpoint(endpoint_id: int, *, headless: bool = False) -> Endpoint:
    return Endpoint(id=endpoint_id, ip="127.0.0.1", business_port="8000", headless=headless)


def _make_launch_spec(deploy_config=None, env=None) -> LaunchSpec:
    return LaunchSpec(
        command=CommandSpec(argv=("vllm", "serve"), env=env or {}),
        probe=ProbeSpec(path="/health", timeout_seconds=1, startup_timeout_seconds=10),
        deploy_config=deploy_config,
    )


def _native_engine_service(
    snapshot_metadata: str | None = None,
    engine_type: str = "sglang",
    startup_acceleration_config: VLLMStartupAccelerationConfig | None = None,
) -> NativeEngineService:
    with (
        patch("motor.node_manager.core.services.native_engine.service.get_backend") as get_backend,
        patch("motor.node_manager.core.services.native_engine.service.ProcessSupervisor") as supervisor_class,
    ):
        service = NativeEngineService(
            engine_type=engine_type,
            config_path="/tmp/config.json",
            device_num=1,
            parallel_config=SimpleNamespace(local_world_size=1),
            enable_multi_endpoints=False,
            snapshot_metadata=snapshot_metadata,
            startup_acceleration_config=startup_acceleration_config,
        )
    service.backend = get_backend.return_value
    service.supervisor = supervisor_class.return_value
    return service


def _make_spec(**overrides) -> VirtualInferenceSpec:
    values = {
        "instance_id": 1,
        "endpoint_id": 0,
        "host": "127.0.0.1",
        "port": 8000,
        "role": PDRole.ROLE_U,
        "engine_type": "vllm",
        "model_name": "test-model",
        "dispatch_profile": DispatchProfile.UNKNOWN,
        "tls_config": None,
        "enabled": True,
        "npu_usage_threshold": 3,
        "max_failure_count": 6,
    }
    values.update(overrides)
    return VirtualInferenceSpec(**values)


def _install_monitor(service, worker, spec=None):
    """Install a mock monitor worker, attaching its spec, and return the worker."""
    worker.spec = spec or _make_spec()
    service._virtual_monitor = worker
    return worker


def _arrange_successful_pull(service, *, deploy_config=None, env=None, endpoints=None):
    """Wire prepare/start and pull once; returns the resulting monitor (may be None)."""
    service.backend.prepare.return_value = _make_launch_spec(deploy_config or _fake_deploy_config(), env=env)
    service.supervisor.start.return_value = True
    service.pull(PDRole.ROLE_U, endpoints or [_make_endpoint(0)], instance_id=1, master_dp_ip="127.0.0.1")
    return service._virtual_monitor


def _log_level_warning_emitted(mock_warning) -> bool:
    return any(
        "ASCEND_GLOBAL_LOG_LEVEL" in str(call.args[0]) if call.args else False for call in mock_warning.call_args_list
    )


def test_pull_forwards_snapshot_metadata_to_launch_context():
    service = _native_engine_service("/snapshot/metadata.json")
    service.backend.prepare.return_value = LaunchSpec(
        command=CommandSpec(argv=("vllm", "serve"), env={}),
        probe=ProbeSpec(path="/snapshot/health", timeout_seconds=1, startup_timeout_seconds=10),
    )
    service.supervisor.start.return_value = True
    endpoint = Endpoint(id=0, ip="127.0.0.1", business_port="8000")

    service.pull(PDRole.ROLE_U, [endpoint], instance_id=1, master_dp_ip="127.0.0.1")

    context = service.backend.prepare.call_args.args[0]
    assert context.snapshot_metadata == "/snapshot/metadata.json"


def test_pull_applies_vllm_startup_acceleration_and_runs_preflights_once():
    startup_config = VLLMStartupAccelerationConfig(
        enable_startup_plan=True,
        enable_graph_reuse=True,
        cache_root="/mnt/vllm-cache",
    )
    service = _native_engine_service(engine_type="vllm", startup_acceleration_config=startup_config)
    service.backend.prepare.return_value = _make_launch_spec(_fake_deploy_config(enable_virtual_inference=False))
    service.supervisor.start.return_value = True

    with (
        patch.object(service_module, "inspect_startup_plan_profiles") as inspect_profiles,
        patch.object(service_module, "inspect_graph_reuse_cache_root") as inspect_graph_cache,
    ):
        service.pull(
            PDRole.ROLE_U,
            [_make_endpoint(0)],
            instance_id=1,
            master_dp_ip="127.0.0.1",
        )

    context = service.backend.prepare.call_args.args[0]
    assert context.environment["VLLM_CACHE_ROOT"] == "/mnt/vllm-cache"
    assert context.environment["VLLM_ENABLE_STARTUP_PLAN"] == "1"
    assert context.environment["VLLM_DISABLE_COMPILE_CACHE"] == "0"
    assert context.engine_config_overrides["enforce_eager"] is False
    assert context.engine_config_overrides["compilation_config"]["cudagraph_mode"] == "FULL"
    assert context.engine_config_overrides["additional_config"]["ascend_compilation_config"] == {
        "enable_npugraph_ex": True
    }
    inspect_profiles.assert_called_once_with("/mnt/vllm-cache")
    inspect_graph_cache.assert_called_once_with("/mnt/vllm-cache")


def test_health_check_returns_dead_pids():
    service = _native_engine_service()
    service.supervisor.dead_pids.return_value = [(101, 0)]

    assert service.health_check() == [(101, 0)]


def test_health_check_empty_when_no_deaths():
    service = _native_engine_service()
    service.supervisor.dead_pids.return_value = []

    assert service.health_check() == []


def test_pull_registers_single_virtual_worker_when_enabled():
    service = _native_engine_service(engine_type="vllm")
    service.backend.prepare.return_value = _make_launch_spec(_fake_deploy_config())
    service.supervisor.start.return_value = True
    service.pull(PDRole.ROLE_U, [_make_endpoint(0)], instance_id=1, master_dp_ip="127.0.0.1")

    assert service._virtual_monitor is not None
    worker = service._virtual_monitor
    assert worker.spec.enabled is True
    assert worker.spec.model_name == "test-model"
    assert worker.spec.npu_usage_threshold == 3
    assert worker.spec.max_failure_count == 6
    assert worker.spec.instance_id == 1
    assert worker.spec.endpoint_id == 0


@pytest.mark.parametrize(
    "engine_type, endpoint_ids, config_kwargs, expect_monitor",
    [
        # config off → no monitor
        ("vllm", [0], {"enable_virtual_inference": False}, False),
        # non-dp0：only endpoint 0 (dp0) is eligible among [0, 1]
        ("vllm", [0, 1], {}, True),
        # unknown engine → no monitor
        ("sglang", [0], {}, False),
        # invalid threshold → no monitor
        ("vllm", [0], {"npu_usage_threshold": 101}, False),
    ],
    ids=["config_off", "non_dp0", "sglang", "invalid_threshold"],
)
def test_pull_virtual_inference_gates(engine_type, endpoint_ids, config_kwargs, expect_monitor):
    service = _native_engine_service(engine_type=engine_type)
    service.backend.prepare.return_value = _make_launch_spec(_fake_deploy_config(**config_kwargs))
    service.supervisor.start.return_value = True
    endpoints = [_make_endpoint(eid) for eid in endpoint_ids]
    service.pull(PDRole.ROLE_U, endpoints, instance_id=1, master_dp_ip="127.0.0.1")

    assert (service._virtual_monitor is not None) is expect_monitor


def test_vllm_non_error_log_level_skips_monitor_but_keeps_engine():
    """Eligible vLLM + final env != 3: no monitor, warning, engine kept."""
    service = _native_engine_service(engine_type="vllm")
    with patch.object(service_module.logger, "warning") as mock_warning:
        monitor = _arrange_successful_pull(service, env={"ASCEND_GLOBAL_LOG_LEVEL": "1", "KEEP": "1"})

    assert monitor is None
    service.supervisor.start.assert_called_once()
    assert _log_level_warning_emitted(mock_warning)


def test_pull_no_eligible_target_clears_previous_monitor():
    service = _native_engine_service(engine_type="vllm")
    service.backend.prepare.return_value = _make_launch_spec(_fake_deploy_config())
    service.supervisor.start.return_value = True
    service.pull(PDRole.ROLE_U, [_make_endpoint(0)], instance_id=1, master_dp_ip="127.0.0.1")
    first_worker = service._virtual_monitor

    service.backend.prepare.return_value = _make_launch_spec(_fake_deploy_config(enable_virtual_inference=False))
    with patch.object(first_worker, "stop") as mock_stop:
        service.pull(PDRole.ROLE_U, [_make_endpoint(0)], instance_id=1, master_dp_ip="127.0.0.1")

    assert service._virtual_monitor is None
    mock_stop.assert_called_once()


def test_pull_worker_build_failure_keeps_engine_and_clears_monitor():
    """Worker build failure must keep the engine but clear any stale monitor."""
    service = _native_engine_service(engine_type="vllm")
    service.backend.prepare.return_value = _make_launch_spec(_fake_deploy_config())
    service.supervisor.start.return_value = True
    service.pull(PDRole.ROLE_U, [_make_endpoint(0)], instance_id=1, master_dp_ip="127.0.0.1")
    first_worker = service._virtual_monitor

    service.backend.prepare.return_value = _make_launch_spec(_fake_deploy_config(virtual_inference_timeout=12.5))
    with (
        patch.object(
            service_module,
            "VirtualInferenceWorker",
            side_effect=RuntimeError("worker build failed"),
        ),
        patch.object(first_worker, "stop") as mock_stop,
    ):
        service.pull(PDRole.ROLE_U, [_make_endpoint(0)], instance_id=1, master_dp_ip="127.0.0.1")

    assert service._virtual_monitor is None
    mock_stop.assert_called_once()
    service.supervisor.stop.assert_not_called()


def test_runtime_state_merges_virtual_abnormal_to_unhealthy():
    service = _native_engine_service(engine_type="vllm")
    service.supervisor.state.return_value = RuntimeState.READY
    worker = mock.MagicMock()
    worker.is_abnormal.return_value = True
    _install_monitor(service, worker)

    assert service.runtime_state(_make_endpoint(0), instance_id=1) == RuntimeState.UNHEALTHY


def test_runtime_state_starts_worker_on_first_ready():
    service = _native_engine_service(engine_type="vllm")
    service.supervisor.state.return_value = RuntimeState.READY
    worker = mock.MagicMock()
    worker.is_abnormal.return_value = False
    _install_monitor(service, worker)

    assert service.runtime_state(_make_endpoint(0), instance_id=1) == RuntimeState.READY
    worker.start.assert_called_once()


def test_stop_stops_worker_and_supervisor():
    service = _native_engine_service(engine_type="vllm")
    worker = mock.MagicMock()
    _install_monitor(service, worker)

    service.stop()

    worker.stop.assert_called_once()
    service.supervisor.stop_all.assert_called_once()
    assert service._virtual_monitor is None


def test_stop_waits_for_inflight_pull_and_monitor_stays_cleared():
    """stop() is serialized with pull() via _pull_lock: after stop() returns, no
    in-flight pull can install a monitor afterwards.
    """
    service = _native_engine_service(engine_type="vllm")
    service.backend.prepare.return_value = _make_launch_spec(_fake_deploy_config())
    service.supervisor.start.return_value = True

    build_started = threading.Event()
    release_build = threading.Event()
    stop_started = threading.Event()
    stop_done = threading.Event()
    real_worker_cls = service_module.VirtualInferenceWorker

    def blocking_build(spec):
        build_started.set()
        release_build.wait(timeout=5)
        return real_worker_cls(spec)

    with patch.object(service_module, "VirtualInferenceWorker", side_effect=blocking_build):
        pull_thread = threading.Thread(
            target=service.pull,
            args=(PDRole.ROLE_U, [_make_endpoint(0)], 1, "127.0.0.1"),
        )
        pull_thread.start()
        assert build_started.wait(timeout=5)

        def stop_and_mark():
            stop_started.set()
            service.stop()
            stop_done.set()

        stop_thread = threading.Thread(target=stop_and_mark)
        stop_thread.start()
        assert stop_started.wait(timeout=5)
        # While the pull is still blocked in the worker build (release_build not
        # set), stop() must not have returned — it is waiting on _pull_lock.
        assert not stop_done.wait(timeout=0.3)

        release_build.set()
        pull_thread.join(timeout=5)
        stop_thread.join(timeout=5)

    assert not pull_thread.is_alive()
    assert not stop_thread.is_alive()
    assert stop_done.is_set()
    # stop() must have waited for the in-flight pull and cleared its monitor;
    # otherwise the pull would install the monitor after stop() returned.
    assert service._virtual_monitor is None
