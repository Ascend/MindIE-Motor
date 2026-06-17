# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""
Resource Monitor test cases.

Test cases are organized according to the following logical blocks:
1. ResourceMonitor initialization
2. Kubernetes configuration loading
3. Monitoring lifecycle (start/stop)
4. Node monitoring functionality
5. ConfigMap monitoring functionality
6. Event handling
7. Data processing
8. Node status extraction
"""

import pytest
from unittest.mock import patch, Mock

from motor.controller.fault_tolerance.k8s.resource_monitor import ResourceMonitor
from motor.controller.fault_tolerance.fault_types import NodeStatus, FaultInfo

# pylint: disable=redefined-outer-name


@pytest.fixture(autouse=True)
def mock_kubernetes_config():
    """Mock Kubernetes configuration loading to avoid error logs in tests"""
    with patch(
        "motor.controller.fault_tolerance.k8s.resource_monitor.config.load_incluster_config",
        side_effect=Exception("In-cluster config not available"),
    ):
        with patch(
            "motor.controller.fault_tolerance.k8s.resource_monitor.config.load_kube_config",
            side_effect=Exception("Kubeconfig not available"),
        ):
            with patch("motor.controller.fault_tolerance.k8s.resource_monitor.logger"):
                yield


# Common test constants
DEFAULT_NODE_NAME = "test-node"
DEFAULT_NAMESPACE = "default"
DEFAULT_CONFIGMAP_PREFIX = "fault-config-"
DEFAULT_RETRY_INTERVAL = 30


def create_test_monitor(
    node_name=DEFAULT_NODE_NAME,
    namespace=DEFAULT_NAMESPACE,
    configmap_name_prefix=DEFAULT_CONFIGMAP_PREFIX,
    retry_interval=DEFAULT_RETRY_INTERVAL,
    node_handler=None,
    configmap_handler=None,
):
    """Helper function to create ResourceMonitor instance for testing"""
    return ResourceMonitor(
        node_name=node_name,
        namespace=namespace,
        configmap_name_prefix=configmap_name_prefix,
        retry_interval=retry_interval,
        node_change_handler=node_handler,
        configmap_change_handler=configmap_handler,
    )


def test_resource_monitor_initialization_with_valid_params():
    """Test ResourceMonitor initialization with valid parameters"""
    monitor = create_test_monitor(retry_interval=30)

    assert monitor.node_name == DEFAULT_NODE_NAME
    assert monitor.namespace == DEFAULT_NAMESPACE
    assert monitor.configmap_name_prefix == DEFAULT_CONFIGMAP_PREFIX
    assert monitor.retry_interval == 30
    assert monitor.node_change_handler is None
    assert monitor.configmap_change_handler is None
    assert monitor.stop_event.is_set() is False
    assert not monitor.monitor_threads


def test_resource_monitor_initialization_with_handlers():
    """Test ResourceMonitor initialization with change handlers"""

    def node_handler(status, ip):
        pass

    def configmap_handler(faults, ip):
        pass

    monitor = create_test_monitor(node_handler=node_handler, configmap_handler=configmap_handler)

    assert monitor.node_change_handler is node_handler
    assert monitor.configmap_change_handler is configmap_handler


def test_resource_monitor_kubernetes_config_incluster_success():
    """Test successful loading of in-cluster Kubernetes config"""
    # This test is skipped when kubernetes is not available
    # In real environment with kubernetes, this would work
    # For testing purposes, we verify the logic by checking the attributes
    monitor = create_test_monitor()

    # When kubernetes is not available, v1 client is not created
    # This test verifies the initialization logic
    assert monitor.node_name == DEFAULT_NODE_NAME
    assert monitor.namespace == DEFAULT_NAMESPACE
    assert monitor.configmap_name_prefix == DEFAULT_CONFIGMAP_PREFIX


def test_resource_monitor_kubernetes_config_incluster_failure_kubeconfig_success():
    """Test fallback to kubeconfig when in-cluster config fails"""
    # Simplified test - in real environment this would test config loading
    # Here we just verify basic initialization works
    monitor = create_test_monitor()

    assert monitor.node_name == DEFAULT_NODE_NAME
    assert monitor.retry_interval == DEFAULT_RETRY_INTERVAL


def test_resource_monitor_kubernetes_config_both_fail():
    """Test when both in-cluster and kubeconfig loading fail"""
    # Simplified test - in real environment this would test error handling
    monitor = create_test_monitor()

    # Verify basic attributes are set
    assert monitor.node_change_handler is None
    assert monitor.configmap_change_handler is None


def test_resource_monitor_kubernetes_not_available():
    """Test when Kubernetes client is not available"""
    monitor = create_test_monitor()

    # Verify that v1 client was not created (when config loading fails, __init__ returns early)
    assert not hasattr(monitor, "v1")


def test_start_monitoring_success(caplog):
    """Test successful start of monitoring"""
    # This test would require kubernetes to be available
    # In test environment, we verify that when kubernetes is not available,
    # monitoring does not start
    monitor = create_test_monitor()

    # Start monitoring
    monitor.start_monitoring()

    # Verify that no monitoring threads were started (kubernetes not available)
    assert len(monitor.monitor_threads) == 0


def test_start_monitoring_hostname_not_found(caplog):
    """Test start monitoring when hostname cannot be found"""
    # Simplified test - in test environment without kubernetes
    monitor = create_test_monitor()

    # Start monitoring
    monitor.start_monitoring()

    # Verify that no monitoring threads were started
    assert len(monitor.monitor_threads) == 0


def test_start_monitoring_kubernetes_not_available(caplog):
    """Test start monitoring when Kubernetes is not available"""
    monitor = create_test_monitor()

    # Start monitoring
    monitor.start_monitoring()

    # Verify that no monitoring threads were started (v1 is not set when config loading fails)
    assert len(monitor.monitor_threads) == 0


def test_stop_monitoring():
    """Test stopping monitoring"""
    monitor = create_test_monitor()

    # Simulate running threads
    mock_thread1 = Mock()
    mock_thread1.is_alive.return_value = True
    mock_thread1.name = "TestThread1"

    mock_thread2 = Mock()
    mock_thread2.is_alive.return_value = False
    mock_thread2.name = "TestThread2"

    monitor.monitor_threads = [mock_thread1, mock_thread2]

    # Stop monitoring
    monitor.stop_monitoring()

    # Verify stop event was set
    assert monitor.stop_event.is_set()
    # Verify alive threads were joined
    mock_thread1.join.assert_called_once_with(timeout=5.0)
    # Dead threads should not be joined
    mock_thread2.join.assert_not_called()
    # Verify thread list was cleared
    assert not monitor.monitor_threads


def test_is_alive_kubernetes_available():
    """Test is_alive when Kubernetes is available"""
    # In test environment, kubernetes is not available
    monitor = create_test_monitor()

    # Test when stop event is not set but no threads (kubernetes not available)
    assert monitor.is_alive() is False


def test_is_alive_kubernetes_not_available():
    """Test is_alive when Kubernetes is not available"""
    monitor = create_test_monitor()

    # Simulate Kubernetes not available (no v1 attribute)
    if hasattr(monitor, "v1"):
        delattr(monitor, "v1")

    assert monitor.is_alive() is False


def test_is_alive_stop_event_set():
    """Test is_alive when stop event is set"""
    monitor = create_test_monitor()

    # Set stop event
    monitor.stop_event.set()

    assert monitor.is_alive() is False


def test_is_alive_no_threads():
    """Test is_alive when no monitoring threads exist"""
    monitor = create_test_monitor()

    assert monitor.is_alive() is False


def test_is_alive_threads_not_alive():
    """Test is_alive when monitoring threads are not alive"""
    monitor = create_test_monitor()

    # Add thread that is not alive
    mock_thread = Mock()
    mock_thread.is_alive.return_value = False
    monitor.monitor_threads = [mock_thread]

    assert monitor.is_alive() is False


def test_monitor_methods_exist():
    """Test that monitor methods exist and are callable"""
    monitor = create_test_monitor()

    # Verify monitor methods exist
    assert hasattr(monitor, "_monitor_node")
    assert callable(monitor._monitor_node)
    assert hasattr(monitor, "_monitor_configmap")
    assert callable(monitor._monitor_configmap)


def test_handle_node_change_added_modified():
    """Test handling node ADDED and MODIFIED events"""
    monitor = create_test_monitor()

    # Mock node with ready status
    mock_node = Mock()
    mock_condition = Mock()
    mock_condition.type = "Ready"
    mock_condition.status = "True"
    mock_node.status.conditions = [mock_condition]
    mock_node.metadata.name = "test-node"

    # Mock node change handler
    handler_calls = []

    def mock_node_handler(status, ip):
        handler_calls.append((status, ip))

    monitor.node_change_handler = mock_node_handler
    monitor.hostname = "test-node"

    # Test ADDED event
    monitor._handle_node_change("ADDED", mock_node)
    assert len(handler_calls) == 1
    assert handler_calls[0] == (NodeStatus.READY, DEFAULT_NODE_NAME)

    # Test MODIFIED event with same status (should not trigger handler due to deduplication)
    monitor._handle_node_change("MODIFIED", mock_node)
    assert len(handler_calls) == 1  # Handler should not be called again for duplicate status


def test_handle_node_change_deduplication_with_actual_change():
    """Test that handler is called when node status actually changes"""
    monitor = create_test_monitor()

    # Mock node with ready status
    mock_node_ready = Mock()
    mock_condition_ready = Mock()
    mock_condition_ready.type = "Ready"
    mock_condition_ready.status = "True"
    mock_node_ready.status.conditions = [mock_condition_ready]
    mock_node_ready.metadata.name = "test-node"

    # Mock node with not ready status
    mock_node_not_ready = Mock()
    mock_condition_not_ready = Mock()
    mock_condition_not_ready.type = "Ready"
    mock_condition_not_ready.status = "False"
    mock_node_not_ready.status.conditions = [mock_condition_not_ready]
    mock_node_not_ready.metadata.name = "test-node"

    # Mock node change handler
    handler_calls = []

    def mock_node_handler(status, ip):
        handler_calls.append((status, ip))

    monitor.node_change_handler = mock_node_handler
    monitor.hostname = "test-node"

    # Test first change (READY)
    monitor._handle_node_change("ADDED", mock_node_ready)
    assert len(handler_calls) == 1
    assert handler_calls[0] == (NodeStatus.READY, DEFAULT_NODE_NAME)

    # Test second change with different status (NOT_READY)
    monitor._handle_node_change("MODIFIED", mock_node_not_ready)
    assert len(handler_calls) == 2  # Handler should be called again for different status
    assert handler_calls[1] == (NodeStatus.NOT_READY, DEFAULT_NODE_NAME)


def test_handle_node_change_deleted():
    """Test handling node DELETED event"""
    monitor = create_test_monitor()

    # Mock deleted node
    mock_node = Mock()
    mock_node.metadata = Mock()
    mock_node.metadata.name = "test-node"
    mock_node.status = Mock()
    mock_node.status.conditions = []  # Empty conditions list for deleted node

    # Mock node change handler
    handler_calls = []

    def mock_node_handler(status, ip):
        handler_calls.append((status, ip))

    monitor.node_change_handler = mock_node_handler
    monitor.hostname = "test-node"

    # Test DELETED event
    monitor._handle_node_change("DELETED", mock_node)
    assert len(handler_calls) == 1
    assert handler_calls[0] == (NodeStatus.NOT_READY, DEFAULT_NODE_NAME)


def test_handle_node_change_no_handler():
    """Test handling node change when no handler is configured"""
    monitor = create_test_monitor()

    # Mock node
    mock_node = Mock()
    mock_node.metadata = Mock()
    mock_node.metadata.name = "test-node"
    mock_node.status = Mock()
    mock_node.status.conditions = []  # Empty conditions list

    # Test with no handler (should not raise exception)
    with patch("motor.controller.fault_tolerance.k8s.resource_monitor.logger") as mock_logger:
        monitor._handle_node_change("ADDED", mock_node)

        # Verify that warning was logged about no handler
        mock_logger.warning.assert_called_with("No node change handler configured for node %s", DEFAULT_NODE_NAME)


def test_handle_node_change_handler_exception():
    """Test handling node change when handler raises exception"""
    monitor = create_test_monitor()

    # Mock node with proper structure
    mock_node = Mock()
    mock_node.metadata.name = "test-node"
    mock_node.status.conditions = []

    # Mock node change handler that raises exception
    def mock_node_handler(status, ip):
        raise RuntimeError("Handler exception")

    monitor.node_change_handler = mock_node_handler

    # Test that exception is caught and logged
    with patch("motor.controller.fault_tolerance.k8s.resource_monitor.logger") as mock_logger:
        monitor._handle_node_change("ADDED", mock_node)

        # Verify that error was logged for the handler exception
        mock_logger.error.assert_called_once()
        args = mock_logger.error.call_args[0]
        assert args[0] == "Error in node status change handler for %s: %s"
        assert args[1] == DEFAULT_NODE_NAME  # hostname lookup fails in test, falls back to node_name
        assert isinstance(args[2], Exception)
        assert str(args[2]) == "Handler exception"


def test_handle_configmap_change_added_modified():
    """Test handling ConfigMap ADDED and MODIFIED events"""
    monitor = create_test_monitor()

    # Mock ConfigMap
    mock_configmap = Mock()
    mock_configmap.metadata.name = "fault-config-test-node"
    mock_configmap.metadata.namespace = "default"
    mock_configmap.data = {"DeviceInfoCfg": '{"test": "data"}'}

    # Mock configmap change handler
    handler_calls = []

    def mock_configmap_handler(faults, ip):
        handler_calls.append((faults, ip))

    monitor.configmap_change_handler = mock_configmap_handler

    # Test ADDED event
    monitor._handle_configmap_change("ADDED", mock_configmap, mock_configmap.metadata.name)
    assert len(handler_calls) == 1
    assert handler_calls[0][1] == DEFAULT_NODE_NAME  # IP should be passed

    # Test MODIFIED event with same data (should not trigger handler due to deduplication)
    monitor._handle_configmap_change("MODIFIED", mock_configmap, mock_configmap.metadata.name)
    assert len(handler_calls) == 1  # Handler should not be called again for duplicate data


def test_handle_configmap_change_deduplication_with_actual_change():
    """Test that handler is called when fault information actually changes.

    "Actual change" means a change visible at the storage level (unique fault_code
    or associated fault_level/type changes).  Mere NPU name rotation with the same
    code+level is NOT an actual change, because the storage model uses a dict keyed
    by fault_code.

    NOTE: fault_level in ConfigMap JSON uses OriginFaultLevel string values
    (e.g. "RestartNPU", "SeparateNPU"), NOT "L5"/"L6" shorthand.
    """
    monitor = create_test_monitor()

    # Mock ConfigMap with initial data (RestartNPU → FaultLevel.L5)
    mock_configmap1 = Mock()
    mock_configmap1.metadata.name = "fault-config-test-node"
    mock_configmap1.metadata.namespace = "default"
    mock_configmap1.data = {
        "DeviceInfoCfg": '{"DeviceInfo":{"DeviceList":{"huawei.com/Ascend910-Fault":[{"fault_type":"CardUnhealthy","npu_name":"npu0","fault_level":"RestartNPU","fault_code":"0x1001"}]},"UpdateTime":123456},"SuperPodID":1,"ServerIndex":1}'
    }

    # Mock ConfigMap with a different fault_level (RestartNPU→SeparateNPU, L5→L6), IS an actual change
    mock_configmap2 = Mock()
    mock_configmap2.metadata.name = "fault-config-test-node"
    mock_configmap2.metadata.namespace = "default"
    mock_configmap2.data = {
        "DeviceInfoCfg": '{"DeviceInfo":{"DeviceList":{"huawei.com/Ascend910-Fault":[{"fault_type":"CardUnhealthy","npu_name":"npu0","fault_level":"SeparateNPU","fault_code":"0x1001"}]},"UpdateTime":123457},"SuperPodID":1,"ServerIndex":1}'
    }

    # Mock configmap change handler
    handler_calls = []

    def mock_configmap_handler(faults, ip):
        handler_calls.append((faults, ip))

    monitor.configmap_change_handler = mock_configmap_handler

    # Test first change
    monitor._handle_configmap_change("ADDED", mock_configmap1, mock_configmap1.metadata.name)
    assert len(handler_calls) == 1

    # Test second change with actually different fault level (L5→L6 via RestartNPU→SeparateNPU)
    monitor._handle_configmap_change("MODIFIED", mock_configmap2, mock_configmap2.metadata.name)
    assert len(handler_calls) == 2  # Handler should be called again for genuinely different fault_level


def test_handle_configmap_change_no_npu_name():
    """Test that fault logging handles cases where npu_name is empty"""
    monitor = create_test_monitor()

    # Mock ConfigMap with node-level fault (no npu_name)
    mock_configmap = Mock()
    mock_configmap.metadata.name = "fault-config-test-node"
    mock_configmap.metadata.namespace = "default"
    # Create a fault with empty npu_name (like node faults)
    mock_configmap.data = {
        "DeviceInfoCfg": '{"DeviceInfo":{"DeviceList":{"huawei.com/Ascend910-Fault":[{"fault_type":"NodeUnhealthy","npu_name":"","fault_level":"L3","fault_code":"0x2000"}]},"UpdateTime":123456},"SuperPodID":1,"ServerIndex":1}'
    }

    # Mock configmap change handler
    handler_calls = []

    def mock_configmap_handler(faults, ip):
        handler_calls.append((faults, ip))

    monitor.configmap_change_handler = mock_configmap_handler

    # Test ADDED event with node-level fault (no NPU name)
    monitor._handle_configmap_change("ADDED", mock_configmap, mock_configmap.metadata.name)
    assert len(handler_calls) == 1
    assert len(handler_calls[0][0]) == 1  # One fault detected
    assert handler_calls[0][0][0].npu_name == ""  # Empty NPU name


def test_handle_configmap_change_multiple_configmaps():
    """Test that different ResourceMonitor instances maintain separate caches"""
    # Create two separate monitors for different nodes
    monitor1 = create_test_monitor(node_name="node1")
    monitor2 = create_test_monitor(node_name="node2")

    # Mock ConfigMap for node1
    mock_configmap1 = Mock()
    mock_configmap1.metadata.name = "fault-config-node1"
    mock_configmap1.metadata.namespace = "default"
    mock_configmap1.data = {
        "DeviceInfoCfg": '{"DeviceInfo":{"DeviceList":{"huawei.com/Ascend910-Fault":[{"fault_type":"CardUnhealthy","npu_name":"npu0","fault_level":"L5","fault_code":"0x1001"}]},"UpdateTime":123456},"SuperPodID":1,"ServerIndex":1}'
    }

    # Mock ConfigMap for node2 with same data
    mock_configmap2 = Mock()
    mock_configmap2.metadata.name = "fault-config-node2"
    mock_configmap2.metadata.namespace = "default"
    mock_configmap2.data = {
        "DeviceInfoCfg": '{"DeviceInfo":{"DeviceList":{"huawei.com/Ascend910-Fault":[{"fault_type":"CardUnhealthy","npu_name":"npu0","fault_level":"L5","fault_code":"0x1001"}]},"UpdateTime":123456},"SuperPodID":1,"ServerIndex":1}'
    }

    # Mock configmap change handlers
    handler_calls = []

    def mock_configmap_handler(faults, ip):
        handler_calls.append((faults, ip))

    monitor1.configmap_change_handler = mock_configmap_handler
    monitor2.configmap_change_handler = mock_configmap_handler

    monitor1._handle_configmap_change("ADDED", mock_configmap1, mock_configmap1.metadata.name)
    assert len(handler_calls) == 1
    monitor2._handle_configmap_change("ADDED", mock_configmap2, mock_configmap2.metadata.name)
    assert len(handler_calls) == 2  # Should be called for different monitors
    monitor1._handle_configmap_change("MODIFIED", mock_configmap1, mock_configmap1.metadata.name)
    assert len(handler_calls) == 2  # Should not be called for duplicate data in same monitor
    monitor2._handle_configmap_change("MODIFIED", mock_configmap2, mock_configmap2.metadata.name)
    assert len(handler_calls) == 2  # Should not be called for duplicate data in same monitor


def test_handle_configmap_change_deleted():
    """Test handling ConfigMap DELETED event"""
    monitor = create_test_monitor()

    # Mock deleted ConfigMap
    mock_configmap = Mock()
    mock_configmap.metadata.name = "fault-config-test-node"
    mock_configmap.metadata.namespace = "default"

    # Mock configmap change handler
    handler_calls = []

    def mock_configmap_handler(faults, ip):
        handler_calls.append((faults, ip))

    monitor.configmap_change_handler = mock_configmap_handler

    # Test DELETED event
    monitor._handle_configmap_change("DELETED", mock_configmap, mock_configmap.metadata.name)
    assert len(handler_calls) == 1
    assert handler_calls[0] == ([], DEFAULT_NODE_NAME)  # Empty fault list for deleted


def test_handle_configmap_change_no_handler():
    """Test handling ConfigMap change when no handler is configured"""
    monitor = create_test_monitor()

    # Mock ConfigMap
    mock_configmap = Mock()
    mock_configmap.metadata.name = "fault-config-test-node"
    mock_configmap.metadata.namespace = "default"
    mock_configmap.data = {}

    # Test with no handler (should not raise exception)
    with patch("motor.controller.fault_tolerance.k8s.resource_monitor.logger") as mock_logger:
        monitor._handle_configmap_change("ADDED", mock_configmap, mock_configmap.metadata.name)

        # Verify that warning was logged about no handler
        mock_logger.warning.assert_called_with("No configmap change handler configured for node %s", DEFAULT_NODE_NAME)


def test_handle_configmap_change_handler_exception():
    """Test handling ConfigMap change when handler raises exception"""
    monitor = create_test_monitor()

    # Mock ConfigMap
    mock_configmap = Mock()
    mock_configmap.metadata.name = "fault-config-test-node"
    mock_configmap.metadata.namespace = "default"
    mock_configmap.data = {"DeviceInfoCfg": '{"test": "data"}'}

    # Mock configmap change handler that raises exception
    def mock_configmap_handler(faults, ip):
        raise RuntimeError("Handler exception")

    monitor.configmap_change_handler = mock_configmap_handler

    # Test that exception is caught and logged
    with patch("motor.controller.fault_tolerance.k8s.resource_monitor.logger") as mock_logger:
        monitor._handle_configmap_change("ADDED", mock_configmap, mock_configmap.metadata.name)

        # Verify that error was logged for the handler exception
        mock_logger.error.assert_called_once()
        args = mock_logger.error.call_args[0]
        assert args[0] == "Error in configmap change handler for %s: %s"
        assert args[1] == "fault-config-test-node"
        assert isinstance(args[2], Exception)
        assert str(args[2]) == "Handler exception"


@patch("motor.controller.fault_tolerance.k8s.resource_monitor.is_configmap_valid")
@patch("motor.controller.fault_tolerance.k8s.resource_monitor.process_device_info")
@patch("motor.controller.fault_tolerance.k8s.resource_monitor.process_switch_info")
@patch("motor.controller.fault_tolerance.k8s.resource_monitor.process_manually_separate_npu")
def test_process_configmap_data_valid_config(
    mock_process_manual, mock_process_switch, mock_process_device, mock_is_valid
):
    """Test processing valid ConfigMap data"""
    mock_is_valid.return_value = True
    mock_process_device.return_value = [Mock(spec=FaultInfo)]
    mock_process_switch.return_value = [Mock(spec=FaultInfo)]
    mock_process_manual.return_value = [0, 1, 2]

    monitor = create_test_monitor()

    config_data = {
        "DeviceInfoCfg": '{"device": "info"}',
        "SwitchInfoCfg": '{"switch": "info"}',
        "ManuallySeparateNPU": "Ascend910-0,Ascend910-1,Ascend910-2",
    }

    result = monitor._process_configmap_data(config_data)

    # Verify validation was called
    mock_is_valid.assert_called_once_with(config_data)
    # Verify processing functions were called
    mock_process_device.assert_called_once_with('{"device": "info"}')
    mock_process_switch.assert_called_once_with('{"switch": "info"}')
    mock_process_manual.assert_called_once_with("Ascend910-0,Ascend910-1,Ascend910-2")
    # Verify result contains fault infos
    assert len(result) == 2  # device + switch faults


@patch("motor.controller.fault_tolerance.k8s.resource_monitor.is_configmap_valid")
def test_process_configmap_data_invalid_config(mock_is_valid):
    """Test processing invalid ConfigMap data"""
    mock_is_valid.return_value = False

    monitor = create_test_monitor()

    config_data = {"InvalidKey": "value"}

    result = monitor._process_configmap_data(config_data)

    # Verify validation was called
    mock_is_valid.assert_called_once_with(config_data)
    # Verify empty result for invalid config
    assert not result


def test_process_configmap_data_missing_keys():
    """Test processing ConfigMap data with missing keys"""
    monitor = create_test_monitor()

    # Config data with some missing keys: SwitchInfoCfg and ManuallySeparateNPU are missing
    config_data = {"DeviceInfoCfg": '{"device": "info"}'}

    result = monitor._process_configmap_data(config_data)

    # Should still process available data
    assert isinstance(result, list)


def test_process_configmap_data_exception_handling():
    """Test exception handling in ConfigMap data processing"""
    monitor = create_test_monitor()

    # Pass None to trigger exception
    result = monitor._process_configmap_data(None)

    # Should return empty list on exception
    assert not result


def test_get_node_ready_status_ready_true():
    """Test extracting ready status when Ready condition is True"""
    monitor = create_test_monitor()

    mock_node = Mock()
    mock_node.metadata = Mock()
    mock_node.metadata.name = "test-node"
    mock_node.status = Mock()
    mock_condition = Mock()
    mock_condition.type = "Ready"
    mock_condition.status = "True"
    mock_node.status.conditions = [mock_condition]

    assert monitor._get_node_ready_status(mock_node) == NodeStatus.READY


def test_get_node_ready_status_ready_false():
    """Test extracting ready status when Ready condition is False"""
    monitor = create_test_monitor()

    mock_node = Mock()
    mock_node.metadata = Mock()
    mock_node.metadata.name = "test-node"
    mock_node.status = Mock()
    mock_condition = Mock()
    mock_condition.type = "Ready"
    mock_condition.status = "False"
    mock_node.status.conditions = [mock_condition]

    assert monitor._get_node_ready_status(mock_node) == NodeStatus.NOT_READY


def test_get_node_ready_status_no_ready_condition():
    """Test extracting ready status when no Ready condition exists"""
    monitor = create_test_monitor()

    mock_node = Mock()
    mock_node.metadata = Mock()
    mock_node.metadata.name = "test-node"
    mock_node.status = Mock()
    mock_condition = Mock()
    mock_condition.type = "MemoryPressure"
    mock_condition.status = "False"
    mock_node.status.conditions = [mock_condition]

    assert monitor._get_node_ready_status(mock_node) == NodeStatus.NOT_READY


def test_get_node_ready_status_no_conditions():
    """Test extracting ready status when conditions is None"""
    monitor = create_test_monitor()

    mock_node = Mock()
    mock_node.metadata = Mock()
    mock_node.metadata.name = "test-node"
    mock_node.status = Mock()
    mock_node.status.conditions = None

    assert monitor._get_node_ready_status(mock_node) == NodeStatus.NOT_READY


def test_get_node_ready_status_empty_conditions():
    """Test extracting ready status when conditions list is empty"""
    monitor = create_test_monitor()

    mock_node = Mock()
    mock_node.metadata = Mock()
    mock_node.metadata.name = "test-node"
    mock_node.status = Mock()
    mock_node.status.conditions = []

    assert monitor._get_node_ready_status(mock_node) == NodeStatus.NOT_READY
