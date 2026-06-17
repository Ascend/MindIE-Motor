#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import json
import os
import tempfile
from unittest.mock import patch, MagicMock

import pytest

from motor.config.endpoint import (
    ParallelConfig,
    ModelConfig,
    EngineConfig,
    HealthCheckConfig,
    DeployConfig,
    EndpointConfig,
    PREFILL_PARALLEL_CONFIG_KEY,
    DECODE_PARALLEL_CONFIG_KEY,
    ENCODE_PARALLEL_CONFIG_KEY,
)
from motor.engine_server.constants import constants


# --- ParallelConfig tests ---


def test_parallel_config_default_values():
    """Test ParallelConfig default values with auto-computation fallback"""
    config = ParallelConfig()
    assert config.dp_size == 1
    assert config.tp_size == 1
    assert config.pp_size == 1
    assert config.world_size == 1  # auto-computed: 1*1*1*1
    assert config.local_world_size == 1  # auto-computed: 1*1*1
    assert config.enable_ep is False
    assert config.dp_rpc_port == 9000


def test_parallel_config_with_resolver_values():
    """Test that world_size and local_world_size are accepted when provided"""
    config = ParallelConfig(dp_size=2, tp_size=2, pp_size=1, pcp_size=1, world_size=4, local_world_size=2)
    assert config.world_size == 4
    assert config.local_world_size == 2


def test_parallel_config_world_size_explicit():
    """Test that world_size can be explicitly set"""
    config = ParallelConfig(dp_size=2, tp_size=2, world_size=8)
    assert config.world_size == 8


def test_parallel_config_from_dict():
    """Test ParallelConfig.from_dict"""
    data = {"dp_size": 4, "tp_size": 2, "pp_size": 1, "dp_rpc_port": 9001, "world_size": 8, "local_world_size": 2}
    config = ParallelConfig.from_dict(data)
    assert config.dp_size == 4
    assert config.tp_size == 2
    assert config.pp_size == 1
    assert config.dp_rpc_port == 9001
    assert config.world_size == 8
    assert config.local_world_size == 2


# --- ModelConfig tests ---


def test_model_config_from_dict():
    """Test ModelConfig.from_dict"""
    data = {
        "model_name": "test-model",
        "model_path": "/path/to/model",
        "npu_mem_utils": 0.9,
        PREFILL_PARALLEL_CONFIG_KEY: {"dp_size": 2, "tp_size": 1},
        DECODE_PARALLEL_CONFIG_KEY: {"dp_size": 4, "tp_size": 2},
        ENCODE_PARALLEL_CONFIG_KEY: {"dp_size": 2, "tp_size": 1},
    }
    config = ModelConfig.from_dict(data)
    assert config.model_name == "test-model"
    assert config.model_path == "/path/to/model"
    assert config.npu_mem_utils == 0.9
    assert config.prefill_parallel_config.dp_size == 2
    assert config.decode_parallel_config.dp_size == 4
    assert config.encode_parallel_config.dp_size == 2


# --- EngineConfig tests ---


def test_engine_config_from_dict():
    """Test EngineConfig.from_dict normalizes hyphen keys to underscores."""
    data = {"max_model_len": 2048, "enforce-eager": True}
    config = EngineConfig.from_dict(data)
    assert config.configs == {"max_model_len": 2048, "enforce_eager": True}
    assert config.get("enforce_eager") is True


def test_engine_config_get():
    """Test EngineConfig.get method"""
    config = EngineConfig(configs={"key1": "val1", "key2": 42})
    assert config.get("key1") == "val1"
    assert config.get("key2") == 42
    assert config.get("missing") is None
    assert config.get("missing", "default") == "default"


def test_engine_config_set():
    """Test EngineConfig.set method"""
    config = EngineConfig(configs={})
    config.set("new_key", "new_value")
    assert config.get("new_key") == "new_value"


# --- HealthCheckConfig tests ---


def test_health_check_config_defaults():
    """Test HealthCheckConfig default values"""
    config = HealthCheckConfig()
    assert config.health_collector_timeout == 2
    assert config.npu_usage_threshold == 3
    assert config.enable_virtual_inference is False


def test_health_check_config_from_dict():
    """Test HealthCheckConfig.from_dict"""
    data = {
        "health_collector_timeout": 5,
        "npu_usage_threshold": 20,
        "enable_virtual_inference": False,
    }
    config = HealthCheckConfig.from_dict(data)
    assert config.health_collector_timeout == 5
    assert config.npu_usage_threshold == 20
    assert config.enable_virtual_inference is False


# --- DeployConfig tests ---


@pytest.fixture
def simple_engine_config_file():
    """Create a temporary JSON file with simple (flat) engine config"""
    config = {
        "engine_type": "vllm",
        "model_config": {
            "model_name": "test-model",
            "model_path": "/path/to/model",
            "npu_mem_utils": 0.9,
            PREFILL_PARALLEL_CONFIG_KEY: {"dp_size": 2, "tp_size": 1},
            DECODE_PARALLEL_CONFIG_KEY: {"dp_size": 2, "tp_size": 1},
            ENCODE_PARALLEL_CONFIG_KEY: {"dp_size": 2, "tp_size": 1},
        },
        "engine_config": {"max_model_len": 2048},
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(config, f)
        temp_path = f.name
    yield temp_path
    try:
        os.unlink(temp_path)
    except FileNotFoundError:
        pass


@pytest.fixture
def pd_engine_config_file():
    """Create a temporary JSON file with PD (prefill/decode) config structure"""
    config = {
        "motor_deploy_config": {
            "p_instances_num": 1,
            "d_instances_num": 1,
        },
        "motor_engine_prefill_config": {
            "engine_type": "vllm",
            "model_config": {
                "model_name": "qwen3-8B",
                "model_path": "/mnt/weight/qwen3_8B",
                "npu_mem_utils": 0.9,
                "parallel_config": {"dp_size": 2, "tp_size": 2, "pp_size": 1},
            },
            "engine_config": {"max_model_len": 2048},
        },
        "motor_engine_decode_config": {
            "engine_type": "vllm",
            "model_config": {
                "model_name": "qwen3-8B",
                "model_path": "/mnt/weight/qwen3_8B",
                "npu_mem_utils": 0.9,
                "parallel_config": {"dp_size": 2, "tp_size": 2, "pp_size": 1},
            },
            "engine_config": {"max_model_len": 2048},
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(config, f)
        temp_path = f.name
    yield temp_path
    try:
        os.unlink(temp_path)
    except FileNotFoundError:
        pass


@pytest.fixture
def pd_hybrid_engine_config_file():
    """Create a temporary JSON file with PD hybrid union config structure"""
    config = {
        "motor_deploy_config": {
            "hybrid_instances_num": 1,
            "single_hybrid_instance_pod_num": 1,
            "hybrid_pod_npu_num": 4,
        },
        "motor_engine_union_config": {
            "engine_type": "vllm",
            "model_config": {
                "model_name": "qwen3-8B",
                "model_path": "/mnt/weight/qwen3_8B",
                "npu_mem_utils": 0.9,
                "parallel_config": {"dp_size": 2, "tp_size": 2, "pp_size": 1},
            },
            "engine_config": {"max_model_len": 2048},
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(config, f)
        temp_path = f.name
    yield temp_path
    try:
        os.unlink(temp_path)
    except FileNotFoundError:
        pass


def test_deploy_config_load_simple(simple_engine_config_file):
    """Test DeployConfig.load with simple flat config"""
    config = DeployConfig.load(simple_engine_config_file)
    assert config.engine_type == "vllm"
    assert config.model_config.model_name == "test-model"
    assert config.engine_config.get("max_model_len") == 2048
    assert config.mgmt_tls_config is None
    assert config.infer_tls_config is None


def test_deploy_config_load_with_role_prefill(pd_engine_config_file):
    """Test DeployConfig.load with role=prefill (PD config)"""
    config = DeployConfig.load(pd_engine_config_file, role="prefill")
    assert config.engine_type == "vllm"
    assert config.model_config.model_name == "qwen3-8B"


def test_deploy_config_load_with_role_decode(pd_engine_config_file):
    """Test DeployConfig.load with role=decode (PD config)"""
    config = DeployConfig.load(pd_engine_config_file, role="decode")
    assert config.engine_type == "vllm"
    assert config.model_config.model_name == "qwen3-8B"


def test_deploy_config_load_with_role_union(pd_hybrid_engine_config_file):
    """Test DeployConfig.load with role=union (PD hybrid config)"""
    config = DeployConfig.load(pd_hybrid_engine_config_file, role="union")
    assert config.engine_type == "vllm"
    assert config.model_config.model_name == "qwen3-8B"
    assert config.engine_config.get("max_model_len") == 2048

    parallel = config.get_parallel_config("union")
    assert parallel.dp_size == 2
    assert parallel.tp_size == 2
    assert parallel.pp_size == 1
    assert config.model_config.decode_parallel_config == config.model_config.prefill_parallel_config


@pytest.fixture
def encode_engine_config_file():
    """Create a temporary JSON file with encode role config section."""
    config = {
        "motor_deploy_config": {},
        "motor_engine_encode_config": {
            "engine_type": "vllm",
            "model_config": {
                "model_name": "test-model-encode",
                "model_path": "/path/to/encode_model",
                "npu_mem_utils": 0.9,
                "parallel_config": {"dp_size": 2, "tp_size": 1},
            },
            "engine_config": {"max_model_len": 2048},
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(config, f)
        temp_path = os.path.realpath(f.name)
    yield temp_path
    try:
        os.unlink(temp_path)
    except FileNotFoundError:
        pass


def test_deploy_config_load_with_role_encode(encode_engine_config_file):
    """Test DeployConfig.load with role=encode."""
    config = DeployConfig.load(encode_engine_config_file, role="encode")
    assert config.engine_type == "vllm"
    assert config.model_config.model_name == "test-model-encode"
    assert config.model_config.encode_parallel_config.dp_size == 2


def test_deploy_config_load_with_health_check(simple_engine_config_file):
    """Test DeployConfig.load includes health_check_config"""
    with open(simple_engine_config_file) as f:
        data = json.load(f)
    data["health_check_config"] = {"npu_usage_threshold": 15, "enable_virtual_inference": False}
    with open(simple_engine_config_file, "w") as f:
        json.dump(data, f)

    config = DeployConfig.load(simple_engine_config_file)
    assert config.health_check_config.npu_usage_threshold == 15
    assert config.health_check_config.enable_virtual_inference is False


def test_deploy_config_get_parallel_config_union(simple_engine_config_file):
    """Test DeployConfig.get_parallel_config for union/prefill role"""
    config = DeployConfig.load(simple_engine_config_file)
    parallel = config.get_parallel_config("union")
    assert parallel == config.model_config.prefill_parallel_config
    assert parallel.dp_size == 2

    parallel_prefill = config.get_parallel_config("prefill")
    assert parallel_prefill == config.model_config.prefill_parallel_config


def test_deploy_config_get_parallel_config_decode(pd_engine_config_file):
    """Test DeployConfig.get_parallel_config for decode role"""
    config = DeployConfig.load(pd_engine_config_file, role="decode")
    parallel = config.get_parallel_config("decode")
    assert parallel == config.model_config.decode_parallel_config


def test_deploy_config_get_parallel_config_encode(simple_engine_config_file):
    """Test DeployConfig.get_parallel_config for encode role"""
    config = DeployConfig.load(simple_engine_config_file, role="encode")
    parallel = config.get_parallel_config("encode")
    assert parallel == config.model_config.encode_parallel_config
    assert parallel.dp_size == 2


def test_deploy_config_get_parallel_config_invalid_role(simple_engine_config_file):
    """Test DeployConfig.get_parallel_config raises for invalid role"""
    config = DeployConfig.load(simple_engine_config_file)
    with pytest.raises(ValueError, match="Unsupported role"):
        config.get_parallel_config("invalid_role")


# --- EndpointConfig tests ---


@pytest.fixture
def valid_config_file_for_endpoint():
    """Create config file that passes FileValidator (real path, non-empty, within size limit)"""
    config = {
        "engine_type": "vllm",
        "model_config": {
            "model_name": "test-model",
            "model_path": "/path/to/model",
            "npu_mem_utils": 0.9,
            PREFILL_PARALLEL_CONFIG_KEY: {"dp_size": 1, "tp_size": 1},
            DECODE_PARALLEL_CONFIG_KEY: {"dp_size": 1, "tp_size": 1},
        },
        "engine_config": {"max_model_len": 2048},
    }
    content = json.dumps(config)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write(content)
        temp_path = os.path.realpath(f.name)
    yield temp_path
    try:
        os.unlink(temp_path)
    except FileNotFoundError:
        pass


def test_endpoint_config_default_values():
    """Test EndpointConfig default values"""
    config = EndpointConfig()
    assert config.engine_type == "vllm"
    assert config.host == "127.0.0.1"
    assert config.role == "union"
    assert config.port == 8000
    assert config.mgmt_port == 9001
    assert config.instance_id == 0
    assert config.dp_rank == 0
    assert config.node_rank == 0
    assert config.config_path is None


def test_endpoint_config_parse_cli_args():
    """Test EndpointConfig.parse_cli_args"""
    args = [
        "prog",
        "--host",
        "192.168.1.1",
        "--role",
        "prefill",
        "--port",
        "8080",
        "--mgmt-port",
        "9080",
        "--config-path",
        "/path/to/config.json",
        "--instance-id",
        "1",
    ]
    with patch("sys.argv", args):
        parsed = EndpointConfig.parse_cli_args()
    assert parsed.host == "192.168.1.1"
    assert parsed.role == "prefill"
    assert parsed.port == 8080
    assert parsed.mgmt_port == 9080
    assert parsed.config_path == "/path/to/config.json"
    assert parsed.instance_id == 1


def test_endpoint_config_parse_node_rank_cli_arg():
    """Test EndpointConfig.parse_cli_args includes --node-rank"""
    args = [
        "prog",
        "--host",
        "192.168.1.1",
        "--role",
        "prefill",
        "--port",
        "8080",
        "--mgmt-port",
        "9080",
        "--config-path",
        "/path/to/config.json",
        "--node-rank",
        "2",
    ]
    with patch("sys.argv", args):
        parsed = EndpointConfig.parse_cli_args()
    assert parsed.node_rank == 2


def test_endpoint_config_node_rank_default():
    """Test EndpointConfig.node_rank defaults to 0 when not specified"""
    args = [
        "prog",
        "--host",
        "192.168.1.1",
        "--role",
        "prefill",
        "--port",
        "8080",
        "--mgmt-port",
        "9080",
        "--config-path",
        "/path/to/config.json",
    ]
    with patch("sys.argv", args):
        parsed = EndpointConfig.parse_cli_args()
    assert parsed.node_rank == 0


def test_endpoint_config_validate_invalid_role():
    """Test EndpointConfig.validate rejects invalid role"""
    config = EndpointConfig(
        host="127.0.0.1",
        role="invalid_role",
        port=8000,
        mgmt_port=9001,
        config_path="/nonexistent",
    )
    with pytest.raises(ValueError, match="role .* is not supported"):
        config.validate()


def test_endpoint_config_validate_invalid_instance_id(valid_config_file_for_endpoint):
    """Test EndpointConfig.validate rejects negative instance_id"""
    config = EndpointConfig(
        host="127.0.0.1",
        role="union",
        port=8000,
        mgmt_port=9001,
        instance_id=-1,
        config_path=valid_config_file_for_endpoint,
    )
    with patch("motor.config.endpoint.FileValidator") as mock_fv:
        mock_validator = MagicMock()
        mock_validator.check_not_soft_link.return_value = mock_validator
        mock_validator.check_file_size.return_value = mock_validator
        mock_validator.check.return_value = mock_validator
        mock_validator.is_valid.return_value = True
        mock_fv.return_value = mock_validator
        with pytest.raises(ValueError, match="instance_id .* illegal"):
            config.validate()


def test_endpoint_config_validate_invalid_host(valid_config_file_for_endpoint):
    """Test EndpointConfig.validate rejects invalid IP"""
    config = EndpointConfig(
        host="256.256.256.256",
        role="union",
        port=8000,
        mgmt_port=9001,
        config_path=valid_config_file_for_endpoint,
    )
    with patch("motor.config.endpoint.FileValidator") as mock_fv:
        mock_validator = MagicMock()
        mock_validator.check_not_soft_link.return_value = mock_validator
        mock_validator.check_file_size.return_value = mock_validator
        mock_validator.check.return_value = mock_validator
        mock_validator.is_valid.return_value = True
        mock_fv.return_value = mock_validator
        with pytest.raises(ValueError, match="parse to ip failed"):
            config.validate()


def test_endpoint_config_validate_invalid_port(valid_config_file_for_endpoint):
    """Test EndpointConfig.validate rejects invalid port"""
    config = EndpointConfig(
        host="127.0.0.1",
        role="union",
        port=80,
        mgmt_port=9001,
        config_path=valid_config_file_for_endpoint,
    )
    with patch("motor.config.endpoint.FileValidator") as mock_fv:
        mock_validator = MagicMock()
        mock_validator.check_not_soft_link.return_value = mock_validator
        mock_validator.check_file_size.return_value = mock_validator
        mock_validator.check.return_value = mock_validator
        mock_validator.is_valid.return_value = True
        mock_fv.return_value = mock_validator
        with pytest.raises(ValueError, match="port must be between"):
            config.validate()


def test_endpoint_config_validate_invalid_dp_rank(valid_config_file_for_endpoint):
    """Test EndpointConfig.validate rejects invalid dp_rank"""
    config = EndpointConfig(
        host="127.0.0.1",
        role="union",
        port=8000,
        mgmt_port=9001,
        dp_rank=70000,
        config_path=valid_config_file_for_endpoint,
    )
    with patch("motor.config.endpoint.FileValidator") as mock_fv:
        mock_validator = MagicMock()
        mock_validator.check_not_soft_link.return_value = mock_validator
        mock_validator.check_file_size.return_value = mock_validator
        mock_validator.check.return_value = mock_validator
        mock_validator.is_valid.return_value = True
        mock_fv.return_value = mock_validator
        with pytest.raises(ValueError, match="is not supported"):
            config.validate()


def test_endpoint_config_validate_config_not_exists():
    """Test EndpointConfig.validate rejects non-existent config file"""
    config = EndpointConfig(
        host="127.0.0.1",
        role="union",
        port=8000,
        mgmt_port=9001,
        config_path="/non/existent/config.json",
    )
    with pytest.raises(ValueError, match="config file .* does not exist"):
        config.validate()


def test_endpoint_config_validate_success(valid_config_file_for_endpoint):
    """Test EndpointConfig.validate passes with valid config"""
    config = EndpointConfig(
        host="127.0.0.1",
        role="union",
        port=8000,
        mgmt_port=9001,
        config_path=valid_config_file_for_endpoint,
    )
    config.validate()


def test_endpoint_config_load_deploy_config(valid_config_file_for_endpoint):
    """Test EndpointConfig.load_deploy_config loads and updates engine_type"""
    config = EndpointConfig(
        host="127.0.0.1",
        role="union",
        port=8000,
        mgmt_port=9001,
        config_path=valid_config_file_for_endpoint,
        deploy_config=None,
    )
    config.deploy_config = DeployConfig.load(valid_config_file_for_endpoint)
    config.load_deploy_config()
    assert config.engine_type == "vllm"
    assert config.deploy_config is not None


def test_endpoint_config_load_deploy_config_updates_kv_port(valid_config_file_for_endpoint):
    """Test load_deploy_config updates kv_port in kv_transfer_config"""
    with open(valid_config_file_for_endpoint) as f:
        data = json.load(f)
    data["engine_config"][constants.KV_TRANSFER_CONFIG] = {
        constants.KV_CONNECTOR: "MooncakeConnector",
        constants.KV_PORT: "20001",
    }
    with open(valid_config_file_for_endpoint, "w") as f:
        json.dump(data, f)

    config = EndpointConfig(
        host="127.0.0.1",
        role="union",
        port=8000,
        mgmt_port=9001,
        config_path=valid_config_file_for_endpoint,
        kv_port=30001,
    )
    config.deploy_config = DeployConfig.load(valid_config_file_for_endpoint)
    config.load_deploy_config()
    kv_config = config.deploy_config.engine_config.get(constants.KV_TRANSFER_CONFIG)
    assert kv_config[constants.KV_PORT] == "30001"


def test_endpoint_config_load_deploy_config_updates_dp_rpc_port_prefill(valid_config_file_for_endpoint):
    """Test load_deploy_config updates dp_rpc_port for prefill role"""
    config = EndpointConfig(
        host="127.0.0.1",
        role="prefill",
        port=8000,
        mgmt_port=9001,
        config_path=valid_config_file_for_endpoint,
        dp_rpc_port=9010,
    )
    config.deploy_config = DeployConfig.load(valid_config_file_for_endpoint)
    config.load_deploy_config()
    assert config.deploy_config.model_config.prefill_parallel_config.dp_rpc_port == 9010


def test_endpoint_config_load_deploy_config_updates_dp_rpc_port_decode(valid_config_file_for_endpoint):
    """Test load_deploy_config updates dp_rpc_port for decode role"""
    pd_config = {
        "motor_deploy_config": {},
        "motor_engine_prefill_config": {
            "engine_type": "vllm",
            "model_config": {
                "model_name": "m",
                "model_path": "/p",
                "npu_mem_utils": 0.9,
                PREFILL_PARALLEL_CONFIG_KEY: {"dp_size": 1},
                DECODE_PARALLEL_CONFIG_KEY: {"dp_size": 1},
            },
            "engine_config": {},
        },
        "motor_engine_decode_config": {
            "engine_type": "vllm",
            "model_config": {
                "model_name": "m",
                "model_path": "/p",
                "npu_mem_utils": 0.9,
                PREFILL_PARALLEL_CONFIG_KEY: {"dp_size": 1},
                DECODE_PARALLEL_CONFIG_KEY: {"dp_size": 1, "dp_rpc_port": 9000},
            },
            "engine_config": {},
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(pd_config, f)
        path = os.path.realpath(f.name)
    try:
        config = EndpointConfig(
            host="127.0.0.1",
            role="decode",
            port=8000,
            mgmt_port=9001,
            config_path=path,
            dp_rpc_port=9020,
        )
        config.deploy_config = DeployConfig.load(path, role="decode")
        config.load_deploy_config()
        assert config.deploy_config.model_config.decode_parallel_config.dp_rpc_port == 9020
    finally:
        os.unlink(path)


def test_endpoint_config_load_deploy_config_updates_dp_rpc_port_encode():
    """Test load_deploy_config updates dp_rpc_port for encode role"""
    encode_config = {
        "motor_deploy_config": {},
        "motor_engine_encode_config": {
            "engine_type": "vllm",
            "model_config": {
                "model_name": "m",
                "model_path": "/p",
                "npu_mem_utils": 0.9,
                "parallel_config": {"dp_size": 1, "dp_rpc_port": 9000},
            },
            "engine_config": {},
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(encode_config, f)
        path = os.path.realpath(f.name)
    try:
        config = EndpointConfig(
            host="127.0.0.1",
            role="encode",
            port=8000,
            mgmt_port=9001,
            config_path=path,
            dp_rpc_port=9020,
        )
        config.deploy_config = DeployConfig.load(path, role="encode")
        config.load_deploy_config()
        assert config.deploy_config.model_config.encode_parallel_config.dp_rpc_port == 9020
    finally:
        os.unlink(path)


def test_endpoint_config_update_engine_config():
    """Test update_engine_config modifies kv-events-config endpoint and replay_endpoint"""
    prefill = ParallelConfig(dp_size=1, tp_size=1)
    decode = ParallelConfig(dp_size=1, tp_size=1)
    encode = ParallelConfig(dp_size=2, tp_size=1)
    model_config = ModelConfig(
        model_name="m",
        model_path="/p",
        npu_mem_utils=0.9,
        encode_parallel_config=encode,
        prefill_parallel_config=prefill,
        decode_parallel_config=decode,
    )
    engine_config = EngineConfig(
        configs={
            "kv-events-config": {
                "endpoint": "127.0.0.1*:10000",
                "replay_endpoint": "127.0.0.1*:10001",
            }
        }
    )
    deploy_config = DeployConfig(
        engine_type="vllm",
        model_config=model_config,
        engine_config=engine_config,
        mgmt_tls_config=None,
        infer_tls_config=None,
    )
    config = EndpointConfig(
        deploy_config=deploy_config,
        dp_rank=2,
    )
    config.update_engine_config()
    kv_events = config.deploy_config.engine_config.get("kv-events-config")
    assert kv_events["endpoint"] == "127.0.0.1*:10002"
    assert kv_events["replay_endpoint"] == "127.0.0.1*:10003"


def test_endpoint_config_update_engine_config_no_kv_events():
    """Test update_engine_config does nothing when kv-events-config is absent"""
    engine_config = EngineConfig(configs={})
    deploy_config = DeployConfig(
        engine_type="vllm",
        model_config=ModelConfig(
            model_name="m",
            model_path="/p",
            npu_mem_utils=0.9,
            encode_parallel_config=ParallelConfig(),
            prefill_parallel_config=ParallelConfig(),
            decode_parallel_config=ParallelConfig(),
        ),
        engine_config=engine_config,
        mgmt_tls_config=None,
        infer_tls_config=None,
    )
    config = EndpointConfig(deploy_config=deploy_config, dp_rank=1)
    config.update_engine_config()
    assert config.deploy_config.engine_config.get("kv-events-config") is None


def test_endpoint_config_update_engine_config_invalid_endpoint_format():
    """Test update_engine_config skips when endpoint format is invalid (no *: separator)"""
    engine_config = EngineConfig(
        configs={
            "kv-events-config": {
                "endpoint": "invalid-format",
                "replay_endpoint": "127.0.0.1*:10001",
            }
        }
    )
    deploy_config = DeployConfig(
        engine_type="vllm",
        model_config=ModelConfig(
            model_name="m",
            model_path="/p",
            npu_mem_utils=0.9,
            encode_parallel_config=ParallelConfig(),
            prefill_parallel_config=ParallelConfig(),
            decode_parallel_config=ParallelConfig(),
        ),
        engine_config=engine_config,
        mgmt_tls_config=None,
        infer_tls_config=None,
    )
    config = EndpointConfig(deploy_config=deploy_config, dp_rank=1)
    config.update_engine_config()
    kv_events = config.deploy_config.engine_config.get("kv-events-config")
    assert kv_events["endpoint"] == "invalid-format"
    assert kv_events["replay_endpoint"] == "127.0.0.1*:10001"
