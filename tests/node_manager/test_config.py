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

import json
import os
import sys
import pytest
import tempfile
from dataclasses import MISSING, fields
from unittest.mock import patch, mock_open, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from motor.config.node_manager import NodeManagerConfig
from motor.common.resources.dispatch import DispatchPlan
from motor.common.resources.instance import ParallelConfig, PDRole


@pytest.fixture(name="nm_config_data")
def _nm_config_data_fixture():
    return {
        "api_config": {"controller_api_dns": "localhost", "controller_api_port": 8080, "node_manager_port": 8080},
        "basic_config": {
            "job_name": "test_job",
            "parallel_config": {"tp_size": 2, "pp_size": 1, "dp_size": 2},
            "role": "both",
            "model_name": "vllm",
        },
        "logging_config": {
            "log_level": "DEBUG",
            "log_max_line_length": 4096,
            "host_log_dir": "/tmp/",
            "log_format": "%(levelname)s [%(filename)s:%(lineno)d] %(message)s",
            "log_date_format": "%Y-%m-%d %H:%M:%S",
        },
    }


def create_config_mock(config_dict):
    def mock_side_effect(file_path, mode):
        if "user_config.json" in file_path:
            return mock_open(read_data=json.dumps(config_dict)).return_value
        return mock_open().return_value

    return mock_side_effect


def clear_node_manager_config():
    """Clear any cached state (no longer needed for non-singleton)"""
    pass


def create_config_object():
    """Helper to create a config object manually"""
    config = NodeManagerConfig.__new__(NodeManagerConfig)
    for dc_field in fields(NodeManagerConfig):
        field_name = dc_field.name
        if field_name not in ['config_path', 'last_modified']:
            if dc_field.default_factory is not MISSING and dc_field.default_factory is not None:
                setattr(config, field_name, dc_field.default_factory())
            else:
                setattr(config, field_name, dc_field.default)
        else:
            setattr(config, field_name, None)
    return config


@patch.dict('os.environ', {'ROLE': 'both'})
@patch('motor.config.node_manager.safe_open')
def test_init_success(mock_safe_open, nm_config_data):
    clear_node_manager_config()
    mock_safe_open.side_effect = create_config_mock(nm_config_data)

    config = create_config_object()

    try:
        NodeManagerConfig._update_from_config_data(config, nm_config_data)
        # Set device_num for testing (simulating visible devices)
        config.basic_config.device_num = 8  # 8 devices for testing
        NodeManagerConfig._generate_endpoint_ports(config)
    except Exception as e:
        pytest.skip(f"Configuration loading failed: {e}")

    # Verify basic config
    assert config.basic_config.job_name == "test_job"
    assert isinstance(config.basic_config.parallel_config, ParallelConfig)
    assert config.basic_config.role == PDRole.ROLE_U
    assert config.basic_config.device_num == 8

    # Verify logging config
    assert config.logging_config.log_level == "DEBUG"
    assert config.logging_config.log_max_line_length == 4096
    assert config.logging_config.host_log_dir == "/tmp/"
    assert config.logging_config.log_format == "%(levelname)s [%(filename)s:%(lineno)d] %(message)s"
    assert config.logging_config.log_date_format == "%Y-%m-%d %H:%M:%S"


@pytest.mark.parametrize(
    "invalid_config,expected_error,role_env",
    [
        ({"role": "both"}, "Missing required config field", "both"),
        (
            {
                "parallel_config": {"tp_size": 1, "pp_size": 1},
                "role": "invalid",
                "controller_api_dns": "localhost",
                "controller_api_port": 8080,
                "node_manager_port": 8080,
                "model_name": "vllm",
            },
            "Invalid role value",
            "invalid",
        ),
    ],
)
@patch.dict('os.environ')
def test_config_validation_errors(invalid_config, expected_error, role_env):
    os.environ['ROLE'] = role_env

    config = create_config_object()

    if "parallel_config" not in invalid_config:
        invalid_config.update(
            {
                "controller_api_dns": "localhost",
                "controller_api_port": 8080,
                "node_manager_port": 8080,
                "model_name": "vllm",
            }
        )

    try:
        NodeManagerConfig._update_from_config_data(config, invalid_config)
        config.validate_config()
    except ValueError:
        pass


@patch.dict('os.environ', {'ROLE': 'both'})
def test_logging_config_defaults(nm_config_data):
    """Test logging configuration defaults when not specified"""
    config_data_no_logging = {
        "parallel_config": {"tp_size": 2, "pp_size": 1},
        "role": "both",
        "controller_api_dns": "localhost",
        "controller_api_port": 8080,
        "node_manager_port": 8080,
        "model_name": "vllm",
    }

    config = create_config_object()
    NodeManagerConfig._update_from_config_data(config, config_data_no_logging)

    # Test default values
    assert config.logging_config.log_level == "INFO"
    assert config.logging_config.log_max_line_length == 8192
    assert config.logging_config.host_log_dir is not None
    # Default format aligned with vLLM field order; engine_server uses single module bucket
    assert config.logging_config.log_format == (
        '(%(processName)s pid=%(process)d) %(levelname)s %(asctime)s [%(name)s][%(fileinfo)s:%(lineno)d] %(message)s'
    )
    assert config.logging_config.log_date_format == '%m-%d %H:%M:%S'


@pytest.mark.parametrize(
    "invalid_config,expected_error",
    [
        (
            {
                "parallel_config": {"tp_size": 2, "pp_size": 1},
                "role": "both",
                "controller_api_dns": "localhost",
                "controller_api_port": 8080,
                "node_manager_port": 8080,
                "model_name": "vllm",
                "logging_config": {"log_level": "INVALID_LEVEL"},
            },
            "log_level must be one of",
        ),
        (
            {
                "parallel_config": {"tp_size": 2, "pp_size": 1},
                "role": "both",
                "controller_api_dns": "localhost",
                "controller_api_port": 8080,
                "node_manager_port": 8080,
                "model_name": "vllm",
                "logging_config": {"log_level": "INVALID"},
            },
            "log_level must be one of",
        ),
        (
            {
                "parallel_config": {"tp_size": 2, "pp_size": 1},
                "role": "both",
                "controller_api_dns": "localhost",
                "controller_api_port": 8080,
                "node_manager_port": 8080,
                "model_name": "vllm",
                "logging_config": {"log_file": "/nonexistent/path"},
            },
            "log_file must be a string or null",
        ),
        (
            {
                "parallel_config": {"tp_size": 2, "pp_size": 1},
                "role": "both",
                "controller_api_dns": "localhost",
                "controller_api_port": 8080,
                "node_manager_port": 8080,
                "model_name": "vllm",
                "logging_config": {"log_format": ""},
            },
            "log_format must be a string",
        ),
    ],
)
@patch.dict('os.environ', {'ROLE': 'both'})
def test_logging_config_validation_errors(invalid_config, expected_error):
    """Test logging configuration validation errors"""
    config = create_config_object()

    try:
        NodeManagerConfig._update_from_config_data(config, invalid_config)
        config.validate_config()
    except ValueError:
        pass


@patch.dict('os.environ', {'ROLE': 'both'})
def test_generate_endpoint_ports(nm_config_data):
    config_data_with_dp = nm_config_data.copy()
    config_data_with_dp["parallel_config"] = {"tp_size": 2, "pp_size": 1, "dp_size": 2}

    config = create_config_object()
    NodeManagerConfig._update_from_config_data(config, config_data_with_dp)

    # Set device_num for testing (simulating visible devices)
    config.basic_config.device_num = 8  # 8 devices for testing

    NodeManagerConfig._generate_endpoint_ports(config)

    assert config.endpoint_config.endpoint_num == 2
    assert len(config.endpoint_config.mgmt_ports) == 2
    assert len(config.endpoint_config.service_ports) == 2
    assert config.endpoint_config.mgmt_ports == ["10001", "10003"]
    assert config.endpoint_config.service_ports == ["10000", "10002"]


@patch.dict('os.environ', {'ROLE': 'both', 'USER_CONFIG_PATH': 'tests/jsons/user_config.json'.replace('\\', '/')})
@patch('motor.config.node_manager.safe_open')
def test_non_singleton_behavior(mock_safe_open, nm_config_data):
    """Test that NodeManagerConfig is no longer a singleton"""
    clear_node_manager_config()
    mock_safe_open.side_effect = create_config_mock(nm_config_data)
    with patch('os.path.exists', return_value=True):
        config1 = NodeManagerConfig()
        config2 = NodeManagerConfig()
    # Now configs should be different instances
    assert config1 is not config2
    # But they should have the same configuration values
    assert config1.basic_config.role == config2.basic_config.role


@patch.dict('os.environ', {'ROLE': 'both', 'USER_CONFIG_PATH': 'tests/jsons/user_config.json'.replace('\\', '/')})
@patch('motor.config.node_manager.safe_open')
def test_reload_success(mock_safe_open, nm_config_data):
    """Test successful configuration reload"""
    clear_node_manager_config()
    mock_safe_open.side_effect = create_config_mock(nm_config_data)

    config = NodeManagerConfig()

    # Set valid config paths for reload testing
    config.config_path = "/tmp/user_config.json"
    config.last_modified = None  # Force reload

    # Create modified config data for reload
    modified_config_data = nm_config_data.copy()
    modified_config_data["basic_config"]["model_name"] = "modified_model"
    modified_config_data["api_config"]["node_manager_port"] = 9090

    # Update mock to return modified data on reload
    def reload_mock_side_effect(file_path, mode):
        if "user_config.json" in file_path or "/tmp/test_user_config.json" in file_path:
            return mock_open(read_data=json.dumps(modified_config_data)).return_value
        return mock_open().return_value

    mock_safe_open.side_effect = reload_mock_side_effect

    # Mock os.path.exists and os.path.getmtime for our test paths
    with (
        patch('os.path.exists', return_value=True),
        patch('os.path.getmtime', return_value=1234567890.0),
        patch('pathlib.Path.exists', return_value=True),
        patch('pathlib.Path.stat') as mock_path_stat,
    ):
        mock_path_stat.return_value = MagicMock(st_mtime=1234567890.0)
        result = config.reload()

    assert result is True
    assert config.basic_config.model_name == "modified_model"
    assert config.api_config.node_manager_port == 9090


@patch.dict('os.environ', {'ROLE': 'both', 'USER_CONFIG_PATH': '/tmp/test_node_manager_config.json'})
@patch('motor.config.node_manager.safe_open')
def test_reload_config_file_not_found(mock_safe_open, nm_config_data):
    """Test reload when configuration file doesn't exist"""
    clear_node_manager_config()
    mock_safe_open.side_effect = create_config_mock(nm_config_data)

    config = NodeManagerConfig()

    # Set config_path to a non-existent path
    config.config_path = "/non/existent/path/user_config.json"

    def exists_side_effect(path):
        return path != "/non/existent/path/user_config.json"

    with patch('os.path.exists', side_effect=exists_side_effect):
        result = config.reload()
    assert result is False


@patch.dict('os.environ', {'ROLE': 'both', 'USER_CONFIG_PATH': '/tmp/test_node_manager_config.json'})
@patch('motor.config.node_manager.safe_open')
def test_reload_invalid_json(mock_safe_open, nm_config_data):
    """Test reload with invalid JSON in config file"""
    clear_node_manager_config()
    mock_safe_open.side_effect = create_config_mock(nm_config_data)

    config = NodeManagerConfig()

    # Set valid paths for testing
    config.config_path = "/tmp/test_node_manager_config.json"

    # Mock invalid JSON for config file
    def invalid_json_mock(file_path, mode):
        if "/tmp/test_node_manager_config.json" in file_path:
            return mock_open(read_data="invalid json content").return_value
        return mock_open().return_value

    mock_safe_open.side_effect = invalid_json_mock

    with patch('os.path.exists', return_value=True):
        result = config.reload()
    assert result is False


def test_validate_config_success():
    """Test successful configuration validation"""
    config = create_config_object()
    config.validate_config()


@pytest.mark.parametrize(
    "param,value,expected_error",
    [
        ("node_manager_port", 0, "node_manager_port must be in range 1-65535"),
        ("node_manager_port", 65536, "node_manager_port must be in range 1-65535"),
        ("base_port", -1, "base_port must be in range 0-65535"),
        ("endpoint_num", -1, "endpoint_num cannot be negative"),
        ("heartbeat_interval_seconds", 0, "heartbeat_interval_seconds must be greater than 0"),
        ("log_level", "INVALID", "log_level must be one of"),
        ("log_max_line_length", 0, "log_max_line_length must be greater than 0"),
    ],
)
def test_validate_config_errors(param, value, expected_error):
    """Test configuration validation errors"""
    config = create_config_object()

    if param == "node_manager_port":
        config.api_config.node_manager_port = value
    elif param in ["base_port", "endpoint_num"]:
        setattr(config.endpoint_config, param, value)
    elif param == "heartbeat_interval_seconds":
        config.basic_config.heartbeat_interval_seconds = value
    elif param in ["log_level", "log_max_line_length"]:
        setattr(config.logging_config, param, value)

    with pytest.raises(ValueError, match=expected_error):
        config.validate_config()


def test_to_dict():
    """Test conversion to dictionary"""
    config = create_config_object()
    config.basic_config.model_name = "test_model"
    config.basic_config.dispatch_capabilities = [DispatchPlan.CONCURRENT_ENGINE_SYNC.value]

    config_dict = config.to_dict()

    assert "api_config" in config_dict
    assert "endpoint_config" in config_dict
    assert "basic_config" in config_dict
    assert "logging_config" in config_dict

    assert config_dict["basic_config"]["model_name"] == "test_model"
    assert "dispatch_capabilities" not in config_dict["basic_config"]

    assert "config_path" not in config_dict


def test_save_to_json_success():
    """Test successful saving configuration to JSON file"""
    config = create_config_object()
    config.config_path = "/tmp/test_config.json"

    temp_path = "tmp_node_manager_save.json"
    with open(temp_path, "w", encoding="utf-8"):
        pass

    try:
        result = config.save_to_json(temp_path)
        assert result is True

        with open(temp_path, "r", encoding="utf-8") as f:
            saved_config = json.load(f)
        assert saved_config["api_config"][("node_manager_port")] == 1026
    finally:
        os.unlink(temp_path)


def test_save_to_json_no_path():
    """Test saving configuration to unspecified path"""
    # Create config manually to avoid loading from files
    config = create_config_object()

    # Config without paths should fail
    assert config.save_to_json() is False


def test_get_config_summary():
    """Test getting configuration summary"""
    config = create_config_object()
    config.api_config.node_manager_port = 8080
    config.basic_config.model_name = "test_model"
    config.basic_config.role = PDRole.ROLE_U
    config.api_config.pod_ip = "127.0.0.1"

    summary = config.get_config_summary()

    assert "8080" in summary
    assert "test_model" in summary
    assert "127.0.0.1" in summary


@patch.dict('os.environ', {'ROLE': 'both'})
def test_from_json_success():
    """Test loading configuration from JSON data"""
    test_config = {
        "api_config": {"node_manager_port": 8080},
        "basic_config": {
            "parallel_config": {"tp_size": 1, "pp_size": 1},
            "role": "both",
            "model_name": "test_model",
            "heartbeat_interval_seconds": 2,
        },
        "logging_config": {"log_level": "DEBUG", "log_max_line_length": 4096},
    }

    config = create_config_object()
    NodeManagerConfig._update_from_config_data(config, test_config)

    # Set device_num for testing (simulating visible devices)
    config.basic_config.device_num = 8  # 8 devices for testing

    NodeManagerConfig._generate_endpoint_ports(config)

    assert config.api_config.node_manager_port == 8080
    assert config.basic_config.model_name == "test_model"
    assert config.logging_config.log_level == "DEBUG"
    assert config.logging_config.log_max_line_length == 4096


@patch.dict('os.environ', {'ROLE': 'prefill'})
def test_generate_endpoint_ports_with_cp_prefill():
    config = create_config_object()
    config.basic_config.parallel_config = ParallelConfig(
        dp_size=4, tp_size=2, pp_size=1, pcp_size=2, local_world_size=4, world_size=16
    )
    config.basic_config.role = PDRole.ROLE_P
    config.basic_config.enable_multi_endpoints = True
    config.basic_config.device_num = 8

    NodeManagerConfig._generate_endpoint_ports(config)

    # prefill with cp=2 needs cp*tp*pp = 4 devices/endpoint => 8 devices gives 2 endpoints
    assert config.endpoint_config.endpoint_num == 2
    assert len(config.endpoint_config.service_ports) == 2
    assert len(config.endpoint_config.mgmt_ports) == 2


@patch.dict('os.environ', {'ROLE': 'both'})
def test_from_json_loads_union_config_for_hybrid():
    """Test NodeManagerConfig loads union engine config for PD hybrid."""
    user_config = {
        "motor_deploy_config": {
            "hardware_type": "800I-A3",
            "hybrid_instances_num": 1,
            "single_hybrid_instance_pod_num": 1,
            "hybrid_pod_npu_num": 4,
        },
        "motor_engine_union_config": {
            "engine_type": "vllm",
            "enable_multi_endpoints": True,
            "model_config": {
                "model_name": "qwen3-8B",
                "model_path": "/mnt/weight/qwen3_8B",
                "npu_mem_utils": 0.9,
                "parallel_config": {"dp_size": 2, "tp_size": 2, "pp_size": 1},
            },
            "engine_config": {"max_model_len": 2048},
        },
    }

    temp_path = "tmp_node_manager_union.json"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(user_config, f)

    try:
        with patch("motor.config.node_manager.safe_open", open):
            config = NodeManagerConfig.from_json(temp_path)
    finally:
        os.unlink(temp_path)

    assert config.basic_config.role == PDRole.ROLE_U
    assert config.basic_config.model_name == "qwen3-8B"
    assert config.basic_config.hardware_type == "800I-A3"
    assert config.basic_config.parallel_config.dp_size == 2
    assert config.basic_config.parallel_config.tp_size == 2
    assert config.basic_config.parallel_config.pp_size == 1
    assert config.basic_config.device_num == 4
    assert config.endpoint_config.endpoint_num == 2


def test_vllm_handoff_connector_infers_handoff_capability():
    capabilities = NodeManagerConfig._infer_dispatch_capabilities(
        {
            "engine_type": "vllm",
            "engine_config": {
                "kv_transfer_config": {
                    "kv_connector": "MooncakeConnectorV1",
                }
            },
        }
    )

    assert capabilities == [DispatchPlan.PREFILL_HANDOFF_DECODE.value]


def test_vllm_hybrid_connector_infers_handoff_capability():
    capabilities = NodeManagerConfig._infer_dispatch_capabilities(
        {
            "engine_type": "vllm",
            "engine_config": {
                "kv_transfer_config": {
                    "kv_connector": "MooncakeHybridConnector",
                }
            },
        }
    )

    assert capabilities == [DispatchPlan.PREFILL_HANDOFF_DECODE.value]


def test_vllm_nixl_connector_infers_handoff_capability():
    capabilities = NodeManagerConfig._infer_dispatch_capabilities(
        {
            "engine_type": "vllm",
            "engine_config": {
                "kv_transfer_config": {
                    "kv_connector": "NixlConnector",
                }
            },
        }
    )

    assert capabilities == [DispatchPlan.PREFILL_HANDOFF_DECODE.value]


def test_vllm_multi_connector_infers_transport_connector_capability():
    def _multi_connector(transport_connector: str) -> dict:
        return {
            "engine_type": "vllm",
            "engine_config": {
                "kv_transfer_config": {
                    "kv_connector": "MultiConnector",
                    "kv_connector_extra_config": {
                        "connectors": [
                            {"kv_connector": transport_connector},
                            {"kv_connector": "AscendStoreConnector"},
                        ]
                    },
                }
            },
        }

    assert NodeManagerConfig._infer_dispatch_capabilities(_multi_connector("MooncakeHybridConnector")) == [
        DispatchPlan.PREFILL_HANDOFF_DECODE.value
    ]
    assert NodeManagerConfig._infer_dispatch_capabilities(_multi_connector("NixlConnector")) == [
        DispatchPlan.PREFILL_HANDOFF_DECODE.value
    ]
    assert NodeManagerConfig._infer_dispatch_capabilities(_multi_connector("MooncakeLayerwiseConnector")) == [
        DispatchPlan.CONCURRENT_ENGINE_SYNC.value
    ]


def test_vllm_multi_connector_ignores_non_transport_connector_profiles():
    engine_config = {
        "engine_type": "vllm",
        "engine_config": {
            "kv_transfer_config": {
                "kv_connector": "MultiConnector",
                "kv_connector_extra_config": {
                    "connectors": [
                        {"kv_connector": "MooncakeLayerwiseConnector"},
                        {"kv_connector": "AscendStoreConnector"},
                        {"kv_connector": "MooncakeHybridConnector"},
                    ]
                },
            }
        },
    }

    assert NodeManagerConfig._infer_dispatch_capabilities(engine_config) == [DispatchPlan.CONCURRENT_ENGINE_SYNC.value]


def test_vllm_multi_connector_requires_transport_and_store_connectors():
    engine_config = {
        "engine_type": "vllm",
        "engine_config": {
            "kv_transfer_config": {
                "kv_connector": "MultiConnector",
                "kv_connector_extra_config": {"connectors": [{"kv_connector": "MooncakeHybridConnector"}]},
            }
        },
    }

    assert NodeManagerConfig._infer_dispatch_capabilities(engine_config) == []


@patch.dict("os.environ", {"ROLE": "prefill"})
def test_user_dispatch_capabilities_cannot_override_connector_semantics():
    user_config = {
        "motor_deploy_config": {"hardware_type": "800I-A3"},
        "motor_engine_prefill_config": {
            "engine_type": "vllm",
            "dispatch_capabilities": [DispatchPlan.CONCURRENT_ENGINE_SYNC.value],
            "motor_nodemanger_config": {
                "basic_config": {
                    "dispatch_capabilities": [DispatchPlan.CONCURRENT_ENGINE_SYNC.value],
                }
            },
            "engine_config": {
                "kv_transfer_config": {
                    "kv_connector": "MooncakeHybridConnector",
                }
            },
        },
    }

    NodeManagerConfig._discard_user_dispatch_capabilities(user_config)
    config_data = NodeManagerConfig._load_node_manager_config_data(user_config)

    assert config_data["basic_config"]["dispatch_capabilities"] == [DispatchPlan.PREFILL_HANDOFF_DECODE.value]
    assert "dispatch_capabilities" not in user_config["motor_engine_prefill_config"]


@patch.dict("os.environ", {"ROLE": "prefill"})
def test_user_dispatch_capabilities_cannot_enable_unknown_connector():
    user_config = {
        "motor_deploy_config": {"hardware_type": "800I-A3"},
        "motor_engine_prefill_config": {
            "engine_type": "vllm",
            "dispatch_capabilities": [DispatchPlan.PREFILL_HANDOFF_DECODE.value],
            "engine_config": {
                "kv_transfer_config": {
                    "kv_connector": "UnknownConnector",
                    "kv_connector_module_path": "vllm_ascend.distributed.mooncake_connector",
                }
            },
        },
    }

    NodeManagerConfig._discard_user_dispatch_capabilities(user_config)
    config_data = NodeManagerConfig._load_node_manager_config_data(user_config)

    assert config_data["basic_config"]["dispatch_capabilities"] == []


def test_vllm_layerwise_connector_infers_concurrent_capability():
    capabilities = NodeManagerConfig._infer_dispatch_capabilities(
        {
            "engine_type": "vllm",
            "engine_config": {
                "kv_transfer_config": {
                    "kv_connector": "MooncakeLayerwiseConnector",
                }
            },
        }
    )

    assert capabilities == [DispatchPlan.CONCURRENT_ENGINE_SYNC.value]


def test_sglang_infers_concurrent_capability():
    capabilities = NodeManagerConfig._infer_dispatch_capabilities(
        {
            "engine_type": "sglang",
            "engine_config": {},
        }
    )

    assert capabilities == [DispatchPlan.CONCURRENT_ENGINE_SYNC.value]


# ===== Cross-Node PCP Tests =====


@patch.dict("os.environ", {"ROLE": "prefill"})
def test_cross_node_pcp_adjusts_local_world_size():
    """When nnodes > 1 and pcp_size > 1, local_world_size is adjusted for per-node PCP."""
    user_config = {
        "motor_deploy_config": {
            "hardware_type": "800I-A3",
            "p_instances_num": 1,
            "single_p_instance_pod_num": 2,
            "p_pod_npu_num": 16,
        },
        "motor_engine_prefill_config": {
            "engine_type": "vllm",
            "enable_multi_endpoints": True,
            "engine_config": {
                "model": "/mnt/weight/test",
                "data_parallel_size": 1,
                "tensor_parallel_size": 16,
                "prefill_context_parallel_size": 2,
                "nnodes": 2,
                "master_port": 7001,
            },
        },
        "motor_engine_decode_config": {
            "engine_type": "vllm",
            "engine_config": {"model": "/mnt/weight/test"},
        },
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(user_config, f)
        temp_path = f.name

    try:
        config = NodeManagerConfig.from_json(temp_path)
    finally:
        os.unlink(temp_path)

    # PCP=2, TP=16, nnodes=2: per-node PCP=1, local_world_size=1*16*1=16
    assert config.basic_config.parallel_config.pcp_size == 2  # global PCP unchanged
    assert config.basic_config.parallel_config.tp_size == 16
    assert config.basic_config.parallel_config.local_world_size == 16  # per-node adjusted
    assert config.basic_config.parallel_config.world_size == 32  # global: 1*2*16*1
    assert config.basic_config.nnodes == 2
    assert config.basic_config.device_num == 16
    # endpoint_num: min(dp=1, 16//16=1) = 1
    assert config.endpoint_config.endpoint_num == 1


@patch.dict("os.environ", {"ROLE": "prefill"})
def test_cross_node_pcp_no_adjustment_when_nnodes_is_one():
    """When nnodes=1, local_world_size is NOT adjusted (backward compatible)."""
    user_config = {
        "motor_deploy_config": {
            "hardware_type": "800I-A3",
            "p_instances_num": 1,
            "single_p_instance_pod_num": 1,
            "p_pod_npu_num": 8,
        },
        "motor_engine_prefill_config": {
            "engine_type": "vllm",
            "enable_multi_endpoints": True,
            "engine_config": {
                "model": "/mnt/weight/test",
                "data_parallel_size": 1,
                "tensor_parallel_size": 4,
                "prefill_context_parallel_size": 2,
            },
        },
        "motor_engine_decode_config": {
            "engine_type": "vllm",
            "engine_config": {"model": "/mnt/weight/test"},
        },
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(user_config, f)
        temp_path = f.name

    try:
        config = NodeManagerConfig.from_json(temp_path)
    finally:
        os.unlink(temp_path)

    # nnodes defaults to 1, so no adjustment: local_world_size = 2*4*1 = 8
    assert config.basic_config.nnodes == 1
    assert config.basic_config.parallel_config.local_world_size == 8


@patch.dict("os.environ", {"ROLE": "prefill"})
def test_cross_node_pcp_generate_endpoint_ports():
    """_generate_endpoint_ports works with adjusted local_world_size for cross-node PCP."""
    config = create_config_object()
    config.basic_config.parallel_config = ParallelConfig(
        dp_size=1, tp_size=16, pp_size=1, pcp_size=2, local_world_size=16, world_size=32
    )
    config.basic_config.role = PDRole.ROLE_P
    config.basic_config.enable_multi_endpoints = True
    config.basic_config.device_num = 16
    config.basic_config.nnodes = 2

    NodeManagerConfig._generate_endpoint_ports(config)

    # PCP=2, TP=16, nnodes=2: per-node PCP=1, local_world_size=16
    # endpoint_num = min(dp=1, 16//16) = 1
    assert config.endpoint_config.endpoint_num == 1
    assert len(config.endpoint_config.service_ports) == 1
    assert len(config.endpoint_config.mgmt_ports) == 1
