# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import json
from dataclasses import replace
from pathlib import Path

import pytest

from motor.common.resources.instance import PDRole
from motor.common.utils.xllm import xllm_kv_endpoints
from motor.config.coordinator import KvConductorConfig
from motor.config.node_manager import NodeManagerConfig, SingleContainerNodemanagerConfig
from motor.node_manager.core.services.native_engine.factory import get_backend
from motor.node_manager.core.services.native_engine.models import LaunchContext


def _context(path: Path, options: dict | None = None) -> LaunchContext:
    config = {
        "motor_deploy_config": {},
        "motor_engine_union_config": {
            "engine_type": "xllm",
            "engine_config": {"model": "/models/qwen", "model_id": "qwen", "max_model_len": 4096, **(options or {})},
        },
        "kv_conductor_config": {"conductor_service": "127.0.0.1", "store_backend": "xllm"},
    }
    path.write_text(json.dumps(config), encoding="utf-8")
    return LaunchContext(
        role=PDRole.ROLE_U,
        instance_id=7,
        dp_rank=0,
        node_rank=0,
        host="127.0.0.1",
        business_port=8000,
        config_path=str(path),
        master_dp_ip="127.0.0.1",
        kv_port=None,
        lookup_rpc_port=None,
        dp_rpc_port=None,
        d2d_peer_ips=(),
        environment={"ASCEND_RT_VISIBLE_DEVICES": "2"},
    )


def test_xllm_native_command_and_conductor_use_identical_local_cache_ports(tmp_path: Path) -> None:
    spec = get_backend("xllm").prepare(_context(tmp_path / "config.json", {"enable_graph": False}))
    assert spec.command.argv[0] == "xllm"
    flags = dict(item[2:].split("=", 1) for item in spec.command.argv[1:])
    assert flags["model"] == "/models/qwen" and flags["model_id"] == "qwen"
    assert flags["port"] == "8000" and flags["master_node_addr"] == "127.0.0.1:13000"
    assert flags["enable_graph"] == "false"
    assert "max_model_len" not in flags
    assert spec.command.env["ASCEND_RT_VISIBLE_DEVICES"] == "2"
    assert spec.probe.path == "/health"
    events = json.loads(flags["kv_events_config"])
    expected = xllm_kv_endpoints("127.0.0.1", 8000, 0, KvConductorConfig())
    assert all(events[key] == value for key, value in expected.items())


@pytest.mark.parametrize("role", [PDRole.ROLE_P, PDRole.ROLE_D, PDRole.ROLE_E])
def test_xllm_rejects_pd_roles_before_launch(tmp_path: Path, role: PDRole) -> None:
    context = replace(_context(tmp_path / "config.json"), role=role)
    with pytest.raises(ValueError, match="union"):
        get_backend("xllm").prepare(context)


@pytest.mark.parametrize(
    "options",
    [
        {"dp_size": 2},
        {"tp_size": 2},
        {"nnodes": 2},
        {"enable_disagg_pd": True},
        {"enable_prefix_cache": False},
        {"num_speculative_tokens": 1},
        {"block_size": 64},
        {"kv_events_config": {"endpoint": "tcp://127.0.0.1:5557"}},
    ],
)
def test_xllm_rejects_unsupported_or_mismatched_engine_configuration(tmp_path: Path, options: dict) -> None:
    with pytest.raises(ValueError):
        get_backend("xllm").prepare(_context(tmp_path / "config.json", options))


@pytest.mark.parametrize(
    "reg",
    [
        KvConductorConfig(xllm_event_port_offset=True),
        KvConductorConfig(xllm_pool_port_offset=10000),
        KvConductorConfig(xllm_event_port_offset=60000),
    ],
)
def test_xllm_rejects_invalid_cache_ports(reg: KvConductorConfig) -> None:
    with pytest.raises((ValueError, TypeError)):
        xllm_kv_endpoints("127.0.0.1", 8000, 0, reg)


def test_xllm_cache_ports_support_ipv6_and_independent_instances() -> None:
    reg = KvConductorConfig(xllm_event_port_offset=1000, xllm_pool_port_offset=2000, xllm_replay_port_offset=3000)
    assert xllm_kv_endpoints("::1", 8000, 0, reg)["endpoint"] == "tcp://[::1]:9000"
    assert xllm_kv_endpoints("::1", 8002, 0, reg)["endpoint"] == "tcp://[::1]:9002"
    with pytest.raises(ValueError, match="one DP"):
        xllm_kv_endpoints("::1", 8000, 1, reg)


def test_single_container_xllm_has_independent_devices_without_vllm_rpc_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ROLE", "union")
    monkeypatch.setenv("INDEX", "1")
    config = SingleContainerNodemanagerConfig.from_json(
        {
            "motor_deploy_config": {"deploy_mode": "single_container"},
            "motor_engine_union_config": {"engine_type": "xllm", "engine_config": {"dp_size": 1}},
        }
    )
    assert config.single_container_flag
    assert config.device_offset == 1 and config.device_num == 1
    assert config.base_port_offset == 2 and config.node_manager_port_offset == 1
    assert config.dp_rpc_port is None


def test_example_loads_full_nodemanager_configuration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ROLE", "union")
    monkeypatch.setenv("INDEX", "1")
    example = Path(__file__).resolve().parents[7] / "model_configs/infer_engines/xllm/user_config.json"
    path = tmp_path / "user_config.json"
    path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    config = NodeManagerConfig.from_json(str(path))
    assert config.basic_config.engine_type == "xllm"
    assert config.basic_config.model_name == "Qwen3-8B"
    assert config.basic_config.device_num == 1
    assert config.api_config.node_manager_port == 3027
    assert config.endpoint_config.base_port == 8002
