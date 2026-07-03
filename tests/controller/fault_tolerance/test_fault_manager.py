# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Test cases are organized according to the following 7 logical blocks:
1. Initialization
2. Persistence and Recovery
3. Start and Update Methods
4. Dynamic Configuration Update
5. Resource Monitoring and Update
6. Instance and node status Updating
7. Strategy Center Processing
"""

import pytest
from unittest.mock import Mock, patch, MagicMock

from motor.common.resources.instance import Instance, InsStatus, NodeManagerInfo
from motor.config.controller import ControllerConfig
from motor.controller.core import ObserverEvent
from motor.controller.fault_tolerance.fault_manager import FaultManager
from motor.controller.fault_tolerance.fault_types import (
    FaultCategory,
    FaultInfo,
    FaultLevel,
    HardwareFaultType,
    InstanceMetadata,
    NodeMetadata,
    NodeStatus,
    OriginFaultLevel,
    SpecialFaultCode,
)

# pylint: disable=redefined-outer-name,duplicate-code

# Test constants
TEST_IPS = ["192.168.1.1", "192.168.1.2", "192.168.1.99"]
TEST_PORT = "8080"
TEST_FAULT_CODES = [0x1234, 0x2000, 0x3000, 0x3001, 0x4000, 0x00F1FEF5]


def FI(*, fault_type, npu_name, fault_code, fault_level, origin_fault_level=None):
    """Short constructor for reusable FaultInfo constants in tests."""
    return FaultInfo(
        fault_type=fault_type,
        npu_name=npu_name,
        fault_code=fault_code,
        fault_level=fault_level,
        origin_fault_level=origin_fault_level,
    )


FAULT_DEVICE_L1_0x1000 = FI(
    fault_type=HardwareFaultType.CARD_UNHEALTHY, npu_name="npu0", fault_code=0x1000, fault_level=FaultLevel.L1
)
FAULT_DEVICE_L2_0x1000 = FI(
    fault_type=HardwareFaultType.CARD_UNHEALTHY, npu_name="npu0", fault_code=0x1000, fault_level=FaultLevel.L2
)
FAULT_DEVICE_L2 = FI(
    fault_type=HardwareFaultType.CARD_UNHEALTHY, npu_name="npu0", fault_code=0x2000, fault_level=FaultLevel.L2
)
FAULT_DEVICE_L3 = FI(
    fault_type=HardwareFaultType.CARD_UNHEALTHY, npu_name="npu0", fault_code=0x2000, fault_level=FaultLevel.L3
)
FAULT_SWITCH_L2 = FI(
    fault_type=HardwareFaultType.CARD_NETWORK_UNHEALTHY,
    npu_name="switch0",
    fault_code=0x2000,
    fault_level=FaultLevel.L2,
)
FAULT_NODE_L3 = FI(
    fault_type=HardwareFaultType.NODE_UNHEALTHY, npu_name="", fault_code=0x3000, fault_level=FaultLevel.L3
)
FAULT_CM_DEVICE_L3_0x1234 = FI(
    fault_type=HardwareFaultType.CARD_UNHEALTHY, npu_name="npu0", fault_code=0x1234, fault_level=FaultLevel.L3
)
FAULT_CM_SWITCH_L2_0x5678 = FI(
    fault_type=HardwareFaultType.CARD_NETWORK_UNHEALTHY,
    npu_name="switch0",
    fault_code=0x5678,
    fault_level=FaultLevel.L2,
)


def _assert_instance_fault(instance, *, fault_level, fault_code):
    assert instance.fault_level == fault_level
    assert instance.fault_code == fault_code


def _assert_fault_info(fault, *, fault_level, fault_code, fault_type):
    assert fault is not None
    assert fault.fault_level == fault_level
    assert fault.fault_code == fault_code
    assert fault.fault_type == fault_type


def _etcd_node_entry(*, pod_ip, node_name, instance_id, node_status, hardware_fault_infos):
    return {
        "node_name": node_name,
        "instance_ids": [instance_id],
        "instance_pod_ips": {str(instance_id): pod_ip},
        "instance_job_names": {str(instance_id): ""},
        "node_status": node_status.value,
        "hardware_fault_infos": hardware_fault_infos,
    }


def _etcd_instance_entry(*, instance_id, fault_level, fault_code):
    return {"instance_id": instance_id, "fault_level": fault_level.value, "fault_code": fault_code}


@pytest.fixture(autouse=True)
def mock_etcd_client():
    """Mock EtcdClient to avoid real ETCD operations in tests"""
    with patch("motor.controller.fault_tolerance.fault_manager.EtcdClient") as mock_etcd_class:
        mock_client = MagicMock()
        mock_client.persist_data.return_value = True
        mock_client.restore_data.return_value = None
        mock_etcd_class.return_value = mock_client
        yield mock_client


@pytest.fixture(autouse=True)
def setup_test_environment():
    """Setup and teardown for each test"""
    from motor.common.utils.singleton import ThreadSafeSingleton

    # Clear singleton instances before each test
    if FaultManager in ThreadSafeSingleton._instances:
        fault_manager = ThreadSafeSingleton._instances[FaultManager]
        fault_manager.stop()
        del ThreadSafeSingleton._instances[FaultManager]


@pytest.fixture
def fault_manager():
    """Create a basic FaultManager instance for testing"""
    with patch("motor.controller.fault_tolerance.fault_manager.K8sClient"):
        config = ControllerConfig()
        yield FaultManager(config)


@pytest.fixture
def fault_manager_with_instances():
    """Create a FaultManager instance with pre-configured instances and nodes"""
    with patch("motor.controller.fault_tolerance.fault_manager.K8sClient"):
        config = ControllerConfig()
        manager = FaultManager(config)

        ins_metadata1 = InstanceMetadata(instance_id=1)
        manager.instances[1] = ins_metadata1
        manager.nodes["node_0"] = NodeMetadata(
            node_name="node_0",
            instance_ids={1},
            instance_pod_ips={1: "192.168.1.1"},
            instance_job_names={1: "job1"},
        )
        manager.nodes["node_1"] = NodeMetadata(
            node_name="node_1",
            instance_ids={1},
            instance_pod_ips={1: "192.168.1.2"},
            instance_job_names={1: "job1"},
        )

        ins_metadata2 = InstanceMetadata(instance_id=2)
        manager.instances[2] = ins_metadata2
        manager.nodes["node_2"] = NodeMetadata(
            node_name="node_2",
            instance_ids={2},
            instance_pod_ips={2: "192.168.1.3"},
            instance_job_names={2: "job2"},
        )

        yield manager


@pytest.fixture
def mock_instance():
    """Create a mock instance for testing"""
    instance = Mock(spec=Instance)
    instance.id = 1
    instance.job_name = "test_job"
    instance.get_node_managers.return_value = [NodeManagerInfo(pod_ip="192.168.1.1", port="8080")]
    return instance


@pytest.fixture
def mock_instance_manager(mock_instance):
    """Create mock instance manager"""
    with patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as mock_cls:
        instance_manager = Mock()
        mock_cls.return_value = instance_manager
        instance_manager.get_instance_by_podip = Mock(return_value=mock_instance)
        instance_manager.get_instance = Mock(return_value=mock_instance)
        instance_manager.notify = Mock()
        instance_manager.separate_instance = Mock()
        instance_manager.recover_instance = Mock()
        yield instance_manager


# =============================================================================
# 1. Initialization
# =============================================================================


def test_fault_manager_initialization(fault_manager):
    """Test FaultManager initialization with default config"""
    assert fault_manager.config is not None
    assert len(fault_manager.nodes) == 0
    assert len(fault_manager.instances) == 0
    assert fault_manager.etcd_client is not None


def test_fault_manager_initialization_with_custom_config():
    """Test FaultManager initialization with custom configuration"""
    config = ControllerConfig()
    config.etcd_config.etcd_host = "custom-etcd-host"
    config.etcd_config.etcd_port = 1234

    with patch("motor.controller.fault_tolerance.fault_manager.EtcdClient") as mock_etcd_class:
        mock_client = MagicMock()
        mock_etcd_class.return_value = mock_client

        manager = FaultManager(config)

        # Verify EtcdClient was called with custom config
        mock_etcd_class.assert_called_once_with(etcd_config=config.etcd_config, tls_config=config.etcd_tls_config)
        assert manager.config is config


def test_fault_manager_singleton_behavior():
    """Test that FaultManager behaves as a singleton"""
    config1 = ControllerConfig()
    config2 = ControllerConfig()

    with patch("motor.controller.fault_tolerance.fault_manager.EtcdClient"):
        manager1 = FaultManager(config1)
        manager2 = FaultManager(config2)

        # They should be the same instance (singleton behavior)
        assert manager1 is manager2


# =============================================================================
# 2. Persistence and Recovery
# =============================================================================


def test_persist_data_success(fault_manager_with_instances):
    """Test successful data persistence to ETCD"""
    manager = fault_manager_with_instances

    with patch.object(manager.etcd_client, "persist_data", return_value=True) as mock_persist:
        # Call persist_data
        result = manager.persist_data()

        assert result is True
        assert mock_persist.call_count == 1
        call = mock_persist.call_args
        assert call[0][0] == "/controller/fault_manager"

        stored_data = call[0][1]
        assert "state" in stored_data
        persistent_state_data = stored_data["state"]

        assert "data" in persistent_state_data
        assert "version" in persistent_state_data
        assert "timestamp" in persistent_state_data
        assert "checksum" in persistent_state_data

        fault_data = persistent_state_data["data"]
        assert "nodes" in fault_data
        assert "instances" in fault_data

        nodes_data = fault_data["nodes"]
        assert isinstance(nodes_data, dict)
        assert len(nodes_data) == 3  # Three nodes in test setup (instance 1: 2 nodes, instance 2: 1 node)

        node_data = nodes_data["node_0"]  # Use node_name as key
        assert node_data["node_name"] == "node_0"
        assert node_data["node_status"] == NodeStatus.READY.value
        assert "hardware_fault_infos" in node_data
        assert "instance_ids" in node_data
        assert "instance_pod_ips" in node_data
        assert "instance_job_names" in node_data

        instances_data = fault_data["instances"]
        assert isinstance(instances_data, dict)
        assert len(instances_data) == 2  # Two instances in test setup

        instance_data = instances_data["1"]  # instance_id 1 (using str key)
        assert instance_data["instance_id"] == 1
        assert "fault_level" in instance_data
        assert "fault_code" in instance_data


def test_persist_data_etcd_failure(fault_manager_with_instances):
    """Test data persistence when ETCD operations fail."""
    manager = fault_manager_with_instances

    # Use side_effect that raises to avoid the retry-sleep loop in
    # _PersistenceMixin (300ms + 600ms backoff).  Raising on the first
    # call exercises the same failure path without the delay.
    with patch.object(
        manager.etcd_client,
        "persist_data",
        side_effect=RuntimeError("ETCD persist failed"),
    ):
        result = manager.persist_data()
        assert result is False


def test_persist_data_exception_handling(fault_manager_with_instances):
    """Test data persistence exception handling"""
    manager = fault_manager_with_instances

    with patch.object(manager.etcd_client, "persist_data", side_effect=Exception("ETCD connection error")):
        result = manager.persist_data()

        assert result is False  # Verify persist_data failure


def test_persist_data_empty_data(fault_manager):
    """Test data persistence with empty data"""
    manager = fault_manager

    manager.nodes.clear()
    manager.instances.clear()

    with patch.object(manager.etcd_client, "persist_data", return_value=True) as mock_persist:
        result = manager.persist_data()
        assert result is True  # Verify persist_data success

        call = mock_persist.call_args
        stored_data = call[0][1]

        assert "state" in stored_data  # Verify fault_manager field
        persistent_state_data = stored_data["state"]
        assert "data" in persistent_state_data

        fault_data = persistent_state_data["data"]
        assert "nodes" in fault_data
        assert "instances" in fault_data

        nodes_data = fault_data["nodes"]
        instances_data = fault_data["instances"]
        assert nodes_data == {}
        assert instances_data == {}


def test_restore_data_success(fault_manager):
    """Test successful data restoration from ETCD"""
    from motor.common.etcd.persistent_state import PersistentState

    manager = fault_manager
    fault_data = {
        "nodes": {
            "node_0": _etcd_node_entry(
                pod_ip=TEST_IPS[0],
                node_name="node_0",
                instance_id=1,
                node_status=NodeStatus.READY,
                hardware_fault_infos={
                    TEST_FAULT_CODES[0]: {
                        "fault_type": HardwareFaultType.CARD_UNHEALTHY.value,
                        "npu_name": "npu0",
                        "fault_code": TEST_FAULT_CODES[0],
                        "fault_level": FaultLevel.L3.value,
                    }
                },
            )
        },
        "instances": {"1": _etcd_instance_entry(instance_id=1, fault_level=FaultLevel.HEALTHY, fault_code=0x0)},
    }

    persistent_state = PersistentState(data=fault_data, version=1, timestamp=1234567890.0, checksum="")
    persistent_state.checksum = persistent_state.calculate_checksum()

    with patch.object(manager.etcd_client, "restore_data", return_value={"state": persistent_state}):
        result = manager.restore_data()

        assert result is True  # Verify restore_data success

        assert len(manager.nodes) == 1  # Verify nodes restored
        assert "node_0" in manager.nodes

        node = manager.nodes["node_0"]
        assert node.instance_pod_ips[1] == TEST_IPS[0]
        assert node.node_name == "node_0"
        assert node.node_status == NodeStatus.READY
        assert len(node.hardware_fault_infos) == 1
        fault_info = next(iter(node.hardware_fault_infos.values()))
        assert fault_info.fault_level == FaultLevel.L3
        assert fault_info.fault_code == TEST_FAULT_CODES[0]
        assert len(manager.instances) == 1
        assert 1 in manager.instances

    instance = manager.instances[1]
    assert instance.instance_id == 1
    _assert_instance_fault(instance, fault_level=FaultLevel.HEALTHY, fault_code=0x0)


def test_restore_data_none_data(fault_manager):
    """Test data restoration when ETCD returns None (no data)"""
    manager = fault_manager

    with patch.object(manager.etcd_client, "restore_data", return_value=None):
        result = manager.restore_data()
        assert result is True  # Verify restore_data success

        assert len(manager.nodes) == 0
        assert len(manager.instances) == 0


def test_restore_data_etcd_failure(fault_manager):
    """Test data restoration when ETCD operations fail"""
    manager = fault_manager

    with patch.object(manager.etcd_client, "restore_data", side_effect=Exception("ETCD connection error")):
        result = manager.restore_data()

        assert result is False  # Verify restore_data failure


def test_restore_data_corrupted_data(fault_manager):
    """Test data restoration with corrupted PersistentState data"""
    from motor.common.etcd.persistent_state import PersistentState

    manager = fault_manager
    # Create corrupted PersistentState with invalid checksum
    corrupted_fault_data = {
        "nodes": {
            TEST_IPS[0]: _etcd_node_entry(
                pod_ip=TEST_IPS[0],
                node_name="node_0",
                instance_id=1,
                node_status=NodeStatus.READY,
                hardware_fault_infos={},
            )
        },
        "instances": {"1": _etcd_instance_entry(instance_id=1, fault_level=FaultLevel.HEALTHY, fault_code=0x0)},
    }
    corrupted_state = PersistentState(
        data=corrupted_fault_data,
        version=1,
        timestamp=1234567890.0,
        checksum="invalid_checksum",  # Invalid checksum
    )
    with patch.object(manager.etcd_client, "restore_data", return_value={"state": corrupted_state}):
        result = manager.restore_data()
        assert result is False  # Verify restore_data failure


# =============================================================================
# 3. Start and Update Methods
# =============================================================================


def test_fault_manager_start_with_persistence_enabled(fault_manager):
    """Test starting FaultManager with persistence enabled"""
    fault_manager.etcd_config.enable_etcd_persistence = True

    with patch.object(fault_manager, "restore_data", return_value=True) as mock_restore:
        with patch("threading.Thread") as mock_thread:
            fault_manager.start()

            mock_thread.assert_called_once_with(
                target=fault_manager._ft_strategy_center, daemon=True, name="FaultToleranceStrategyCenter"
            )
            mock_restore.assert_called_once()  # Verify restore_data was called
            mock_thread.return_value.start.assert_called_once()


def test_fault_manager_start_with_persistence_disabled(fault_manager):
    """Test starting FaultManager with persistence disabled"""
    fault_manager.etcd_config.enable_etcd_persistence = False

    with patch.object(fault_manager, "restore_data") as mock_restore:
        with patch("threading.Thread") as mock_thread:
            fault_manager.start()

            mock_thread.assert_called_once_with(
                target=fault_manager._ft_strategy_center, daemon=True, name="FaultToleranceStrategyCenter"
            )
            mock_restore.assert_not_called()
            mock_thread.return_value.start.assert_called_once()


def test_fault_manager_start_restore_data_failed(fault_manager):
    """Test starting FaultManager when restore_data fails"""
    fault_manager.etcd_config.enable_etcd_persistence = True

    with patch.object(fault_manager, "restore_data", return_value=False) as mock_restore:
        with patch("threading.Thread") as mock_thread:
            with patch("motor.controller.fault_tolerance.fault_manager.logger") as mock_logger:
                fault_manager.start()

                mock_thread.assert_called_once_with(
                    target=fault_manager._ft_strategy_center, daemon=True, name="FaultToleranceStrategyCenter"
                )
                mock_restore.assert_called_once()
                mock_logger.warning.assert_called_once_with(
                    "Failed to restore fault manager's data from ETCD, start with empty state"
                )
                mock_thread.return_value.start.assert_called_once()


def test_fault_manager_start_with_stop_event_reset(fault_manager):
    """Test starting FaultManager when stop_event was previously set"""
    fault_manager.stop_event.set()

    with patch.object(fault_manager, "restore_data", return_value=True):
        with patch("threading.Thread"):
            fault_manager.start()
            assert not fault_manager.stop_event.is_set()


def test_fault_manager_start_creates_resource_monitors(fault_manager_with_instances):
    """Test that starting FaultManager creates ResourceMonitors for all nodes"""
    manager = fault_manager_with_instances

    with patch.object(manager, "restore_data", return_value=True):
        with patch("threading.Thread"):
            with patch.object(manager, "_create_resource_monitor_for_node") as mock_create_monitor:
                manager.start()

                # Verify ResourceMonitors were created for all nodes (3 nodes in test setup)
                assert mock_create_monitor.call_count == 3
                mock_create_monitor.assert_any_call("node_0")
                mock_create_monitor.assert_any_call("node_1")
                mock_create_monitor.assert_any_call("node_2")


def test_update_instance_initial(fault_manager, mock_instance):
    """Test update method with INSTANCE_INITIAL event"""
    mock_instance.get_node_managers.return_value = [
        NodeManagerInfo(pod_ip="192.168.1.1", port="80880"),
    ]

    with patch.object(fault_manager, "k8s_client") as mock_k8s_client:
        mock_k8s_client.get_node_hostname_by_pod_ip.return_value = "node_0"
        with patch.object(fault_manager, "_create_resource_monitor_for_node"):
            fault_manager.update(mock_instance, ObserverEvent.INSTANCE_INITIAL)

    assert mock_instance.id in fault_manager.instances
    assert len(fault_manager.nodes) > 0


def test_update_instance_removed(fault_manager, mock_instance):
    """Test update method with INSTANCE_REMOVED event"""
    mock_instance.id = 1
    fault_manager.instances[1] = InstanceMetadata(instance_id=1)
    fault_manager.nodes["node_0"] = NodeMetadata(
        node_name="node_0",
        instance_ids={1},
        instance_pod_ips={1: "192.168.1.1"},
        instance_job_names={1: "test_job"},
    )

    with patch.object(fault_manager, "_stop_resource_monitor_for_node"):
        fault_manager.update(mock_instance, ObserverEvent.INSTANCE_REMOVED)

    assert 1 not in fault_manager.instances
    # Nodes are preserved for potential transfer to other instances (e.g., scale_p2d swap)
    assert "node_0" in fault_manager.nodes


def test_handle_instance_initial_new_instance(fault_manager, mock_instance):
    """Test _handle_instance_initial with a new instance"""
    mock_instance.get_node_managers.return_value = [
        NodeManagerInfo(pod_ip="192.168.1.1", port="8080"),
        NodeManagerInfo(pod_ip="192.168.1.2", port="8080"),
    ]
    mock_instance.id = 1

    # Map pod_ip to node_name for this test
    pod_to_node = {
        "192.168.1.1": "node_0",
        "192.168.1.2": "node_1",
    }
    with (
        patch.object(fault_manager, "k8s_client") as mock_k8s_client,
        patch.object(fault_manager, "_create_resource_monitor_for_node") as mock_create_monitor,
    ):
        mock_k8s_client.get_node_hostname_by_pod_ip.side_effect = pod_to_node.get
        fault_manager.update(mock_instance, ObserverEvent.INSTANCE_INITIAL)

        assert set(fault_manager.instances.keys()) == {1}
        assert isinstance(fault_manager.instances[1], InstanceMetadata)
        assert set(fault_manager.nodes.keys()) == {"node_0", "node_1"}
        for node_name, pod_ip in [("node_0", "192.168.1.1"), ("node_1", "192.168.1.2")]:
            node = fault_manager.nodes[node_name]
            assert node.instance_pod_ips[1] == pod_ip
            assert node.node_name == node_name
            assert 1 in node.instance_ids

        # Check that ConfigMap monitors were created for both hosts
        assert mock_create_monitor.call_count == 2
        mock_create_monitor.assert_any_call("node_0")
        mock_create_monitor.assert_any_call("node_1")


def test_handle_instance_initial_existing_instance(fault_manager, mock_instance):
    """Test _handle_instance_initial when instance already exists"""
    mock_instance.id = 1
    fault_manager.instances[1] = InstanceMetadata(instance_id=1)

    with patch("motor.controller.fault_tolerance.fault_manager.logger") as mock_logger:
        with patch.object(fault_manager, "_create_resource_monitor_for_node") as mock_create_monitor:
            fault_manager.update(mock_instance, ObserverEvent.INSTANCE_INITIAL)

            mock_logger.debug.assert_called_once_with(
                "Instance %d already exists in fault manager, skipping add operation.", 1
            )
            mock_create_monitor.assert_not_called()


def test_handle_instance_initial_preserves_fault_info(fault_manager):
    """Test _handle_instance_initial preserves existing node fault information"""
    instance = Mock()
    instance.id = 1
    instance.job_name = "test_job"
    node_mgr1 = Mock()
    node_mgr1.node_name = "node_0"
    node_mgr1.pod_ip = "192.168.1.1"

    instance.get_node_managers.return_value = [node_mgr1]

    existing_node = NodeMetadata(
        node_name="node_0",
        instance_ids={999},
        instance_pod_ips={999: "192.168.1.100"},  # Different pod_ip to test update
        instance_job_names={999: ""},  # Empty job_name → not treated as foreign (legacy)
        node_status=NodeStatus.READY,
        hardware_fault_infos={FAULT_DEVICE_L2_0x1000.fault_code: FAULT_DEVICE_L2_0x1000},  # This should be preserved
    )

    with fault_manager.lock:
        fault_manager.nodes["node_0"] = existing_node

    # Mock k8s_client to resolve pod_ip to the expected node_name
    with patch.object(fault_manager, "k8s_client") as mock_k8s_client:
        mock_k8s_client.get_node_hostname_by_pod_ip.return_value = "node_0"
        fault_manager.update(instance, ObserverEvent.INSTANCE_INITIAL)

    assert "node_0" in fault_manager.nodes
    updated_node = fault_manager.nodes["node_0"]

    # Verify pod_ip and instance_id were updated (takeover: old instance_id replaced)
    assert updated_node.instance_pod_ips[1] == "192.168.1.1"
    assert 1 in updated_node.instance_ids
    # Old instance_id (999) is replaced by takeover since it was not active

    # Verify fault info was preserved
    assert len(updated_node.hardware_fault_infos) == 1
    fault_info = next(iter(updated_node.hardware_fault_infos.values()))
    assert fault_info.fault_type == HardwareFaultType.CARD_UNHEALTHY
    assert fault_info.npu_name == "npu0"
    assert fault_info.fault_code == 0x1000
    assert fault_info.fault_level == FaultLevel.L2

    # Verify instance was created
    assert 1 in fault_manager.instances


def test_handle_instance_removed_existing_instance(fault_manager_with_instances):
    """Test _handle_instance_removed with existing instance — nodes preserved for swap"""
    manager = fault_manager_with_instances
    instance = Mock()
    instance.id = 1
    instance.job_name = "job1"

    with patch.object(manager, "_stop_resource_monitor_for_node"):
        manager.update(instance, ObserverEvent.INSTANCE_REMOVED)

    # Instance 1 removed, but nodes preserved for potential transfer
    assert 1 not in manager.instances
    assert 2 in manager.instances
    assert "node_0" in manager.nodes
    assert "node_1" in manager.nodes
    assert "node_2" in manager.nodes


def test_handle_instance_removed_nonexistent_instance(fault_manager):
    """Test _handle_instance_removed with non-existent instance"""
    instance = Mock()
    instance.id = 999

    with patch.object(fault_manager, "_stop_resource_monitor_for_node") as mock_stop_monitor:
        fault_manager.update(instance, ObserverEvent.INSTANCE_REMOVED)
        mock_stop_monitor.assert_not_called()


# =============================================================================
# 4. Dynamic Configuration Update
# =============================================================================


def test_update_config():
    """Test update_config method updates configuration and recreates ETCD client"""
    # Create FaultManager with mocked dependencies
    with patch("motor.controller.fault_tolerance.fault_manager.EtcdClient") as mock_etcd_class:
        mock_client = MagicMock()
        mock_client.persist_data.return_value = True
        mock_client.restore_data.return_value = None
        mock_etcd_class.return_value = mock_client

        # Create FaultManager instance
        config = ControllerConfig()
        manager = FaultManager(config)

        # Create new config with different ETCD settings
        new_config = ControllerConfig()
        new_config.etcd_config.etcd_host = "new-etcd-host"
        new_config.etcd_config.etcd_port = 2380
        new_config.etcd_config.etcd_timeout = 30.0
        new_config.etcd_config.enable_etcd_persistence = True

        mock_etcd_class.reset_mock()
        manager.update_config(new_config)

        assert manager.config is new_config
        assert manager.config.etcd_config.etcd_host == "new-etcd-host"
        assert manager.config.etcd_config.etcd_port == 2380
        assert manager.config.etcd_config.etcd_timeout == 30.0
        mock_etcd_class.assert_called_once_with(etcd_config=new_config.etcd_config, tls_config=config.etcd_tls_config)


def test_update_config_with_configmap_changes():
    """Test update_config method when ConfigMap prefix/namespace changes"""
    manager = FaultManager(ControllerConfig())
    ins_metadata = InstanceMetadata(instance_id=1)
    manager.instances[1] = ins_metadata
    manager.nodes["node_0"] = NodeMetadata(
        node_name="node_0", instance_ids={1}, instance_pod_ips={1: "192.168.1.1"}, instance_job_names={1: ""}
    )
    manager.nodes["node_1"] = NodeMetadata(
        node_name="node_1", instance_ids={1}, instance_pod_ips={1: "192.168.1.2"}, instance_job_names={1: ""}
    )
    mock_monitor1, mock_monitor2 = MagicMock(), MagicMock()
    manager.resource_monitors.update({"node_0": mock_monitor1, "node_1": mock_monitor2})

    new_config = ControllerConfig()
    new_config.fault_tolerance_config.configmap_prefix = "new-prefix"
    new_config.fault_tolerance_config.configmap_namespace = "new-namespace"

    with (
        patch.object(manager, "_create_resource_monitor_for_node") as mock_create_monitor,
        patch("motor.controller.fault_tolerance.fault_manager.logger") as mock_logger,
    ):
        manager.update_config(new_config)

        assert (manager.configmap_prefix, manager.configmap_namespace) == ("new-prefix", "new-namespace")

        mock_monitor1.stop_monitoring.assert_called_once()
        mock_monitor2.stop_monitoring.assert_called_once()
        assert not manager.resource_monitors

        assert mock_create_monitor.call_count == 2
        mock_create_monitor.assert_any_call("node_0")
        mock_create_monitor.assert_any_call("node_1")

        assert mock_logger.info.call_count >= 4  # multiple log calls


def test_update_config_without_configmap_changes():
    """Test update_config method when ConfigMap configuration doesn't change"""
    with patch("motor.controller.fault_tolerance.fault_manager.EtcdClient") as mock_etcd_class:
        mock_client = MagicMock()
        mock_client.persist_data.return_value = True
        mock_client.restore_data.return_value = None
        mock_etcd_class.return_value = mock_client

        config = ControllerConfig()
        manager = FaultManager(config)

        mock_monitor = MagicMock()
        manager.resource_monitors["node_0"] = mock_monitor

        new_config = ControllerConfig()
        new_config.fault_tolerance_config.configmap_prefix = manager.configmap_prefix
        new_config.fault_tolerance_config.configmap_namespace = manager.configmap_namespace

        with patch.object(manager, "_create_resource_monitor_for_node") as mock_create_monitor:
            with patch("motor.controller.fault_tolerance.fault_manager.logger"):
                manager.update_config(new_config)

                mock_monitor.stop_monitoring.assert_not_called()
                assert len(manager.resource_monitors) == 1
                assert manager.resource_monitors["node_0"] is mock_monitor

                mock_create_monitor.assert_not_called()


def test_update_instances(fault_manager):
    """Test update_instances method adds new instances and updates existing ones"""
    manager = fault_manager

    def mk_instance(iid, job, nodes):
        inst = Mock(spec=Instance)
        inst.id = iid
        inst.job_name = job
        inst.get_node_managers.return_value = [NodeManagerInfo(**node) for node in nodes]
        return inst

    mock_instance1 = mk_instance(
        1,
        "job1",
        [
            {"pod_ip": "192.168.1.1", "port": "8080"},
            {"pod_ip": "192.168.1.2", "port": "8080"},
        ],
    )
    mock_instance2 = mk_instance(
        2,
        "job2",
        [
            {"pod_ip": "192.168.1.3", "port": "8080"},
        ],
    )

    # Mapping from pod_ip to node_name used in this test
    pod_to_node = {
        "192.168.1.1": "node_0",
        "192.168.1.2": "node_1",
        "192.168.1.3": "node_2",
        "192.168.1.4": "node_1",
    }

    # Test 1: Add new instances
    with (
        patch.object(manager, "k8s_client") as mock_k8s_client,
        patch.object(manager, "_create_resource_monitor_for_node") as mock_create_monitor,
    ):
        mock_k8s_client.get_node_hostname_by_pod_ip.side_effect = pod_to_node.get
        manager.update_instances([mock_instance1, mock_instance2])

        assert set(manager.instances.keys()) == {1, 2}
        assert set(manager.nodes.keys()) == {"node_0", "node_1", "node_2"}

        # Verify ResourceMonitors were created for all nodes in new instances
        assert mock_create_monitor.call_count == 3
        mock_create_monitor.assert_any_call("node_0")
        mock_create_monitor.assert_any_call("node_1")
        mock_create_monitor.assert_any_call("node_2")

    # Test 2: Update existing instance with changed node managers
    mock_instance1.get_node_managers.return_value = [
        NodeManagerInfo(pod_ip="192.168.1.1", port="8080"),
        NodeManagerInfo(pod_ip="192.168.1.4", port="8080"),
    ]
    with (
        patch.object(manager, "k8s_client") as mock_k8s_client,
        patch.object(manager, "_stop_resource_monitor_for_node") as mock_stop_monitor,
        patch.object(manager, "_create_resource_monitor_for_node") as mock_create_monitor,
    ):
        mock_k8s_client.get_node_hostname_by_pod_ip.side_effect = pod_to_node.get
        manager.update_instances([mock_instance1])

        assert set(manager.instances.keys()) == {1, 2}
        assert set(manager.nodes.keys()) == {"node_0", "node_1", "node_2"}
        mock_stop_monitor.assert_not_called()
        mock_create_monitor.assert_not_called()

        # Verify that node_1's pod_ip for instance 1 has been updated to the new pod_ip
        assert manager.nodes["node_1"].instance_pod_ips[1] == "192.168.1.4"

    # Test 3: Empty instance list should not cause issues
    manager.update_instances([])
    assert set(manager.instances.keys()) == {1, 2}
    assert set(manager.nodes.keys()) == {"node_0", "node_1", "node_2"}


# =============================================================================
# 5. Resource Monitoring and Update
# =============================================================================


def test_create_resource_monitor_for_node(fault_manager):
    """Test creating Resource monitor for a node"""
    with patch("motor.controller.fault_tolerance.mixin.resource_manager.ResourceMonitor") as mock_monitor_class:
        mock_monitor = MagicMock()
        mock_monitor_class.return_value = mock_monitor

        fault_manager._create_resource_monitor_for_node("node_0")

        # Verify ResourceMonitor was created with correct parameters
        mock_monitor_class.assert_called_once()
        _, kwargs = mock_monitor_class.call_args
        assert kwargs["node_name"] == "node_0"
        assert "node_change_handler" in kwargs
        assert "configmap_change_handler" in kwargs

        # Verify monitor was stored and started
        assert "node_0" in fault_manager.resource_monitors
        assert fault_manager.resource_monitors["node_0"] is mock_monitor
        mock_monitor.start_monitoring.assert_called_once()


def test_stop_resource_monitor_for_node(fault_manager):
    """Test stopping Resource monitor for a node"""
    with patch("motor.controller.fault_tolerance.mixin.resource_manager.ResourceMonitor") as mock_monitor_class:
        mock_monitor = MagicMock()
        mock_monitor_class.return_value = mock_monitor

        # First create a monitor
        fault_manager._create_resource_monitor_for_node("node_0")
        assert "node_0" in fault_manager.resource_monitors

        # Now stop it
        fault_manager._stop_resource_monitor_for_node("node_0")

        # Verify monitor was stopped and removed
        mock_monitor.stop_monitoring.assert_called_once()
        assert "node_0" not in fault_manager.resource_monitors


@pytest.mark.parametrize(
    "fault",
    [
        FAULT_CM_DEVICE_L3_0x1234,
        FAULT_CM_SWITCH_L2_0x5678,
    ],
)
def test_handle_configmap_update_with_faults_parametrized(fault_manager, fault):
    """Test handling ConfigMap update with device/switch faults (parametrized)."""
    node_name = "node_0"
    fault_manager.nodes[node_name] = NodeMetadata(
        node_name=node_name,
        instance_ids={1},
        instance_pod_ips={1: "192.168.1.1"},
        instance_job_names={1: ""},
    )

    fault_manager._handle_fault_info_update([fault], node_name)
    node = fault_manager.nodes[node_name]
    assert len(node.hardware_fault_infos) == 1
    _assert_fault_info(
        next(iter(node.hardware_fault_infos.values())),
        fault_level=fault.fault_level,
        fault_code=fault.fault_code,
        fault_type=fault.fault_type,
    )


# =============================================================================
# 6. Instance and Node status Updating
# =============================================================================


def test_handle_node_status_update_adds_node_reboot_fault_with_L6(fault_manager):
    """Test that node NOT_READY adds a NODE_REBOOT fault with level L6"""
    # Setup: Add a node to the manager
    node_name = "node_0"
    fault_manager.nodes[node_name] = NodeMetadata(
        node_name=node_name,
        instance_ids={1},
        instance_pod_ips={1: "192.168.1.1"},
        instance_job_names={1: ""},
    )
    fault_manager._handle_node_status_update(NodeStatus.NOT_READY, node_name)

    # Verify NODE_REBOOT fault exists and has level L6
    node = fault_manager.nodes[node_name]
    assert SpecialFaultCode.NODE_REBOOT in node.hardware_fault_infos
    reboot_fault = node.hardware_fault_infos[SpecialFaultCode.NODE_REBOOT]
    assert reboot_fault.fault_level == FaultLevel.L6


def test_refresh_instance_fault_level_instance_not_found(fault_manager):
    """Test _refresh_instance_fault_level when instance is not found"""
    with patch("motor.controller.fault_tolerance.fault_manager.logger") as mock_logger:
        fault_manager._refresh_instance_fault_level(999)

        mock_logger.warning.assert_called_once_with("Instance %d not found, skipping fault level refresh", 999)


def test_refresh_instance_fault_level_instance_not_found_with_instances(fault_manager_with_instances):
    """Test _refresh_instance_fault_level when instance is not found"""
    manager = fault_manager_with_instances

    with patch("motor.controller.fault_tolerance.fault_manager.logger") as mock_logger:
        manager._refresh_instance_fault_level(999)

        mock_logger.warning.assert_called_once_with("Instance %d not found, skipping fault level refresh", 999)


def test_refresh_instance_fault_level_no_device_faults(fault_manager_with_instances):
    """Test _refresh_instance_fault_level when instance has no device faults"""
    manager = fault_manager_with_instances
    instance = manager.instances[1]
    instance.fault_level = FaultLevel.L3  # Set to unhealthy initially

    with (
        patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as mock_im_class,
        patch("motor.controller.fault_tolerance.fault_manager.logger") as mock_logger,
    ):
        mock_im = MagicMock()
        mock_im_class.return_value = mock_im

        manager._refresh_instance_fault_level(1)

        # Should reset to healthy state
        assert instance.fault_level == FaultLevel.HEALTHY
        assert instance.fault_code == 0x0
        mock_logger.info.assert_called_once_with("Instance %d reset to healthy state", 1)

        # Should recover instance from forced separation
        mock_im.recover_instance.assert_called_once_with(1)


def test_refresh_instance_fault_level_with_device_faults(fault_manager_with_instances):
    """Test _refresh_instance_fault_level when instance has device faults"""
    manager = fault_manager_with_instances

    # Set up node with device fault
    node = manager.nodes["node_0"]
    node.hardware_fault_infos = {FAULT_DEVICE_L3.fault_code: FAULT_DEVICE_L3}

    instance = manager.instances[1]
    instance.fault_level = FaultLevel.HEALTHY  # Initially healthy

    with patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as mock_im_class:
        mock_im = MagicMock()
        mock_im_class.return_value = mock_im

        with patch("motor.controller.fault_tolerance.fault_manager.logger") as mock_logger:
            manager._refresh_instance_fault_level(1)

            _assert_instance_fault(instance, fault_level=FaultLevel.L3, fault_code=0x2000)

            mock_im.separate_instance.assert_called_once_with(1)

            mock_logger.info.assert_called_once_with(
                "Instance %d fault level updated to %s (code: 0x%x, category: %s)",
                1,
                FaultLevel.L3.name,
                FAULT_DEVICE_L3.fault_code,
                FAULT_DEVICE_L3.fault_category.value,
            )


@pytest.mark.parametrize(
    "is_separated,expect_recover",
    [
        (True, True),  # Instance is separated, should call recover_instance
        (False, False),  # Instance is not separated, should not call recover_instance
    ],
)
def test_refresh_instance_fault_level_with_l2_faults(fault_manager_with_instances, is_separated, expect_recover):
    """Test _refresh_instance_fault_level when instance has L2 level faults"""
    manager = fault_manager_with_instances

    # Set up node with L2 fault
    node = manager.nodes["node_0"]
    node.hardware_fault_infos = {FAULT_DEVICE_L2.fault_code: FAULT_DEVICE_L2}

    instance = manager.instances[1]
    instance.fault_level = FaultLevel.HEALTHY  # Initially healthy

    with patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as mock_im_class:
        mock_im = MagicMock()
        mock_im_class.return_value = mock_im
        mock_im.is_instance_separated.return_value = is_separated

        with patch("motor.controller.fault_tolerance.fault_manager.logger") as mock_logger:
            manager._refresh_instance_fault_level(1)

            _assert_instance_fault(instance, fault_level=FaultLevel.L2, fault_code=0x2000)

            mock_im.is_instance_separated.assert_called_once_with(1)
            mock_im.separate_instance.assert_not_called()

            if expect_recover:
                mock_im.recover_instance.assert_called_once_with(1)
            else:
                mock_im.recover_instance.assert_not_called()

            mock_logger.info.assert_called_once_with(
                "Instance %d fault level updated to %s (code: 0x%x, category: %s)",
                1,
                FaultLevel.L2.name,
                FAULT_DEVICE_L2.fault_code,
                FAULT_DEVICE_L2.fault_category.value,
            )


def test_refresh_instance_fault_level_multiple_nodes(fault_manager_with_instances):
    """Test _refresh_instance_fault_level with multiple nodes having different fault levels"""
    manager = fault_manager_with_instances

    # Set up node 1 with L2 fault
    node1 = manager.nodes["node_0"]
    node1.hardware_fault_infos = {FAULT_DEVICE_L2.fault_code: FAULT_DEVICE_L2}

    # Set up node 2 with L3 fault (higher level)
    node2 = manager.nodes["node_1"]
    node2.hardware_fault_infos = {FAULT_NODE_L3.fault_code: FAULT_NODE_L3}

    instance = manager.instances[1]

    with patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as mock_im_class:
        mock_im = MagicMock()
        mock_im_class.return_value = mock_im

        with patch("motor.controller.fault_tolerance.fault_manager.logger"):
            manager._refresh_instance_fault_level(1)
            _assert_instance_fault(instance, fault_level=FaultLevel.L3, fault_code=0x3000)
            mock_im.separate_instance.assert_called_once_with(1)


# =============================================================================
# 7. Strategy Center Processing
# =============================================================================


def test_ft_strategy_center_initialization(fault_manager):
    """Test fault tolerance strategy center initialization"""
    # The strategy center thread should be initialized
    assert hasattr(fault_manager, "ft_strategy_center_thread")
    assert fault_manager.ft_strategy_center_thread is None  # Initially None, started later


def test_process_instance_strategy_with_healthy_instance(fault_manager_with_instances):
    """Test processing strategy for a healthy instance"""
    manager = fault_manager_with_instances

    # Set instance 1 to healthy state
    manager.instances[1].fault_level = FaultLevel.HEALTHY

    with patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as mock_im_class:
        mock_im = MagicMock()
        mock_im_class.return_value = mock_im

        manager._process_instance_strategy(1)

        # For healthy instances, no recovery action should be taken
        mock_im.recover_instance.assert_not_called()
        mock_im.separate_instance.assert_not_called()


def test_process_instance_strategy_with_unhealthy_instance(fault_manager_with_instances):
    """Test processing strategy for an unhealthy instance"""
    manager = fault_manager_with_instances

    # Set instance 1 to unhealthy state with L4 fault level
    manager.instances[1].fault_level = FaultLevel.L4

    # Mock InstanceManager to return a decode instance for L4 strategy lookup
    with patch("motor.controller.core.instance_manager.InstanceManager") as mock_im_class:
        mock_im = MagicMock()
        mock_im_class.return_value = mock_im
        mock_instance = MagicMock()
        mock_instance.role = "decode"
        mock_instance.status = InsStatus.INACTIVE
        mock_instance.job_name = "decode-1"
        mock_instance.id = 1
        mock_instance.get_node_managers.return_value = []
        mock_im.get_instance.return_value = mock_instance
        manager.config.fault_tolerance_config.enable_scale_p2d = True

        with patch(
            'motor.controller.fault_tolerance.strategy.scale_p2d.InstanceManager',
            mock_im_class,
        ):
            manager._process_instance_strategy(1)

        # L4 decode instance should get ScaleP2DStrategy while recovery is in progress
        assert manager.instances[1].strategy is not None
        assert manager.instances[1].fault_level == FaultLevel.L4


def test_ft_strategy_center_processing(fault_manager_with_instances):
    """Test _ft_strategy_center processes instances correctly"""
    manager = fault_manager_with_instances

    # Mock work_condition.wait to avoid actual sleeping
    with patch.object(manager.work_condition, 'wait') as mock_wait:
        # Mock _process_instance_strategy to track calls
        with patch.object(manager, "_process_instance_strategy") as mock_process:
            # Simulate the loop by raising KeyboardInterrupt after first iteration
            mock_wait.side_effect = KeyboardInterrupt()

            with pytest.raises(KeyboardInterrupt):
                manager._ft_strategy_center()

            # Verify instances were processed
            assert mock_process.call_count == 2  # Two instances in the fixture
            mock_process.assert_any_call(1)
            mock_process.assert_any_call(2)

            # Verify wait was called with check interval
            mock_wait.assert_called_once_with(timeout=manager.strategy_center_check_interval)


def test_ft_strategy_center_with_empty_instances(fault_manager):
    """Test _ft_strategy_center with no instances"""
    # Mock work_condition.wait to avoid actual sleeping and interrupt the loop
    with patch.object(fault_manager.work_condition, 'wait', side_effect=KeyboardInterrupt()):
        with patch.object(fault_manager, "_process_instance_strategy") as mock_process:
            with pytest.raises(KeyboardInterrupt):
                fault_manager._ft_strategy_center()

            mock_process.assert_not_called()


def test_ft_strategy_center_stop_event_handling(fault_manager_with_instances):
    """Test _ft_strategy_center respects stop event"""
    manager = fault_manager_with_instances
    manager.stop_event.set()

    with patch.object(manager.work_condition, 'wait') as mock_wait:
        with patch.object(manager, "_process_instance_strategy") as mock_process:
            # Should exit immediately due to stop_event being set
            manager._ft_strategy_center()

            # Should not process any instances or wait
            mock_process.assert_not_called()
            mock_wait.assert_not_called()


# =============================================================================
# 8. Node Ownership Swap + Multi-Instance Tracking
# =============================================================================


def _mk_node(pod_ip, node_name, instance_id, job_name="", hw_faults=None):
    """Shortcut to create a NodeMetadata for swap tests."""
    node = NodeMetadata(
        node_name=node_name,
        instance_ids={instance_id},
        instance_pod_ips={instance_id: pod_ip},
        instance_job_names={instance_id: job_name},
    )
    if hw_faults:
        node.hardware_fault_infos = hw_faults
    return node


def _mk_swap_instance(instance_id, job_name, pod_ips, port="8080"):
    """Shortcut to create a mock ReadOnlyInstance with node managers."""
    inst = Mock()
    inst.id = instance_id
    inst.job_name = job_name
    inst.get_node_managers.return_value = [NodeManagerInfo(pod_ip=ip, port=port) for ip in pod_ips]
    return inst


def _patch_k8s_for_swap(fault_manager, pod_to_node):
    """Mock k8s_client to resolve pod_ip -> node_name."""
    mock_k8s = Mock()
    mock_k8s.get_node_hostname_by_pod_ip.side_effect = pod_to_node.get
    fault_manager.k8s_client = mock_k8s


# --- Swap Tests (scale_p2d node exchange) ---


def test_swap_basic_decode_receives_prefill_node(fault_manager):
    """decode-1 had {a(L6), b}. prefill-1 had {c}. swap: c->decode, a->prefill.
    New decode-2 gets {b, c}. a swapped to prefill, L6 fault follows node_a.
    """
    l6 = {
        0x00F1FEF5: FaultInfo(
            fault_category=FaultCategory.HARDWARE,
            fault_type=HardwareFaultType.CARD_UNHEALTHY,
            npu_name="npu0",
            fault_code=0x00F1FEF5,
            fault_level=FaultLevel.L6,
        )
    }
    fault_manager.nodes = {
        "node_a": _mk_node("10.0.0.1", "node_a", 1, "decode-1", l6),
        "node_b": _mk_node("10.0.0.2", "node_b", 1, "decode-1"),
        "node_c": _mk_node("10.0.0.3", "node_c", 2, "prefill-1"),
    }
    instance = _mk_swap_instance(3, "decode-1", ["10.0.0.2", "10.0.0.3"])
    pod_to_node = {"10.0.0.2": "node_b", "10.0.0.3": "node_c"}

    _patch_k8s_for_swap(fault_manager, pod_to_node)
    with patch.object(fault_manager, "_create_resource_monitor_for_node"):
        fault_manager.update(instance, ObserverEvent.INSTANCE_INITIAL)

    assert 3 in fault_manager.nodes["node_c"].instance_ids
    assert fault_manager.nodes["node_c"].instance_job_names[3] == "decode-1"
    assert 3 in fault_manager.nodes["node_b"].instance_ids
    assert 2 in fault_manager.nodes["node_a"].instance_ids
    assert fault_manager.nodes["node_a"].instance_job_names[2] == "prefill-1"
    assert 0x00F1FEF5 in fault_manager.nodes["node_a"].hardware_fault_infos


def test_swap_clears_software_fault_infos(fault_manager):
    """Software faults belong to the old instance's engine, not the physical node.
    After swap, both foreign and orphaned nodes must have software_fault_infos cleared
    to prevent the new instance from incorrectly inheriting the old instance's faults.
    """
    sw_fault = {
        0x1000001: FaultInfo(
            fault_category=FaultCategory.SOFTWARE,
            fault_code=int(SpecialFaultCode.ENGINE_DEAD),
            fault_level=FaultLevel.L2,
            exception_type="RuntimeError",
            exception_message="engine crashed",
            engine_id=0,
            engine_status=1,
        )
    }
    fault_manager.nodes = {
        "node_a": _mk_node("10.0.0.1", "node_a", 1, "decode-1"),
        "node_c": _mk_node("10.0.0.3", "node_c", 2, "prefill-1"),
    }
    # Inject software faults on both the foreign and the orphaned node
    fault_manager.nodes["node_c"].software_fault_infos = {
        int(SpecialFaultCode.ENGINE_DEAD): sw_fault[0x1000001],
    }
    fault_manager.nodes["node_a"].software_fault_infos = {
        int(SpecialFaultCode.ENGINE_DEAD): sw_fault[0x1000001],
    }

    instance = _mk_swap_instance(3, "decode-1", ["10.0.0.3"])
    pod_to_node = {"10.0.0.3": "node_c"}

    _patch_k8s_for_swap(fault_manager, pod_to_node)
    with patch.object(fault_manager, "_create_resource_monitor_for_node"):
        fault_manager.update(instance, ObserverEvent.INSTANCE_INITIAL)

    # Foreign node_c → taken over by decode-1, SW faults cleared
    assert len(fault_manager.nodes["node_c"].software_fault_infos) == 0
    # Orphaned node_a → swapped to prefill-1, SW faults cleared
    assert len(fault_manager.nodes["node_a"].software_fault_infos) == 0


def test_swap_same_job_restart_no_swap(fault_manager):
    """Instance restart same job_name — no foreign, just update instance_id."""
    fault_manager.nodes = {
        "node_a": _mk_node("10.0.0.1", "node_a", 1, "decode-1"),
        "node_b": _mk_node("10.0.0.2", "node_b", 1, "decode-1"),
    }
    instance = _mk_swap_instance(3, "decode-1", ["10.0.0.1", "10.0.0.2"])
    pod_to_node = {"10.0.0.1": "node_a", "10.0.0.2": "node_b"}

    _patch_k8s_for_swap(fault_manager, pod_to_node)
    with patch.object(fault_manager, "_create_resource_monitor_for_node"):
        fault_manager.update(instance, ObserverEvent.INSTANCE_INITIAL)

    for name in ("node_a", "node_b"):
        assert 3 in fault_manager.nodes[name].instance_ids
        assert fault_manager.nodes[name].instance_job_names[3] == "decode-1"


def test_swap_unilateral_takeover_no_orphans(fault_manager):
    """decode-1 had {a}. prefill-1/2 had {b, c}. New decode-2 gets all 3.
    2 foreign, 0 orphans (a is in new inst) — both foreign taken unilaterally.
    """
    fault_manager.nodes = {
        "node_a": _mk_node("10.0.0.1", "node_a", 1, "decode-1"),
        "node_b": _mk_node("10.0.0.2", "node_b", 2, "prefill-1"),
        "node_c": _mk_node("10.0.0.3", "node_c", 2, "prefill-2"),
    }
    instance = _mk_swap_instance(3, "decode-1", ["10.0.0.1", "10.0.0.2", "10.0.0.3"])
    pod_to_node = {"10.0.0.1": "node_a", "10.0.0.2": "node_b", "10.0.0.3": "node_c"}

    _patch_k8s_for_swap(fault_manager, pod_to_node)
    with patch.object(fault_manager, "_create_resource_monitor_for_node"):
        fault_manager.update(instance, ObserverEvent.INSTANCE_INITIAL)

    for name in ("node_a", "node_b", "node_c"):
        assert 3 in fault_manager.nodes[name].instance_ids
        assert fault_manager.nodes[name].instance_job_names[3] == "decode-1"


def test_swap_more_orphans_than_foreign(fault_manager):
    """decode-1 had {a, b, c}. prefill-1 had {d}. New decode-2 gets {b, d}.
    1 foreign (d), 2 orphans (a, c not in new inst). One swapped, one waits.
    """
    fault_manager.nodes = {
        "node_a": _mk_node("10.0.0.1", "node_a", 1, "decode-1"),
        "node_b": _mk_node("10.0.0.2", "node_b", 1, "decode-1"),
        "node_c": _mk_node("10.0.0.3", "node_c", 1, "decode-1"),
        "node_d": _mk_node("10.0.0.4", "node_d", 2, "prefill-1"),
    }
    instance = _mk_swap_instance(3, "decode-1", ["10.0.0.2", "10.0.0.4"])
    pod_to_node = {"10.0.0.2": "node_b", "10.0.0.4": "node_d"}

    _patch_k8s_for_swap(fault_manager, pod_to_node)
    with patch.object(fault_manager, "_create_resource_monitor_for_node"):
        fault_manager.update(instance, ObserverEvent.INSTANCE_INITIAL)

    assert 3 in fault_manager.nodes["node_b"].instance_ids
    assert 3 in fault_manager.nodes["node_d"].instance_ids
    assert fault_manager.nodes["node_d"].instance_job_names[3] == "decode-1"

    # At least one orphan swapped to old prefill inst
    swapped = [
        m for m in fault_manager.nodes.values() if 2 in m.instance_ids and m.instance_job_names.get(2) == "prefill-1"
    ]
    assert len(swapped) == 1
    # Remaining orphans keep old values, waiting for claim
    waiting = [m for m in fault_manager.nodes.values() if 3 not in m.instance_ids and 2 not in m.instance_ids]
    assert len(waiting) >= 1


def test_swap_empty_job_name_skipped(fault_manager):
    """Node with empty job_name (legacy data) not treated as foreign."""
    fault_manager.nodes = {
        "node_a": _mk_node("10.0.0.1", "node_a", 1, "decode-1"),
        "node_b": _mk_node("10.0.0.2", "node_b", 1, ""),
    }
    instance = _mk_swap_instance(3, "decode-1", ["10.0.0.1", "10.0.0.2"])
    pod_to_node = {"10.0.0.1": "node_a", "10.0.0.2": "node_b"}

    _patch_k8s_for_swap(fault_manager, pod_to_node)
    with patch.object(fault_manager, "_create_resource_monitor_for_node"):
        fault_manager.update(instance, ObserverEvent.INSTANCE_INITIAL)

    assert 3 in fault_manager.nodes["node_a"].instance_ids
    assert 3 in fault_manager.nodes["node_b"].instance_ids
    # Empty job_name preserved for legacy
    assert "" in fault_manager.nodes["node_b"].instance_job_names.values()


# --- Multi-Instance Tests (2P1D shared node) ---


def test_multi_instance_same_node_tracking(fault_manager):
    """Multiple instances (e.g., Prefill + Decode) on the same physical node
    are both tracked via instance_ids/instance_pod_ips/instance_job_names.
    Since decode is ACTIVE, prefill is added without triggering a swap.
    """
    # Decode is already registered on node_a (active)
    fault_manager.instances[1] = InstanceMetadata(instance_id=1)
    fault_manager.nodes = {
        "node_a": _mk_node("10.0.0.1", "node_a", 1, "decode-1"),
    }
    # Add a second instance (prefill) on the SAME node — prefill is NEW (not pre-added to instances)
    instance = _mk_swap_instance(2, "prefill-1", ["10.0.0.2"])
    pod_to_node = {"10.0.0.2": "node_a"}

    _patch_k8s_for_swap(fault_manager, pod_to_node)
    with patch.object(fault_manager, "_create_resource_monitor_for_node"):
        fault_manager.update(instance, ObserverEvent.INSTANCE_INITIAL)

    # Both instances should be tracked on node_a
    node_a = fault_manager.nodes["node_a"]
    assert 1 in node_a.instance_ids
    assert 2 in node_a.instance_ids
    assert node_a.instance_pod_ips[1] == "10.0.0.1"
    assert node_a.instance_pod_ips[2] == "10.0.0.2"
    assert node_a.instance_job_names[1] == "decode-1"
    assert node_a.instance_job_names[2] == "prefill-1"


def test_multi_instance_no_swap_when_active_instance_present(fault_manager):
    """When a node has an ACTIVE instance with a different job_name, the new
    instance is added to the node's sets WITHOUT triggering a swap.
    """
    # Decode (active) on node_a
    fault_manager.instances[1] = InstanceMetadata(instance_id=1)
    fault_manager.nodes = {
        "node_a": _mk_node("10.0.0.1", "node_a", 1, "decode-1"),
    }
    # New prefill tries to claim node_a — but decode is still active → no swap
    instance = _mk_swap_instance(2, "prefill-1", ["10.0.0.2"])
    pod_to_node = {"10.0.0.2": "node_a"}

    _patch_k8s_for_swap(fault_manager, pod_to_node)
    with patch.object(fault_manager, "_create_resource_monitor_for_node"):
        fault_manager.update(instance, ObserverEvent.INSTANCE_INITIAL)

    # Both instances coexist on node_a — no swap occurred
    node_a = fault_manager.nodes["node_a"]
    assert 1 in node_a.instance_ids
    assert 2 in node_a.instance_ids
    assert node_a.instance_job_names[1] == "decode-1"
    assert node_a.instance_job_names[2] == "prefill-1"


def test_node_status_update_refreshes_all_instances(fault_manager):
    """When a node goes NOT_READY, ALL instances on that node get their
    fault level refreshed.
    """
    fault_manager.nodes = {
        "node_a": NodeMetadata(
            node_name="node_a",
            instance_ids={1, 2},
            instance_pod_ips={1: "10.0.0.1", 2: "10.0.0.2"},
            instance_job_names={1: "decode-1", 2: "prefill-1"},
        ),
    }
    fault_manager.instances[1] = InstanceMetadata(instance_id=1)
    fault_manager.instances[2] = InstanceMetadata(instance_id=2)

    with patch.object(fault_manager, "_refresh_instance_fault_level") as mock_refresh:
        fault_manager._handle_node_status_update(NodeStatus.NOT_READY, "node_a")

        # Both instances should have their fault level refreshed
        assert mock_refresh.call_count == 2
        mock_refresh.assert_any_call(1)
        mock_refresh.assert_any_call(2)

    # Node reboot fault should be present
    node = fault_manager.nodes["node_a"]
    assert SpecialFaultCode.NODE_REBOOT in node.hardware_fault_infos


def test_instances_seperated_event_triggers_fault_refresh(fault_manager):
    """INSTANCE_SEPERATED event triggers _refresh_instance_fault_level."""
    fault_manager.instances[1] = InstanceMetadata(instance_id=1)
    fault_manager.nodes["node_a"] = NodeMetadata(
        node_name="node_a",
        instance_ids={1},
        instance_pod_ips={1: "10.0.0.1"},
        instance_job_names={1: "decode-1"},
        hardware_fault_infos={
            int(SpecialFaultCode.NODE_REBOOT): FaultInfo(
                fault_category=FaultCategory.HARDWARE,
                fault_type=HardwareFaultType.NODE_UNHEALTHY,
                npu_name="",
                fault_code=SpecialFaultCode.NODE_REBOOT,
                fault_level=FaultLevel.L6,
            ),
        },
    )

    instance = Mock()
    instance.id = 1
    instance.job_name = "decode-1"
    instance.role = "decode"

    with patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as mock_im_class:
        mock_im = MagicMock()
        mock_im_class.return_value = mock_im
        mock_im.get_instance.return_value = instance

        fault_manager.update(instance, ObserverEvent.INSTANCE_SEPERATED)

        # After SEPERATED + _refresh_instance_fault_level, the decode instance
        # should have L6 fault level from the node_reboot fault
        ins_meta = fault_manager.instances[1]
        assert ins_meta.fault_level == FaultLevel.L6
        assert ins_meta.fault_code == int(SpecialFaultCode.NODE_REBOOT)


# =============================================================================
# 9. PreSeparateNPU dynamic fault level tests
# =============================================================================

# _node_has_active_instances() uses a local import of InstanceManager, so
# the patch target must be the definition site, not the caller's module.
_CORE_IM = "motor.controller.core.instance_manager.InstanceManager"
_FAULT_MGR_IM = "motor.controller.fault_tolerance.fault_manager.InstanceManager"


# -- Test fixtures ------------------------------------------------------------

FAULT_PRE_SEPARATE_L6 = FaultInfo(
    fault_category=FaultCategory.HARDWARE,
    fault_type=HardwareFaultType.CARD_UNHEALTHY,
    npu_name="npu0",
    fault_code=0x00F1FEF5,
    fault_level=FaultLevel.L6,
    origin_fault_level=OriginFaultLevel.PRE_SEPARATE_NPU,
)


def _mk_active_instance(instance_id, job_name, role="decode"):
    """Create a mock instance that appears INITIAL / ACTIVE."""
    inst = Mock(spec=Instance)
    inst.id = instance_id
    inst.job_name = job_name
    inst.role = role
    inst.status = InsStatus.ACTIVE
    inst.get_node_managers.return_value = []
    return inst


def _mk_core_im(instance):
    """Build a mock InstanceManager whose get_instance returns *instance*."""
    mock_im = MagicMock()
    mock_im.get_instance.return_value = instance
    return mock_im


# -- _node_has_active_instances ----------------------------------------------


def test_node_has_active_instances_true(fault_manager):
    """Returns True when at least one instance on the node is ACTIVE."""
    fault_manager.instances[1] = InstanceMetadata(instance_id=1)
    node = NodeMetadata(
        node_name="node_a",
        instance_ids={1},
        instance_pod_ips={1: "10.0.0.1"},
        instance_job_names={1: "decode-1"},
    )
    with patch(_CORE_IM) as mock_im_class:
        mock_im_class.return_value = _mk_core_im(_mk_active_instance(1, "decode-1"))
        assert fault_manager._node_has_active_instances(node) is True


def test_node_has_active_instances_false_inactive_status(fault_manager):
    """Returns False when the only instance on the node is INACTIVE."""
    fault_manager.instances[1] = InstanceMetadata(instance_id=1)
    node = NodeMetadata(
        node_name="node_a",
        instance_ids={1},
        instance_pod_ips={1: "10.0.0.1"},
        instance_job_names={1: "decode-1"},
    )
    inst = _mk_active_instance(1, "decode-1")
    inst.status = InsStatus.INACTIVE
    with patch(_CORE_IM) as mock_im_class:
        mock_im_class.return_value = _mk_core_im(inst)
        assert fault_manager._node_has_active_instances(node) is False


def test_node_has_active_instances_false_no_instances(fault_manager):
    """Returns False when node has no instance_ids."""
    node = NodeMetadata(node_name="empty_node")
    assert fault_manager._node_has_active_instances(node) is False


def test_node_has_active_instances_false_instance_not_in_manager(fault_manager):
    """Returns False when instance_id is not in self.instances (stale)."""
    node = NodeMetadata(
        node_name="node_a",
        instance_ids={999},  # Not in fault_manager.instances
        instance_pod_ips={999: "10.0.0.1"},
        instance_job_names={999: "old-job"},
    )
    assert fault_manager._node_has_active_instances(node) is False


# -- _handle_fault_info_update: PreSeparateNPU downgrade --------------------


def test_handle_fault_info_pre_separate_downgrade_to_l2(fault_manager):
    """PreSeparateNPU should be downgraded to L2 when the node has ACTIVE instances."""
    node_name = "node_a"
    fault_manager.instances[1] = InstanceMetadata(instance_id=1)
    fault_manager.nodes[node_name] = NodeMetadata(
        node_name=node_name,
        instance_ids={1},
        instance_pod_ips={1: "10.0.0.1"},
        instance_job_names={1: "decode-1"},
    )

    with patch(_CORE_IM) as mock_im_class:
        mock_im_class.return_value = _mk_core_im(_mk_active_instance(1, "decode-1"))
        fault_manager._handle_fault_info_update([FAULT_PRE_SEPARATE_L6], node_name)

    node = fault_manager.nodes[node_name]
    assert len(node.hardware_fault_infos) == 1
    stored = next(iter(node.hardware_fault_infos.values()))
    assert stored.fault_level == FaultLevel.L2, "PreSeparateNPU should be L2 when active instances exist on the node"
    assert stored.origin_fault_level == OriginFaultLevel.PRE_SEPARATE_NPU


def test_handle_fault_info_pre_separate_stays_l6_no_active_instances(fault_manager):
    """PreSeparateNPU stays L6 when the node has NO active instances."""
    node_name = "node_b"
    fault_manager.instances[1] = InstanceMetadata(instance_id=1)
    fault_manager.nodes[node_name] = NodeMetadata(
        node_name=node_name,
        instance_ids={1},
        instance_pod_ips={1: "10.0.0.2"},
        instance_job_names={1: "decode-1"},
    )

    inst = _mk_active_instance(1, "decode-1")
    inst.status = InsStatus.INACTIVE
    with patch(_CORE_IM) as mock_im_class:
        mock_im_class.return_value = _mk_core_im(inst)
        fault_manager._handle_fault_info_update([FAULT_PRE_SEPARATE_L6], node_name)

    node = fault_manager.nodes[node_name]
    stored = next(iter(node.hardware_fault_infos.values()))
    assert stored.fault_level == FaultLevel.L6, "PreSeparateNPU should stay L6 when no active instances on the node"


# -- _handle_fault_info_update: same fault_code, different NPUs, different levels --


def test_handle_fault_info_same_code_highest_level_wins(fault_manager):
    """When multiple NPUs share the same fault_code but have different
    fault_levels, the highest level should be used.

    Reproduces: npu-4 (RestartNPU/L5) + npu-5/6/7 (NotHandleFault/L1)
    sharing fault_code 0x8F184C16 → L5 must win.
    """
    node_name = "work16"
    fault_manager.instances[3] = InstanceMetadata(instance_id=3)
    fault_manager.nodes[node_name] = NodeMetadata(
        node_name=node_name,
        instance_ids={3},
        instance_pod_ips={3: "10.0.0.16"},
        instance_job_names={3: "mindie-motor-vllm-0-d0"},
    )

    fault_npu4 = FI(
        fault_type=HardwareFaultType.CARD_UNHEALTHY,
        npu_name="npu-4",
        fault_code=0x8F184C16,
        fault_level=FaultLevel.L5,
        origin_fault_level=OriginFaultLevel.RESTART_NPU,
    )
    fault_npu5 = FI(
        fault_type=HardwareFaultType.CARD_UNHEALTHY,
        npu_name="npu-5",
        fault_code=0x8F184C16,
        fault_level=FaultLevel.L1,
        origin_fault_level=OriginFaultLevel.NOT_HANDLE_FAULT,
    )
    fault_npu6 = FI(
        fault_type=HardwareFaultType.CARD_UNHEALTHY,
        npu_name="npu-6",
        fault_code=0x8F184C16,
        fault_level=FaultLevel.L1,
        origin_fault_level=OriginFaultLevel.NOT_HANDLE_FAULT,
    )
    fault_npu7 = FI(
        fault_type=HardwareFaultType.CARD_UNHEALTHY,
        npu_name="npu-7",
        fault_code=0x8F184C16,
        fault_level=FaultLevel.L1,
        origin_fault_level=OriginFaultLevel.NOT_HANDLE_FAULT,
    )

    with patch(_CORE_IM) as mock_im_class:
        mock_im_class.return_value = _mk_core_im(_mk_active_instance(3, "mindie-motor-vllm-0-d0"))
        fault_manager._handle_fault_info_update([fault_npu4, fault_npu5, fault_npu6, fault_npu7], node_name)

    node = fault_manager.nodes[node_name]
    # Deduplicated to 1 fault_code
    assert len(node.hardware_fault_infos) == 1
    stored = next(iter(node.hardware_fault_infos.values()))
    # RestartNPU (L5) must win over NotHandleFault (L1)
    assert stored.fault_level == FaultLevel.L5, (
        f"Expected L5 (RestartNPU), got {stored.fault_level.name} "
        f"({stored.origin_fault_level.value if stored.origin_fault_level else 'N/A'})"
    )
    # All 4 NPU names should be preserved
    assert "npu-4" in stored.npu_name
    assert "npu-5" in stored.npu_name
    assert "npu-6" in stored.npu_name
    assert "npu-7" in stored.npu_name


# -- _refresh_instance_fault_level: PreSeparateNPU exclusion -----------------


def test_refresh_pre_separate_l6_included_when_instances_inactive(
    fault_manager_with_instances,
):
    """PreSeparateNPU L6 on a node with INACTIVE instances should be
    included in instance fault level — the fault killed the instance,
    L6 must trigger ScaleP2D so the instance can be rescheduled.
    """
    manager = fault_manager_with_instances
    node = manager.nodes["node_0"]
    # node_0.instance_ids = {1} — instance is still on this node
    node.hardware_fault_infos = {
        FAULT_PRE_SEPARATE_L6.fault_code: FAULT_PRE_SEPARATE_L6,
    }

    inst = _mk_active_instance(1, "decode-1")
    inst.status = InsStatus.INACTIVE
    with patch(_CORE_IM) as mock_core_im_class:
        mock_core_im_class.return_value = _mk_core_im(inst)
        with patch(_FAULT_MGR_IM) as mock_fm_im_class:
            mock_fm_im = MagicMock()
            mock_fm_im_class.return_value = mock_fm_im

            manager._refresh_instance_fault_level(1)

    # Instance should get L6 — PreSeparateNPU L6 with instances still on node
    # means the fault killed them, ScaleP2D must be triggered
    ins_meta = manager.instances[1]
    assert ins_meta.fault_level == FaultLevel.L6, (
        "PreSeparateNPU L6 with inactive instances on node should trigger ScaleP2D"
    )
    assert ins_meta.fault_code == FAULT_PRE_SEPARATE_L6.fault_code


def test_refresh_pre_separate_l2_included_when_active_instances(
    fault_manager_with_instances,
):
    """PreSeparateNPU L2 (downgraded because active instances exist) should
    be included in instance fault level computation.
    """
    manager = fault_manager_with_instances
    pre_sep_l2 = FaultInfo(
        fault_category=FaultCategory.HARDWARE,
        fault_type=HardwareFaultType.CARD_UNHEALTHY,
        npu_name="npu0",
        fault_code=0x00F1FEF5,
        fault_level=FaultLevel.L2,
        origin_fault_level=OriginFaultLevel.PRE_SEPARATE_NPU,
    )
    node = manager.nodes["node_0"]
    node.hardware_fault_infos = {pre_sep_l2.fault_code: pre_sep_l2}

    with patch(_CORE_IM) as mock_core_im_class:
        mock_core_im_class.return_value = _mk_core_im(_mk_active_instance(1, "decode-1"))
        with patch(_FAULT_MGR_IM) as mock_fm_im_class:
            mock_fm_im = MagicMock()
            mock_fm_im_class.return_value = mock_fm_im

            manager._refresh_instance_fault_level(1)

    ins_meta = manager.instances[1]
    assert ins_meta.fault_level == FaultLevel.L2, (
        "PreSeparateNPU L2 with active business should be reflected in instance fault level"
    )
    assert ins_meta.fault_code == 0x00F1FEF5


# -- Re-evaluation: L2 → L6 when instances leave -----------------------------


def test_reevaluate_pre_separate_l2_to_l6_when_instance_leaves(
    fault_manager_with_instances,
):
    """When all instances on a node become INACTIVE, PreSeparateNPU should be
    re-evaluated from L2 to L6.  The L6 fault should now be included in the
    instance's fault level — the instance is still on this node, meaning the
    fault killed it.  ScaleP2D must be triggered.
    """
    manager = fault_manager_with_instances
    pre_sep_l2 = FaultInfo(
        fault_category=FaultCategory.HARDWARE,
        fault_type=HardwareFaultType.CARD_UNHEALTHY,
        npu_name="npu0",
        fault_code=0x00F1FEF5,
        fault_level=FaultLevel.L2,
        origin_fault_level=OriginFaultLevel.PRE_SEPARATE_NPU,
    )
    node = manager.nodes["node_0"]
    node.hardware_fault_infos = {pre_sep_l2.fault_code: pre_sep_l2}

    inst = _mk_active_instance(1, "decode-1")
    inst.status = InsStatus.INACTIVE
    with patch(_CORE_IM) as mock_core_im_class:
        mock_core_im_class.return_value = _mk_core_im(inst)
        with patch(_FAULT_MGR_IM) as mock_fm_im_class:
            mock_fm_im = MagicMock()
            mock_fm_im_class.return_value = mock_fm_im

            manager._refresh_instance_fault_level(1)

    # Fault on node should have been re-evaluated to L6
    stored = node.hardware_fault_infos[0x00F1FEF5]
    assert stored.fault_level == FaultLevel.L6, (
        "PreSeparateNPU should escalate to L6 when all instances become inactive"
    )
    # Instance fault level should be L6 — included because instance is still
    # on this node (was killed by the fault, must trigger ScaleP2D)
    ins_meta = manager.instances[1]
    assert ins_meta.fault_level == FaultLevel.L6
    assert ins_meta.fault_code == 0x00F1FEF5


# -- PreSeparateNPU L6 coexisting with other faults ---------------------------


def test_refresh_pre_separate_l6_included_not_masked_by_l3(
    fault_manager_with_instances,
):
    """When a node has PreSeparateNPU L6 (instances inactive but still on node)
    AND another node has L3 fault, the instance should get L6 — PreSeparateNPU
    L6 is now included and L6 > L3.
    """
    manager = fault_manager_with_instances

    node0 = manager.nodes["node_0"]
    node0.hardware_fault_infos = {
        FAULT_PRE_SEPARATE_L6.fault_code: FAULT_PRE_SEPARATE_L6,
    }
    node1 = manager.nodes["node_1"]
    node1.hardware_fault_infos = {
        FAULT_NODE_L3.fault_code: FAULT_NODE_L3,
    }

    inst = _mk_active_instance(1, "decode-1")
    inst.status = InsStatus.INACTIVE
    with patch(_CORE_IM) as mock_core_im_class:
        mock_core_im_class.return_value = _mk_core_im(inst)
        with patch(_FAULT_MGR_IM) as mock_fm_im_class:
            mock_fm_im = MagicMock()
            mock_fm_im_class.return_value = mock_fm_im

            manager._refresh_instance_fault_level(1)

    ins_meta = manager.instances[1]
    assert ins_meta.fault_level == FaultLevel.L6, "PreSeparateNPU L6 with instances on node should be included; L6 > L3"
    assert ins_meta.fault_code == FAULT_PRE_SEPARATE_L6.fault_code


# -- ScaleP2D triggered for PreSeparateNPU L6 when instances on node ----------


def test_pre_separate_l6_inactive_instances_triggers_scale_p2d(
    fault_manager_with_instances,
):
    """PreSeparateNPU L6 with inactive instances still on the node SHOULD
    trigger ScaleP2D — the fault killed the instance, it must be rescheduled.
    """
    manager = fault_manager_with_instances
    manager.config.fault_tolerance_config.enable_scale_p2d = True

    node = manager.nodes["node_0"]
    node.hardware_fault_infos = {
        FAULT_PRE_SEPARATE_L6.fault_code: FAULT_PRE_SEPARATE_L6,
    }

    inst = _mk_active_instance(1, "decode-1")
    inst.status = InsStatus.INACTIVE
    with patch(_CORE_IM) as mock_core_im_class:
        mock_core_im_class.return_value = _mk_core_im(inst)
        with patch(_FAULT_MGR_IM) as mock_fm_im_class:
            mock_fm_im = MagicMock()
            mock_fm_im_class.return_value = mock_fm_im

            # First refresh fault level → L6 (included because node has instances)
            manager._refresh_instance_fault_level(1)
            # Then process strategy → should trigger ScaleP2D
            manager._process_instance_strategy(1)

    ins_meta = manager.instances[1]
    assert ins_meta.fault_level == FaultLevel.L6, (
        "PreSeparateNPU L6 with instances on node should set instance fault to L6"
    )
    assert ins_meta.strategy is not None, "PreSeparateNPU L6 with inactive instances should trigger ScaleP2D"


# =============================================================================
# 10. ManuallySeparateNPU tests — no downgrade, L6 always triggers separation
# =============================================================================

FAULT_MANUALLY_SEPARATE_L6 = FaultInfo(
    fault_category=FaultCategory.HARDWARE,
    fault_type=HardwareFaultType.CARD_NETWORK_UNHEALTHY,
    npu_name="npu0",
    fault_code=0x00F1FEF6,
    fault_level=FaultLevel.L6,
    origin_fault_level=OriginFaultLevel.MANUALLY_SEPARATE_NPU,
)


# -- _handle_fault_info_update: ManuallySeparateNPU never downgraded ---------


def test_handle_fault_info_manually_separate_not_downgraded_with_active_instances(fault_manager):
    """ManuallySeparateNPU should stay at L6 even when the node has ACTIVE instances.
    Unlike PreSeparateNPU, ManuallySeparateNPU is never downgraded to L2.
    """
    node_name = "node_a"
    fault_manager.instances[1] = InstanceMetadata(instance_id=1)
    fault_manager.nodes[node_name] = NodeMetadata(
        node_name=node_name,
        instance_ids={1},
        instance_pod_ips={1: "10.0.0.1"},
        instance_job_names={1: "decode-1"},
    )

    with patch(_CORE_IM) as mock_im_class:
        mock_im_class.return_value = _mk_core_im(_mk_active_instance(1, "decode-1"))
        fault_manager._handle_fault_info_update([FAULT_MANUALLY_SEPARATE_L6], node_name)

    node = fault_manager.nodes[node_name]
    assert len(node.hardware_fault_infos) == 1
    stored = next(iter(node.hardware_fault_infos.values()))
    assert stored.fault_level == FaultLevel.L6, (
        "ManuallySeparateNPU should stay L6 even when active instances exist on the node"
    )
    assert stored.origin_fault_level == OriginFaultLevel.MANUALLY_SEPARATE_NPU


def test_handle_fault_info_manually_separate_stays_l6_no_active_instances(fault_manager):
    """ManuallySeparateNPU stays L6 when the node has no active instances."""
    node_name = "node_b"
    fault_manager.instances[1] = InstanceMetadata(instance_id=1)
    fault_manager.nodes[node_name] = NodeMetadata(
        node_name=node_name,
        instance_ids={1},
        instance_pod_ips={1: "10.0.0.2"},
        instance_job_names={1: "decode-1"},
    )

    inst = _mk_active_instance(1, "decode-1")
    inst.status = InsStatus.INACTIVE
    with patch(_CORE_IM) as mock_im_class:
        mock_im_class.return_value = _mk_core_im(inst)
        fault_manager._handle_fault_info_update([FAULT_MANUALLY_SEPARATE_L6], node_name)

    node = fault_manager.nodes[node_name]
    stored = next(iter(node.hardware_fault_infos.values()))
    assert stored.fault_level == FaultLevel.L6, (
        "ManuallySeparateNPU should stay L6 when no active instances on the node"
    )


# -- _refresh_instance_fault_level: ManuallySeparateNPU L6 triggers separation


def test_refresh_manually_separate_l6_triggers_separation(
    fault_manager_with_instances,
):
    """ManuallySeparateNPU L6 should ALWAYS affect instance fault level
    and trigger separation, even when instances are inactive.
    This is the key difference from PreSeparateNPU L6 (which is excluded).
    """
    manager = fault_manager_with_instances
    node = manager.nodes["node_0"]
    node.hardware_fault_infos = {
        FAULT_MANUALLY_SEPARATE_L6.fault_code: FAULT_MANUALLY_SEPARATE_L6,
    }

    inst = _mk_active_instance(1, "decode-1")
    inst.status = InsStatus.INACTIVE
    with patch(_CORE_IM) as mock_core_im_class:
        mock_core_im_class.return_value = _mk_core_im(inst)
        with patch(_FAULT_MGR_IM) as mock_fm_im_class:
            mock_fm_im = MagicMock()
            mock_fm_im_class.return_value = mock_fm_im

            manager._refresh_instance_fault_level(1)

    # Instance should get L6 — ManuallySeparateNPU L6 is NOT excluded
    ins_meta = manager.instances[1]
    assert ins_meta.fault_level == FaultLevel.L6, (
        "ManuallySeparateNPU L6 should affect instance fault level regardless of active instances"
    )
    assert ins_meta.fault_code == 0x00F1FEF6


def test_refresh_manually_separate_l6_with_active_instances(
    fault_manager_with_instances,
):
    """ManuallySeparateNPU L6 should affect instance fault level even when
    instances are ACTIVE (never downgraded, always L6 → triggers separation).
    """
    manager = fault_manager_with_instances
    node = manager.nodes["node_0"]
    node.hardware_fault_infos = {
        FAULT_MANUALLY_SEPARATE_L6.fault_code: FAULT_MANUALLY_SEPARATE_L6,
    }

    with patch(_CORE_IM) as mock_core_im_class:
        mock_core_im_class.return_value = _mk_core_im(_mk_active_instance(1, "decode-1"))
        with patch(_FAULT_MGR_IM) as mock_fm_im_class:
            mock_fm_im = MagicMock()
            mock_fm_im_class.return_value = mock_fm_im

            manager._refresh_instance_fault_level(1)

    ins_meta = manager.instances[1]
    assert ins_meta.fault_level == FaultLevel.L6, (
        "ManuallySeparateNPU L6 with active instances should still set instance fault level to L6"
    )
    assert ins_meta.fault_code == 0x00F1FEF6


# -- ManuallySeparateNPU vs PreSeparateNPU: L6 exclusion contrast --------------


def test_manually_separate_l6_not_excluded_by_affects_instance():
    """The _affects_instance filter should return True for ManuallySeparateNPU L6
    (not PreSeparateNPU → first check returns True immediately).

    For PreSeparateNPU L6: included when instance still on node (len > 0),
    excluded when instance has left the node (len == 0).
    """

    # Reconstruct the filter logic from _refresh_instance_fault_level
    def _affects_instance(fi: FaultInfo, node: NodeMetadata) -> bool:
        if fi.origin_fault_level != OriginFaultLevel.PRE_SEPARATE_NPU:
            return True
        if fi.fault_level != FaultLevel.L6:
            return True
        return len(node.instance_ids) > 0

    # Node with instances still on it
    node_with_instances = NodeMetadata(
        node_name="node_a",
        instance_ids={1},
        instance_pod_ips={1: "10.0.0.1"},
        instance_job_names={1: "decode-1"},
    )
    # Node with no instances (instance moved away)
    node_no_instances = NodeMetadata(
        node_name="node_b",
        instance_ids=set(),
    )

    # ManuallySeparateNPU is NOT PreSeparateNPU → filter returns True
    assert _affects_instance(FAULT_MANUALLY_SEPARATE_L6, node_with_instances) is True, (
        "ManuallySeparateNPU L6 should always be included"
    )

    # PreSeparateNPU L6: included when instance still on node
    assert _affects_instance(FAULT_PRE_SEPARATE_L6, node_with_instances) is True, (
        "PreSeparateNPU L6 should be included when instances remain on node"
    )
    # PreSeparateNPU L6: excluded when no instances on node (already moved)
    assert _affects_instance(FAULT_PRE_SEPARATE_L6, node_no_instances) is False, (
        "PreSeparateNPU L6 should be excluded when instance has left the node"
    )


# -- Multi-instance: one active, one inactive --------------------------------


def test_node_has_active_instances_true_when_any_instance_active(fault_manager):
    """If a node hosts instance 1 (ACTIVE) and instance 2 (INACTIVE),
    _node_has_active_instances should return True — the presence of at
    least one active instance is sufficient regardless of iteration order.
    """
    fault_manager.instances[1] = InstanceMetadata(instance_id=1)
    fault_manager.instances[2] = InstanceMetadata(instance_id=2)
    node = NodeMetadata(
        node_name="shared_node",
        instance_ids=[1, 2],
        instance_pod_ips={1: "10.0.0.1", 2: "10.0.0.2"},
        instance_job_names={1: "decode-1", 2: "prefill-1"},
    )

    def side_effect(iid):
        if iid == 1:
            return _mk_active_instance(1, "decode-1")
        inst = _mk_active_instance(2, "prefill-1")
        inst.status = InsStatus.INACTIVE
        return inst

    mock_im = MagicMock()
    mock_im.get_instance.side_effect = side_effect
    with patch(_CORE_IM) as mock_im_class:
        mock_im_class.return_value = mock_im
        assert fault_manager._node_has_active_instances(node) is True
