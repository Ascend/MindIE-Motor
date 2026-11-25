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

import concurrent.futures
import time
import threading
from unittest.mock import MagicMock, Mock, patch

import pytest

from motor.common.resources.endpoint import DeviceInfo, Endpoint
from motor.common.resources.instance import InsStatus, Instance, NodeManagerInfo, ParallelConfig
from motor.config.controller import ControllerConfig
from motor.controller.core import ObserverEvent
from motor.controller.fault_tolerance.engine_recovery.runtime import FtPhase, get_ft_runtime_store
from motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling.context import ScaleDownContext
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
    hardware_fault_identity,
)
from motor.controller.fault_tolerance.engine_recovery.planner import build_recovery_plan
from motor.controller.fault_tolerance.engine_recovery.serving import ServingOverlay
from motor.controller.fault_tolerance.strategy import (
    DpScaleDownStrategy,
    EngineRelaunchStrategy,
    InstanceReconfigurationStrategy,
    EngineFastRecoveryStrategy,
    ScaleP2DStrategy,
)
from motor.controller.fault_tolerance.engine_recovery.runner import EngineRecoveryStrategy

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
    fault_type=HardwareFaultType.CARD_UNHEALTHY,
    npu_name="npu0",
    fault_code=0x1000,
    fault_level=FaultLevel.L1,
)
FAULT_DEVICE_L2_0x1000 = FI(
    fault_type=HardwareFaultType.CARD_UNHEALTHY,
    npu_name="npu0",
    fault_code=0x1000,
    fault_level=FaultLevel.L2,
)
FAULT_DEVICE_L2 = FI(
    fault_type=HardwareFaultType.CARD_UNHEALTHY,
    npu_name="npu0",
    fault_code=0x2000,
    fault_level=FaultLevel.L2,
)
FAULT_DEVICE_L3 = FI(
    fault_type=HardwareFaultType.CARD_UNHEALTHY,
    npu_name="npu0",
    fault_code=0x2000,
    fault_level=FaultLevel.L3,
)
FAULT_SWITCH_L2 = FI(
    fault_type=HardwareFaultType.CARD_NETWORK_UNHEALTHY,
    npu_name="switch0",
    fault_code=0x2000,
    fault_level=FaultLevel.L2,
)
FAULT_NODE_L3 = FI(
    fault_type=HardwareFaultType.NODE_UNHEALTHY,
    npu_name="",
    fault_code=0x3000,
    fault_level=FaultLevel.L3,
)
FAULT_CM_DEVICE_L3_0x1234 = FI(
    fault_type=HardwareFaultType.CARD_UNHEALTHY,
    npu_name="npu0",
    fault_code=0x1234,
    fault_level=FaultLevel.L3,
)
FAULT_CM_SWITCH_L2_0x5678 = FI(
    fault_type=HardwareFaultType.CARD_NETWORK_UNHEALTHY,
    npu_name="switch0",
    fault_code=0x5678,
    fault_level=FaultLevel.L2,
)


def _reportable_instance(pod_ip: str, nm_port: str = "1026", engine_ids: tuple[int, ...] = ()) -> MagicMock:
    """Mock instance that passes software-fault reporter identity validation."""
    instance = MagicMock()
    from motor.common.resources.instance import FtCapabilitySnapshot

    instance.ft_capability = FtCapabilitySnapshot()
    instance.get_node_managers.return_value = [NodeManagerInfo(pod_ip=pod_ip, port=nm_port)]
    instance.has_node_mgr.side_effect = lambda ip: ip == pod_ip
    instance.get_all_endpoints.side_effect = lambda include_headless=False: [
        MagicMock(id=engine_id) for engine_id in engine_ids
    ]
    return instance


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
    return {
        "instance_id": instance_id,
        "fault_level": fault_level.value,
        "fault_code": fault_code,
    }


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

    store = get_ft_runtime_store()
    store.set_persist_callback(None)
    store.clear()
    # Clear singleton instances before each test
    if FaultManager in ThreadSafeSingleton._instances:
        fault_manager = ThreadSafeSingleton._instances[FaultManager]
        fault_manager.stop()
        del ThreadSafeSingleton._instances[FaultManager]
    yield
    store.set_persist_callback(None)
    store.clear()


@pytest.fixture
def fault_manager():
    """Create a basic FaultManager instance for testing"""
    with patch("motor.controller.fault_tolerance.fault_manager.K8sClient"):
        config = ControllerConfig()
        mgr = FaultManager(config)
        yield mgr
        mgr.executor.shutdown(wait=True)


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
        manager.executor.shutdown(wait=True)


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

    with patch.object(
        manager.etcd_client,
        "persist_data",
        side_effect=Exception("ETCD connection error"),
    ):
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
        "ft_runtime": {
            "1": {
                "phase": FtPhase.SCALING_DOWN.value,
                "fallback_strategy": "ScaleP2DStrategy",
            }
        },
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
    assert instance.prev_strategy_failed is True
    assert instance.prev_strategy_name == "DpScaleDownStrategy"
    assert instance.prev_strategy_fallback == "ScaleP2DStrategy"


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

    with patch.object(
        manager.etcd_client,
        "restore_data",
        side_effect=Exception("ETCD connection error"),
    ):
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
                target=fault_manager._ft_strategy_center,
                daemon=True,
                name="FaultToleranceStrategyCenter",
            )
            mock_restore.assert_called_once()  # Verify restore_data was called
            mock_thread.return_value.start.assert_called_once()


def test_fault_manager_start_with_persistence_disabled(fault_manager):
    """Test starting FaultManager with persistence disabled"""
    fault_manager.etcd_config.enable_etcd_persistence = False

    with patch.object(fault_manager, "restore_data") as mock_restore, patch("threading.Thread") as mock_thread:
        fault_manager.start()

        mock_thread.assert_called_once_with(
            target=fault_manager._ft_strategy_center,
            daemon=True,
            name="FaultToleranceStrategyCenter",
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
                    target=fault_manager._ft_strategy_center,
                    daemon=True,
                    name="FaultToleranceStrategyCenter",
                )
                mock_restore.assert_called_once()
                mock_logger.warning.assert_called_once_with(
                    "Failed to restore fault manager's data from ETCD, start with empty state"
                )
                mock_thread.return_value.start.assert_called_once()


def test_fault_manager_start_with_stop_event_reset(fault_manager):
    """Test starting FaultManager when stop_event was previously set"""
    fault_manager.stop_event.set()

    with patch.object(fault_manager, "restore_data", return_value=True), patch("threading.Thread"):
        fault_manager.start()
        assert not fault_manager.stop_event.is_set()


def test_fault_manager_start_creates_resource_monitors(fault_manager_with_instances):
    """Test that starting FaultManager creates ResourceMonitors for all nodes"""
    manager = fault_manager_with_instances

    with patch.object(manager, "restore_data", return_value=True), patch("threading.Thread"):
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
    get_ft_runtime_store().transition(1, phase=FtPhase.RECONFIGURING)

    with patch.object(fault_manager, "_stop_resource_monitor_for_node"):
        fault_manager.update(mock_instance, ObserverEvent.INSTANCE_REMOVED)

    assert 1 not in fault_manager.instances
    assert get_ft_runtime_store().get(1) is None
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
                "Instance %d already exists in fault manager, skipping add operation.",
                1,
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
        with patch.object(manager, "persist_data", return_value=True) as persist:
            manager.update_config(new_config)
            store = get_ft_runtime_store()
            store.clear()
            store.transition(991, phase=FtPhase.SCALING_DOWN)
            persist.assert_called_once()
            store.set_persist_callback(None)
            store.clear()

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
        node_name="node_0",
        instance_ids={1},
        instance_pod_ips={1: "192.168.1.1"},
        instance_job_names={1: ""},
    )
    manager.nodes["node_1"] = NodeMetadata(
        node_name="node_1",
        instance_ids={1},
        instance_pod_ips={1: "192.168.1.2"},
        instance_job_names={1: ""},
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

        assert (manager.configmap_prefix, manager.configmap_namespace) == (
            "new-prefix",
            "new-namespace",
        )

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


def test_refresh_instance_fault_level_instance_not_found_with_instances(
    fault_manager_with_instances,
):
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

            mock_logger.info.assert_any_call(
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


def test_refresh_same_level_fault_does_not_treat_unhealthy_as_fast_recovery_eligibility(
    fault_manager_with_instances,
):
    manager = fault_manager_with_instances
    node = manager.nodes["node_0"]
    node.hardware_fault_infos = {FAULT_DEVICE_L2.fault_code: FAULT_DEVICE_L2}
    node.software_fault_infos = {
        "1000002:0": FaultInfo.from_exception(RuntimeError("unhealthy"), engine_id=0, engine_status=2)
    }

    with patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as instance_manager_cls:
        instance_manager_cls.return_value.is_instance_separated.return_value = False
        manager._refresh_instance_fault_level(1)

    _assert_instance_fault(manager.instances[1], fault_level=FaultLevel.L2, fault_code=FAULT_DEVICE_L2.fault_code)


def test_software_faults_are_isolated_between_instances_sharing_pod_ip(fault_manager_with_instances):
    """Completing one instance must not consume another instance's engine fault."""
    manager = fault_manager_with_instances
    node = manager.nodes["node_0"]
    node.instance_ids = {1, 2}
    node.instance_pod_ips = {1: "127.0.0.1", 2: "127.0.0.1"}
    # Same pod, different NodeManager processes (e.g. single-container P+D).
    instance1 = _reportable_instance("127.0.0.1", nm_port="1026", engine_ids=(0,))
    instance2 = _reportable_instance("127.0.0.1", nm_port="1027", engine_ids=(1,))

    prefill_fault = FaultInfo.from_exception(
        RuntimeError("prefill unhealthy"), engine_id=0, engine_status=2, instance_id=1
    )
    decode_fault = FaultInfo.from_exception(RuntimeError("decode dead"), engine_id=1, engine_status=1, instance_id=2)
    with patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as instance_manager_cls:
        instance_manager_cls.return_value.get_instance.side_effect = lambda iid: {1: instance1, 2: instance2}[iid]
        assert manager.report_software_fault(
            prefill_fault, pod_ip="127.0.0.1", instance_id=1, node_manager_port="1026"
        ).accepted
        assert manager.report_software_fault(
            decode_fault, pod_ip="127.0.0.1", instance_id=2, node_manager_port="1027"
        ).accepted

    manager._consume_software_evidence(1)

    assert manager.instances[1].software_fault_infos == {}
    assert len(manager.instances[2].software_fault_infos) == 1
    with patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as instance_manager_cls:
        instance_manager_cls.return_value.get_instance.return_value = _hardware_dp_instance()
        context = manager._build_recovery_context(2)
    assert context.pending_removed_ranks == (1,)


def test_card_fault_is_isolated_to_device_owner_in_mixed_deployment(fault_manager):
    manager = fault_manager
    pod_ip = "192.0.2.10"
    manager.instances[1] = InstanceMetadata(instance_id=1)
    manager.instances[2] = InstanceMetadata(instance_id=2)
    manager.nodes["node-a"] = NodeMetadata(
        node_name="node-a",
        instance_ids={1, 2},
        instance_pod_ips={1: pod_ip, 2: pod_ip},
        instance_job_names={1: "prefill-0", 2: "decode-0"},
        hardware_fault_infos={
            "9000:npu-15": FI(
                fault_type=HardwareFaultType.CARD_UNHEALTHY,
                npu_name="npu-15",
                fault_code=0x9000,
                fault_level=FaultLevel.L5,
                origin_fault_level=OriginFaultLevel.RESTART_NPU,
            )
        },
    )
    prefill = Instance(
        job_name="prefill-0",
        model_name="model",
        id=1,
        role="prefill",
        parallel_config=ParallelConfig(dp_size=1),
    )
    prefill.add_endpoints(
        pod_ip,
        {0: Endpoint(id=0, ip=pod_ip, business_port="8000", device_infos=[DeviceInfo(device_id="0", rank_id="0")])},
    )
    decode = Instance(
        job_name="decode-0",
        model_name="model",
        id=2,
        role="decode",
        parallel_config=ParallelConfig(dp_size=1),
    )
    decode.add_endpoints(
        pod_ip,
        {0: Endpoint(id=0, ip=pod_ip, business_port="8001", device_infos=[DeviceInfo(device_id="15", rank_id="0")])},
    )

    with patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as instance_manager_cls:
        instance_manager = instance_manager_cls.return_value
        instance_manager.get_instance.side_effect = lambda instance_id: {1: prefill, 2: decode}[instance_id]
        instance_manager.is_instance_separated.return_value = False
        manager._refresh_instance_fault_level(1)
        manager._refresh_instance_fault_level(2)

    assert manager.instances[1].fault_level == FaultLevel.HEALTHY
    assert manager.instances[2].fault_level == FaultLevel.L5
    instance_manager.separate_instance.assert_called_once_with(2)


@pytest.mark.parametrize("engine_status", [1, 2])
def test_first_engine_fault_only_records_evidence_before_managed_preparation(
    fault_manager_with_instances, engine_status
):
    manager = fault_manager_with_instances
    manager.config.fault_tolerance_config.enable_dp_scale_down = True
    manager.config.fault_tolerance_config.enable_fast_recovery = True
    fault = FaultInfo.from_exception(RuntimeError("engine fault"), engine_id=3, engine_status=engine_status)
    instance = _reportable_instance("192.168.1.1", engine_ids=(3,))
    with (
        patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as instance_manager_cls,
        patch.object(ServingOverlay, "withdraw") as withdraw,
        patch("motor.controller.fault_tolerance.fault_manager.threading.Thread") as thread,
    ):
        instance_manager_cls.return_value.get_instance.return_value = instance
        assert manager.report_software_fault(
            fault, pod_ip="192.168.1.1", instance_id=1, node_manager_port="1026"
        ).accepted
        first_versions = dict(manager.instances[1].software_fault_versions)
        assert manager.report_software_fault(
            fault, pod_ip="192.168.1.1", instance_id=1, node_manager_port="1026"
        ).accepted
    faults = manager.instances[1].software_fault_infos
    assert list(faults) == ["192.168.1.1:1026:3"]
    assert faults["192.168.1.1:1026:3"].engine_status == engine_status
    assert manager.instances[1].software_fault_versions == first_versions
    withdraw.assert_not_called()
    thread.assert_not_called()
    assert get_ft_runtime_store().get(1) is None


@pytest.mark.parametrize("fault_level", [FaultLevel.HEALTHY, FaultLevel.L1])
@pytest.mark.parametrize("dead_committed", [[], [1]])
def test_not_handle_fault_allows_serving_republication(fault_manager_with_instances, fault_level, dead_committed):
    """Fast-recovery completion must restore routing even with background NotHandle evidence."""
    manager = fault_manager_with_instances
    manager.instances[1].fault_level = fault_level
    instance = Instance(
        job_name="decode-0", model_name="model", id=1, role="decode", parallel_config=ParallelConfig(dp_size=2)
    )
    instance.add_endpoints(
        "192.168.1.1", {rank: Endpoint(id=rank, ip="192.168.1.1", business_port=str(8000 + rank)) for rank in (0, 1)}
    )
    store = get_ft_runtime_store()
    store.transition(1, phase=FtPhase.WAITING_ENGINE_FAULT, dead_committed=dead_committed, serving_published=False)
    with (
        patch.object(manager, "_is_superseded_instance", return_value=False),
        patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as instance_manager_cls,
        patch(
            "motor.controller.fault_tolerance.engine_recovery.serving.CoordinatorApiClient.send_instance_refresh",
            side_effect=[False, True],
        ) as refresh,
    ):
        instance_manager_cls.return_value.get_instance.return_value = instance
        manager._process_instance_strategy(1)
        assert store.get(1)["serving_published"] is False
        manager._process_instance_strategy(1)
    assert refresh.call_count == 2
    assert store.get(1)["can_serve"] is True
    assert store.get(1)["dead_committed"] == dead_committed
    published = refresh.call_args.args[0].instances[0]
    assert {endpoint.id for endpoint in published.get_all_endpoints()} == {0, 1} - set(dead_committed)


@pytest.mark.parametrize("blocked_by", ["running", "failed", "actionable", "superseded", "reconfigured"])
def test_serving_republication_keeps_recovery_safety_guards(fault_manager_with_instances, blocked_by):
    manager = fault_manager_with_instances
    metadata = manager.instances[1]
    metadata.fault_level = FaultLevel.L2 if blocked_by == "actionable" else FaultLevel.L1
    metadata.prev_strategy_failed = blocked_by == "failed"
    if blocked_by == "running":
        metadata.strategy = Mock()
        metadata.strategy.is_finished.return_value = False
    store = get_ft_runtime_store()
    store.transition(1, phase=FtPhase.WAITING_ENGINE_FAULT, serving_published=False)
    if blocked_by == "reconfigured":
        store.mark_instance_reconfiguration_dispatched(1)
    with (
        patch.object(manager, "_is_superseded_instance", return_value=blocked_by == "superseded"),
        patch.object(manager, "_build_recovery_context", return_value=ScaleDownContext()),
        patch.object(ServingOverlay, "publish") as publish,
    ):
        manager._process_instance_strategy(1)
    publish.assert_not_called()
    assert store.get(1)["can_serve"] is False


def test_engine_fault_does_not_withdraw_when_dp_scale_down_is_disabled(fault_manager_with_instances):
    manager = fault_manager_with_instances
    fault = FaultInfo.from_exception(RuntimeError("engine fault"), engine_id=0, engine_status=1)
    instance = _reportable_instance("192.168.1.1", engine_ids=(0,))

    with (
        patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as instance_manager_cls,
        patch.object(ServingOverlay, "withdraw") as withdraw,
    ):
        instance_manager_cls.return_value.get_instance.return_value = instance
        assert manager.report_software_fault(fault, pod_ip="192.168.1.1", instance_id=1).accepted

    withdraw.assert_not_called()
    assert get_ft_runtime_store().get(1) is None


def test_new_dp_status_replaces_previous_status_in_collection_round(fault_manager_with_instances):
    """A newer status for the same reporter+engine replaces the prior one,
    regardless of fault code: DEAD and UNHEALTHY must never coexist.
    """
    manager = fault_manager_with_instances
    unhealthy = FaultInfo.from_exception(RuntimeError("unhealthy"), 0, 2, instance_id=1)
    dead = FaultInfo.from_exception(RuntimeError("dead"), 0, 1, instance_id=1)
    instance = _reportable_instance("192.168.1.1", engine_ids=(0,))

    with patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as instance_manager_cls:
        instance_manager_cls.return_value.get_instance.return_value = instance
        assert manager.report_software_fault(unhealthy, pod_ip="192.168.1.1", instance_id=1).accepted
        # UNHEALTHY -> DEAD replaces the entry and clears the unhealthy timestamp.
        assert manager.report_software_fault(dead, pod_ip="192.168.1.1", instance_id=1).accepted
        faults = list(manager.instances[1].software_fault_infos.values())
        assert [f.engine_status for f in faults] == [1]
        assert list(manager.instances[1].software_dead_observed_at) == [0]
        assert manager.instances[1].software_unhealthy_observed_at == {}

        # DEAD -> UNHEALTHY replaces the entry and clears the dead timestamp.
        unhealthy_again = FaultInfo.from_exception(RuntimeError("recovered to unhealthy"), 0, 2, instance_id=1)
        assert manager.report_software_fault(unhealthy_again, pod_ip="192.168.1.1", instance_id=1).accepted

    faults = list(manager.instances[1].software_fault_infos.values())
    assert [f.engine_status for f in faults] == [2]
    assert manager.instances[1].software_dead_observed_at == {}
    assert 0 in manager.instances[1].software_unhealthy_observed_at


def test_report_software_fault_rejects_unowned_endpoint_zero(fault_manager_with_instances):
    """Endpoint zero is a valid global DP rank and must be ownership-checked."""
    manager = fault_manager_with_instances
    instance = _reportable_instance("192.168.1.1", engine_ids=(1,))
    fault = FaultInfo.from_exception(RuntimeError("engine zero died"), engine_id=0, engine_status=1)

    with patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as instance_manager_cls:
        instance_manager_cls.return_value.get_instance.return_value = instance
        result = manager.report_software_fault(
            fault,
            pod_ip="192.168.1.1",
            instance_id=1,
            node_manager_port="1026",
        )

    assert not result.accepted
    assert result.reason == "engine_id not registered on instance"
    assert manager.instances[1].software_fault_infos == {}


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


def test_process_instance_strategy_with_unhealthy_instance(
    fault_manager_with_instances,
):
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
            "motor.controller.fault_tolerance.strategy.scale_p2d.InstanceManager",
            mock_im_class,
        ):
            manager._process_instance_strategy(1)

        # L4 decode instance should get ScaleP2DStrategy while recovery is in progress
        assert manager.instances[1].strategy is not None
        assert manager.instances[1].fault_level == FaultLevel.L4


@pytest.mark.parametrize("instance_error", [None, RuntimeError("instance removed")])
def test_ft_strategy_center_processing(fault_manager_with_instances, instance_error):
    """Test _ft_strategy_center processes instances correctly"""
    manager = fault_manager_with_instances

    # Mock work_condition.wait to avoid actual sleeping
    with patch.object(manager.work_condition, "wait") as mock_wait:
        # Mock _process_instance_strategy to track calls
        with patch.object(manager, "_process_instance_strategy") as mock_process:
            mock_process.side_effect = [instance_error, None]
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
    with patch.object(fault_manager.work_condition, "wait", side_effect=KeyboardInterrupt()):
        with patch.object(fault_manager, "_process_instance_strategy") as mock_process:
            with pytest.raises(KeyboardInterrupt):
                fault_manager._ft_strategy_center()

            mock_process.assert_not_called()


def test_ft_strategy_center_stop_event_handling(fault_manager_with_instances):
    """Test _ft_strategy_center respects stop event"""
    manager = fault_manager_with_instances
    manager.stop_event.set()

    with patch.object(manager.work_condition, "wait") as mock_wait:
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
    """INSTANCE_SEPARATED event triggers _refresh_instance_fault_level."""
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

        fault_manager.update(instance, ObserverEvent.INSTANCE_SEPARATED)

        # After SEPARATED + _refresh_instance_fault_level, the decode instance
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

FAULT_MANUALLY_SEPARATE_L6 = FaultInfo(
    fault_category=FaultCategory.HARDWARE,
    fault_type=HardwareFaultType.CARD_NETWORK_UNHEALTHY,
    npu_name="npu0",
    fault_code=0x00F1FEF6,
    fault_level=FaultLevel.L6,
    origin_fault_level=OriginFaultLevel.MANUALLY_SEPARATE_NPU,
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


# -- Shared node-state helpers -------------------------------------------------


def _seed_fault_node(manager, fault=None, *, status=InsStatus.ACTIVE, node_name="node_a"):
    manager.instances[1] = InstanceMetadata(instance_id=1)
    node = NodeMetadata(
        node_name=node_name,
        instance_ids={1},
        instance_pod_ips={1: "10.0.0.1"},
        instance_job_names={1: "decode-1"},
    )
    if fault is not None:
        node.hardware_fault_infos = {fault.fault_code: fault.model_copy()}
    manager.nodes[node_name] = node
    instance = _mk_active_instance(1, "decode-1")
    instance.status = status
    return node, instance


def _refresh_with_instance(manager, instance):
    instance_manager = _mk_core_im(instance)
    with patch(_CORE_IM, return_value=instance_manager), patch(_FAULT_MGR_IM, return_value=instance_manager):
        manager._refresh_instance_fault_level(1)
    return instance_manager


@pytest.mark.parametrize(
    "statuses,tracked,expected",
    [
        ([InsStatus.ACTIVE], [True], True),
        ([InsStatus.INACTIVE], [True], False),
        ([InsStatus.ACTIVE], [False], False),
        ([InsStatus.INACTIVE, InsStatus.ACTIVE], [True, True], True),
    ],
    ids=["active", "inactive", "stale", "mixed"],
)
def test_node_has_active_instances(fault_manager, statuses, tracked, expected):
    instances = {}
    ids = set(range(1, len(statuses) + 1))
    for instance_id, (status, is_tracked) in enumerate(zip(statuses, tracked), 1):
        instance = _mk_active_instance(instance_id, f"job-{instance_id}")
        instance.status = status
        instances[instance_id] = instance
        if is_tracked:
            fault_manager.instances[instance_id] = InstanceMetadata(instance_id=instance_id)
    node = NodeMetadata(node_name="node_a", instance_ids=ids)
    instance_manager = MagicMock()
    instance_manager.get_instance.side_effect = instances.get
    with patch(_CORE_IM, return_value=instance_manager):
        assert fault_manager._node_has_active_instances(node) is expected


# -- Fault ingestion ----------------------------------------------------------


@pytest.mark.parametrize(
    "fault,status,expected_level",
    [
        (FAULT_PRE_SEPARATE_L6, InsStatus.ACTIVE, FaultLevel.L2),
        (FAULT_PRE_SEPARATE_L6, InsStatus.INACTIVE, FaultLevel.L6),
        (FAULT_MANUALLY_SEPARATE_L6, InsStatus.ACTIVE, FaultLevel.L6),
        (FAULT_MANUALLY_SEPARATE_L6, InsStatus.INACTIVE, FaultLevel.L6),
    ],
    ids=["pre-active", "pre-inactive", "manual-active", "manual-inactive"],
)
def test_handle_separate_fault_level(fault_manager, fault, status, expected_level):
    node, instance = _seed_fault_node(fault_manager, status=status)
    with patch(_CORE_IM, return_value=_mk_core_im(instance)):
        fault_manager._handle_fault_info_update([fault.model_copy()], node.node_name)
    stored = next(iter(node.hardware_fault_infos.values()))
    assert (stored.fault_level, stored.origin_fault_level) == (expected_level, fault.origin_fault_level)


def test_handle_fault_info_same_code_keeps_each_npu(fault_manager):
    node, instance = _seed_fault_node(fault_manager, node_name="work16")
    faults = [
        FI(
            fault_type=HardwareFaultType.CARD_UNHEALTHY,
            npu_name=f"npu-{chip}",
            fault_code=0x8F184C16,
            fault_level=FaultLevel.L5 if chip == 4 else FaultLevel.L1,
            origin_fault_level=OriginFaultLevel.RESTART_NPU if chip == 4 else OriginFaultLevel.NOT_HANDLE_FAULT,
        )
        for chip in range(4, 8)
    ]
    with patch(_CORE_IM, return_value=_mk_core_im(instance)):
        fault_manager._handle_fault_info_update(faults, node.node_name)
    assert {fault.npu_name: fault.fault_level for fault in node.hardware_fault_infos.values()} == {
        "npu-4": FaultLevel.L5,
        "npu-5": FaultLevel.L1,
        "npu-6": FaultLevel.L1,
        "npu-7": FaultLevel.L1,
    }


# -- Fault refresh and recovery -----------------------------------------------


@pytest.mark.parametrize(
    "fault,status,expected_level",
    [
        (FAULT_PRE_SEPARATE_L6.model_copy(update={"fault_level": FaultLevel.L2}), InsStatus.ACTIVE, FaultLevel.L2),
        (FAULT_PRE_SEPARATE_L6.model_copy(update={"fault_level": FaultLevel.L2}), InsStatus.INACTIVE, FaultLevel.L6),
        (FAULT_MANUALLY_SEPARATE_L6, InsStatus.ACTIVE, FaultLevel.L6),
        (FAULT_MANUALLY_SEPARATE_L6, InsStatus.INACTIVE, FaultLevel.L6),
    ],
    ids=["pre-active", "pre-reevaluated", "manual-active", "manual-inactive"],
)
def test_refresh_separate_fault_level(fault_manager_with_instances, fault, status, expected_level):
    manager = fault_manager_with_instances
    manager.nodes["node_0"].hardware_fault_infos = {fault.fault_code: fault.model_copy()}
    instance = _mk_active_instance(1, "decode-1")
    instance.status = status

    _refresh_with_instance(manager, instance)

    metadata = manager.instances[1]
    assert (metadata.fault_level, metadata.fault_code) == (expected_level, fault.fault_code)
    if fault.origin_fault_level == OriginFaultLevel.PRE_SEPARATE_NPU:
        assert manager.nodes["node_0"].hardware_fault_infos[fault.fault_code].fault_level == expected_level


def test_pre_separate_l6_triggers_scale_p2d(fault_manager_with_instances):
    manager = fault_manager_with_instances
    manager.config.fault_tolerance_config.enable_scale_p2d = True
    manager.nodes["node_0"].hardware_fault_infos = {
        FAULT_PRE_SEPARATE_L6.fault_code: FAULT_PRE_SEPARATE_L6.model_copy()
    }
    instance = _mk_active_instance(1, "decode-1")
    instance.status = InsStatus.INACTIVE
    core_instance_manager = _mk_core_im(instance)
    instance_manager = MagicMock()

    with (
        patch(_CORE_IM, return_value=core_instance_manager),
        patch(_FAULT_MGR_IM, return_value=instance_manager),
        patch.object(manager.executor, "submit") as submit,
    ):
        manager._refresh_instance_fault_level(1)
        manager._process_instance_strategy(1)

    assert manager.instances[1].fault_level == FaultLevel.L6
    assert manager.instances[1].strategy is not None
    submit.assert_called_once()


@pytest.mark.parametrize(
    "previous,enable_scale_down,enable_relaunch,expected",
    [
        (None, True, True, EngineRelaunchStrategy),
        ("EngineFastRecoveryStrategy", True, True, InstanceReconfigurationStrategy),
        ("EngineFastRecoveryStrategy", True, False, InstanceReconfigurationStrategy),
        ("EngineFastRecoveryStrategy", False, True, InstanceReconfigurationStrategy),
        ("EngineFastRecoveryStrategy", False, False, InstanceReconfigurationStrategy),
        ("DpScaleDownStrategy", True, False, InstanceReconfigurationStrategy),
        ("EngineRelaunchStrategy", True, True, InstanceReconfigurationStrategy),
        ("InstanceReconfigurationStrategy", True, True, None),
    ],
    ids=[
        "baseline-to-relaunch",
        "fast-recovery-skips-enabled-relaunch",
        "fast-recovery-without-relaunch-to-reconfiguration",
        "fast-recovery-without-scale-down-to-reconfiguration",
        "fast-recovery-without-optional-policies-to-reconfiguration",
        "scale-down-to-reconfiguration",
        "relaunch-to-reconfiguration",
        "reconfiguration-not-repeated",
    ],
)
def test_failed_strategy_uses_next_fallback(
    fault_manager_with_instances,
    previous,
    enable_scale_down,
    enable_relaunch,
    expected,
):
    manager = fault_manager_with_instances
    manager.config.fault_tolerance_config.enable_dp_scale_down = enable_scale_down
    manager.config.fault_tolerance_config.enable_engine_relaunch = enable_relaunch
    metadata = manager.instances[1]
    metadata.fault_level = FaultLevel.L2
    metadata.fault_code = int(SpecialFaultCode.ENGINE_UNHEALTHY)
    metadata.prev_strategy_failed = True
    metadata.prev_strategy_name = previous
    metadata.strategy = None

    with patch.object(manager.executor, "submit") as mock_submit:
        manager._process_instance_strategy(1)

    if expected is None:
        mock_submit.assert_not_called()
        assert metadata.strategy is None
    else:
        mock_submit.assert_called_once()
        assert isinstance(metadata.strategy, expected)
        assert metadata.strategy._controller_config is manager.config


def test_healthy_scale_down_failure_reconfigures_only_when_scale_up_enabled(fault_manager_with_instances):
    manager = fault_manager_with_instances
    metadata = manager.instances[1]
    metadata.fault_level = FaultLevel.HEALTHY
    metadata.fault_code = 0
    metadata.prev_strategy_failed = True
    metadata.prev_strategy_name = "DpScaleDownStrategy"
    metadata.strategy = None
    get_ft_runtime_store().transition(1, phase=FtPhase.RECONFIGURING)
    manager.config.fault_tolerance_config.enable_dp_scale_up = False

    with patch.object(manager.executor, "submit") as submit:
        manager._process_instance_strategy(1)
    submit.assert_not_called()
    assert metadata.strategy is None

    manager.config.fault_tolerance_config.enable_dp_scale_up = True
    with patch.object(manager.executor, "submit") as submit:
        manager._process_instance_strategy(1)
    submit.assert_called_once()
    assert isinstance(metadata.strategy, InstanceReconfigurationStrategy)


def test_late_hardware_fault_escalates_failed_reconfiguration_to_scale_p2d(fault_manager_with_instances):
    manager = fault_manager_with_instances
    metadata = manager.instances[1]
    metadata.fault_level = FaultLevel.L6
    metadata.fault_code = 0x1
    metadata.prev_strategy_failed = True
    metadata.prev_strategy_name = "InstanceReconfigurationStrategy"
    manager.strategies[FaultLevel.L6] = lambda *_: ScaleP2DStrategy

    with (
        patch.object(manager, "_is_superseded_instance", return_value=False),
        patch.object(
            manager,
            "_build_recovery_context",
            return_value=ScaleDownContext(source="hardware", hardware_fault_observed=True),
        ),
        patch.object(manager.executor, "submit") as submit,
    ):
        manager._process_instance_strategy(1)

    submit.assert_called_once()
    assert isinstance(metadata.strategy, ScaleP2DStrategy)


def test_healthy_instance_does_not_probe_fast_recovery(fault_manager_with_instances):
    manager = fault_manager_with_instances

    with (
        patch.object(EngineFastRecoveryStrategy, "is_applicable", return_value=True) as is_applicable,
        patch.object(manager.executor, "submit") as submit,
    ):
        manager._process_instance_strategy(1)

    is_applicable.assert_not_called()
    submit.assert_not_called()


def test_dead_rank_scale_down_precedes_fast_recovery_applicability(fault_manager_with_instances):
    manager = fault_manager_with_instances
    manager.config.fault_tolerance_config.enable_dp_scale_down = True
    metadata = manager.instances[1]
    metadata.fault_level = FaultLevel.L2
    metadata.fault_code = int(SpecialFaultCode.ENGINE_DEAD)
    manager.nodes["node_1"].software_fault_infos = {
        "1000001:1": FaultInfo.from_exception(RuntimeError("executor died"), engine_id=1, engine_status=1)
    }
    manager.nodes["node_0"].software_fault_infos = {
        "1000002:0": FaultInfo.from_exception(RuntimeError("peer failed"), engine_id=0, engine_status=2)
    }
    manager.nodes["node_1"].software_fault_infos = {
        "1000001:1": FaultInfo.from_exception(RuntimeError("dead"), engine_id=1, engine_status=1)
    }
    manager.nodes["node_1"].hardware_fault_infos = {
        "hardware:1": FI(
            fault_type=HardwareFaultType.CARD_UNHEALTHY,
            npu_name="",
            fault_code=0x2000,
            fault_level=FaultLevel.L4,
        )
    }

    with (
        patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as instance_manager_cls,
        patch.object(EngineFastRecoveryStrategy, "is_applicable", return_value=True),
        patch.object(manager.executor, "submit") as submit,
    ):
        instance_manager_cls.return_value.get_instance.return_value = _hardware_dp_instance()
        manager._process_instance_strategy(1)

    submit.assert_called_once()
    assert isinstance(metadata.strategy, EngineRecoveryStrategy)
    assert metadata.strategy.strategy_context.pending_removed_ranks == (1,)


def test_fault_level_without_ft_evidence_waits_for_status(fault_manager_with_instances):
    manager = fault_manager_with_instances
    manager.config.fault_tolerance_config.enable_dp_scale_down = True
    metadata = manager.instances[1]
    metadata.fault_level = FaultLevel.L2
    metadata.fault_code = int(SpecialFaultCode.ENGINE_UNHEALTHY)

    with (
        patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as instance_manager_cls,
        patch.object(manager.executor, "submit") as submit,
    ):
        instance_manager_cls.return_value.get_instance.return_value = _hardware_dp_instance()
        manager._process_instance_strategy(1)

    submit.assert_not_called()
    assert metadata.strategy is None


@pytest.mark.parametrize(
    "hardware_case",
    ["none", "actionable", "observation"],
)
def test_all_dp_unhealthy_evaluates_fast_recovery(fault_manager_with_instances, hardware_case):
    manager = fault_manager_with_instances
    manager.config.fault_tolerance_config.enable_dp_scale_down = True
    manager.config.fault_tolerance_config.enable_fast_recovery = True
    metadata = manager.instances[1]
    metadata.fault_level = FaultLevel.L2
    metadata.fault_code = int(SpecialFaultCode.ENGINE_UNHEALTHY)
    metadata.fault_collection_started_at = 1.0
    for rank, node_name in enumerate(("node_0", "node_1")):
        manager.nodes[node_name].software_fault_infos = {
            f"1:1000002:{rank}": FaultInfo.from_exception(RuntimeError("collective failed"), rank, 2, instance_id=1)
        }
    if hardware_case != "none":
        observation_only = hardware_case == "observation"
        manager.nodes["node_0"].hardware_fault_infos = {
            "hardware:0": FI(
                fault_type=HardwareFaultType.CARD_UNHEALTHY,
                npu_name="",
                fault_code=0x81078603 if observation_only else 0x2000,
                fault_level=FaultLevel.L2 if observation_only else FaultLevel.L4,
            )
        }
    with (
        patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as instance_manager_cls,
        patch.object(EngineFastRecoveryStrategy, "is_applicable", return_value=True),
        patch.object(manager.executor, "submit") as submit,
    ):
        instance_manager_cls.return_value.get_instance.return_value = _hardware_dp_instance()
        manager._process_instance_strategy(1)

    submit.assert_called_once()
    if hardware_case == "actionable":
        assert isinstance(metadata.strategy, InstanceReconfigurationStrategy)
    else:
        assert isinstance(metadata.strategy, EngineRecoveryStrategy)


@pytest.mark.parametrize(
    ("enable_relaunch", "expected"),
    [(True, EngineRelaunchStrategy), (False, InstanceReconfigurationStrategy)],
)
def test_incomplete_ft_status_round_times_out_to_fallback(fault_manager_with_instances, enable_relaunch, expected):
    manager = fault_manager_with_instances
    manager.config.fault_tolerance_config.enable_dp_scale_down = True
    manager.config.fault_tolerance_config.enable_engine_relaunch = enable_relaunch
    manager.config.fault_tolerance_config.fault_collection_timeout_seconds = 5
    metadata = manager.instances[1]
    metadata.fault_level = FaultLevel.L2
    metadata.fault_code = int(SpecialFaultCode.ENGINE_UNHEALTHY)
    metadata.fault_collection_started_at = 1.0
    manager.nodes["node_0"].software_fault_infos = {
        "1:1000002:0": FaultInfo.from_exception(RuntimeError("collective failed"), 0, 2, instance_id=1)
    }

    with (
        patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as instance_manager_cls,
        patch("motor.controller.fault_tolerance.fault_manager.time.time", return_value=7.0),
        patch.object(manager.executor, "submit") as submit,
    ):
        instance_manager_cls.return_value.get_instance.return_value = _hardware_dp_instance()
        manager._process_instance_strategy(1)

    submit.assert_called_once()
    assert isinstance(metadata.strategy, expected)


def test_fast_recovery_failure_falls_back_directly_to_reconfiguration(fault_manager_with_instances):
    manager = fault_manager_with_instances
    manager.config.fault_tolerance_config.enable_dp_scale_down = True
    metadata = manager.instances[1]
    metadata.fault_level = FaultLevel.L2
    metadata.fault_code = int(SpecialFaultCode.ENGINE_UNHEALTHY)
    placeholder = EngineFastRecoveryStrategy()
    placeholder.execute(1)
    metadata.strategy = placeholder
    metadata.strategy_fault_level = FaultLevel.L2
    manager.nodes["node_0"].software_fault_infos = {
        "1000002:0": FaultInfo.from_exception(RuntimeError("peer fault"), engine_id=0, engine_status=2)
    }
    manager.nodes["node_1"].software_fault_infos = {
        "1000001:1": FaultInfo.from_exception(RuntimeError("executor died"), engine_id=1, engine_status=1)
    }

    with (
        patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as instance_manager_cls,
        patch.object(manager, "_refresh_instance_fault_level"),
        patch.object(manager.executor, "submit") as submit,
    ):
        instance_manager_cls.return_value.get_instance.return_value = _hardware_dp_instance()
        manager._process_instance_strategy(1)
        manager._process_instance_strategy(1)

    submit.assert_called_once()
    assert isinstance(metadata.strategy, InstanceReconfigurationStrategy)
    submit.return_value.add_done_callback.assert_called_once_with(manager._on_strategy_finished)


@pytest.mark.parametrize("failed", [True, False])
def test_strategy_completion_records_failure_flag(fault_manager_with_instances, failed):
    manager = fault_manager_with_instances
    strategy = MagicMock()
    strategy.is_finished.return_value = True
    strategy.is_failed.return_value = failed
    strategy.strategy_context = ScaleDownContext()
    metadata = manager.instances[1]
    metadata.strategy = strategy
    metadata.strategy_fault_level = FaultLevel.L2
    metadata.prev_strategy_failed = not failed
    if not failed:
        get_ft_runtime_store().transition(
            1,
            phase=FtPhase.SCALED_DOWN_RUNNING,
            original_dp_ranks=[0, 1],
            dead_committed=[1],
            serving_published=True,
        )

    with (
        patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as mock_im_class,
        patch.object(manager, "_consume_software_evidence") as mock_clear,
        patch.object(manager, "_build_recovery_context") as build_context,
        patch.object(manager, "_refresh_instance_fault_level"),
    ):
        mock_im_class.return_value = MagicMock()
        manager._process_instance_strategy(1)

    assert metadata.prev_strategy_failed is failed
    assert metadata.strategy is None
    build_context.assert_not_called()
    if failed:
        mock_clear.assert_not_called()
    else:
        assert get_ft_runtime_store().get(1)["phase"] == FtPhase.SCALED_DOWN_RUNNING.value


def test_new_higher_level_evidence_does_not_preempt_active_recovery_round(fault_manager_with_instances):
    manager = fault_manager_with_instances
    manager.config.fault_tolerance_config.enable_dp_scale_down = False
    metadata = manager.instances[1]
    metadata.fault_level = FaultLevel.L3
    metadata.strategy_fault_level = FaultLevel.L2
    manager.strategies[FaultLevel.L3] = lambda *_: EngineRelaunchStrategy
    current = MagicMock()
    current.is_finished.return_value = True
    current_future = MagicMock()
    current_future.done.return_value = False
    metadata.strategy = current
    metadata.strategy_future = current_future

    with (
        patch.object(manager.executor, "submit") as submit,
        patch.object(manager, "_refresh_instance_fault_level"),
    ):
        manager._process_instance_strategy(1)
        current.stop.assert_not_called()
        submit.assert_not_called()
        assert metadata.strategy is current
        assert metadata.strategy_preempted is False


def _hardware_dp_instance() -> Instance:
    instance = Instance(
        job_name="decode-1",
        model_name="model",
        id=1,
        role="decode",
        parallel_config=ParallelConfig(dp_size=2),
    )
    instance.add_endpoints(
        "192.168.1.1",
        {
            0: Endpoint(
                id=0,
                ip="192.168.1.1",
                business_port="8000",
                device_infos=[DeviceInfo(device_id="0", rank_id="0")],
            )
        },
    )
    instance.add_endpoints(
        "192.168.1.2",
        {
            1: Endpoint(
                id=1,
                ip="192.168.1.2",
                business_port="8000",
                device_infos=[DeviceInfo(device_id="1", rank_id="1")],
            )
        },
    )
    for endpoint in instance.get_all_endpoints():
        endpoint.hb_timestamp = time.time()
    instance.status = InsStatus.ACTIVE
    from motor.common.resources.instance import FtCapabilitySnapshot

    instance.ft_capability = FtCapabilitySnapshot(
        enabled=True,
        scale_down_supported=True,
        external_lb=True,
    )
    return instance


def _single_dp_instance() -> Instance:
    instance = Instance(
        job_name="decode-single",
        model_name="model",
        id=1,
        role="decode",
        parallel_config=ParallelConfig(dp_size=1),
    )
    instance.add_endpoints(
        "192.168.1.1",
        {
            0: Endpoint(
                id=0,
                ip="192.168.1.1",
                business_port="8000",
                device_infos=[DeviceInfo(device_id="0", rank_id="0")],
            )
        },
    )
    instance.status = InsStatus.ACTIVE
    return instance


def test_active_single_dp_engine_dead_prefers_relaunch_over_reconfiguration(fault_manager_with_instances):
    """A docker-only instance must not turn one dead engine into a stop request."""
    manager = fault_manager_with_instances
    manager.config.fault_tolerance_config.enable_dp_scale_down = True
    manager.config.fault_tolerance_config.enable_engine_relaunch = True
    metadata = manager.instances[1]
    metadata.fault_level = FaultLevel.L2
    metadata.fault_code = int(SpecialFaultCode.ENGINE_DEAD)
    manager.nodes["node_0"].software_fault_infos = {
        "1:1000001:0": FaultInfo.from_exception(
            RuntimeError("engine died"), engine_id=0, engine_status=1, instance_id=1
        )
    }
    instance_manager = MagicMock()
    instance_manager.get_instance.return_value = _single_dp_instance()
    instance_manager.get_instance_by_job_name.return_value = instance_manager.get_instance.return_value

    with (
        patch("motor.controller.fault_tolerance.fault_manager.InstanceManager", return_value=instance_manager),
        patch.object(manager.executor, "submit") as submit,
    ):
        manager._process_instance_strategy(1)

    submit.assert_called_once()
    assert isinstance(metadata.strategy, EngineRelaunchStrategy)


@pytest.mark.parametrize(
    ("enable_engine_relaunch", "expected_strategy"),
    [
        (True, EngineRelaunchStrategy),
        (False, InstanceReconfigurationStrategy),
    ],
)
def test_pre_ready_engine_dead_uses_baseline_recovery_without_dp_collection(
    fault_manager_with_instances,
    enable_engine_relaunch,
    expected_strategy,
):
    manager = fault_manager_with_instances
    manager.config.fault_tolerance_config.enable_dp_scale_down = True
    manager.config.fault_tolerance_config.enable_engine_relaunch = enable_engine_relaunch
    metadata = manager.instances[1]
    metadata.recovery_ready = False
    manager.nodes["node_0"].hardware_fault_infos = {
        "startup-card": FI(
            fault_type=HardwareFaultType.CARD_UNHEALTHY,
            npu_name="Ascend910-0",
            fault_code=0x9001,
            fault_level=FaultLevel.L5,
        )
    }
    instance = _reportable_instance("192.168.1.1", engine_ids=(0,))
    instance.id = 1
    instance.job_name = "job1"
    instance.status = InsStatus.INITIAL
    instance_manager = MagicMock()
    instance_manager.get_instance.return_value = instance
    instance_manager.get_instance_by_job_name.return_value = instance
    instance_manager.is_instance_separated.return_value = False
    fault = FaultInfo.from_exception(RuntimeError("engine died during startup"), 0, 1, instance_id=1)

    with (
        patch("motor.controller.fault_tolerance.fault_manager.InstanceManager", return_value=instance_manager),
        patch.object(ServingOverlay, "withdraw") as withdraw,
        patch.object(manager.executor, "submit") as submit,
    ):
        result = manager.report_software_fault(fault, pod_ip="192.168.1.1", instance_id=1)
        manager._process_instance_strategy(1)

    assert result.accepted
    assert metadata.fault_level == FaultLevel.L2
    assert metadata.fault_collection_started_at is None
    assert get_ft_runtime_store().get(1)["phase"] != FtPhase.WAITING_ENGINE_FAULT
    assert get_ft_runtime_store().get(1)["action"] != "SCALE_DOWN"
    withdraw.assert_not_called()
    submit.assert_called_once()
    assert isinstance(metadata.strategy, expected_strategy)


def test_pre_ready_relaunch_success_does_not_repeat_without_new_fault(fault_manager_with_instances):
    """Settling a startup relaunch must clear the old L2 before the next scheduling pass."""
    from motor.controller.fault_tolerance.strategy.engine_ft.engine_relaunch import RelaunchState

    manager = fault_manager_with_instances
    manager.config.fault_tolerance_config.enable_dp_scale_down = True
    manager.config.fault_tolerance_config.enable_engine_relaunch = True
    metadata = manager.instances[1]
    metadata.recovery_ready = False
    instance = _reportable_instance("192.168.1.1", engine_ids=(0,))
    instance.id = 1
    instance.job_name = "job1"
    instance.status = InsStatus.INITIAL
    instance_manager = MagicMock()
    instance_manager.get_instance.return_value = instance
    instance_manager.get_instance_by_job_name.return_value = instance
    instance_manager.is_instance_separated.return_value = False
    fault = FaultInfo.from_exception(RuntimeError("startup engine exit"), 0, 1, instance_id=1)
    with (
        patch("motor.controller.fault_tolerance.fault_manager.InstanceManager", return_value=instance_manager),
        patch.object(manager.executor, "submit") as submit,
    ):
        assert manager.report_software_fault(fault, pod_ip="192.168.1.1", instance_id=1).accepted
        strategy = EngineRelaunchStrategy()
        manager._prepare_recovery_task(1, strategy, manager._capture_software_evidence(1))
        get_ft_runtime_store().prepare_after_engine_relaunch(1, strategy.recovery_id)
        strategy._finish(RelaunchState.SUCCESS)
        metadata.strategy = strategy
        metadata.strategy_future = _done_future()
        manager._process_instance_strategy(1)
        assert not metadata.software_fault_infos
        assert metadata.fault_level == FaultLevel.HEALTHY
        assert metadata.fault_code == 0
        manager._process_instance_strategy(1)
        submit.assert_not_called()


def test_higher_hardware_fault_uses_scale_down_despite_engine_unhealthy(fault_manager_with_instances):
    manager = fault_manager_with_instances
    manager.config.fault_tolerance_config.enable_dp_scale_down = True
    manager.instances[1].fault_level = FaultLevel.L5
    manager.instances[1].fault_code = 0x8F184C16
    manager.instances[1].hardware_fault_observed_at = 10.0
    manager.instances[1].fault_collection_started_at = 11.0
    manager.instances[1].software_dead_observed_at = {1: 11.0}
    manager.instances[1].software_unhealthy_observed_at = {0: 11.0}
    manager.nodes["node_1"].hardware_fault_infos = {
        "8f184c16:Ascend910-1": FI(
            fault_type=HardwareFaultType.CARD_UNHEALTHY,
            npu_name="Ascend910-1",
            fault_code=0x8F184C16,
            fault_level=FaultLevel.L5,
        )
    }
    manager.nodes["node_0"].software_fault_infos = {
        "1000002:0": FaultInfo.from_exception(RuntimeError("unhealthy"), engine_id=0, engine_status=2)
    }
    manager.nodes["node_1"].software_fault_infos = {
        "1000001:1": FaultInfo.from_exception(RuntimeError("dead"), engine_id=1, engine_status=1)
    }
    manager.strategies[FaultLevel.L5] = lambda *_: ScaleP2DStrategy

    instance_manager = MagicMock()
    instance_manager.get_instance.return_value = _hardware_dp_instance()
    with (
        patch("motor.controller.fault_tolerance.fault_manager.InstanceManager", return_value=instance_manager),
        patch.object(manager.executor, "submit") as mock_submit,
    ):
        manager._process_instance_strategy(1)

    mock_submit.assert_called_once()
    strategy = manager.instances[1].strategy
    assert isinstance(strategy, EngineRecoveryStrategy)
    assert strategy.strategy_context.pending_removed_ranks == (1,)
    assert strategy.strategy_context.source == "hardware"
    assert strategy.strategy_context.fallback_strategy == "InstanceReconfigurationStrategy"


def test_next_collection_excludes_previously_committed_dead_rank(
    fault_manager_with_instances,
):
    manager = fault_manager_with_instances
    instance = _hardware_dp_instance()
    get_ft_runtime_store().transition(
        1,
        phase=FtPhase.SCALED_DOWN_RUNNING,
        original_dp_ranks=[0, 1],
        dead_committed=[0],
    )
    manager.nodes["node_1"].software_fault_infos = {
        "dead-1": FaultInfo.from_exception(RuntimeError("dead"), 1, 1, instance_id=1)
    }

    with patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as instance_manager_cls:
        instance_manager_cls.return_value.get_instance.return_value = instance
        context = manager._build_recovery_context(1)

    assert context.pending_removed_ranks == (1,)
    assert context.collection_complete is False
    assert context.all_dp_removed is True
    get_ft_runtime_store().clear()


def test_hardware_first_waits_five_seconds_then_reconfigures_without_ft(
    fault_manager_with_instances,
):
    manager = fault_manager_with_instances
    manager.config.fault_tolerance_config.enable_dp_scale_down = True
    manager.config.fault_tolerance_config.hardware_ft_correlation_window_sec = 5
    metadata = manager.instances[1]
    metadata.fault_level = FaultLevel.L5
    metadata.fault_code = 0x9001
    metadata.hardware_fault_observed_at = 10.0
    manager.nodes["node_1"].hardware_fault_infos = {
        "card-1": FI(
            fault_type=HardwareFaultType.CARD_UNHEALTHY,
            npu_name="Ascend910-1",
            fault_code=0x9001,
            fault_level=FaultLevel.L5,
        )
    }

    instance_manager = MagicMock()
    instance_manager.get_instance.return_value = _hardware_dp_instance()
    with (
        patch("motor.controller.fault_tolerance.fault_manager.InstanceManager", return_value=instance_manager),
        patch("motor.controller.fault_tolerance.fault_manager.time.time", return_value=14.9),
        patch.object(manager.executor, "submit") as submit,
    ):
        manager._process_instance_strategy(1)
        submit.assert_called_once()
        assert isinstance(metadata.strategy, EngineRecoveryStrategy)
        metadata.strategy = None
        metadata.strategy_future = None
        submit.reset_mock()
        patcher = patch("motor.controller.fault_tolerance.fault_manager.time.time", return_value=15.0)
        with patcher:
            manager._process_instance_strategy(1)

    submit.assert_called_once()
    assert isinstance(metadata.strategy, InstanceReconfigurationStrategy)


def test_pre_ready_hardware_fault_is_baselined_without_opening_window(
    fault_manager_with_instances,
):
    manager = fault_manager_with_instances
    metadata = manager.instances[1]
    metadata.recovery_ready = False

    manager._record_hardware_fault_event(1, {"node_0:card-0"})

    assert metadata.hardware_fault_observed_at is None
    assert metadata.ignored_pre_ready_hardware_faults == {"node_0:card-0"}


def test_ready_baselines_hardware_and_discards_startup_ft_status(
    fault_manager_with_instances,
):
    manager = fault_manager_with_instances
    manager.instances[1].fault_collection_deadline = 100.0
    manager.instances[1].hardware_correlation_deadline = 100.0
    manager.config.fault_tolerance_config.enable_dp_scale_down = True
    metadata = manager.instances[1]
    metadata.recovery_ready = False
    fault = FI(
        fault_type=HardwareFaultType.CARD_UNHEALTHY,
        npu_name="Ascend910-0",
        fault_code=0x9001,
        fault_level=FaultLevel.L5,
    )
    manager.nodes["node_0"].hardware_fault_infos = {"card-0": fault}
    manager.nodes["node_0"].software_fault_infos = {
        "startup-dead": FaultInfo.from_exception(RuntimeError("starting"), 0, 1, instance_id=1)
    }
    replacement = Mock(id=1, job_name="job1")
    superseded = Mock(id=0, job_name="job1")
    instance_manager = MagicMock()
    instance_manager.get_instances.return_value = [superseded, replacement]
    get_ft_runtime_store().transition(0, phase=FtPhase.RECONFIGURING)

    with (
        patch.object(manager, "_refresh_instance_fault_level") as refresh,
        patch("motor.controller.fault_tolerance.fault_manager.InstanceManager", return_value=instance_manager),
    ):
        manager.update(replacement, ObserverEvent.INSTANCE_READY)
        get_ft_runtime_store().transition(1, phase=FtPhase.SCALING_DOWN)
        manager.update(superseded, ObserverEvent.INSTANCE_READY)

    assert metadata.recovery_ready is True
    assert hardware_fault_identity("node_0", fault) in metadata.ignored_pre_ready_hardware_faults
    assert manager.nodes["node_0"].software_fault_infos == {}
    assert get_ft_runtime_store().get(0) is None
    assert get_ft_runtime_store().get(1)["phase"] == FtPhase.SCALING_DOWN.value
    refresh.assert_called_once_with(1)

    assert manager.instances[1].fault_collection_deadline is None
    assert manager.instances[1].hardware_correlation_deadline is None


def test_ready_does_not_ignore_startup_hardware_when_scale_down_is_disabled(
    fault_manager_with_instances,
):
    manager = fault_manager_with_instances
    manager.config.fault_tolerance_config.enable_dp_scale_down = False
    metadata = manager.instances[1]
    metadata.recovery_ready = False
    fault = FI(
        fault_type=HardwareFaultType.CARD_UNHEALTHY,
        npu_name="Ascend910-0",
        fault_code=0x9001,
        fault_level=FaultLevel.L5,
    )
    manager.nodes["node_0"].hardware_fault_infos = {"card-0": fault}

    with patch.object(manager, "_refresh_instance_fault_level"):
        manager._mark_instance_recovery_ready(1)

    assert metadata.ignored_pre_ready_hardware_faults == set()


def test_hardware_first_complete_ft_after_window_still_scales_down(
    fault_manager_with_instances,
):
    manager = fault_manager_with_instances
    manager.config.fault_tolerance_config.enable_dp_scale_down = True
    manager.config.fault_tolerance_config.hardware_ft_correlation_window_sec = 5
    metadata = manager.instances[1]
    metadata.fault_level = FaultLevel.L5
    metadata.fault_code = 0x9001
    metadata.hardware_fault_observed_at = 10.0
    metadata.fault_collection_started_at = 15.1
    manager.nodes["node_1"].hardware_fault_infos = {
        "card-1": FI(
            fault_type=HardwareFaultType.CARD_UNHEALTHY,
            npu_name="Ascend910-1",
            fault_code=0x9001,
            fault_level=FaultLevel.L5,
        )
    }
    manager.nodes["node_0"].software_fault_infos = {
        "unhealthy-0": FaultInfo.from_exception(RuntimeError("peer"), 0, 2, instance_id=1),
        "dead-1": FaultInfo.from_exception(RuntimeError("dead"), 1, 1, instance_id=1),
    }

    instance_manager = MagicMock()
    instance_manager.get_instance.return_value = _hardware_dp_instance()
    with (
        patch("motor.controller.fault_tolerance.fault_manager.InstanceManager", return_value=instance_manager),
        patch.object(manager.executor, "submit") as submit,
    ):
        manager._process_instance_strategy(1)

    submit.assert_called_once()
    assert isinstance(metadata.strategy, EngineRecoveryStrategy)


@pytest.mark.parametrize("reported_rank", [0, 1], ids=["survivor", "affected-rank"])
def test_hardware_scale_down_waits_for_dead_and_survivor_evidence(fault_manager_with_instances, reported_rank):
    manager = fault_manager_with_instances
    instance = _hardware_dp_instance()
    manager.nodes["node_1"].hardware_fault_infos = {
        "card-1": FI(
            fault_type=HardwareFaultType.CARD_UNHEALTHY,
            npu_name="Ascend910-1",
            fault_code=0x9001,
            fault_level=FaultLevel.L5,
        )
    }
    manager.nodes[f"node_{reported_rank}"].software_fault_infos = {
        f"unhealthy-{reported_rank}": FaultInfo.from_exception(RuntimeError("peer"), reported_rank, 2, instance_id=1)
    }

    with patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as instance_manager_cls:
        instance_manager_cls.return_value.get_instance.return_value = instance
        context = manager._build_recovery_context(1)

    assert context.pending_removed_ranks == ()
    assert context.hardware_affected_ranks == (1,)
    assert context.collection_complete is False
    assert context.engine_fault_observed is True


def test_node_hardware_fault_selects_all_dp_ranks_on_faulty_nodes(
    fault_manager_with_instances,
):
    manager = fault_manager_with_instances
    instance = _hardware_dp_instance()
    manager.nodes["node_0"].hardware_fault_infos = {
        "all-devices": FI(
            fault_type=HardwareFaultType.CARD_UNHEALTHY,
            npu_name="",
            fault_code=0x81078603,
            fault_level=FaultLevel.L5,
        )
    }
    manager.nodes["node_1"].hardware_fault_infos = {
        "all-devices-peer": FI(
            fault_type=HardwareFaultType.CARD_UNHEALTHY,
            npu_name="",
            fault_code=0x81078603,
            fault_level=FaultLevel.L5,
        )
    }
    manager.nodes["node_0"].software_fault_infos = {
        "dead-0": FaultInfo.from_exception(RuntimeError("dead"), 0, 1, instance_id=1),
        "unhealthy-1": FaultInfo.from_exception(RuntimeError("peer"), 1, 2, instance_id=1),
    }

    with patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as instance_manager_cls:
        instance_manager_cls.return_value.get_instance.return_value = instance
        context = manager._build_recovery_context(1)

    assert context.pending_removed_ranks == (0,)
    assert context.hardware_affected_ranks == (0, 1)
    assert context.collection_complete is True
    assert context.all_dp_removed is False


def test_l2_hardware_fault_covering_all_dp_is_not_actionable(
    fault_manager_with_instances,
):
    manager = fault_manager_with_instances
    instance = _hardware_dp_instance()
    for node_name in ("node_0", "node_1"):
        manager.nodes[node_name].hardware_fault_infos = {
            "pre-separate": FI(
                fault_type=HardwareFaultType.CARD_UNHEALTHY,
                npu_name="",
                fault_code=0x81078603,
                fault_level=FaultLevel.L2,
            )
        }

    with patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as instance_manager_cls:
        instance_manager_cls.return_value.get_instance.return_value = instance
        context = manager._build_recovery_context(1)

    assert context.source == "software"
    assert context.pending_removed_ranks == ()
    assert context.all_dp_removed is False


def test_node_hardware_fault_keeps_dp_on_other_nodes_as_survivor(fault_manager_with_instances):
    manager = fault_manager_with_instances
    instance = _hardware_dp_instance()
    manager.nodes["node_1"].hardware_fault_infos = {
        "node-fault": FI(
            fault_type=HardwareFaultType.NODE_UNHEALTHY,
            npu_name="",
            fault_code=0x9002,
            fault_level=FaultLevel.L5,
        )
    }
    manager.nodes["node_0"].software_fault_infos = {
        "survivor": FaultInfo.from_exception(RuntimeError("peer"), 0, 2, instance_id=1)
    }

    with patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as instance_manager_cls:
        instance_manager_cls.return_value.get_instance.return_value = instance
        context = manager._build_recovery_context(1)

    assert context.pending_removed_ranks == ()
    assert context.hardware_affected_ranks == (1,)
    assert context.collection_complete is False
    assert context.all_dp_removed is False


def test_all_dp_on_faulty_node_reconfigures_instead_of_scaling_down(fault_manager):
    fault_manager.config.fault_tolerance_config.enable_dp_scale_down = True
    context = ScaleDownContext(
        pending_removed_ranks=(0, 1),
        source="hardware",
        collection_complete=True,
        engine_fault_observed=True,
        all_dp_removed=True,
        hardware_fault_observed=True,
        hardware_affected_ranks=(0, 1),
        hardware_ft_observed=True,
    )

    plan = build_recovery_plan(
        fault_manager.config,
        context,
        DpScaleDownStrategy,
        FaultLevel.L5,
    )

    assert plan.strategy is InstanceReconfigurationStrategy


def test_inactive_instance_without_fault_evidence_waits_for_rescheduling(fault_manager):
    context = ScaleDownContext(source="software", engine_recovery_eligible=False)

    plan = build_recovery_plan(
        fault_manager.config,
        context,
        None,
        FaultLevel.L1,
    )

    assert plan.strategy is None
    assert plan.fallback is InstanceReconfigurationStrategy


def test_hardware_fault_without_engine_fault_does_not_start_l2_recovery(
    fault_manager_with_instances,
):
    manager = fault_manager_with_instances
    metadata = manager.instances[1]
    metadata.fault_level = FaultLevel.L2
    metadata.fault_code = 0x81078603
    manager.nodes["node_0"].hardware_fault_infos = {
        "existing": FI(
            fault_type=HardwareFaultType.CARD_UNHEALTHY,
            npu_name="",
            fault_code=0x81078603,
            fault_level=FaultLevel.L2,
        )
    }

    with (
        patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as instance_manager_cls,
        patch.object(manager.executor, "submit") as submit,
    ):
        instance_manager_cls.return_value.get_instance.return_value = _hardware_dp_instance()
        manager._process_instance_strategy(1)

    submit.assert_not_called()


def test_unmapped_hardware_fault_keeps_original_scale_p2d(fault_manager_with_instances):
    manager = fault_manager_with_instances
    manager.config.fault_tolerance_config.enable_dp_scale_down = True
    manager.instances[1].fault_level = FaultLevel.L5
    manager.instances[1].fault_code = 0x2000
    manager.nodes["node_1"].hardware_fault_infos = {
        "2000:switch0": FAULT_SWITCH_L2.model_copy(update={"fault_level": FaultLevel.L5})
    }
    manager.nodes["node_0"].hardware_fault_infos = {
        "2000:Ascend910-0": FI(
            fault_type=HardwareFaultType.CARD_UNHEALTHY,
            npu_name="Ascend910-0",
            fault_code=0x2000,
            fault_level=FaultLevel.L5,
        )
    }
    manager.strategies[FaultLevel.L5] = lambda *_: ScaleP2DStrategy

    instance_manager = MagicMock()
    instance_manager.get_instance.return_value = _hardware_dp_instance()
    with (
        patch("motor.controller.fault_tolerance.fault_manager.InstanceManager", return_value=instance_manager),
        patch.object(manager.executor, "submit"),
    ):
        manager._process_instance_strategy(1)

    assert isinstance(manager.instances[1].strategy, ScaleP2DStrategy)


def test_failed_hardware_scale_down_uses_reconfiguration_fallback(fault_manager_with_instances):
    manager = fault_manager_with_instances
    manager.config.fault_tolerance_config.enable_dp_scale_down = True
    metadata = manager.instances[1]
    metadata.fault_level = FaultLevel.L5
    metadata.fault_code = 0x8F184C16
    metadata.prev_strategy_failed = True
    metadata.prev_strategy_name = "DpScaleDownStrategy"
    metadata.prev_strategy_fallback = "ScaleP2DStrategy"

    manager.nodes["node_1"].hardware_fault_infos = {
        "card": FI(
            fault_type=HardwareFaultType.CARD_UNHEALTHY,
            npu_name="Ascend910-1",
            fault_code=0x8F184C16,
            fault_level=FaultLevel.L5,
        )
    }
    instance_manager = MagicMock()
    instance_manager.get_instance.return_value = _hardware_dp_instance()
    with (
        patch("motor.controller.fault_tolerance.fault_manager.InstanceManager", return_value=instance_manager),
        patch.object(manager.executor, "submit"),
    ):
        manager._process_instance_strategy(1)

    assert isinstance(metadata.strategy, InstanceReconfigurationStrategy)


def test_partial_engine_snapshot_waits_despite_fast_recovery_hook(fault_manager_with_instances):
    manager = fault_manager_with_instances
    manager.config.fault_tolerance_config.enable_dp_scale_down = True
    metadata = manager.instances[1]
    metadata.fault_level = FaultLevel.L2
    metadata.fault_code = int(SpecialFaultCode.ENGINE_UNHEALTHY)
    manager.nodes["node_0"].hardware_fault_infos = {
        "2000:Ascend910-0": FI(
            fault_type=HardwareFaultType.CARD_UNHEALTHY,
            npu_name="Ascend910-0",
            fault_code=0x2000,
            fault_level=FaultLevel.L2,
        )
    }
    manager.nodes["node_0"].software_fault_infos = {
        "1000002:0": FaultInfo.from_exception(RuntimeError("unhealthy"), engine_id=0, engine_status=2)
    }

    instance_manager = MagicMock()
    instance_manager.get_instance.return_value = _hardware_dp_instance()
    with (
        patch("motor.controller.fault_tolerance.fault_manager.InstanceManager", return_value=instance_manager),
        patch.object(EngineFastRecoveryStrategy, "is_applicable", return_value=True),
        patch.object(manager.executor, "submit"),
    ):
        manager._process_instance_strategy(1)

    assert isinstance(manager.instances[1].strategy, EngineRecoveryStrategy)


def test_handled_hardware_fault_does_not_retrigger_recovery(fault_manager_with_instances):
    manager = fault_manager_with_instances
    fault = FI(
        fault_type=HardwareFaultType.CARD_UNHEALTHY,
        npu_name="Ascend910-1",
        fault_code=0x8F184C16,
        fault_level=FaultLevel.L5,
    )
    manager.nodes["node_1"].hardware_fault_infos = {"8f184c16:Ascend910-1": fault}
    metadata = manager.instances[1]
    metadata.fault_level = FaultLevel.L5
    metadata.fault_code = fault.fault_code
    metadata.handled_hardware_faults.add(hardware_fault_identity("node_1", fault))
    instance_manager = _mk_core_im(_hardware_dp_instance())

    with patch(_FAULT_MGR_IM, return_value=instance_manager):
        manager._refresh_instance_fault_level(1)

    assert metadata.fault_level == FaultLevel.HEALTHY
    instance_manager.separate_instance.assert_not_called()
    instance_manager.recover_instance.assert_called_once_with(1)


def test_mappable_l5_hardware_fault_defers_legacy_separation_for_dp_scale_down(fault_manager_with_instances):
    manager = fault_manager_with_instances
    manager.config.fault_tolerance_config.enable_dp_scale_down = True
    fault = FI(
        fault_type=HardwareFaultType.CARD_UNHEALTHY,
        npu_name="Ascend910-1",
        fault_code=0x8F184C16,
        fault_level=FaultLevel.L5,
    )
    manager.nodes["node_1"].hardware_fault_infos = {"8f184c16:Ascend910-1": fault}

    instance_manager = MagicMock()
    instance_manager.get_instance.return_value = _hardware_dp_instance()
    with patch("motor.controller.fault_tolerance.fault_manager.InstanceManager", return_value=instance_manager):
        manager._refresh_instance_fault_level(1)

    assert manager.instances[1].fault_level == FaultLevel.L5
    instance_manager.separate_instance.assert_not_called()


def _endpoint_with_chip_ids(pod_ip: str, chip_ids: list[int]):
    return Endpoint(
        id=0,
        ip=pod_ip,
        business_port="10000",
        device_infos=[DeviceInfo(device_id=str(chip), rank_id=str(idx)) for idx, chip in enumerate(chip_ids)],
    )


FAULT_A2_LINKDOWN_CHIP6 = FaultInfo(
    fault_category=FaultCategory.HARDWARE,
    fault_type=HardwareFaultType.CARD_NETWORK_UNHEALTHY,
    npu_name="Ascend910-6",
    fault_code=int(SpecialFaultCode.CARD_NETWORK_LINKDOWN),
    fault_level=FaultLevel.L6,
    origin_fault_level=OriginFaultLevel.PRE_SEPARATE_NPU,
)


@pytest.mark.parametrize("chip,owner_id", [(6, 1), (4, 2)], ids=["prefill", "decode"])
def test_a2_linkdown_isolates_only_npu_owner(fault_manager, chip, owner_id):
    fault_manager.config.hardware_type = "800I_A2"
    node_name = "node-37-210"
    fault_manager.instances.update({1: InstanceMetadata(instance_id=1), 2: InstanceMetadata(instance_id=2)})
    fault = FAULT_A2_LINKDOWN_CHIP6.model_copy(update={"npu_name": f"Ascend910-{chip}"})
    fault_manager.nodes[node_name] = NodeMetadata(
        node_name=node_name,
        instance_ids={1, 2},
        instance_pod_ips={1: "10.244.246.54", 2: "10.244.246.27"},
        instance_job_names={1: "vllm-0-p0", 2: "vllm-0-d0"},
        hardware_fault_infos={fault.fault_code: fault},
    )
    instances = {
        1: _mk_active_instance(1, "vllm-0-p0", role="prefill"),
        2: _mk_active_instance(2, "vllm-0-d0", role="decode"),
    }
    instances[1].get_all_endpoints.return_value = (_endpoint_with_chip_ids("10.244.246.54", [6, 7]),)
    instances[2].get_all_endpoints.return_value = (_endpoint_with_chip_ids("10.244.246.27", [4, 5]),)
    instance_manager = MagicMock()
    instance_manager.get_instance.side_effect = instances.get

    with patch(_CORE_IM, return_value=instance_manager), patch(_FAULT_MGR_IM, return_value=instance_manager):
        fault_manager._refresh_instance_fault_level(1)
        fault_manager._refresh_instance_fault_level(2)

    other_id = 2 if owner_id == 1 else 1
    assert fault_manager.instances[owner_id].fault_level == FaultLevel.L6
    assert fault_manager.instances[other_id].fault_level == FaultLevel.HEALTHY
    assert instance_manager.separate_instance.call_args_list == [((owner_id,),)]


@pytest.mark.parametrize(
    "role,node_count,fault,expected_level",
    [
        ("prefill", 1, FAULT_A2_LINKDOWN_CHIP6, FaultLevel.L6),
        ("decode", 1, FAULT_A2_LINKDOWN_CHIP6, FaultLevel.L6),
        ("union", 1, FAULT_A2_LINKDOWN_CHIP6, FaultLevel.L2),
        ("union", 2, FAULT_A2_LINKDOWN_CHIP6, FaultLevel.L6),
        ("decode", 1, FAULT_PRE_SEPARATE_L6, FaultLevel.L2),
    ],
    ids=["prefill", "decode", "union-single", "union-multi", "non-isolation"],
)
def test_a2_separate_fault_ingestion(fault_manager, role, node_count, fault, expected_level):
    fault_manager.config.hardware_type = "800I_A2"
    job = f"vllm-0-{role[0]}0"
    node, instance = _seed_fault_node(fault_manager, node_name=f"node-{role}")
    node.instance_job_names[1] = job
    instance.job_name = job
    instance.role = role
    instance.get_node_managers_num.return_value = node_count

    with patch(_CORE_IM, return_value=_mk_core_im(instance)):
        fault_manager._handle_fault_info_update([fault.model_copy()], node.node_name)

    assert next(iter(node.hardware_fault_infos.values())).fault_level == expected_level


def test_process_instance_strategy_skips_superseded_instance(fault_manager_with_instances):
    """Stale instance id must not launch a new strategy after assembler creates a replacement."""
    manager = fault_manager_with_instances
    manager.instances[1].fault_level = FaultLevel.L6
    manager.instances[1].fault_code = int(SpecialFaultCode.CARD_NETWORK_LINKDOWN)

    stale = _mk_active_instance(1, "vllm-0-p0", role="prefill")
    current = _mk_active_instance(4, "vllm-0-p0", role="prefill")
    mock_im = MagicMock()
    mock_im.get_instance.return_value = stale
    mock_im.get_instance_by_job_name.return_value = current

    with patch("motor.controller.fault_tolerance.fault_manager.InstanceManager", return_value=mock_im):
        manager._process_instance_strategy(1)

    assert manager.instances[1].strategy is None
    manager.executor.shutdown(wait=False)


def test_superseded_decode_still_checks_replacement_with_scale_p2d(fault_manager_with_instances):
    manager = fault_manager_with_instances
    manager.instances[1].fault_level = FaultLevel.L6
    manager.instances[1].fault_code = 0x1
    manager.strategies[FaultLevel.L6] = lambda *_: ScaleP2DStrategy

    stale = _mk_active_instance(1, "vllm-0-d0", role="decode")
    stale.status = InsStatus.INACTIVE
    replacement = _mk_active_instance(4, "vllm-0-d0", role="decode")
    replacement.status = InsStatus.INITIAL
    mock_im = MagicMock()
    mock_im.get_instance.return_value = stale
    mock_im.get_instance_by_job_name.return_value = replacement

    with (
        patch("motor.controller.fault_tolerance.fault_manager.InstanceManager", return_value=mock_im),
        patch.object(manager.executor, "submit") as submit,
    ):
        manager._process_instance_strategy(1)

    submit.assert_called_once()
    assert isinstance(manager.instances[1].strategy, ScaleP2DStrategy)


def test_scale_p2d_success_is_per_job_and_node_not_fault_code(fault_manager_with_instances):
    """A replacement id or extra code on A must not release more P capacity."""
    manager = fault_manager_with_instances
    node = manager.nodes["node_0"]
    node.hardware_fault_infos = {0x2000: FAULT_DEVICE_L3.model_copy()}
    pending = manager.get_pending_scale_p2d_nodes(1, "job1")
    manager.mark_scale_p2d_nodes_handled("job1", pending)
    with patch.object(manager, "_refresh_instance_fault_level"):
        manager._handle_fault_info_update([FAULT_DEVICE_L3.model_copy(), FAULT_NODE_L3.model_copy()], "node_0")
    node.instance_ids.add(4)
    node.instance_job_names[4] = "job1"
    manager.instances[4] = InstanceMetadata(instance_id=4)

    assert manager.get_pending_scale_p2d_nodes(4, "job1") == {}
    assert manager.get_pending_scale_p2d_nodes(4, "other-decode") == pending
    manager.nodes["node_1"].hardware_fault_infos = {0x3000: FAULT_NODE_L3.model_copy()}
    assert set(manager.get_pending_scale_p2d_nodes(1, "job1")) == {"node_1"}
    assert len(node.hardware_fault_infos) == 2
    assert not manager.instances[1].handled_hardware_faults
    restored = NodeMetadata.model_validate(node.model_dump(mode="json"))
    assert restored.scale_p2d_handled_jobs == {"job1"}


@pytest.mark.parametrize("superseded", [False, True])
def test_scale_p2d_handled_node_is_not_resubmitted(fault_manager_with_instances, superseded):
    """The submission gate must cover both normal and stale D-instance rounds."""
    manager = fault_manager_with_instances
    metadata = manager.instances[1]
    metadata.fault_level = FaultLevel.L6
    manager.nodes["node_0"].hardware_fault_infos = {0x2000: FAULT_DEVICE_L3.model_copy()}
    manager.mark_scale_p2d_nodes_handled("job1", manager.get_pending_scale_p2d_nodes(1, "job1"))
    manager.strategies[FaultLevel.L6] = lambda *_: ScaleP2DStrategy
    with (
        patch.object(manager, "_is_superseded_instance", return_value=superseded),
        patch.object(manager, "_build_recovery_context", return_value=ScaleDownContext()),
        patch.object(manager.executor, "submit") as submit,
    ):
        manager._process_instance_strategy(1)
        manager._process_instance_strategy(1)
    submit.assert_not_called()
    assert metadata.fault_level == FaultLevel.L6


@pytest.mark.parametrize("source", ["configmap", "node"])
def test_scale_p2d_rearms_only_after_all_actionable_hardware_clears(fault_manager_with_instances, source):
    """CM removal alone is not recovery while the Node still reports NOT_READY."""
    manager = fault_manager_with_instances
    manager.etcd_config.enable_etcd_persistence = True
    node = manager.nodes["node_0"]
    with patch.object(manager, "_refresh_instance_fault_level"):
        manager._handle_fault_info_update([FAULT_DEVICE_L3.model_copy()], "node_0")
        manager._handle_node_status_update(NodeStatus.NOT_READY, "node_0")
        old_pending = manager.get_pending_scale_p2d_nodes(1, "job1")
        manager.mark_scale_p2d_nodes_handled("job1", old_pending)
        if source == "configmap":
            manager._handle_fault_info_update([], "node_0")
        else:
            manager._handle_node_status_update(NodeStatus.READY, "node_0")
        assert manager.get_pending_scale_p2d_nodes(1, "job1") == {}
        with patch.object(manager, "persist_data") as persist_reset:
            manager._handle_fault_info_update([], "node_0")
            manager._handle_node_status_update(NodeStatus.READY, "node_0")
        persist_reset.assert_called_once()
        assert not node.scale_p2d_handled_jobs
        manager._handle_fault_info_update([FAULT_DEVICE_L3.model_copy()], "node_0")
    # A late success from the old round must not consume a new fault episode.
    manager.mark_scale_p2d_nodes_handled("job1", old_pending)
    assert manager.get_pending_scale_p2d_nodes(1, "job1")


def test_scale_p2d_missing_snapshot_is_not_already_handled(fault_manager):
    assert fault_manager.get_pending_scale_p2d_nodes(999, "decode") is None


def test_scale_p2d_completion_does_not_acknowledge_dp_faults(fault_manager_with_instances):
    """ScaleP2D completion must not turn persistent hardware evidence healthy."""
    manager = fault_manager_with_instances
    metadata = manager.instances[1]
    fault = FAULT_DEVICE_L3.model_copy(update={"fault_level": FaultLevel.L6})
    manager.nodes["node_0"].hardware_fault_infos = {0x2000: fault}
    key = hardware_fault_identity("node_0", fault)
    strategy = ScaleP2DStrategy()
    strategy.bind_context(ScaleDownContext(fault_keys=(key,)))
    strategy._is_finished = True
    metadata.strategy = strategy
    with patch("motor.controller.fault_tolerance.fault_manager.InstanceManager"):
        assert manager._complete_finished_strategy(1, metadata)
    assert not metadata.handled_hardware_faults
    assert metadata.fault_level == FaultLevel.L6
    assert metadata.strategy is None


# -- Non-A2 linkdown handling -------------------------------------------------


def _seed_linkdown_node(manager, node_name="node_a"):
    node, instance = _seed_fault_node(manager, node_name=node_name)
    instance.job_name = "vllm-0-d0"
    node.instance_job_names[1] = instance.job_name
    instance_manager = MagicMock()
    instance_manager.get_instance.side_effect = lambda instance_id: instance if instance_id == 1 else None
    instance_manager.get_instance_by_job_name.return_value = instance
    return node, instance_manager


def test_non_a2_linkdown_drop_keeps_real_faults(fault_manager):
    fault_manager.config.hardware_type = "800I_A3"
    node, instance_manager = _seed_linkdown_node(fault_manager)
    with patch(_CORE_IM, return_value=instance_manager), patch(_FAULT_MGR_IM, return_value=instance_manager):
        fault_manager._handle_fault_info_update(
            [FAULT_A2_LINKDOWN_CHIP6.model_copy(), FAULT_PRE_SEPARATE_L6.model_copy()], node.node_name
        )

    stored = list(node.hardware_fault_infos.values())
    assert all(fault.fault_code != int(SpecialFaultCode.CARD_NETWORK_LINKDOWN) for fault in stored)
    assert next(fault for fault in stored if fault.fault_code == 0x00F1FEF5).fault_level == FaultLevel.L2


def test_non_a2_linkdown_cannot_block_engine_dead_relaunch(fault_manager):
    from motor.controller.fault_tolerance.strategy.strategy import level2_strategy

    fault_manager.config.hardware_type = "800I_A3"
    node, instance_manager = _seed_linkdown_node(fault_manager)
    instance = instance_manager.get_instance(1)
    with patch(_CORE_IM, return_value=instance_manager), patch(_FAULT_MGR_IM, return_value=instance_manager):
        instance.get_all_endpoints.return_value = []
        fault_manager._handle_fault_info_update([FAULT_A2_LINKDOWN_CHIP6.model_copy()], node.node_name)
        fault_manager.report_software_fault(
            FaultInfo.from_exception(RuntimeError("engine died"), engine_id=1, engine_status=1),
            pod_ip="10.0.0.1",
        )

    metadata = fault_manager.instances[1]
    assert node.hardware_fault_infos == {}
    assert (metadata.fault_level, metadata.fault_code) == (FaultLevel.L2, int(SpecialFaultCode.ENGINE_DEAD))
    assert level2_strategy(metadata.fault_code, 1, fault_manager.config) is EngineRelaunchStrategy


@pytest.mark.parametrize(
    "hardware_type,stored,fragments,count_message",
    [
        ("800I_A3", False, ("linkdown", "800I_A3", "not stored"), "Ignored 1 linkdown fault"),
        ("", True, ("hardware_type is unset", "may suppress ENGINE_DEAD"), "Stored 1 linkdown fault"),
    ],
    ids=["non-a2-dropped", "unknown-hardware-legacy"],
)
def test_linkdown_warning_is_rate_limited(caplog, fault_manager, hardware_type, stored, fragments, count_message):
    fault_manager.config.hardware_type = hardware_type
    node, instance_manager = _seed_linkdown_node(fault_manager)
    with (
        patch(_CORE_IM, return_value=instance_manager),
        patch(_FAULT_MGR_IM, return_value=instance_manager),
        caplog.at_level("WARNING"),
    ):
        for _ in range(2):
            fault_manager._handle_fault_info_update([FAULT_A2_LINKDOWN_CHIP6.model_copy()], node.node_name)

    assert bool(node.hardware_fault_infos) is stored
    assert all(fragment in caplog.text for fragment in fragments)
    assert caplog.text.count(count_message) == 1


def _seed_l3_hardware_fault(manager, fault_code, *, instance_id=1, node_name="node_0"):
    """Seed a hardware CardUnhealthy L3 fault on a node owned by the instance."""
    manager.instances[instance_id] = InstanceMetadata(instance_id=instance_id)
    fault = FI(
        fault_type=HardwareFaultType.CARD_UNHEALTHY,
        npu_name="npu0",
        fault_code=fault_code,
        fault_level=FaultLevel.L3,
    )
    node = NodeMetadata(
        node_name=node_name,
        instance_ids={instance_id},
        instance_pod_ips={instance_id: "10.0.0.1"},
        instance_job_names={instance_id: "job1"},
    )
    node.hardware_fault_infos = {fault.fault_code: fault.model_copy()}
    manager.nodes[node_name] = node
    return node, fault


@pytest.mark.parametrize("fault_cleared", [False, True])
def test_dispatched_reconfiguration_does_not_rearm_old_hbm_recovery(fault_manager_with_instances, fault_cleared):
    manager = fault_manager_with_instances
    node, _ = _seed_l3_hardware_fault(manager, 0x80E01801)
    metadata = manager.instances[1]
    metadata.fault_level = FaultLevel.L3
    metadata.fault_code = 0x80E01801
    store = get_ft_runtime_store()
    store.mark_instance_reconfiguration_dispatched(1)
    if fault_cleared:
        node.hardware_fault_infos.clear()
    with (
        patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as instance_manager,
        patch.object(manager.executor, "submit") as submit,
    ):
        manager._refresh_instance_fault_level(1)
        manager._process_instance_strategy(1)
    submit.assert_not_called()
    instance_manager.return_value.recover_instance.assert_not_called()
    assert store.is_reconfiguration_dispatched(1)
    assert not store.is_reconfiguration_dispatched(2)


def test_inactive_instance_planner_does_not_return_generated_recovery():
    plan = build_recovery_plan(
        ControllerConfig(),
        ScaleDownContext(engine_recovery_eligible=False),
        EngineFastRecoveryStrategy,
        FaultLevel.L3,
    )
    assert plan.strategy is None


def _kill_recover_kwargs(instance_id: int, metadata: InstanceMetadata) -> dict:
    return {
        "instance_id": instance_id,
        "metadata": metadata,
        "original": [0, 1],
        "expanded": {1},
        "target_dp": 1,
        "phase": FtPhase.SCALED_DOWN_RUNNING.value,
        "seed": {1},
        "to_remove": [1],
    }


def _done_future() -> concurrent.futures.Future:
    future: concurrent.futures.Future = concurrent.futures.Future()
    future.set_result(True)
    return future


def test_kill_recover_reserves_future_before_submit(fault_manager):
    """A re-entrant start_user_recovery must see the reservation and submit once."""
    instance_id = 7
    metadata = InstanceMetadata(instance_id=instance_id)
    fault_manager.instances[instance_id] = metadata
    submitted: dict = {}

    def submit(fn, *args, **kwargs):
        second = fault_manager._try_kill_problem_pod_workers(**_kill_recover_kwargs(instance_id, metadata))
        assert second is None
        future = _done_future()
        submitted["fn"] = fn
        submitted["future"] = future
        submitted["calls"] = submitted.get("calls", 0) + 1
        return future

    fault_manager.executor.submit = submit
    result = fault_manager._try_kill_problem_pod_workers(**_kill_recover_kwargs(instance_id, metadata))

    assert result is not None
    assert result["accepted"] is True
    assert result["action"] == "kill_problem_pod_workers"
    assert submitted["calls"] == 1
    assert submitted["fn"].__name__ == "run_problem_pod_recover"
    assert fault_manager._problem_pod_recover_futures[instance_id] is submitted["future"]
    assert metadata.strategy is None


def _seed_empty_shell_runtime(instance_id: int) -> None:
    get_ft_runtime_store().transition(
        instance_id,
        phase=FtPhase.SCALED_DOWN_RUNNING,
        dead_committed=[1],
        original_dp_ranks=[0, 1],
    )


class _TrackingLock:
    def __init__(self):
        self._lock = threading.RLock()
        self.held = 0

    def __enter__(self):
        self._lock.acquire()  # pylint: disable=consider-using-with
        self.held += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        self.held -= 1
        return self._lock.__exit__(exc_type, exc, tb)


def test_empty_shell_scale_up_reserves_strategy_before_transition(fault_manager):
    """transition and submit run without metadata.lock; a re-entrant recover sees the reservation."""
    instance_id = 5
    metadata = InstanceMetadata(instance_id=instance_id)
    metadata.lock = _TrackingLock()
    fault_manager.instances[instance_id] = metadata
    _seed_empty_shell_runtime(instance_id)
    store = get_ft_runtime_store()
    real_transition = store.transition
    observed = {"held": None}

    def transition(*args, **kwargs):
        observed["held"] = metadata.lock.held
        return real_transition(*args, **kwargs)

    submitted: dict = {}

    def submit(fn, *args, **kwargs):
        with pytest.raises(RuntimeError, match="recovery already in progress"):
            fault_manager.start_user_recovery()
        future = _done_future()
        submitted["fn"] = fn
        submitted["future"] = future
        submitted["calls"] = submitted.get("calls", 0) + 1
        return future

    fault_manager.executor.submit = submit
    instance_manager = Mock()
    instance_manager.get_instance.return_value = Mock()
    with (
        patch("motor.controller.fault_tolerance.fault_manager.InstanceManager", return_value=instance_manager),
        patch(
            "motor.controller.fault_tolerance.fault_manager.TopologyResolver.expand_problem_pod_ranks",
            side_effect=ValueError("topology unavailable"),
        ),
        patch(
            "motor.controller.fault_tolerance.fault_manager.TopologyResolver.recyclable_pods",
            return_value=["192.0.2.2"],
        ),
        patch.object(store, "transition", side_effect=transition),
    ):
        result = fault_manager.start_user_recovery()

    assert observed["held"] == 0
    assert submitted["calls"] == 1
    assert submitted["fn"].__name__ == "_execute_strategy"
    assert result == [
        {
            "instance_id": instance_id,
            "action": "scale_up",
            "original_dp_ranks": [0, 1],
            "phase": FtPhase.SCALED_DOWN_RUNNING.value,
        }
    ]
    assert metadata.strategy_future is submitted["future"]


def test_empty_shell_scale_up_clears_reservation_when_submit_fails(fault_manager):
    instance_id = 6
    metadata = InstanceMetadata(instance_id=instance_id)
    fault_manager.instances[instance_id] = metadata
    _seed_empty_shell_runtime(instance_id)

    def submit(*_args, **_kwargs):
        raise RuntimeError("submit failed")

    fault_manager.executor.submit = submit
    instance_manager = Mock()
    instance_manager.get_instance.return_value = Mock()
    with (
        patch("motor.controller.fault_tolerance.fault_manager.InstanceManager", return_value=instance_manager),
        patch(
            "motor.controller.fault_tolerance.fault_manager.TopologyResolver.expand_problem_pod_ranks",
            side_effect=ValueError("topology unavailable"),
        ),
        patch(
            "motor.controller.fault_tolerance.fault_manager.TopologyResolver.recyclable_pods",
            return_value=["192.0.2.2"],
        ),
        pytest.raises(RuntimeError, match="submit failed"),
    ):
        fault_manager.start_user_recovery()

    assert metadata.strategy is None
    assert metadata.strategy_future is None


def test_empty_shell_worker_exception_releases_strategy_slot(fault_manager_with_instances):
    """A failed alternate entry point must not leave its FM slot occupied."""
    manager = fault_manager_with_instances
    metadata = manager.instances[1]
    _seed_empty_shell_runtime(1)
    strategy = DpScaleDownStrategy()
    with (
        patch("motor.controller.fault_tolerance.fault_manager.TopologyResolver.recyclable_pods", return_value=["pod"]),
        patch.object(strategy, "run_scale_up_recovery", side_effect=RuntimeError("unexpected")),
    ):
        manager._accept_empty_shell_scale_up(
            1, metadata, Mock(id=1), strategy, [0, 1], [1], FtPhase.SCALED_DOWN_RUNNING.value
        )
        assert metadata.strategy_future.exception(timeout=5) is None

    assert strategy.is_finished()
    assert strategy.is_failed()
    assert get_ft_runtime_store().get(1)["last_error"] == "unexpected"
    with patch.object(manager, "_refresh_instance_fault_level"), patch.object(manager, "persist_data"):
        assert manager._complete_finished_strategy(1, metadata)
    assert metadata.strategy is None
    assert metadata.strategy_future is None
    assert metadata.prev_strategy_failed


@pytest.mark.parametrize(
    "updates,remaining",
    [([(0, 1), (1, 2)], {1, 2}), ([(0, 2)], set())],
    ids=["new-faults-retained", "repeat-consumed"],
)
def test_settlement_consumes_only_matching_software_evidence(fault_manager_with_instances, updates, remaining):
    manager = fault_manager_with_instances
    instance = _reportable_instance("192.168.1.1", engine_ids=(0, 1))
    with patch("motor.controller.fault_tolerance.fault_manager.InstanceManager", return_value=_mk_core_im(instance)):
        fault = FaultInfo.from_exception(RuntimeError("collective"), 0, 2, instance_id=1)
        manager.report_software_fault(fault, pod_ip="192.168.1.1", instance_id=1)
        observed = manager._capture_software_evidence(1)
        for rank, status in updates:
            fault = FaultInfo.from_exception(RuntimeError("reported again"), rank, status, instance_id=1)
            fault.timestamp = "23:59:59"
            manager.report_software_fault(fault, pod_ip="192.168.1.1", instance_id=1)
    manager._consume_software_evidence(1, observed)
    faults = manager.instances[1].software_fault_infos.values()
    assert len(faults) == len(remaining)
    assert {fault.engine_status for fault in faults} == remaining


@pytest.mark.parametrize(
    "mutation,cleanup,hardware,committed,expected",
    [
        (False, False, False, False, EngineRelaunchStrategy),
        (True, False, False, False, EngineRelaunchStrategy),
        (False, True, False, False, EngineRelaunchStrategy),
        (False, False, True, False, InstanceReconfigurationStrategy),
        (False, False, False, True, EngineRelaunchStrategy),
    ],
)
def test_failed_shrink_routes_by_hardware_evidence_after_worker_exit(
    fault_manager_with_instances,
    mutation,
    cleanup,
    hardware,
    committed,
    expected,
):
    manager = fault_manager_with_instances
    manager.config.fault_tolerance_config.enable_engine_relaunch = True
    metadata = manager.instances[1]
    metadata.fault_level = FaultLevel.L2
    metadata.fault_code = int(SpecialFaultCode.ENGINE_DEAD)
    strategy = DpScaleDownStrategy()
    strategy.bind_context(ScaleDownContext(hardware_fault_observed=hardware))
    strategy.mutation_started = mutation
    strategy.cleanup_failed = cleanup
    strategy.mark_failed()
    strategy._is_finished = True
    metadata.strategy = strategy
    get_ft_runtime_store().transition(1, dead_committed=[1] if committed else [])
    with (
        patch.object(manager, "_refresh_instance_fault_level"),
        patch.object(manager.executor, "submit"),
        patch.object(
            manager,
            "_build_recovery_context",
            return_value=ScaleDownContext(hardware_fault_observed=hardware),
        ),
    ):
        assert manager._complete_finished_strategy(1, metadata)
        manager._process_instance_strategy(1)
    assert isinstance(metadata.strategy, expected)


def test_active_shrink_cancels_new_dead_after_apply_but_waits_worker_exit(fault_manager_with_instances):
    manager = fault_manager_with_instances
    metadata = manager.instances[1]
    metadata.fault_level = FaultLevel.L2
    strategy = DpScaleDownStrategy()
    strategy.bind_context(ScaleDownContext(pending_removed_ranks=(1,)))
    strategy.mutation_started = True
    metadata.strategy = strategy
    with (
        patch.object(manager, "_build_recovery_context", return_value=ScaleDownContext(pending_removed_ranks=(0, 1))),
        patch.object(manager.executor, "submit") as submit,
    ):
        manager._process_instance_strategy(1)
    assert strategy.event.is_set()
    assert strategy.invalidated
    assert metadata.strategy is strategy
    submit.assert_not_called()


def test_old_hardware_episode_cannot_acknowledge_refault_after_success(fault_manager_with_instances):
    manager = fault_manager_with_instances
    metadata = manager.instances[1]
    node = manager.nodes["node_0"]
    fault = FAULT_DEVICE_L3.model_copy()
    key = hardware_fault_identity("node_0", fault)
    node.hardware_fault_infos = {fault.fault_code: fault}
    node.scale_p2d_fault_generation = "new-episode"
    strategy = DpScaleDownStrategy()
    strategy.bind_context(ScaleDownContext(fault_keys=(key,), hardware_evidence_versions=((key, "old-episode"),)))
    strategy._is_finished = True
    metadata.strategy = strategy
    with patch.object(manager, "_refresh_instance_fault_level"):
        assert manager._complete_finished_strategy(1, metadata)
    assert key not in metadata.handled_hardware_faults


def test_settlement_keeps_slot_busy_until_evidence_consumption_finishes(fault_manager_with_instances):
    manager = fault_manager_with_instances
    metadata = manager.instances[1]
    strategy = DpScaleDownStrategy()
    strategy.bind_context(ScaleDownContext())
    strategy._is_finished = True
    metadata.strategy = strategy

    def consume(*_args):
        assert metadata.strategy is strategy
        with metadata.lock:
            assert manager._recovery_round_busy(1, metadata)

    with (
        patch.object(manager, "_consume_software_evidence", side_effect=consume),
        patch.object(manager, "_refresh_instance_fault_level"),
    ):
        assert manager._complete_finished_strategy(1, metadata)
    assert metadata.strategy is None
    assert metadata.strategy_settling is False


@pytest.mark.parametrize("removed", [False, True])
def test_unexpected_worker_exception_is_failed_and_reapable(removed):
    strategy = DpScaleDownStrategy()
    strategy.recovery_id = "worker"
    store = get_ft_runtime_store()
    store.transition(1, recovery_id=strategy.recovery_id)
    transition = store.transition

    def record_failure(*args, **kwargs):
        if removed:
            store.remove(1)
        return transition(*args, **kwargs)

    with (
        patch.object(strategy, "execute", side_effect=RuntimeError("unexpected")),
        patch.object(store, "transition", side_effect=record_failure),
    ):
        FaultManager._execute_strategy(strategy, 1)
    assert strategy.is_finished()
    assert strategy.is_failed()
    assert strategy.cleanup_failed
    if removed:
        assert store.get(1) is None
    else:
        assert store.get(1)["stage"] == "failed"


def test_native_suicide_does_not_defer_isolation_for_shrink(fault_manager_with_instances):
    from motor.controller.fault_tolerance.strategy import NmSuicideStrategy

    manager = fault_manager_with_instances
    manager.config.fault_tolerance_config.enable_dp_scale_down = True
    manager.nodes["node_1"].hardware_fault_infos = {
        0x9001: FI(
            fault_type=HardwareFaultType.CARD_UNHEALTHY,
            npu_name="Ascend910-1",
            fault_code=0x9001,
            fault_level=FaultLevel.L6,
        )
    }
    manager.strategies[FaultLevel.L6] = lambda *_: NmSuicideStrategy
    im = _mk_core_im(_hardware_dp_instance())
    with patch("motor.controller.fault_tolerance.fault_manager.InstanceManager", return_value=im):
        manager._refresh_instance_fault_level(1)
    im.separate_instance.assert_called_once_with(1)


@pytest.mark.parametrize("heartbeat_first", [True, False], ids=["heartbeat-first", "ft-first"])
def test_engine_abnormal_heartbeat_preserves_scale_down_preparation(fault_manager_with_instances, heartbeat_first):
    """An online NM's abnormal engine heartbeat must not choose Relaunch ahead of shrink."""
    from motor.common.resources.endpoint import EndpointStatus
    from motor.controller.fault_tolerance.engine_recovery.runtime import partial_loss_policy

    manager = fault_manager_with_instances
    manager.config.fault_tolerance_config.enable_dp_scale_down = True
    manager.config.fault_tolerance_config.enable_engine_relaunch = True
    metadata = manager.instances[1]
    metadata.recovery_ready = True
    instance = _hardware_dp_instance()
    for endpoint in instance.get_all_endpoints():
        endpoint.hb_timestamp = time.time()
    instance.get_all_endpoints()[1].status = EndpointStatus.ABNORMAL
    im = _mk_core_im(instance)
    im.get_instance_by_job_name.return_value = instance
    im.is_instance_separated.return_value = False
    instance.add_node_mgr("192.168.1.2", "1026")
    fault = FaultInfo.from_exception(RuntimeError("engine core died"), 1, 1, instance_id=1)

    with (
        patch("motor.controller.fault_tolerance.fault_manager.InstanceManager", return_value=im),
        patch.object(manager.executor, "submit") as submit,
    ):
        submit.return_value = concurrent.futures.Future()
        if heartbeat_first:
            instance.status = InsStatus.INACTIVE
        assert manager.report_software_fault(fault, "192.168.1.2", 1, "1026").accepted
        if not heartbeat_first:
            instance.status = InsStatus.INACTIVE
        manager._process_instance_strategy(1)
        strategy = metadata.strategy
        assert isinstance(strategy, EngineRecoveryStrategy)
        assert strategy.strategy_context.collection_complete is False
        assert get_ft_runtime_store().get(1)["pending_removed_ranks"] == [1]
        assert partial_loss_policy(1).protect_transaction
        # The executor has not run; protection must already be in place.
        submit.assert_called_once()
        manager._process_instance_strategy(1)
        assert metadata.strategy is strategy
        submit.assert_called_once()
        # Reuse the existing failure settlement; a still-running worker keeps its slot.
        strategy.mark_failed()
        strategy._is_finished = True
        manager._process_instance_strategy(1)
        assert metadata.strategy is strategy
        submit.assert_called_once()
        submit.return_value.set_result(False)
        manager._process_instance_strategy(1)
        assert metadata.strategy is None
        manager._process_instance_strategy(1)
        assert isinstance(metadata.strategy, EngineRelaunchStrategy)
        assert submit.call_count == 2


@pytest.mark.parametrize(
    "blocked_by",
    ["stale-heartbeat", "pre-ready", "forced-separation", "reconfiguring", "warming-up", "disabled"],
)
def test_inactive_engine_fault_does_not_bypass_recovery_boundaries(fault_manager_with_instances, blocked_by):
    from motor.common.resources.endpoint import EndpointStatus

    manager = fault_manager_with_instances
    manager.config.fault_tolerance_config.enable_dp_scale_down = blocked_by != "disabled"
    manager.config.fault_tolerance_config.enable_engine_relaunch = True
    metadata = manager.instances[1]
    metadata.recovery_ready = blocked_by != "pre-ready"
    instance = _hardware_dp_instance()
    instance.status = InsStatus.INACTIVE
    for endpoint in instance.get_all_endpoints():
        endpoint.hb_timestamp = time.time()
    instance.get_all_endpoints()[1].status = EndpointStatus.ABNORMAL
    if blocked_by == "stale-heartbeat":
        instance.get_all_endpoints()[1].hb_timestamp = time.time() - 30
    if blocked_by in {"reconfiguring", "warming-up"}:
        get_ft_runtime_store().transition(
            1, phase=FtPhase.RECONFIGURING if blocked_by == "reconfiguring" else FtPhase.WARMING_UP
        )
    manager.nodes["node_1"].software_fault_infos = {
        "dead": FaultInfo.from_exception(RuntimeError("engine died"), 1, 1, instance_id=1)
    }
    im = _mk_core_im(instance)
    im.is_instance_separated.return_value = blocked_by == "forced-separation"
    with patch("motor.controller.fault_tolerance.fault_manager.InstanceManager", return_value=im):
        context = manager._build_recovery_context(1)
    assert context.engine_recovery_eligible is False


@pytest.mark.parametrize("heartbeat_caught_up", [True, False], ids=["normal-heartbeat", "delayed-heartbeat"])
def test_successful_relaunch_does_not_reuse_old_dead_for_shrink(fault_manager_with_instances, heartbeat_caught_up):
    """Successful Relaunch consumes its DEAD evidence before planning another recovery."""
    from motor.common.resources.endpoint import EndpointStatus
    from motor.controller.fault_tolerance.strategy.engine_ft.engine_relaunch import RelaunchState

    manager = fault_manager_with_instances
    manager.config.fault_tolerance_config.enable_dp_scale_down = True
    manager.config.fault_tolerance_config.enable_engine_relaunch = True
    metadata = manager.instances[1]
    metadata.recovery_ready = True
    instance = _hardware_dp_instance()
    instance.status = InsStatus.ACTIVE if heartbeat_caught_up else InsStatus.INACTIVE
    for endpoint in instance.get_all_endpoints():
        endpoint.hb_timestamp = time.time()
        endpoint.status = EndpointStatus.NORMAL
    if not heartbeat_caught_up:
        instance.get_all_endpoints()[1].status = EndpointStatus.ABNORMAL
    metadata.software_fault_infos["dead"] = FaultInfo.from_exception(
        RuntimeError("engine died before relaunch"), 1, 1, instance_id=1
    )
    metadata.fault_level = FaultLevel.L2
    metadata.fault_code = int(SpecialFaultCode.ENGINE_DEAD)
    strategy = EngineRelaunchStrategy()
    strategy.bind_context(ScaleDownContext(engine_fault_observed=True, pending_removed_ranks=(1,)))
    manager._prepare_recovery_task(1, strategy, manager._capture_software_evidence(1))
    get_ft_runtime_store().transition(1, phase=FtPhase.DECIDING)
    get_ft_runtime_store().prepare_after_engine_relaunch(1, strategy.recovery_id)
    strategy._finish(RelaunchState.SUCCESS)
    metadata.strategy = strategy
    metadata.strategy_future = _done_future()
    im = _mk_core_im(instance)
    im.get_instance_by_job_name.return_value = instance
    im.is_instance_separated.return_value = False
    with (
        patch("motor.controller.fault_tolerance.fault_manager.InstanceManager", return_value=im),
        patch.object(manager.executor, "submit") as submit,
    ):
        manager._process_instance_strategy(1)
        assert metadata.strategy is None
        assert metadata.software_fault_infos == {}
        manager._process_instance_strategy(1)
        submit.assert_not_called()
    assert get_ft_runtime_store().get(1)["phase"] == FtPhase.NORMAL.value


def test_binding_fallback_preserves_cause_but_clears_previous_observations(fault_manager):
    store = get_ft_runtime_store()
    store.transition(
        1,
        phase=FtPhase.DECIDING,
        action="SCALE_DOWN",
        stage="failed",
        request_id="old-request",
        pending_removed_ranks=[1],
        original_dp_ranks=[0, 1],
        last_error="scale-down preparation timed out",
    )
    strategy = EngineRelaunchStrategy()
    fault_manager._prepare_recovery_task(1, strategy, {})
    status = store.get(1)
    assert status["action"] == "RELAUNCH"
    assert status["fallback_reason"] == "scale-down preparation timed out"
    assert status["last_error"] is None
    assert status["request_id"] == ""
    assert status["pending_removed_ranks"] == []
    assert status["original_dp_ranks"] == [0, 1]


@pytest.mark.parametrize("fails", [False, True])
def test_relaunch_worker_reports_phase_and_unhandled_failure(fault_manager, fails):
    strategy = EngineRelaunchStrategy()
    fault_manager._prepare_recovery_task(1, strategy, {})
    assert not get_ft_runtime_store().get(1)["recovery_paused"]

    def execute(_instance_id):
        status = get_ft_runtime_store().get(1)
        assert status["phase"] == FtPhase.RELAUNCHING
        assert status["stage"] == "executing"
        if fails:
            raise RuntimeError("unexpected relaunch failure")

    with (
        patch.object(strategy, "execute", side_effect=execute),
        patch("motor.controller.fault_tolerance.fault_manager.InstanceManager") as im,
        patch(
            "motor.controller.fault_tolerance.engine_recovery.serving.ServingOverlay.pause_for_recovery",
            side_effect=AssertionError("native relaunch must not depend on Coordinator pause"),
        ),
    ):
        im.return_value.get_instance.return_value = Mock(id=1)
        fault_manager._execute_strategy(strategy, 1)
    assert strategy.is_finished()
    assert strategy.is_failed() is fails
    if fails:
        status = get_ft_runtime_store().get(1)
        assert status["stage"] == "failed"
        assert status["last_error"] == "unexpected relaunch failure"


@pytest.mark.parametrize(
    "phase,action,code,previous",
    [
        ("WARMING_UP", "", 0, "DpScaleDownStrategy"),
        ("RELAUNCHING", "RELAUNCH", SpecialFaultCode.ENGINE_DEAD, "EngineRelaunchStrategy"),
        ("FAST_RECOVERING", "FAST_RECOVERY", SpecialFaultCode.ENGINE_UNHEALTHY, "EngineFastRecoveryStrategy"),
    ],
)
def test_restore_interrupted_recovery_retains_failure_chain(fault_manager, phase, action, code, previous):
    from motor.common.etcd.persistent_state import PersistentState

    fault_manager.config.fault_tolerance_config.enable_dp_scale_up = True
    level = FaultLevel.HEALTHY if phase == "WARMING_UP" else FaultLevel.L2
    data = {
        "instances": {"1": _etcd_instance_entry(instance_id=1, fault_level=level, fault_code=int(code))},
        "ft_runtime": {"1": {"phase": phase, "action": action}},
    }
    state = PersistentState(data=data, version=1, timestamp=1234567890.0, checksum="")
    state.checksum = state.calculate_checksum()
    with patch.object(fault_manager.etcd_client, "restore_data", return_value={"state": state}):
        assert fault_manager.restore_data()
    metadata, runtime = fault_manager.instances[1], get_ft_runtime_store().get(1)
    assert metadata.prev_strategy_name == previous
    assert metadata.prev_strategy_failed
    assert runtime["phase"] == FtPhase.RECONFIGURING
    if phase == "WARMING_UP":
        assert runtime["last_error"] == "controller restarted during DP scale-up; original fallback required"
        assert metadata.prev_strategy_fallback == "InstanceReconfigurationStrategy"
        with patch.object(fault_manager.executor, "submit") as submit:
            fault_manager._process_instance_strategy(1)
        submit.assert_called_once()
        assert isinstance(metadata.strategy, InstanceReconfigurationStrategy)
    elif action == "RELAUNCH":
        assert "engine relaunch" in runtime["last_error"]


def test_fast_fallback_relaunch_relapse_guard_clears_only_after_stable_ready_window(fault_manager):
    manager = fault_manager
    metadata = InstanceMetadata(instance_id=7, relaunch_relapse_guard=True)
    manager.config.fault_tolerance_config.engine_relaunch_stability_sec = 60
    with (
        patch.object(manager, "_engines_normal", return_value=True),
        patch("motor.controller.fault_tolerance.fault_manager.time.time", return_value=10) as clock,
    ):
        manager._update_relaunch_relapse_guard(7, metadata)
        assert metadata.relaunch_relapse_guard
        clock.return_value = 69
        manager._update_relaunch_relapse_guard(7, metadata)
        assert metadata.relaunch_relapse_guard
        clock.return_value = 70
        manager._update_relaunch_relapse_guard(7, metadata)
        assert not metadata.relaunch_relapse_guard


@pytest.mark.parametrize("display_name", ["renamed policy", "EngineRelaunchStrategy"])
def test_healthy_failure_uses_behavioral_result_not_display_name(fault_manager_with_instances, display_name):
    from motor.controller.fault_tolerance.engine_recovery.contracts import FailurePolicy, RecoveryAdvice, RecoveryResult

    manager = fault_manager_with_instances
    metadata = manager.instances[1]
    metadata.fault_level = FaultLevel.HEALTHY
    metadata.prev_strategy_failed = True
    metadata.prev_strategy_name = display_name
    metadata.prev_recovery_result = RecoveryResult(
        failure_policy=FailurePolicy.FAST_RECOVERY,
        advice=RecoveryAdvice.RECONFIGURE,
        evaluate_without_fault=True,
    )
    with patch.object(manager.executor, "submit"):
        manager._process_instance_strategy(1)
    assert isinstance(metadata.strategy, InstanceReconfigurationStrategy)


@pytest.mark.parametrize(
    "status,engine_status,relaunch,expected",
    [
        (InsStatus.ACTIVE, 1, False, None),
        (InsStatus.ACTIVE, 2, True, EngineRelaunchStrategy),
        (InsStatus.INACTIVE, 1, False, InstanceReconfigurationStrategy),
        (InsStatus.INACTIVE, 2, True, EngineRelaunchStrategy),
        (InsStatus.INITIAL, 1, True, EngineRelaunchStrategy),
        (InsStatus.INITIAL, 2, False, InstanceReconfigurationStrategy),
    ],
)
def test_disabled_optional_features_keep_native_submission_without_pause(
    fault_manager_with_instances, status, engine_status, relaunch, expected
):
    manager = fault_manager_with_instances
    ft = manager.config.fault_tolerance_config
    ft.enable_fast_recovery = False
    ft.enable_dp_scale_down = False
    ft.enable_engine_relaunch = relaunch
    instance = _hardware_dp_instance()
    instance.status = status
    metadata = manager.instances[1]
    metadata.fault_level = FaultLevel.L2
    metadata.fault_code = int(SpecialFaultCode.ENGINE_DEAD if engine_status == 1 else SpecialFaultCode.ENGINE_UNHEALTHY)
    metadata.software_fault_infos = {
        "engine": FaultInfo.from_exception(RuntimeError("engine fault"), 0, engine_status, instance_id=1)
    }
    im = _mk_core_im(instance)
    with (
        patch("motor.controller.fault_tolerance.fault_manager.InstanceManager", return_value=im),
        patch("motor.controller.fault_tolerance.engine_recovery.serving.ServingOverlay.pause_for_recovery") as pause,
        patch.object(manager.executor, "submit") as submit,
    ):
        manager._process_instance_strategy(1)
    pause.assert_not_called()
    if expected is None:
        assert metadata.strategy is None
        submit.assert_not_called()
    else:
        assert isinstance(metadata.strategy, expected)
        submit.assert_called_once()
    runtime = get_ft_runtime_store().get(1)
    assert runtime is None or runtime["recovery_paused"] is False


@pytest.mark.parametrize("candidate_kind", ["owner", "shrink"])
@pytest.mark.parametrize("reject", [False, True, "removed"], ids=["submitted", "rejected", "removed"])
def test_renamed_policy_keeps_preparation_and_submission_rollback(fault_manager_with_instances, candidate_kind, reject):
    from motor.controller.fault_tolerance.engine_recovery.contracts import RecoveryPlan, RecoverySource
    from motor.controller.fault_tolerance.engine_recovery.runtime import partial_loss_policy

    class RenamedOwner(EngineRecoveryStrategy):
        pass

    class RenamedShrink(DpScaleDownStrategy):
        pass

    manager = fault_manager_with_instances
    metadata = manager.instances[1]
    metadata.fault_level = FaultLevel.L2
    metadata.fault_code = int(SpecialFaultCode.ENGINE_DEAD)
    candidate = RenamedOwner if candidate_kind == "owner" else RenamedShrink
    expected_owner = RenamedOwner if candidate_kind == "owner" else EngineRecoveryStrategy
    context = ScaleDownContext(engine_fault_observed=True, pending_removed_ranks=(1,))
    plan = RecoveryPlan(RecoverySource.SOFTWARE, candidate, context, EngineRelaunchStrategy)

    def submit(_fn, strategy, _instance_id):
        assert isinstance(strategy, expected_owner)
        assert strategy.strategy_context.pending_removed_ranks == plan.context.pending_removed_ranks
        expected_fallback = EngineRelaunchStrategy.__name__ if candidate_kind == "shrink" else ""
        assert strategy.strategy_context.fallback_strategy == expected_fallback
        assert context.fallback_strategy == ""
        assert get_ft_runtime_store().get(1)["recovery_id"] == strategy.recovery_id
        assert partial_loss_policy(1).protect_transaction
        assert all(
            callable(getattr(strategy, field)) for field in ("context_provider", "evidence_provider", "plan_provider")
        )
        if reject:
            if reject == "removed":
                get_ft_runtime_store().remove(1)
            raise RuntimeError("executor unavailable")
        return Mock()

    with (
        patch.object(manager, "_build_recovery_context", return_value=context),
        patch.object(manager, "_build_engine_recovery_plan", return_value=plan),
        patch.object(manager.executor, "submit", side_effect=submit),
    ):
        if reject:
            with pytest.raises(RuntimeError, match="executor unavailable"):
                manager._process_instance_strategy(1)
        else:
            manager._process_instance_strategy(1)
    assert partial_loss_policy(1).protect_transaction is (not reject)
    if reject:
        assert metadata.strategy is None
        assert metadata.strategy_future is None
        runtime = get_ft_runtime_store().get(1)
        if reject == "removed":
            assert runtime is None
        else:
            assert not runtime["recovery_paused"]
    else:
        assert isinstance(metadata.strategy, expected_owner)
        assert metadata.strategy_future is not None
        assert metadata.strategy_fault_level == FaultLevel.L2


def test_removed_instance_cannot_be_prepared_or_submitted_from_stale_metadata(fault_manager_with_instances):
    manager = fault_manager_with_instances
    metadata = manager.instances[1]
    metadata.fault_level = FaultLevel.L2
    metadata.fault_code = int(SpecialFaultCode.ENGINE_DEAD)
    prepare = manager._prepare_recovery_task
    store = get_ft_runtime_store()

    def remove_before_preparation(instance_id, strategy, evidence):
        manager.update(Mock(id=instance_id, job_name="job1"), ObserverEvent.INSTANCE_REMOVED)
        prepare(instance_id, strategy, evidence)

    with (
        patch.object(manager, "_prepare_recovery_task", side_effect=remove_before_preparation),
        patch.object(manager.executor, "submit") as submit,
    ):
        with pytest.raises(RuntimeError, match="removed instance"):
            manager._process_instance_strategy(1)
    submit.assert_not_called()
    assert 1 not in manager.instances
    assert store.get(1) is None
    assert store.persistent_data() == {}


def test_initial_and_preparation_plans_share_native_selection(fault_manager_with_instances):
    from motor.controller.fault_tolerance.strategy import NmSuicideStrategy

    manager = fault_manager_with_instances
    metadata = manager.instances[1]
    metadata.fault_level = FaultLevel.L2
    metadata.fault_code = int(SpecialFaultCode.ENGINE_DEAD)
    manager.strategies[FaultLevel.L6] = Mock(return_value=NmSuicideStrategy)
    context = ScaleDownContext(engine_fault_observed=True)
    with (
        patch.object(manager, "_build_recovery_context", return_value=context),
        patch.object(manager, "_build_engine_recovery_plan", wraps=manager._build_engine_recovery_plan) as planner,
        patch.object(manager.executor, "submit"),
    ):
        manager._process_instance_strategy(1)
        assert planner.call_count == 1
        assert isinstance(metadata.strategy, EngineRelaunchStrategy)
        metadata.fault_level, metadata.fault_code = FaultLevel.L6, 123
        updated = manager._build_engine_recovery_plan(1, context)
    assert updated.strategy is NmSuicideStrategy
    assert manager.strategies[FaultLevel.L6].call_count == 1
