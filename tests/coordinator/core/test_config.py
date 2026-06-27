#!/usr/bin/env python3
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
import tempfile
import time

import pytest

from motor.config.coordinator import CoordinatorConfig


@pytest.fixture
def _temp_json_file():
    """Fixture for temporary JSON file that gets cleaned up."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.coordinator.json', delete=False) as f:
        _fpath = f.name

    yield _fpath

    try:
        os.remove(_fpath)
    except FileNotFoundError:
        pass


@pytest.fixture
def sample_config_data():
    """Sample configuration data for testing"""
    return {
        "logging_config": {"log_level": "DEBUG", "log_max_line_length": 4096},
        "exception_config": {"max_retry": 10},
        "scheduler_config": {"deploy_mode": "single_node"},
        "api_key_config": {"enable_api_key": True},
    }


# Complete configuration template for testing
COMPLETE_CONFIG = {
    "logging_config": {
        "log_level": "DEBUG",
        "log_max_line_length": 4096,
        "log_file": "/tmp/test.log",
        "log_format": "%(levelname)s [%(filename)s:%(lineno)d] %(message)s",
        "log_date_format": "%Y-%m-%d %H:%M:%S",
    },
    "prometheus_metrics_config": {"reuse_time": 3},
    "exception_config": {
        "max_retry": 5,
        "retry_delay": 0.2,
        "first_token_timeout": 600,
        "infer_timeout": 3600,
    },
    "tls_config": {},
    "scheduler_config": {"deploy_mode": "single_node", "scheduler_type": "load_balance"},
    "timeout_config": {
        "request_timeout": 30,
        "connection_timeout": 10,
        "read_timeout": 15,
        "write_timeout": 15,
        "keep_alive_timeout": 60,
    },
    "api_key_config": {
        "enable_api_key": True,
        "valid_keys": ["key1", "key2"],
        "header_name": "X-API-Key",
        "key_prefix": "Bearer ",
        "skip_paths": ["/liveness", "/metrics"],
    },
    "rate_limit_config": {
        "enable_rate_limit": True,
        "max_requests": 100,
        "window_size": 60,
        "scope": "global",
        "skip_paths": ["/liveness"],
        "error_message": "Rate limit exceeded",
        "error_status_code": 429,
    },
    "standby_config": {
        "enable_master_standby": True,
        "master_standby_check_interval": 5,
        "master_lock_ttl": 60,
        "master_lock_retry_interval": 5,
        "master_lock_max_failures": 3,
        "master_lock_key": "/master/lock",
    },
    "etcd_config": {"etcd_host": "localhost", "etcd_port": 2379, "etcd_timeout": 5, "enable_etcd_persistence": True},
    "api_config": {
        "coordinator_api_host": "127.0.0.1",
        "coordinator_api_infer_port": 1026,
        "coordinator_api_mgmt_port": 1025,
    },
}


def test_default_config_initialization():
    """Test default configuration initialization"""
    config = CoordinatorConfig()

    # Verify default values
    assert config.logging_config.log_level == "INFO"
    assert config.logging_config.log_max_line_length == 8192
    assert config.prometheus_metrics_config.reuse_time == 3
    assert config.exception_config.max_retry == 5
    assert config.exception_config.reschedule_enabled is True
    assert config.exception_config.recompute_enabled is True
    assert config.exception_config.first_token_timeout == 600
    assert not hasattr(config.scheduler_config, "deploy_mode")
    assert config.scheduler_config.scheduler_type.value == "load_balance"
    assert config.timeout_config.request_timeout == 30
    assert config.api_key_config.enable_api_key is False
    assert config.rate_limit_config.enable_rate_limit is False
    assert config.api_config.coordinator_api_infer_port == 1025
    assert config.api_config.coordinator_api_mgmt_port == 1026


def test_from_json_success(_temp_json_file):
    """Test loading configuration from valid JSON file"""
    test_config = {
        "logging_config": {"log_level": "DEBUG", "log_max_line_length": 4096},
        "exception_config": {"max_retry": 10},
        "scheduler_config": {"deploy_mode": "single_node"},
        "api_key_config": {
            "enable_api_key": True,
            "valid_keys": ["test-key"],
            "header_name": "X-API-Key",
            "key_prefix": "Bearer ",
        },
        "rate_limit_config": {
            "enable_rate_limit": True,
            "max_requests": 100,
            "window_size": 60,
            "error_status_code": 429,
        },
    }

    with open(_temp_json_file, 'w', encoding="utf-8") as f:
        json.dump(test_config, f)

    config = CoordinatorConfig.from_json(_temp_json_file)
    assert config.logging_config.log_level == "DEBUG"
    assert config.logging_config.log_max_line_length == 4096
    assert config.exception_config.max_retry == 10
    assert not hasattr(config.scheduler_config, "deploy_mode")
    assert config.api_key_config.enable_api_key is True
    assert config.rate_limit_config.enable_rate_limit is True
    assert config.config_path == _temp_json_file


def test_from_json_migrates_deprecated_recompute_config(_temp_json_file, caplog):
    test_config = {
        "exception_config": {
            "recompute_enabled": False,
            "recompute_max_retry": 9,
        }
    }
    with open(_temp_json_file, 'w', encoding="utf-8") as f:
        json.dump(test_config, f)

    config = CoordinatorConfig.from_json(_temp_json_file)

    assert config.exception_config.reschedule_enabled is False
    assert not hasattr(config.exception_config, "recompute_max_retry")
    assert "recompute_enabled is deprecated" in caplog.text
    assert "recompute_max_retry is no longer supported" in caplog.text


def test_new_reschedule_config_takes_precedence_over_deprecated_alias(_temp_json_file):
    test_config = {
        "exception_config": {
            "recompute_enabled": False,
            "reschedule_enabled": True,
        }
    }
    with open(_temp_json_file, 'w', encoding="utf-8") as f:
        json.dump(test_config, f)

    config = CoordinatorConfig.from_json(_temp_json_file)

    assert config.exception_config.reschedule_enabled is True


def test_from_json_maps_hybrid_instances(_temp_json_file):
    """Test PD hybrid deploy config maps hybrid instances for runtime compatibility"""
    user_config = {
        "motor_deploy_config": {
            "hybrid_instances_num": 3,
            "single_hybrid_instance_pod_num": 1,
            "hybrid_pod_npu_num": 4,
        },
        "motor_coordinator_config": {
            "scheduler_config": {
                "deploy_mode": "single_node",
            }
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
    with open(_temp_json_file, 'w', encoding="utf-8") as f:
        json.dump(user_config, f)

    config = CoordinatorConfig.from_json(_temp_json_file)

    assert not hasattr(config.scheduler_config, "deploy_mode")
    assert config.deploy_config.hybrid_instances_num == 3
    assert config.deploy_config.single_hybrid_instance_pod_num == 1
    assert config.deploy_config.hybrid_pod_npu_num == 4
    assert config.deploy_config.p_instances_num == 3
    assert config.deploy_config.d_instances_num == 3


def test_from_json_with_invalid_json(_temp_json_file):
    """Test loading configuration from invalid JSON file"""
    with open(_temp_json_file, 'w', encoding="utf-8") as f:
        f.write("invalid json content")

    # Should use default configuration instead of raising exception
    config = CoordinatorConfig.from_json(_temp_json_file)
    assert config is not None
    assert config.api_config.coordinator_api_infer_port == 1025  # default value


def test_from_json_file_not_found():
    """Test loading configuration from non-existent file"""
    # Should use default configuration instead of raising exception
    config = CoordinatorConfig.from_json("/non/existent/file.json")
    assert config is not None
    assert config.api_config.coordinator_api_infer_port == 1025  # default value


def test_from_json_loads_token_sampling_config_top_level(_temp_json_file):
    """``token_sampling_config`` merges from flat coordinator JSON."""
    test_config = {
        "token_sampling_config": {
            "precision_check_enabled": True,
            "interval_seconds": 45.5,
            "logprobs_count": 3,
        }
    }
    with open(_temp_json_file, "w", encoding="utf-8") as f:
        json.dump(test_config, f)

    config = CoordinatorConfig.from_json(_temp_json_file)
    assert config.token_sampling_config.precision_check_enabled is True
    assert config.token_sampling_config.interval_seconds == 45.5
    assert config.token_sampling_config.logprobs_count == 3


def test_from_json_loads_token_sampling_config_motor_coordinator_wrapper(_temp_json_file):
    """``token_sampling_config`` loads from ``motor_coordinator_config`` user config shape."""
    wrapped = {
        "motor_coordinator_config": {
            "token_sampling_config": {
                "precision_check_enabled": True,
                "interval_seconds": 60.0,
                "logprobs_count": 2,
            }
        }
    }
    with open(_temp_json_file, "w", encoding="utf-8") as f:
        json.dump(wrapped, f)

    config = CoordinatorConfig.from_json(_temp_json_file)
    assert config.token_sampling_config.precision_check_enabled is True
    assert config.token_sampling_config.interval_seconds == 60.0
    assert config.token_sampling_config.logprobs_count == 2


def test_token_sampling_config_validation_non_positive_interval():
    with pytest.raises(ValueError, match="token_sampling_config.interval_seconds"):
        c = CoordinatorConfig()
        c.token_sampling_config.interval_seconds = 0
        c.validate_config()


def test_token_sampling_config_validation_non_positive_logprobs():
    with pytest.raises(ValueError, match="token_sampling_config.logprobs_count"):
        c = CoordinatorConfig()
        c.token_sampling_config.logprobs_count = 0
        c.validate_config()


def test_token_sampling_config_validation_non_positive_precision_threshold():
    with pytest.raises(ValueError, match="token_sampling_config.precision_issue_threshold"):
        c = CoordinatorConfig()
        c.token_sampling_config.precision_issue_threshold = 0
        c.validate_config()


def test_token_sampling_config_validation_non_positive_probe_attempts():
    with pytest.raises(ValueError, match="token_sampling_config.probe_max_attempts"):
        c = CoordinatorConfig()
        c.token_sampling_config.probe_max_attempts = 0
        c.validate_config()


def test_token_sampling_config_validation_non_positive_probe_timeout():
    with pytest.raises(ValueError, match="token_sampling_config.probe_timeout_seconds"):
        c = CoordinatorConfig()
        c.token_sampling_config.probe_timeout_seconds = 0
        c.validate_config()


def test_config_validation_success():
    """Test successful configuration validation"""
    config = CoordinatorConfig()
    # Should not raise any exception
    config.validate_config()


@pytest.mark.parametrize(
    "param,value,expected_error",
    [
        ("log_max_line_length", -1, "log_max_line_length must be greater than 0"),
        ("max_retry", -1, "max_retry cannot be negative"),
        ("retry_delay", -0.1, "retry_delay must be greater than 0"),
        ("first_token_timeout", -1, "first_token_timeout must be greater than 0"),
        ("infer_timeout", 0, "infer_timeout must be greater than 0"),
        ("request_timeout", -1, "request_timeout must be greater than 0"),
        ("connection_timeout", 0, "connection_timeout must be greater than 0"),
        ("read_timeout", -1, "read_timeout must be greater than 0"),
        ("write_timeout", 0, "write_timeout must be greater than 0"),
        ("keep_alive_timeout", -1, "keep_alive_timeout must be greater than 0"),
        ("coordinator_api_infer_port", 0, "coordinator_api_infer_port must be in range 1-65535"),
        ("coordinator_api_mgmt_port", 65536, "coordinator_api_mgmt_port must be in range 1-65535"),
        ("max_requests", -1, "max_requests must be greater than 0"),
        ("window_size", 0, "window_size must be greater than 0"),
        ("error_status_code", 99, "error_status_code must be in range 100-599"),
        ("error_status_code", 600, "error_status_code must be in range 100-599"),
        ("reuse_time", 0, "reuse_time must be greater than 0"),
        ("master_standby_check_interval", -1, "master_standby_check_interval must be greater than 0"),
        ("etcd_port", 0, "etcd_port must be in range 1-65535"),
        ("etcd_timeout", 0, "etcd_timeout must be greater than 0"),
    ],
)
def test_config_validation_errors(param, value, expected_error):
    """Test various configuration validation errors"""
    with pytest.raises(ValueError, match=expected_error):
        config = CoordinatorConfig()
        if param in ["log_max_line_length"]:
            setattr(config.logging_config, param, value)
        elif param in ["max_retry", "retry_delay", "first_token_timeout", "infer_timeout"]:
            setattr(config.exception_config, param, value)
        elif param in ["request_timeout", "connection_timeout", "read_timeout", "write_timeout", "keep_alive_timeout"]:
            setattr(config.timeout_config, param, value)
        elif param in ["coordinator_api_infer_port", "coordinator_api_mgmt_port"]:
            setattr(config.api_config, param, value)
        elif param in ["max_requests", "window_size", "error_status_code"]:
            setattr(config.rate_limit_config, param, value)
        elif param in ["reuse_time"]:
            setattr(config.prometheus_metrics_config, param, value)
        elif param in ["master_standby_check_interval"]:
            setattr(config.standby_config, param, value)
        elif param in ["etcd_port", "etcd_timeout"]:
            setattr(config.etcd_config, param, value)
        config.validate_config()


def test_config_validation_multiple_errors():
    """Test multiple configuration errors"""
    with pytest.raises(ValueError) as exc_info:
        config = CoordinatorConfig()
        config.exception_config.max_retry = -1
        config.rate_limit_config.max_requests = -1
        config.validate_config()
    error_msg = str(exc_info.value)
    assert "max_retry cannot be negative" in error_msg
    assert "max_requests must be greater than 0" in error_msg


def test_to_dict():
    """Test configuration serialization to dict"""
    config = CoordinatorConfig()
    config_dict = config.to_dict()

    # Check that all config sections are present
    expected_keys = [
        'logging_config',
        'prometheus_metrics_config',
        'exception_config',
        'scheduler_config',
        'inference_workers_config',
        'infer_tls_config',
        'mgmt_tls_config',
        'etcd_tls_config',
        'timeout_config',
        'api_key_config',
        'rate_limit_config',
        'standby_config',
        'etcd_config',
        'aigw_model',
        'api_config',
    ]

    for key in expected_keys:
        assert key in config_dict

    # Check that internal fields are not present
    assert 'config_path' not in config_dict
    assert 'last_modified' not in config_dict

    # Check enum serialization
    assert 'deploy_mode' not in config_dict['scheduler_config']
    assert config_dict['scheduler_config']['scheduler_type'] == 'load_balance'
    assert config_dict['exception_config']['reschedule_enabled'] is True
    assert 'recompute_enabled' not in config_dict['exception_config']
    assert 'recompute_max_retry' not in config_dict['exception_config']


def test_save_to_json(_temp_json_file):
    """Test saving configuration to JSON file"""
    config = CoordinatorConfig()
    config.logging_config.log_level = "DEBUG"
    config.exception_config.max_retry = 10

    success = config.save_to_json(_temp_json_file)
    assert success is True

    # Verify saved content
    with open(_temp_json_file, 'r', encoding="utf-8") as f:
        saved_data = json.load(f)

    assert saved_data['logging_config']['log_level'] == 'DEBUG'
    assert saved_data['exception_config']['max_retry'] == 10
    assert 'deploy_mode' not in saved_data['scheduler_config']


def test_save_to_json_invalid_path():
    """Test saving configuration to invalid path"""
    config = CoordinatorConfig()
    success = config.save_to_json("/invalid/path/config.json")
    assert success is False


def test_config_summary():
    """Test configuration summary generation."""
    config = CoordinatorConfig()
    summary = config.get_config_summary()

    assert "Coordinator Configuration Summary" in summary
    assert "Log Level" in summary
    assert "Log Max Line Length" in summary
    assert "HTTP Pod IP" in summary
    assert "HTTP Pod DNS" in summary
    assert "Inference Port" in summary
    assert "Management Port" in summary
    assert "Deploy Mode" not in summary
    assert "Scheduler Type" in summary
    assert "API Key Auth" in summary
    assert "Rate Limiting" in summary
    assert "Master/Standby" in summary
    assert "Config Path" in summary


def test_config_summary_includes_hybrid_fields(_temp_json_file):
    """Test configuration summary includes PD hybrid deploy fields."""
    user_config = {
        "motor_deploy_config": {
            "hybrid_instances_num": 3,
            "single_hybrid_instance_pod_num": 1,
            "hybrid_pod_npu_num": 4,
        },
        "motor_coordinator_config": {
            "scheduler_config": {
                "deploy_mode": "single_node",
            }
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
    with open(_temp_json_file, 'w', encoding="utf-8") as f:
        json.dump(user_config, f)

    config = CoordinatorConfig.from_json(_temp_json_file)
    summary = config.get_config_summary()

    assert "hybrid_instances_num: 3" in summary
    assert "single_hybrid_instance_pod_num: 1" in summary
    assert "hybrid_pod_npu_num: 4" in summary


def test_multiple_instances():
    """Test that multiple instances can be created independently"""
    config1 = CoordinatorConfig()
    config2 = CoordinatorConfig()
    assert config1 is not config2

    # Modify one instance and verify the other is not affected
    original_value = config1.exception_config.max_retry
    config1.exception_config.max_retry = 999
    assert config2.exception_config.max_retry == original_value


def test_reload_config(_temp_json_file):
    """Test configuration reload functionality"""
    # Create initial config
    initial_config = {"exception_config": {"max_retry": 5}}
    with open(_temp_json_file, 'w', encoding="utf-8") as f:
        json.dump(initial_config, f)

    config = CoordinatorConfig.from_json(_temp_json_file)
    assert config.exception_config.max_retry == 5

    # Modify config file
    updated_config = {"exception_config": {"max_retry": 10}}
    with open(_temp_json_file, 'w', encoding="utf-8") as f:
        json.dump(updated_config, f)

    # Force update file modification time
    current_time = time.time()
    os.utime(_temp_json_file, (current_time, current_time))

    # Reload config
    success = config.reload()
    assert success is True
    assert config.exception_config.max_retry == 10


def test_reload_config_file_not_modified(_temp_json_file):
    """Test reload when config file is not modified"""
    initial_config = {"exception_config": {"max_retry": 5}}
    with open(_temp_json_file, 'w', encoding="utf-8") as f:
        json.dump(initial_config, f)

    config = CoordinatorConfig.from_json(_temp_json_file)

    # Reload without modifying file
    success = config.reload()
    assert success is True  # Should return True because no change needed


def test_reload_config_file_not_found():
    """Test reload when config file doesn't exist"""
    config = CoordinatorConfig()
    config.config_path = "/non/existent/file.json"
    success = config.reload()
    assert success is False


def test_from_json_maps_union_kv_events_to_prefill_kv_event_config(_temp_json_file):
    """PD hybrid: auto-merge prefill_kv_event_config from motor_engine_union_config."""
    user_config = {
        "motor_deploy_config": {"hybrid_instances_num": 1},
        "motor_coordinator_config": {
            "scheduler_config": {
                "scheduler_type": "kv_cache_affinity",
            }
        },
        "motor_engine_union_config": {
            "engine_type": "vllm",
            "engine_config": {
                "model": "/mnt/weight/qwen3_8B",
                "block-size": 64,
                "kv-events-config": {
                    "endpoint": "tcp://*:5557",
                    "replay_endpoint": "tcp://*:6667",
                },
            },
        },
        "kv_conductor_config": {"http_server_port": 14444},
    }
    with open(_temp_json_file, 'w', encoding="utf-8") as f:
        json.dump(user_config, f)

    config = CoordinatorConfig.from_json(_temp_json_file)
    kr = config.scheduler_config.kv_conductor_config

    assert config.scheduler_config.scheduler_type.value == "kv_cache_affinity"
    assert kr.endpoint == "tcp://*:5557"
    assert kr.replay_endpoint == "tcp://*:6667"
    assert kr.model_path == "/mnt/weight/qwen3_8B"
    assert kr.http_server_port == 14444
    assert kr.block_size == 64


def test_from_json_prefill_kv_event_prefers_prefill_over_union(_temp_json_file):
    """When both prefill and union exist, prefill engine section wins."""
    user_config = {
        "motor_coordinator_config": {},
        "motor_engine_prefill_config": {
            "engine_type": "vllm",
            "engine_config": {
                "model": "/prefill/model",
                "kv-events-config": {
                    "endpoint": "tcp://*:1111",
                    "replay_endpoint": "tcp://*:2222",
                },
            },
        },
        "motor_engine_union_config": {
            "engine_type": "vllm",
            "engine_config": {
                "model": "/union/model",
                "kv-events-config": {
                    "endpoint": "tcp://*:5557",
                    "replay_endpoint": "tcp://*:6667",
                },
            },
        },
        "kv_conductor_config": {"http_server_port": 13333},
    }
    with open(_temp_json_file, 'w', encoding="utf-8") as f:
        json.dump(user_config, f)

    config = CoordinatorConfig.from_json(_temp_json_file)
    kr = config.scheduler_config.kv_conductor_config

    assert kr.endpoint == "tcp://*:1111"
    assert kr.replay_endpoint == "tcp://*:2222"
    assert kr.model_path == "/prefill/model"


def test_from_json_union_without_kv_events_skips_auto_merge(_temp_json_file):
    """Union without kv-events-config does not populate prefill_kv_event_config."""
    user_config = {
        "motor_coordinator_config": {},
        "motor_engine_union_config": {
            "engine_type": "vllm",
            "engine_config": {
                "model": "/mnt/weight/qwen3_8B",
                "max_model_len": 2048,
            },
        },
    }
    with open(_temp_json_file, 'w', encoding="utf-8") as f:
        json.dump(user_config, f)

    config = CoordinatorConfig.from_json(_temp_json_file)

    assert config.scheduler_config.kv_conductor_config.endpoint == ""
    assert config.scheduler_config.kv_conductor_config.model_path == ""


def test_from_json_maps_prefill_kv_events_regression(_temp_json_file):
    """PD separate: auto-merge prefill_kv_event_config from motor_engine_prefill_config."""
    user_config = {
        "motor_engine_prefill_config": {
            "engine_type": "vllm",
            "engine_config": {
                "model": "/mnt/weight/qwen3_8B",
                "block-size": 32,
                "kv-events-config": {
                    "endpoint": "tcp://*:5557",
                    "replay_endpoint": "tcp://*:6667",
                },
            },
        },
        "kv_conductor_config": {"http_server_port": 15555},
    }
    with open(_temp_json_file, 'w', encoding="utf-8") as f:
        json.dump(user_config, f)

    config = CoordinatorConfig.from_json(_temp_json_file)
    kr = config.scheduler_config.kv_conductor_config

    assert kr.endpoint == "tcp://*:5557"
    assert kr.replay_endpoint == "tcp://*:6667"
    assert kr.model_path == "/mnt/weight/qwen3_8B"
    assert kr.http_server_port == 15555
    assert kr.block_size == 32
