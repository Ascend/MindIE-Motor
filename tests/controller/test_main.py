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
import signal
import pytest
from unittest.mock import MagicMock, patch, ANY

from motor.controller.main import (
    parse_arguments,
    stop_all_modules,
    signal_handler,
    init_all_modules,
    start_all_modules,
    on_config_updated,
    on_become_master,
    on_become_standby,
    modules,
)


@pytest.fixture(autouse=True)
def cleanup_modules():
    """Clean up global modules dictionary"""
    modules.clear()
    yield
    modules.clear()


def test_parse_arguments_default():
    """Test default argument parsing"""
    with patch("sys.argv", ["main.py"]):
        args = parse_arguments()
        assert args.config is None


def test_parse_arguments_with_config():
    """Test specified config file parameter"""
    config_path = "/path/to/config.json"
    with patch("sys.argv", ["main.py", "--config", config_path]):
        args = parse_arguments()
        assert args.config == config_path

    with patch("sys.argv", ["main.py", "-c", config_path]):
        args = parse_arguments()
        assert args.config == config_path


def test_stop_all_modules():
    """Test stopping all modules"""
    # Create mock modules
    mock_module1 = MagicMock()
    mock_module2 = MagicMock()
    mock_module3 = MagicMock()

    # Set up modules dictionary
    modules.update({"Module1": mock_module1, "Module2": mock_module2, "Module3": mock_module3})

    stop_all_modules()

    # Verify all modules' stop methods are called
    mock_module1.stop.assert_called_once()
    mock_module2.stop.assert_called_once()
    mock_module3.stop.assert_called_once()


def test_stop_all_modules_no_stop_method():
    """Test stopping modules without stop method"""
    # Create mock modules
    mock_module1 = MagicMock()
    mock_module2 = MagicMock()
    del mock_module2.stop  # Remove stop method

    modules.update({"Module1": mock_module1, "Module2": mock_module2})

    # Should not raise exception
    stop_all_modules()

    mock_module1.stop.assert_called_once()
    assert not hasattr(mock_module2, "stop")


@patch("sys.exit")
def test_signal_handler(mock_exit):
    """Test signal handler"""
    with patch("motor.controller.main.stop_all_modules") as mock_stop:
        signal_handler(signal.SIGINT, None)

        mock_stop.assert_called_once()


@patch("motor.controller.main.logger")
def test_init_all_modules_success(mock_logger):
    """Test successful module initialization"""
    # Create mock config
    mock_config = MagicMock()
    mock_config.fault_tolerance_config.enable_fault_tolerance = False
    mock_config.om_config.om_enable = False

    # Create mock instance manager
    mock_instance_manager = MagicMock()
    mock_observer1 = MagicMock()
    mock_observer2 = MagicMock()

    with (
        patch("motor.controller.main.config", mock_config),
        patch("motor.controller.main.InstanceManager", return_value=mock_instance_manager),
        patch("motor.controller.main.InstanceAssembler", return_value=mock_observer1),
        patch("motor.controller.main.EventPusher", return_value=mock_observer2),
        patch("motor.controller.main.ControllerAPI"),
    ):
        init_all_modules()

        # Verify modules are created
        assert "InstanceManager" in modules
        assert "InstanceAssembler" in modules
        assert "EventPusher" in modules
        assert "ControllerAPI" in modules

        # Verify observers are attached (only EventPusher from observers_list)
        mock_instance_manager.attach.assert_called_once_with(mock_observer2)


@patch("motor.controller.main.logger")
def test_init_all_modules_no_manager(mock_logger):
    """Test case when module initialization fails"""
    # Create mock config
    mock_config = MagicMock()
    mock_config.fault_tolerance_config.enable_fault_tolerance = False

    with (
        patch("motor.controller.main.config", mock_config),
        patch("motor.controller.main.InstanceManager", return_value=None),
        patch("motor.controller.main.InstanceAssembler"),
        patch("motor.controller.main.EventPusher"),
        patch("motor.controller.main.ControllerAPI"),
    ):
        # Should not raise exception
        init_all_modules()

        mock_logger.error.assert_called_once()


@patch("motor.controller.main.logger")
def test_start_all_modules(mock_logger):
    """Test starting modules"""
    # Create mock modules
    mock_module1 = MagicMock()
    mock_module2 = MagicMock()
    mock_module3 = MagicMock()

    modules.update({"Module1": mock_module1, "Module2": mock_module2, "Module3": mock_module3})

    start_all_modules()

    # Verify all modules' start methods are called
    mock_module1.start.assert_called_once()
    mock_module2.start.assert_called_once()
    mock_module3.start.assert_called_once()


def test_start_all_modules_no_start_method():
    """Test starting modules without start method"""
    # Create mock modules
    mock_module1 = MagicMock()
    mock_module2 = MagicMock()
    del mock_module2.start  # Remove start method

    modules.update({"Module1": mock_module1, "Module2": mock_module2})

    # Should not raise exception
    start_all_modules()

    mock_module1.start.assert_called_once()
    assert not hasattr(mock_module2, "start")


def test_init_all_modules_fault_tolerance_enabled():
    """Test enabling fault tolerance in module initialization"""
    # Create mock config
    mock_config = MagicMock()
    mock_config.fault_tolerance_config.enable_fault_tolerance = True

    # Create mock modules
    mock_instance_manager = MagicMock()
    mock_fault_manager = MagicMock()

    with (
        patch("motor.controller.main.config", mock_config),
        patch("motor.controller.main.InstanceManager", return_value=mock_instance_manager),
        patch("motor.controller.main.InstanceAssembler"),
        patch("motor.controller.main.EventPusher"),
        patch("motor.controller.main.ControllerAPI"),
        patch(
            "motor.controller.fault_tolerance.FaultManager",
            return_value=mock_fault_manager,
        ),
    ):
        init_all_modules()

        # Verify FaultManager is created when fault tolerance is enabled
        assert "FaultManager" in modules


@patch("motor.controller.main.logger")
def test_on_config_updated_config_none(mock_logger):
    """Test on_config_updated when config is None"""
    with (
        patch("motor.controller.main.config", None),
        patch("motor.controller.main.previous_fault_tolerance_enabled", False),
    ):
        on_config_updated()

        mock_logger.error.assert_called_once_with("Configuration is None in config update callback")


@patch("motor.controller.main.logger")
def test_on_config_updated_enable_fault_tolerance(mock_logger):
    """Test enabling fault tolerance in config update"""
    # Create mock config
    mock_config = MagicMock()
    mock_config.fault_tolerance_config.enable_fault_tolerance = True
    mock_config.om_config.om_enable = False

    # Create mock modules
    mock_instance_manager = MagicMock()
    mock_fault_manager = MagicMock()

    modules_copy = {"InstanceManager": mock_instance_manager}

    with (
        patch("motor.controller.main.config", mock_config),
        patch("motor.controller.main.modules", modules_copy),
        patch("motor.controller.main.previous_fault_tolerance_enabled", False),
        patch(
            "motor.controller.fault_tolerance.FaultManager",
            return_value=mock_fault_manager,
        ),
    ):
        on_config_updated()

        # Verify FaultManager is created and started
        assert modules_copy["FaultManager"] is mock_fault_manager
        mock_fault_manager.start.assert_called_once()
        mock_instance_manager.attach.assert_called_once_with(mock_fault_manager)
        mock_logger.info.assert_any_call("Fault tolerance feature enabled, starting FaultManager...")


@patch("motor.controller.main.logger")
def test_on_config_updated_disable_fault_tolerance(mock_logger):
    """Test disabling fault tolerance in config update"""
    # Create mock config
    mock_config = MagicMock()
    mock_config.fault_tolerance_config.enable_fault_tolerance = False

    # Create mock modules
    mock_fault_manager = MagicMock()
    modules_copy = {"FaultManager": mock_fault_manager}

    with (
        patch("motor.controller.main.config", mock_config),
        patch("motor.controller.main.modules", modules_copy),
        patch("motor.controller.main.previous_fault_tolerance_enabled", True),
    ):
        on_config_updated()

        # Verify FaultManager is stopped and removed
        mock_fault_manager.stop.assert_called_once()
        assert "FaultManager" not in modules_copy
        mock_logger.info.assert_any_call("Fault tolerance feature disabled, stopping FaultManager...")


@patch("motor.controller.main.logger")
def test_on_config_updated_no_fault_tolerance_change(mock_logger):
    """Test when fault tolerance state doesn't change"""
    # Create mock config
    mock_config = MagicMock()
    mock_config.fault_tolerance_config.enable_fault_tolerance = False

    # Create mock modules
    mock_module1 = MagicMock()
    mock_module2 = MagicMock()
    modules_copy = {"Module1": mock_module1, "Module2": mock_module2}

    with (
        patch("motor.controller.main.config", mock_config),
        patch("motor.controller.main.modules", modules_copy),
        patch("motor.controller.main.previous_fault_tolerance_enabled", False),
    ):
        on_config_updated()

        # Verify modules are updated
        mock_module1.update_config.assert_called_once_with(mock_config)
        mock_module2.update_config.assert_called_once_with(mock_config)
        mock_logger.info.assert_any_call("Updating configuration for all modules...")


@patch("motor.controller.main.logger")
def test_on_config_updated_enable_fault_tolerance_exception(mock_logger):
    """Test exception when enabling fault tolerance"""
    # Create mock config
    mock_config = MagicMock()
    mock_config.fault_tolerance_config.enable_fault_tolerance = True

    modules_copy = {}

    with (
        patch("motor.controller.main.config", mock_config),
        patch("motor.controller.main.modules", modules_copy),
        patch("motor.controller.main.previous_fault_tolerance_enabled", False),
        patch(
            "motor.controller.fault_tolerance.FaultManager",
            side_effect=Exception("Test error"),
        ),
    ):
        on_config_updated()

        mock_logger.error.assert_called_with("Failed to start FaultManager: %s", ANY)


@patch("motor.controller.main.logger")
def test_on_config_updated_disable_fault_tolerance_exception(mock_logger):
    """Test exception when disabling fault tolerance"""
    # Create mock config
    mock_config = MagicMock()
    mock_config.fault_tolerance_config.enable_fault_tolerance = False

    # Create mock modules
    mock_fault_manager = MagicMock()
    mock_fault_manager.stop.side_effect = Exception("Test error")
    modules_copy = {"FaultManager": mock_fault_manager}

    with (
        patch("motor.controller.main.config", mock_config),
        patch("motor.controller.main.modules", modules_copy),
        patch("motor.controller.main.previous_fault_tolerance_enabled", True),
    ):
        on_config_updated()

        mock_logger.error.assert_called_with("Failed to stop FaultManager: %s", ANY)


@patch("motor.controller.main.logger")
def test_on_config_updated_module_update_exception(mock_logger):
    """Test exception when updating module config"""
    # Create mock config
    mock_config = MagicMock()
    mock_config.fault_tolerance_config.enable_fault_tolerance = False

    # Create mock modules
    mock_module1 = MagicMock()
    mock_module2 = MagicMock()
    mock_module2.update_config.side_effect = Exception("Update error")
    modules_copy = {"Module1": mock_module1, "Module2": mock_module2}

    with (
        patch("motor.controller.main.config", mock_config),
        patch("motor.controller.main.modules", modules_copy),
        patch("motor.controller.main.previous_fault_tolerance_enabled", False),
    ):
        on_config_updated()

        # Verify Module1 was updated successfully
        mock_module1.update_config.assert_called_once_with(mock_config)
        # Verify error was logged for Module2
        mock_logger.error.assert_called_with("Failed to update configuration for %s: %s", "Module2", ANY)


@patch("motor.controller.main.logger")
def test_on_become_master_initialize_modules(mock_logger):
    """Test on_become_master when modules are not initialized"""
    # Clear modules
    modules.clear()

    # Create mock config
    mock_config = MagicMock()

    with (
        patch("motor.controller.main.config", mock_config),
        patch("motor.controller.main.init_all_modules") as mock_init,
        patch("motor.controller.main.start_all_modules") as mock_start,
    ):
        on_become_master(should_report_event=False)

        mock_init.assert_called_once()
        mock_start.assert_called_once_with(exclude_modules={"ControllerAPI"})


@patch("motor.controller.main.logger")
def test_on_become_master_modules_already_initialized(mock_logger):
    """Test on_become_master when modules are already initialized"""
    # Set up existing modules
    modules["TestModule"] = MagicMock()

    with (
        patch("motor.controller.main.init_all_modules") as mock_init,
        patch("motor.controller.main.start_all_modules") as mock_start,
    ):
        on_become_master(should_report_event=False)

        mock_init.assert_not_called()
        mock_start.assert_called_once_with(exclude_modules={"ControllerAPI"})


@patch("motor.controller.main.logger")
def test_on_become_standby(mock_logger):
    """Test on_become_standby"""
    with patch("motor.controller.main.stop_all_modules") as mock_stop:
        on_become_standby()

        mock_stop.assert_called_once_with(exclude_modules={"ControllerAPI"})


@patch("motor.controller.main.logger")
def test_start_all_modules_with_exclude(mock_logger):
    """Test starting modules with exclude_modules parameter"""
    # Create mock modules
    mock_module1 = MagicMock()
    mock_module2 = MagicMock()
    mock_module3 = MagicMock()

    modules.update({"Module1": mock_module1, "Module2": mock_module2, "Module3": mock_module3})

    start_all_modules(exclude_modules={"Module2"})

    # Verify Module2 was not started
    mock_module1.start.assert_called_once()
    mock_module3.start.assert_called_once()
    mock_module2.start.assert_not_called()


@patch("motor.controller.main.logger")
def test_stop_all_modules_with_exclude(mock_logger):
    """Test stopping modules with exclude_modules parameter"""
    # Create mock modules
    mock_module1 = MagicMock()
    mock_module2 = MagicMock()
    mock_module3 = MagicMock()

    modules.update({"Module1": mock_module1, "Module2": mock_module2, "Module3": mock_module3})

    stop_all_modules(exclude_modules={"Module2"})

    # Verify Module2 was not stopped
    mock_module1.stop.assert_called_once()
    mock_module3.stop.assert_called_once()
    mock_module2.stop.assert_not_called()


@patch("motor.controller.main.logger")
def test_start_all_modules_exclude_none(mock_logger):
    """Test start_all_modules with exclude_modules=None"""
    # Create mock modules
    mock_module1 = MagicMock()
    mock_module2 = MagicMock()

    modules.update({"Module1": mock_module1, "Module2": mock_module2})

    start_all_modules(exclude_modules=None)

    # Verify all modules are started
    mock_module1.start.assert_called_once()
    mock_module2.start.assert_called_once()


@patch("motor.controller.main.logger")
def test_stop_all_modules_exclude_none(mock_logger):
    """Test stop_all_modules with exclude_modules=None"""
    # Create mock modules
    mock_module1 = MagicMock()
    mock_module2 = MagicMock()

    modules.update({"Module1": mock_module1, "Module2": mock_module2})

    stop_all_modules(exclude_modules=None)

    # Verify all modules are stopped
    mock_module1.stop.assert_called_once()
    mock_module2.stop.assert_called_once()


def test_signal_handler_with_config_watcher():
    """Test signal handler with config watcher present"""
    mock_watcher = MagicMock()

    with (
        patch("motor.controller.main.stop_event") as mock_stop_event,
        patch("motor.controller.main.stop_all_modules") as mock_stop,
        patch("motor.controller.main.config_watcher", mock_watcher),
        patch("sys.exit"),
    ):
        signal_handler(signal.SIGINT, None)

        mock_stop_event.set.assert_called_once()
        mock_stop.assert_called_once()
        mock_watcher.stop.assert_called_once()


def test_signal_handler_no_config_watcher():
    """Test signal handler without config watcher"""
    with (
        patch("motor.controller.main.stop_event") as mock_stop_event,
        patch("motor.controller.main.stop_all_modules") as mock_stop,
        patch("motor.controller.main.config_watcher", None),
        patch("sys.exit"),
    ):
        signal_handler(signal.SIGTERM, None)

        mock_stop_event.set.assert_called_once()
        mock_stop.assert_called_once()
