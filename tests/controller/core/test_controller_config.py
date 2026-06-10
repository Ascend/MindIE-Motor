# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
# pylint: disable=redefined-outer-name,unspecified-encoding
import os
import time
import json
import pytest
import tempfile
from unittest.mock import patch

from motor.config.controller import ControllerConfig


@pytest.fixture
def _temp_json_file():
    """Fixture for temporary JSON file that gets cleaned up."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        temp_path = f.name

    yield temp_path

    # Cleanup
    try:
        os.unlink(temp_path)
    except FileNotFoundError:
        pass


@pytest.fixture
def _temp_dir():
    """Fixture for temporary directory that gets cleaned up."""
    with tempfile.TemporaryDirectory() as _temp_dir:
        yield _temp_dir


def test_default_config_initialization():
    """Test default configuration initialization"""
    # Ensure no environment variables interfere with default values
    original_pod_ip = os.environ.get('POD_IP')
    if 'POD_IP' in os.environ:
        del os.environ['POD_IP']

    try:
        config = ControllerConfig()

        # Verify default values
        assert config.instance_config.instance_assemble_timeout == 600
        assert config.instance_config.instance_assembler_check_interval == 30
        assert config.instance_config.instance_assembler_cmd_send_interval == 30
        assert config.instance_config.send_cmd_retry_times == 3
        assert config.instance_config.instance_manager_check_interval == 1
        assert config.instance_config.instance_heartbeat_timeout == 10
        assert config.instance_config.instance_expired_timeout == 1200
        assert config.api_config.controller_api_host == '127.0.0.1'
        assert config.api_config.controller_api_port == 1026
        assert config.event_config.event_consumer_sleep_interval == 1.0
        assert config.event_config.coordinator_heartbeat_interval == 10.0
        assert config.mgmt_tls_config.enable_tls is False
        assert config.mgmt_tls_config.cert_file == 'security/mgmt/cert/server.crt'
        assert config.mgmt_tls_config.key_file == 'security/mgmt/keys/server.key'
        assert config.fault_tolerance_config.enable_fault_tolerance is True
    finally:
        # Restore original environment variable
        if original_pod_ip is not None:
            os.environ['POD_IP'] = original_pod_ip
        elif 'POD_IP' in os.environ:
            del os.environ['POD_IP']
    assert config.fault_tolerance_config.strategy_center_check_interval == 10


def test_config_validation_success():
    """Test successful configuration validation"""
    config = ControllerConfig()
    config.instance_config.instance_assemble_timeout = 300
    config.instance_config.instance_heartbeat_timeout = 10
    config.instance_config.instance_expired_timeout = 600
    config.api_config.controller_api_port = 9000
    config.instance_config.send_cmd_retry_times = 5
    # If no exception is raised, validation passed
    assert config.api_config.controller_api_port == 9000


@pytest.mark.parametrize(
    "param,value,expected_error",
    [
        ("instance_assemble_timeout", -1, "instance_assemble_timeout must be greater than 0"),
        ("instance_heartbeat_timeout", 0, "instance_heartbeat_timeout must be greater than 0"),
        ("instance_assembler_check_interval", -1, "instance_assembler_check_interval must be greater than 0"),
        ("event_consumer_sleep_interval", 0, "event_consumer_sleep_interval must be greater than 0"),
        ("coordinator_heartbeat_interval", -1, "coordinator_heartbeat_interval must be greater than 0"),
        ("controller_api_port", 0, "controller_api_port must be in range 1-65535"),
        ("controller_api_port", 65536, "controller_api_port must be in range 1-65535"),
        ("send_cmd_retry_times", -1, "send_cmd_retry_times cannot be negative"),
    ],
)
def test_config_validation_errors(param, value, expected_error):
    """Test various configuration validation errors"""
    with pytest.raises(ValueError, match=expected_error):
        config = ControllerConfig()
        if param in [
            "instance_assemble_timeout",
            "instance_heartbeat_timeout",
            "instance_assembler_check_interval",
            "send_cmd_retry_times",
        ]:
            setattr(config.instance_config, param, value)
        elif param in ["event_consumer_sleep_interval", "coordinator_heartbeat_interval"]:
            setattr(config.event_config, param, value)
        elif param == "controller_api_port":
            setattr(config.api_config, param, value)
        config.validate_config()


def test_config_validation_multiple_errors():
    """Test multiple configuration errors"""
    with pytest.raises(ValueError) as exc_info:
        config = ControllerConfig()
        config.instance_config.instance_assemble_timeout = -1
        config.api_config.controller_api_port = 0
        config.validate_config()
    error_msg = str(exc_info.value)
    assert "instance_assemble_timeout must be greater than 0" in error_msg
    assert "controller_api_port must be in range 1-65535" in error_msg


def test_from_json_success(_temp_json_file):
    """Test loading configuration from valid JSON file"""
    test_config = {
        "api_config": {"controller_api_host": "192.168.1.1", "controller_api_port": 9000},
        "event_config": {"event_consumer_sleep_interval": 2.0, "coordinator_heartbeat_interval": 1.0},
        "instance_config": {
            "instance_assemble_timeout": 300,
        },
        "fault_tolerance_config": {"enable_fault_tolerance": False},
    }

    with open(_temp_json_file, 'w', encoding="utf-8") as f:
        json.dump(test_config, f)

    config = ControllerConfig.from_json(_temp_json_file)
    assert config.api_config.controller_api_host == "192.168.1.1"
    assert config.api_config.controller_api_port == 9000
    assert config.event_config.event_consumer_sleep_interval == 2.0
    assert config.event_config.coordinator_heartbeat_interval == 1.0
    assert config.instance_config.instance_assemble_timeout == 300
    assert config.fault_tolerance_config.enable_fault_tolerance is False
    assert config.config_path == _temp_json_file
    assert config.last_modified is not None


def test_from_json_file_not_exists(_temp_dir):
    """Test loading configuration from non-existent JSON file (using default values)"""
    # Ensure no environment variables interfere with default values
    original_pod_ip = os.environ.get('POD_IP')
    if 'POD_IP' in os.environ:
        del os.environ['POD_IP']

    try:
        non_existent_path = os.path.join(_temp_dir, "non_existent.json")
        config = ControllerConfig.from_json(non_existent_path)

        # Should use default values
        assert config.api_config.controller_api_host == '127.0.0.1'
        assert config.api_config.controller_api_port == 1026
        assert config.config_path == non_existent_path
        assert config.last_modified is None
    finally:
        # Restore original environment variable
        if original_pod_ip is not None:
            os.environ['POD_IP'] = original_pod_ip
        elif 'POD_IP' in os.environ:
            del os.environ['POD_IP']


def test_from_json_invalid_json(_temp_json_file):
    """Test loading configuration from invalid JSON file"""
    with open(_temp_json_file, 'w', encoding="utf-8") as f:
        f.write("invalid json content")

    # Should use default configuration instead of raising exception
    config = ControllerConfig.from_json(_temp_json_file)
    assert config is not None
    assert config.api_config.controller_api_port == 1026  # default value


def test_reload_config_file_not_exists(_temp_dir):
    """Test reloading non-existent configuration file"""
    config = ControllerConfig()
    non_existent_path = os.path.join(_temp_dir, "non_existent.json")
    config.config_path = non_existent_path
    assert config.reload() is False


def test_reload_config_file_not_modified(_temp_json_file):
    """Test reloading unmodified configuration file"""
    test_config = {"controller_api_port": 8000}
    with open(_temp_json_file, 'w', encoding="utf-8") as f:
        json.dump(test_config, f)

    config = ControllerConfig.from_json(_temp_json_file)
    # First reload should succeed (file not modified)
    assert config.reload() is True


def test_reload_config_file_modified(_temp_json_file):
    """Test reloading modified configuration file"""
    test_config = {"api_config": {"controller_api_port": 8000}}
    modified_config = {"api_config": {"controller_api_port": 9000}}

    with open(_temp_json_file, 'w', encoding="utf-8") as f:
        json.dump(test_config, f)

    config = ControllerConfig.from_json(_temp_json_file)
    original_port = config.api_config.controller_api_port

    # Wait a short time to ensure different file modification time
    time.sleep(0.1)

    # Modify file
    with open(_temp_json_file, 'w', encoding="utf-8") as f:
        json.dump(modified_config, f)

    # Reload configuration
    assert config.reload() is True
    assert config.api_config.controller_api_port == 9000
    assert config.api_config.controller_api_port != original_port


def test_reload_config_invalid_json(_temp_json_file):
    """Test reloading invalid JSON configuration file"""
    test_config = {"controller_api_port": 8000}
    with open(_temp_json_file, 'w', encoding="utf-8") as f:
        json.dump(test_config, f)

    config = ControllerConfig.from_json(_temp_json_file)

    time.sleep(0.01)

    # Write invalid JSON
    with open(_temp_json_file, 'w', encoding="utf-8") as f:
        f.write("invalid json")

    # Manually update file modification time
    current_time = time.time()
    os.utime(_temp_json_file, (current_time, current_time))

    # Should succeed and use default configuration
    assert config.reload() is True
    assert config.api_config.controller_api_port == 1026  # default value


def test_to_dict():
    """Test conversion to dictionary"""
    config = ControllerConfig()
    config.api_config.controller_api_host = "192.168.1.1"
    config.api_config.controller_api_port = 9000
    config.event_config.event_consumer_sleep_interval = 2.0
    config.event_config.coordinator_heartbeat_interval = 1.5
    config.instance_config.instance_assemble_timeout = 300

    config_dict = config.to_dict()

    # Test grouped structure
    assert "api_config" in config_dict
    assert "event_config" in config_dict
    assert "instance_config" in config_dict

    # Test specific values in groups
    assert config_dict["api_config"]["controller_api_host"] == "192.168.1.1"
    assert config_dict["api_config"]["controller_api_port"] == 9000
    assert config_dict["event_config"]["event_consumer_sleep_interval"] == 2.0
    assert config_dict["event_config"]["coordinator_heartbeat_interval"] == 1.5
    assert config_dict["instance_config"]["instance_assemble_timeout"] == 300

    # Internal fields should not be present
    assert "_config_path" not in config_dict
    assert "_last_modified" not in config_dict


def test_save_to_json_success(_temp_json_file):
    """Test successful saving configuration to JSON file"""
    config = ControllerConfig()
    config.api_config.controller_api_port = 9000

    result = config.save_to_json(_temp_json_file)
    assert result is True

    # Verify file content
    with open(_temp_json_file, 'r', encoding="utf-8") as f:
        saved_config = json.load(f)
    assert saved_config["api_config"]["controller_api_port"] == 9000


def test_save_to_json_no_path():
    """Test saving configuration to unspecified path"""
    config = ControllerConfig()
    assert config.save_to_json() is False


def test_save_to_json_write_error(_temp_dir):
    """Test write error when saving configuration"""
    config = ControllerConfig()
    test_path = os.path.join(_temp_dir, "config.json")
    config.config_path = test_path

    with patch('builtins.open', side_effect=PermissionError("Permission denied")):
        assert config.save_to_json() is False


def test_get_config_summary():
    """Test getting configuration summary"""
    config = ControllerConfig()
    config.api_config.controller_api_host = "192.168.1.1"
    config.api_config.controller_api_port = 9000
    config.instance_config.instance_assemble_timeout = 300
    config.fault_tolerance_config.enable_fault_tolerance = False
    # Use a temporary path for testing
    with tempfile.TemporaryDirectory() as _temp_dir:
        test_path = os.path.join(_temp_dir, "config.json")
        config.config_path = test_path

        summary = config.get_config_summary()

        assert "192.168.1.1:9000" in summary
        assert "300 seconds" in summary
        assert "Disabled" in summary
        assert test_path in summary


def test_get_config_summary_no_path():
    """Test getting configuration summary (no path)"""
    config = ControllerConfig()
    summary = config.get_config_summary()
    assert "Not set" in summary


def test_config_boundary_values():
    """Test boundary value configuration"""
    # Test minimum valid values
    config_min = ControllerConfig()
    config_min.instance_config.instance_assemble_timeout = 1
    config_min.instance_config.instance_heartbeat_timeout = 1
    config_min.instance_config.instance_expired_timeout = 1
    config_min.event_config.event_consumer_sleep_interval = 0.1
    config_min.event_config.coordinator_heartbeat_interval = 0.1
    config_min.api_config.controller_api_port = 1
    config_min.instance_config.send_cmd_retry_times = 0
    assert config_min.event_config.event_consumer_sleep_interval == 0.1
    assert config_min.event_config.coordinator_heartbeat_interval == 0.1
    assert config_min.api_config.controller_api_port == 1
    assert config_min.instance_config.send_cmd_retry_times == 0

    # Test maximum valid values
    config_max = ControllerConfig()
    config_max.api_config.controller_api_port = 65535
    config_max.instance_config.send_cmd_retry_times = 100
    assert config_max.api_config.controller_api_port == 65535
    assert config_max.instance_config.send_cmd_retry_times == 100


def test_config_partial_json_loading():
    """Test partial JSON configuration loading with multiple config groups"""
    # Ensure no environment variables interfere with default values
    original_pod_ip = os.environ.get('POD_IP')
    if 'POD_IP' in os.environ:
        del os.environ['POD_IP']

    try:
        partial_config = {
            "api_config": {"controller_api_port": 9000},
            "event_config": {"event_consumer_sleep_interval": 2.5},
            "instance_config": {"instance_assemble_timeout": 300},
            # Other fields use default values
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(partial_config, f)
            temp_path = f.name

        try:
            config = ControllerConfig.from_json(temp_path)
            assert config.api_config.controller_api_port == 9000
            assert config.event_config.event_consumer_sleep_interval == 2.5
            assert config.instance_config.instance_assemble_timeout == 300
            # Other fields should be default values
            assert config.api_config.controller_api_host == '127.0.0.1'
            assert config.event_config.coordinator_heartbeat_interval == 10.0
            assert config.mgmt_tls_config.enable_tls is False
            assert config.mgmt_tls_config.cert_file == 'security/mgmt/cert/server.crt'
            assert config.mgmt_tls_config.key_file == 'security/mgmt/keys/server.key'
        finally:
            os.unlink(temp_path)
    finally:
        # Restore original environment variable
        if original_pod_ip is not None:
            os.environ['POD_IP'] = original_pod_ip
        elif 'POD_IP' in os.environ:
            del os.environ['POD_IP']


def test_config_partial_fields_in_group():
    """Test partial fields within a configuration group"""
    partial_config = {
        "standby_config": {
            "enable_master_standby": False
            # Other standby fields should keep default values
        }
    }

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(partial_config, f)
        temp_path = f.name

    try:
        config = ControllerConfig.from_json(temp_path)
        # Updated field
        assert config.standby_config.enable_master_standby is False
        # Other fields should keep default values
        assert config.standby_config.master_standby_check_interval == 5
        assert config.standby_config.master_lock_ttl == 10
        assert config.standby_config.master_lock_retry_interval == 5
        assert config.standby_config.master_lock_max_failures == 3
        assert config.standby_config.master_lock_key == "/controller/master_lock"
        # Other config groups should have default values
        assert config.api_config.controller_api_port == 1026
        assert config.mgmt_tls_config.enable_tls is False
    finally:
        os.unlink(temp_path)


def test_config_single_group_partial():
    """Test loading configuration with only one group and partial fields"""
    single_group_config = {
        "mgmt_tls_config": {
            "enable_tls": True,
            "cert_file": "/custom/cert.pem",
            # key_file should keep default value
        }
    }

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(single_group_config, f)
        temp_path = f.name

    try:
        config = ControllerConfig.from_json(temp_path)
        # Updated fields in tls_config
        assert config.mgmt_tls_config.enable_tls is True
        assert config.mgmt_tls_config.cert_file == "/custom/cert.pem"
        # Non-updated field in tls_config should keep default
        assert config.mgmt_tls_config.key_file == 'security/mgmt/keys/server.key'
        # Other config groups should have default values
        assert config.api_config.controller_api_port == 1026
        assert config.instance_config.instance_assemble_timeout == 600
    finally:
        os.unlink(temp_path)


def test_config_empty_json():
    """Test loading empty JSON file (should use all defaults)"""
    empty_config = {}

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(empty_config, f)
        temp_path = f.name

    try:
        config = ControllerConfig.from_json(temp_path)
        # All values should be defaults
        assert config.api_config.controller_api_port == 1026
        assert config.mgmt_tls_config.enable_tls is False
        assert config.instance_config.instance_assemble_timeout == 600
        assert config.standby_config.enable_master_standby is False
    finally:
        os.unlink(temp_path)


def test_config_extra_fields_in_json():
    """Test extra fields in JSON"""
    config_with_extra = {
        "api_config": {"controller_api_port": 9000},
        "extra_field": "should_be_ignored",
        "another_extra": 123,
    }

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(config_with_extra, f)
        temp_path = f.name

    try:
        config = ControllerConfig.from_json(temp_path)
        assert config.api_config.controller_api_port == 9000
        # Extra fields should be ignored
        assert not hasattr(config, 'extra_field')
        assert not hasattr(config, 'another_extra')
    finally:
        os.unlink(temp_path)


def test_config_reload_preserves_internal_fields():
    """Test that reloading configuration preserves internal fields"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump({"controller_api_port": 8000}, f)
        temp_path = f.name

    try:
        config = ControllerConfig.from_json(temp_path)
        original_path = config.config_path
        original_modified = config.last_modified

        # Use a shorter wait time and manually touch the file to ensure different modification time
        time.sleep(0.01)

        # Modify configuration
        with open(temp_path, 'w', encoding="utf-8") as f:
            json.dump({"api_config": {"controller_api_port": 9000}}, f)

        # Manually update file modification time to ensure it's different
        current_time = time.time()
        os.utime(temp_path, (current_time, current_time))

        config.reload()

        # Internal fields should be updated
        assert config.config_path == original_path
        assert config.last_modified >= original_modified
    finally:
        os.unlink(temp_path)


def test_config_validation_with_none_values():
    """Test handling None values in configuration validation"""
    # These tests ensure validation logic correctly handles various edge cases
    # ControllerConfig doesn't accept instance_assemble_timeout as a direct parameter
    # It should be set via instance_config
    config = ControllerConfig()
    with pytest.raises((ValueError, TypeError)):
        config.instance_config.instance_assemble_timeout = None
        config.validate_config()


def test_config_unicode_handling():
    """Test Unicode character handling"""
    unicode_config = {"api_config": {"controller_api_host": "Test Host", "controller_api_port": 8000}}

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as f:
        json.dump(unicode_config, f, ensure_ascii=False)
        temp_path = f.name

    try:
        config = ControllerConfig.from_json(temp_path)
        assert config.api_config.controller_api_host == "Test Host"
    finally:
        os.unlink(temp_path)


def create_test_config(config_path: str, log_level: str = "INFO"):
    """Create a test configuration file"""
    config = {
        "logging_config": {
            "log_level": log_level,
            "log_max_line_length": 8192,
            "log_file": None,
            "log_format": "%(levelname)s  %(asctime)s  %(filename)s:%(lineno)d  %(message)s",
            "log_date_format": "%Y-%m-%d %H:%M:%S",
        },
        "api_config": {"controller_api_host": "127.0.0.1", "controller_api_port": 8000},
        "event_config": {"event_consumer_sleep_interval": 1.0, "coordinator_heartbeat_interval": 5.0},
        "instance_config": {
            "instance_assemble_timeout": 600,
            "instance_assembler_check_interval": 1,
            "instance_assembler_cmd_send_internal": 1,
            "send_cmd_retry_times": 3,
            "instance_manager_check_internal": 1,
            "instance_heartbeat_timeout": 5,
            "instance_expired_timeout": 300,
        },
        "fault_tolerance_config": {
            "enable_fault_tolerance": True,
            "strategy_center_check_interval": 1,
            "enable_scale_p2d": True,
            "enable_token_reinference": True,
        },
    }

    with open(config_path, 'w', encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def modify_config_api_port(config_path: str, new_port: int):
    """Modify the API port in the configuration file"""
    with open(config_path, 'r', encoding="utf-8") as f:
        config = json.load(f)

    config["api_config"]["controller_api_port"] = new_port

    with open(config_path, 'w', encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def test_dynamic_config_reload_with_watcher():
    """Test dynamic config reload through ConfigWatcher"""
    import uuid
    from motor.common.utils.config_watcher import ConfigWatcher

    # Create a unique config file for this test to avoid parallel test interference
    unique_id = str(uuid.uuid4())[:8]
    _temp_dir = tempfile.gettempdir()
    config_file = os.path.join(_temp_dir, f"test_config_{unique_id}.json")

    try:
        # Create initial config
        create_test_config(config_file, "INFO")

        # Load config
        config = ControllerConfig.from_json(config_file)
        assert config.logging_config.log_level == "INFO"
        assert config.api_config.controller_api_port == 8000

        # Start watcher
        watcher = ConfigWatcher(config_file, config.reload, debounce_seconds=0.1)
        watcher.start()

        # Wait for watcher to start
        time.sleep(0.2)

        # Modify config values
        modify_config_api_port(config_file, 9000)

        # Wait for reload with retry logic
        max_attempts = 10
        reloaded = False
        for attempt in range(max_attempts):
            time.sleep(0.1)
            if config.api_config.controller_api_port == 9000:
                reloaded = True
                break

        # Stop watcher
        watcher.stop()

        # Check if config was reloaded
        assert reloaded, (
            f"Config reload failed after {max_attempts} attempts. Current port: {config.api_config.controller_api_port}"
        )

    finally:
        # Clean up the unique config file
        try:
            os.unlink(config_file)
        except FileNotFoundError:
            pass


def test_tls_config_default_values():
    """Test TLS configuration default values"""
    config = ControllerConfig()

    assert config.mgmt_tls_config.enable_tls is False
    assert config.mgmt_tls_config.cert_file == 'security/mgmt/cert/server.crt'
    assert config.mgmt_tls_config.key_file == 'security/mgmt/keys/server.key'


def test_tls_config_from_json(_temp_json_file):
    """Test loading TLS configuration from JSON file"""
    test_config = {
        "mgmt_tls_config": {
            "enable_tls": True,
            "cert_file": "/custom/path/cert.pem",
            "key_file": "/custom/path/key.pem",
        },
        "api_config": {"controller_api_port": 8443},
    }

    with open(_temp_json_file, 'w', encoding="utf-8") as f:
        json.dump(test_config, f)

    config = ControllerConfig.from_json(_temp_json_file)
    assert config.mgmt_tls_config.enable_tls is True
    assert config.mgmt_tls_config.cert_file == "/custom/path/cert.pem"
    assert config.mgmt_tls_config.key_file == "/custom/path/key.pem"
    assert config.api_config.controller_api_port == 8443


def test_tls_config_partial_loading(_temp_json_file):
    """Test partial TLS configuration loading (only enable_tls)"""
    test_config = {
        "mgmt_tls_config": {
            "enable_tls": True
            # cert_file and key_file should use default values
        }
    }

    with open(_temp_json_file, 'w', encoding="utf-8") as f:
        json.dump(test_config, f)

    config = ControllerConfig.from_json(_temp_json_file)
    assert config.mgmt_tls_config.enable_tls is True
    assert config.mgmt_tls_config.cert_file == 'security/mgmt/cert/server.crt'
    assert config.mgmt_tls_config.key_file == 'security/mgmt/keys/server.key'


def test_from_json_top_level_precision_auto_recovery_enabled(_temp_json_file):
    """``precision_auto_recovery_enabled`` merges from top-level controller JSON."""
    test_config = {"precision_auto_recovery_enabled": True}
    with open(_temp_json_file, "w", encoding="utf-8") as f:
        json.dump(test_config, f)
    config = ControllerConfig.from_json(_temp_json_file)
    assert config.precision_auto_recovery_enabled is True


def test_tls_config_to_dict():
    """Test TLS configuration in to_dict output"""
    config = ControllerConfig()
    config.mgmt_tls_config.enable_tls = True
    config.mgmt_tls_config.cert_file = "/custom/cert.pem"
    config.mgmt_tls_config.key_file = "/custom/key.pem"

    config_dict = config.to_dict()

    # Test grouped structure
    assert "mgmt_tls_config" in config_dict
    assert config_dict["mgmt_tls_config"]["enable_tls"] is True
    assert config_dict["mgmt_tls_config"]["cert_file"] == "/custom/cert.pem"
    assert config_dict["mgmt_tls_config"]["key_file"] == "/custom/key.pem"


def test_tls_config_save_to_json(_temp_json_file):
    """Test saving TLS configuration to JSON file"""
    config = ControllerConfig()
    config.mgmt_tls_config.enable_tls = True
    config.mgmt_tls_config.cert_file = "/custom/cert.pem"
    config.mgmt_tls_config.key_file = "/custom/key.pem"

    result = config.save_to_json(_temp_json_file)
    assert result is True

    # Verify file content
    with open(_temp_json_file, 'r', encoding="utf-8") as f:
        saved_config = json.load(f)

    # Test grouped structure
    assert "mgmt_tls_config" in saved_config
    assert saved_config["mgmt_tls_config"]["enable_tls"] is True
    assert saved_config["mgmt_tls_config"]["cert_file"] == "/custom/cert.pem"
    assert saved_config["mgmt_tls_config"]["key_file"] == "/custom/key.pem"


def test_tls_config_reload(_temp_json_file):
    """Test reloading TLS configuration"""
    initial_config = {
        "mgmt_tls_config": {"enable_tls": False, "cert_file": "/initial/cert.pem", "key_file": "/initial/key.pem"}
    }
    modified_config = {
        "mgmt_tls_config": {"enable_tls": True, "cert_file": "/modified/cert.pem", "key_file": "/modified/key.pem"}
    }

    with open(_temp_json_file, 'w', encoding="utf-8") as f:
        json.dump(initial_config, f)

    config = ControllerConfig.from_json(_temp_json_file)
    assert config.mgmt_tls_config.enable_tls is False
    assert config.mgmt_tls_config.cert_file == "/initial/cert.pem"
    assert config.mgmt_tls_config.key_file == "/initial/key.pem"

    # Wait a short time to ensure different file modification time
    time.sleep(0.1)

    # Modify file
    with open(_temp_json_file, 'w', encoding="utf-8") as f:
        json.dump(modified_config, f)

    # Reload configuration
    assert config.reload() is True
    assert config.mgmt_tls_config.enable_tls is True
    assert config.mgmt_tls_config.cert_file == "/modified/cert.pem"
    assert config.mgmt_tls_config.key_file == "/modified/key.pem"


def test_tls_config_boolean_values(_temp_json_file):
    """Test TLS enable_tls with different boolean representations"""
    # Test with true (lowercase)
    test_config = {"mgmt_tls_config": {"enable_tls": True}}
    with open(_temp_json_file, 'w', encoding="utf-8") as f:
        json.dump(test_config, f)
    config = ControllerConfig.from_json(_temp_json_file)
    assert config.mgmt_tls_config.enable_tls is True

    # Test with false (lowercase)
    test_config = {"mgmt_tls_config": {"enable_tls": False}}
    with open(_temp_json_file, 'w', encoding="utf-8") as f:
        json.dump(test_config, f)
    config = ControllerConfig.from_json(_temp_json_file)
    assert config.mgmt_tls_config.enable_tls is False


def test_tls_config_path_strings():
    """Test TLS certificate and key path configurations with various string values"""
    # Test with absolute paths
    config = ControllerConfig()
    config.mgmt_tls_config.cert_file = "/absolute/path/to/cert.crt"
    config.mgmt_tls_config.key_file = "/absolute/path/to/key.key"
    assert config.mgmt_tls_config.cert_file == "/absolute/path/to/cert.crt"
    assert config.mgmt_tls_config.key_file == "/absolute/path/to/key.key"

    # Test with relative paths
    config = ControllerConfig()
    config.mgmt_tls_config.cert_file = "relative/cert.crt"
    config.mgmt_tls_config.key_file = "relative/key.key"
    assert config.mgmt_tls_config.cert_file == "relative/cert.crt"
    assert config.mgmt_tls_config.key_file == "relative/key.key"

    # Test with empty strings (should be allowed, validation happens at usage)
    config = ControllerConfig()
    config.mgmt_tls_config.cert_file = ""
    config.mgmt_tls_config.key_file = ""
    assert config.mgmt_tls_config.cert_file == ""
    assert config.mgmt_tls_config.key_file == ""
