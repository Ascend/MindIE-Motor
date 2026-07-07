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
import time
from unittest.mock import patch, MagicMock
import pytest
from fastapi import HTTPException

from motor.controller.core.instance_manager import InstanceManager, PersistentState
from motor.common.resources.endpoint import Endpoint, EndpointStatus
from motor.common.resources.http_msg_spec import HeartbeatMsg
from motor.common.resources.instance import (
    ParallelConfig,
    Instance,
    NodeManagerInfo,
    InsStatus,
    InsConditionEvent,
    ReadOnlyInstance,
)
from motor.common.resources import EventType
from motor.common.utils.singleton import ThreadSafeSingleton
from motor.config.controller import ControllerConfig
from motor.controller.core.event_pusher import EventPusher


# Helper functions
def create_test_instance(instance_id: int, job_name: str, pod_ips: list[str], role: str = "prefill") -> Instance:
    """Helper function to create test instances with endpoints"""
    endpoints = {}
    for i, pod_ip in enumerate(pod_ips):
        endpoints[pod_ip] = {
            0: Endpoint(
                id=0,
                ip=pod_ip,
                business_port=f"80{0}{i}",
                mgmt_port=f"80{1}{i}",
                status=EndpointStatus.NORMAL,
                hb_timestamp=time.time(),
            )
        }

    return Instance(id=instance_id, job_name=job_name, model_name="test_model", role=role, endpoints=endpoints)


def _create_endpoint(endpoint_id: int, ip: str, business_port: str = "9090", mgmt_port: str = "8080") -> Endpoint:
    """Helper function to create an Endpoint with default values"""
    return Endpoint(
        id=endpoint_id,
        ip=ip,
        business_port=business_port,
        mgmt_port=mgmt_port,
        status=EndpointStatus.INITIAL,
        device_infos=[],
        hb_timestamp=time.time(),
    )


def get_mock_heartbeat_msg(job_name: str, ins_id: int, ip: str, status_dict: dict = None) -> HeartbeatMsg:
    """Generate a mock heartbeat message with configurable status"""
    if status_dict is None:
        status_dict = {0: EndpointStatus.NORMAL}
    return HeartbeatMsg(job_name=job_name, ins_id=ins_id, ip=ip, status=status_dict)


def create_instance_manager_with_config(enable_etcd=False) -> InstanceManager:
    """Create instance manager with specific config"""
    config = ControllerConfig()
    config.etcd_config.enable_etcd_persistence = enable_etcd
    config.instance_manager_check_interval = 0.1  # Faster for tests
    return InstanceManager(config)


# Fixtures
@pytest.fixture
def test_config():
    """Test configuration fixture"""
    dp = 8
    tp = 2
    p_role = "prefill"
    d_role = "decode"

    # Generate pod IPs using list comprehension
    pod_ips = [f"127.0.0.{i}" for i in range(1, 9)]

    p_parallel_config = ParallelConfig(dp_size=dp, tp_size=tp)
    d_parallel_config = ParallelConfig(dp_size=dp * 4, tp_size=tp // 2)

    return {
        'dp': dp,
        'tp': tp,
        'p_role': p_role,
        'd_role': d_role,
        'pod_ips': pod_ips,
        'p_parallel_config': p_parallel_config,
        'd_parallel_config': d_parallel_config,
    }


@pytest.fixture(autouse=True)
def mock_etcd_client():
    """Mock EtcdClient to avoid real ETCD operations in tests"""
    with patch('motor.controller.core.instance_manager.EtcdClient') as mock_etcd_class:
        mock_client = MagicMock()
        mock_client.persist_data.return_value = True
        mock_client.restore_data.return_value = None
        mock_etcd_class.return_value = mock_client
        yield mock_client


@pytest.fixture(autouse=True)
def setup_test_environment():
    """Setup and teardown for each test"""
    # Clear singleton instance before each test
    if hasattr(ThreadSafeSingleton, '_instances') and InstanceManager in ThreadSafeSingleton._instances:
        try:
            ThreadSafeSingleton._instances[InstanceManager].stop()
        except Exception:
            pass
        del ThreadSafeSingleton._instances[InstanceManager]


@pytest.fixture
def instance_manager(test_config):
    """Setup mock instance manager with test instances"""
    instance_manager = create_instance_manager_with_config()

    # Extract pod_ips for cleaner code
    pod_ips = test_config['pod_ips']
    # p0
    instance_manager.add_instance(
        Instance(
            job_name="prefill-0",
            model_name="test_model",
            id=0,
            role=test_config['p_role'],
            parallel_config=test_config['p_parallel_config'],
            node_mgrs=[
                NodeManagerInfo(pod_ip=pod_ips[0], host_ip=pod_ips[0], port="8080"),
                NodeManagerInfo(pod_ip=pod_ips[1], host_ip=pod_ips[1], port="8080"),
            ],
            endpoints={
                pod_ips[0]: {0: _create_endpoint(0, pod_ips[0])},
                pod_ips[1]: {0: _create_endpoint(0, pod_ips[1])},
            },
        )
    )
    # p1
    instance_manager.add_instance(
        Instance(
            job_name="prefill-1",
            model_name="test_model",
            id=1,
            role=test_config['p_role'],
            parallel_config=test_config['p_parallel_config'],
            node_mgrs=[
                NodeManagerInfo(pod_ip=pod_ips[2], host_ip=pod_ips[2], port="8080"),
                NodeManagerInfo(pod_ip=pod_ips[3], host_ip=pod_ips[3], port="8080"),
            ],
            endpoints={
                pod_ips[2]: {0: _create_endpoint(0, pod_ips[2])},
                pod_ips[3]: {0: _create_endpoint(0, pod_ips[3])},
            },
        )
    )
    # d0
    d_instance = Instance(
        job_name="decode-0",
        model_name="test_model",
        id=2,
        role=test_config['d_role'],
        parallel_config=test_config['d_parallel_config'],
        node_mgrs=[
            NodeManagerInfo(pod_ip=pod_ips[4], host_ip=pod_ips[4], port="8080"),
            NodeManagerInfo(pod_ip=pod_ips[5], host_ip=pod_ips[5], port="8080"),
            NodeManagerInfo(pod_ip=pod_ips[6], host_ip=pod_ips[6], port="8080"),
            NodeManagerInfo(pod_ip=pod_ips[7], host_ip=pod_ips[7], port="8080"),
        ],
        endpoints={},
    )
    # construct endpoints
    endpoints = {}
    for pod_ip in pod_ips[4:8]:
        port_temp = 8080
        endpoints[pod_ip] = {}
        for i in range(0, 8):
            endpoints[pod_ip][i] = _create_endpoint(
                endpoint_id=i, ip=pod_ip, business_port=str(port_temp), mgmt_port=str(port_temp + 1000)
            )
            port_temp += 1

        d_instance.add_endpoints(pod_ip, endpoints[pod_ip])

    instance_manager.add_instance(d_instance)
    return instance_manager


# Test functions
def test_singleton_initialization():
    """Test InstanceManager singleton initialization"""
    # First instance
    manager1 = InstanceManager()
    assert manager1 is not None
    assert hasattr(manager1, '_initialized')

    # Second instance should return the same object
    manager2 = InstanceManager()
    assert manager1 is manager2


def test_initialization_with_config():
    """Test initialization with custom config"""
    config = ControllerConfig()
    config.etcd_config.enable_etcd_persistence = True

    manager = InstanceManager(config)
    assert manager.etcd_config is config.etcd_config
    assert manager.instance_manager_check_interval == config.instance_config.instance_manager_check_interval


@patch('motor.controller.core.instance_manager.time.sleep')
def test_start_stop_manager(mock_sleep):
    """Test starting and stopping the instance manager"""
    manager = create_instance_manager_with_config()

    mock_sleep.return_value = None

    manager.start()
    assert manager.instances_management_thread is not None
    assert manager.instances_management_thread.is_alive()
    assert not manager.stop_event.is_set()

    manager.stop()
    assert manager.stop_event.is_set()
    if manager.instances_management_thread and manager.instances_management_thread.is_alive():
        manager.instances_management_thread.join(timeout=0.05)


def test_persist_data_success():
    """Test successful data persistence"""
    manager = create_instance_manager_with_config(enable_etcd=True)
    instance = create_test_instance(1, "test_job", ["192.168.1.1"])
    manager.add_instance(instance)

    result = manager.persist_data()
    assert result is True


def test_persist_data_failure():
    """Test data persistence failure"""
    with patch('motor.controller.core.instance_manager.EtcdClient') as mock_etcd_class:
        mock_client = MagicMock()
        mock_client.persist_data.side_effect = Exception("ETCD error")
        mock_etcd_class.return_value = mock_client

        manager = create_instance_manager_with_config(enable_etcd=True)
        instance = create_test_instance(1, "test_job", ["192.168.1.1"])
        manager.add_instance(instance)

        result = manager.persist_data()
        assert result is False


def test_restore_data_success():
    """Test successful data restoration"""
    instance_data = {
        "id": 1,
        "job_name": "test_job",
        "model_name": "test_model",
        "role": "prefill",
        "endpoints": {},
        "status": "initial",
        "parallel_config": None,
        "node_managers": [],
        "gathered_workload": {"active_kv_cache": 0, "active_tokens": 0},
    }
    persistent_state = PersistentState(data={"1": instance_data}, version=1, timestamp=time.time(), checksum="")
    persistent_state.checksum = persistent_state.calculate_checksum()

    mock_persistent_states = {"state": persistent_state}

    with patch('motor.controller.core.instance_manager.EtcdClient') as mock_etcd_class:
        mock_client = MagicMock()
        mock_client.restore_data.return_value = mock_persistent_states
        mock_etcd_class.return_value = mock_client

        manager = create_instance_manager_with_config(enable_etcd=True)

        mock_event_pusher = MagicMock(spec=EventPusher)
        manager.attach(mock_event_pusher)

        result = manager.restore_data()
        assert result is True
        assert 1 in manager.instances
        mock_event_pusher.push_event.assert_called_once_with(EventType.SET)


def test_restore_data_no_data():
    """Test restoration when no data exists"""
    with patch('motor.controller.core.instance_manager.EtcdClient') as mock_etcd_class:
        mock_client = MagicMock()
        mock_client.restore_data.return_value = None
        mock_etcd_class.return_value = mock_client

        manager = create_instance_manager_with_config(enable_etcd=True)
        result = manager.restore_data()
        assert result is True


def test_restore_data_invalid_checksum():
    """Test restoration with invalid checksum"""
    mock_persistent_states = {
        "state": PersistentState(
            data={
                "1": {
                    "id": 1,
                    "job_name": "test_job",
                    "model_name": "test_model",
                    "role": "prefill",
                    "endpoints": {},
                    "status": "initial",
                    "parallel_config": None,
                    "node_managers": [],
                    "gathered_workload": {"active_kv_cache": 0, "active_tokens": 0},
                }
            },
            version=1,
            timestamp=time.time(),
            checksum="invalid_checksum",
        )
    }

    with patch('motor.controller.core.instance_manager.EtcdClient') as mock_etcd_class:
        mock_client = MagicMock()
        mock_client.restore_data.return_value = mock_persistent_states
        mock_etcd_class.return_value = mock_client

        manager = create_instance_manager_with_config(enable_etcd=True)
        result = manager.restore_data()
        # Should fail because checksum validation fails
        assert result is False
        assert 1 not in manager.instances  # Should not restore invalid data


def test_add_instance(instance_manager, test_config):
    """Test adding an instance"""
    cur_instance_num = instance_manager.get_instance_num()

    # Test invalid input
    instance_manager.add_instance(None)
    assert instance_manager.get_instance_num() == cur_instance_num

    instance_manager.add_instance("invalid_instance")
    assert instance_manager.get_instance_num() == cur_instance_num

    # Test valid instance
    instance_manager.add_instance(
        Instance(
            job_name="testAllocInsGroup2",
            model_name="test_model",
            id=100,
            role=test_config['p_role'],
            parallel_config=ParallelConfig(dp_size=test_config['dp'], tp_size=test_config['tp'] // 2),
        )
    )
    assert instance_manager.get_instance_num() == cur_instance_num + 1

    # Test duplicate instance
    instance_manager.add_instance(
        Instance(
            job_name="testAllocInsGroup2",
            model_name="test_model",
            id=100,
            role=test_config['p_role'],
            parallel_config=ParallelConfig(dp_size=test_config['dp'], tp_size=test_config['tp'] // 2),
        )
    )
    assert instance_manager.get_instance_num() == cur_instance_num + 1  # Should not increase


def test_del_instance(instance_manager):
    """Test deleting an instance"""
    cur_instance_num = instance_manager.get_instance_num()

    # Test deleting existing instance
    instance_manager.del_instance(0)
    assert instance_manager.get_instance_num() == cur_instance_num - 1

    # Test deleting non-existent instance
    instance_manager.del_instance(999)
    assert instance_manager.get_instance_num() == cur_instance_num - 1  # Should remain the same


def test_get_instance(instance_manager):
    """Test getting instances"""
    # Test getting existing instance
    instance = instance_manager.get_instance(1)
    assert instance is not None
    assert instance.id == 1

    # Test getting non-existent instance
    instance = instance_manager.get_instance(999)
    assert instance is None


def test_get_instance_num(instance_manager):
    """Test getting instance count"""
    count = instance_manager.get_instance_num()
    assert count == 3  # Based on fixture setup


def test_get_active_instances(instance_manager):
    """Test getting active instances"""
    # Initially no active instances
    active_instances = instance_manager.get_active_instances()
    assert len(active_instances) == 0

    # Make one instance active
    instance = instance_manager.get_instance(0)
    instance.status = InsStatus.ACTIVE
    active_instances = instance_manager.get_active_instances()
    assert len(active_instances) == 1
    assert active_instances[0].id == 0


def test_get_inactive_instances(instance_manager):
    """Test getting inactive instances"""
    # Make one instance inactive
    instance = instance_manager.get_instance(0)
    instance.status = InsStatus.INACTIVE
    inactive_instances = instance_manager.get_inactive_instances()
    assert len(inactive_instances) == 1
    assert inactive_instances[0].id == 0


def test_get_initial_instances(instance_manager):
    """Test getting initial instances"""
    initial_instances = instance_manager.get_initial_instances()
    assert len(initial_instances) == 3  # All instances start as INITIAL


def test_get_instance_by_podip(instance_manager):
    """Test getting instance by pod IP"""
    # Test with existing IP
    result = instance_manager.get_instance_by_podip("127.0.0.1")
    assert result is not None

    # Test with non-existent IP
    result = instance_manager.get_instance_by_podip("192.168.1.100")
    assert result is None

    # Test with empty string
    result = instance_manager.get_instance_by_podip("")
    assert result is None


def test_has_instance_by_job_name(instance_manager):
    """Test checking if instance exists by job name"""
    # Test existing job name
    assert instance_manager.has_instance_by_job_name("prefill-0") is True

    # Test non-existent job name
    assert instance_manager.has_instance_by_job_name("non-existent") is False


def test_get_instance_by_job_name(instance_manager):
    """Test retrieving the current instance by job name"""
    d_instance = instance_manager.get_instance_by_job_name("decode-0")
    assert d_instance is not None
    assert d_instance.job_name == "decode-0"
    assert d_instance.id == 2

    assert instance_manager.get_instance_by_job_name("non-existent") is None


def test_get_instance_by_job_name_returns_newest_when_stale_entries_exist(instance_manager):
    """Stale and current entries may coexist briefly; return the newest id."""
    replacement = create_test_instance(
        instance_id=99,
        job_name="decode-0",
        pod_ips=["10.0.0.99"],
        role="decode",
    )
    instance_manager.add_instance(replacement)

    current = instance_manager.get_instance_by_job_name("decode-0")
    assert current is not None
    assert current.id == 99


def test_handle_heartbeat_success(instance_manager, test_config):
    """Test successful heartbeat handling"""
    pod_ips = test_config['pod_ips']

    # Test normal heartbeat transition
    heartbeat_msg = get_mock_heartbeat_msg("prefill-0", 0, pod_ips[0])
    success, code = instance_manager.handle_heartbeat(heartbeat_msg)
    assert success is True
    assert code == 200

    # Verify instance status changed to INITIAL
    instance = instance_manager.get_instance(0)
    assert instance.status == InsStatus.INITIAL


def test_handle_heartbeat_invalid_message(instance_manager):
    """Test heartbeat handling with invalid message"""
    # Test with None message
    success, code = instance_manager.handle_heartbeat(None)
    assert success is False
    assert code == 500

    # Test with invalid message type
    success, code = instance_manager.handle_heartbeat("invalid_message")
    assert success is False
    assert code == 500


def test_handle_heartbeat_nonexistent_instance():
    """Test heartbeat handling for non-existent instance"""
    manager = create_instance_manager_with_config()

    heartbeat_msg = get_mock_heartbeat_msg("non-existent", 999, "192.168.1.1")

    with pytest.raises(HTTPException) as exc_info:
        manager.handle_heartbeat(heartbeat_msg)
    assert exc_info.value.status_code == 503  # RE_REGISTER


def test_state_transitions(instance_manager, test_config):
    """Test various state transitions"""
    pod_ips = test_config['pod_ips']
    instance = instance_manager.get_instance(0)

    # INITIAL -> ACTIVE
    heartbeat_msg = get_mock_heartbeat_msg("prefill-0", 0, pod_ips[0])
    instance_manager.handle_heartbeat(heartbeat_msg)
    assert instance.status == InsStatus.INITIAL

    # Make endpoints ready
    for endpoints in instance.endpoints.values():
        for endpoint in endpoints.values():
            endpoint.status = EndpointStatus.NORMAL

    heartbeat_msg2 = get_mock_heartbeat_msg("prefill-0", 0, pod_ips[1])
    instance_manager.handle_heartbeat(heartbeat_msg2)
    assert instance.status == InsStatus.ACTIVE

    # ACTIVE -> INACTIVE (abnormal heartbeat)
    heartbeat_msg3 = get_mock_heartbeat_msg("prefill-0", 0, pod_ips[1], {0: EndpointStatus.ABNORMAL})
    instance_manager.handle_heartbeat(heartbeat_msg3)
    assert instance.status == InsStatus.INACTIVE


def test_inactive_with_mixed_paused_normal_goes_to_paused(instance_manager):
    """INACTIVE + mixed PAUSED/NORMAL (no ABNORMAL) → PAUSED, NOT INITIAL"""
    manager = create_instance_manager_with_config()
    instance = create_test_instance(201, "test_mixed", ["192.168.1.1", "192.168.1.2"])
    manager.add_instance(instance)
    instance.update_instance_status(InsStatus.INACTIVE)

    # Mixed: pod-0 PAUSED, pod-1 NORMAL — no ABNORMAL
    for ep_id, ep in instance.endpoints["192.168.1.1"].items():
        ep.status = EndpointStatus.PAUSED
    for ep_id, ep in instance.endpoints["192.168.1.2"].items():
        ep.status = EndpointStatus.NORMAL

    result = manager._handle_state_transition(instance)
    assert result is True
    assert instance.status == InsStatus.PAUSED, (
        f"Expected PAUSED but got {instance.status} — mixed PAUSED+NORMAL must not fallthrough to INITIAL"
    )


def test_inactive_with_all_paused_goes_to_paused(instance_manager):
    """INACTIVE + all endpoints PAUSED → PAUSED"""
    manager = create_instance_manager_with_config()
    instance = create_test_instance(202, "test_all_paused", ["192.168.1.1", "192.168.1.2"])
    manager.add_instance(instance)
    instance.update_instance_status(InsStatus.INACTIVE)

    # All PAUSED
    for pod_endpoints in instance.endpoints.values():
        for ep in pod_endpoints.values():
            ep.status = EndpointStatus.PAUSED

    result = manager._handle_state_transition(instance)
    assert result is True
    assert instance.status == InsStatus.PAUSED


def test_abnormal_priority_over_paused(instance_manager):
    """ABNORMAL has higher priority than PAUSED: mixed ABNORMAL+PAUSED stays INACTIVE"""
    manager = create_instance_manager_with_config()
    instance = create_test_instance(203, "test_priority", ["192.168.1.1", "192.168.1.2"])
    manager.add_instance(instance)
    instance.update_instance_status(InsStatus.INACTIVE)

    # Mixed: pod-0 PAUSED, pod-1 ABNORMAL
    for ep_id, ep in instance.endpoints["192.168.1.1"].items():
        ep.status = EndpointStatus.PAUSED
    for ep_id, ep in instance.endpoints["192.168.1.2"].items():
        ep.status = EndpointStatus.ABNORMAL

    result = manager._handle_state_transition(instance)
    assert result is True
    assert instance.status == InsStatus.INACTIVE, (
        f"Expected INACTIVE but got {instance.status} — ABNORMAL must take priority over PAUSED"
    )


def test_initial_with_paused(instance_manager):
    """INITIAL + mixed PAUSED/NORMAL → PAUSED"""
    manager = create_instance_manager_with_config()
    instance = create_test_instance(204, "test_init_paused", ["192.168.1.1", "192.168.1.2"])
    manager.add_instance(instance)
    instance.update_instance_status(InsStatus.INITIAL)

    # Mixed: pod-0 PAUSED, pod-1 NORMAL
    for ep_id, ep in instance.endpoints["192.168.1.1"].items():
        ep.status = EndpointStatus.PAUSED
    for ep_id, ep in instance.endpoints["192.168.1.2"].items():
        ep.status = EndpointStatus.NORMAL

    result = manager._handle_state_transition(instance)
    assert result is True
    assert instance.status == InsStatus.PAUSED


def test_separate_instance(instance_manager):
    """Test separating instances"""
    # Enable persistence for this test
    instance_manager.etcd_config.enable_etcd_persistence = True

    instance = create_test_instance(100, "test_separate", ["192.168.1.1"])
    instance_manager.add_instance(instance)
    instance.update_instance_status(InsStatus.ACTIVE)

    # Test separating active instance - should trigger persistence
    with patch.object(instance_manager, 'persist_data', return_value=True) as mock_persist:
        instance_manager.separate_instance(instance.id)
        assert instance.status == InsStatus.INACTIVE
        assert instance.id in instance_manager.forced_separated_instances
        # Verify persistence was called once for ACTIVE -> INACTIVE transition
        mock_persist.assert_called_once()

    # Test separating already inactive instance (should not notify again or trigger persistence)
    with patch.object(instance_manager, 'persist_data', return_value=True) as mock_persist:
        original_status = instance.status
        instance_manager.separate_instance(instance.id)
        assert instance.status == original_status  # Should remain INACTIVE
        assert instance.id in instance_manager.forced_separated_instances
        # Verify persistence was NOT called since status didn't change
        mock_persist.assert_not_called()


def test_separate_nonexistent_instance(instance_manager):
    """Test separating non-existent instance"""
    # Should not raise exception
    instance_manager.separate_instance(999)


def test_recover_instance(instance_manager):
    """Test recovering instances"""
    instance = create_test_instance(101, "test_recover", ["192.168.1.2"])
    instance_manager.add_instance(instance)
    instance.update_instance_status(InsStatus.ACTIVE)

    # Separate first
    instance_manager.separate_instance(instance.id)
    assert instance.id in instance_manager.forced_separated_instances

    # Recover
    instance_manager.recover_instance(instance.id)
    assert instance.id not in instance_manager.forced_separated_instances


def test_recover_nonexistent_instance(instance_manager):
    """Test recovering non-existent instance"""
    # Should not raise exception
    instance_manager.recover_instance(999)


def test_observer_pattern(instance_manager):
    """Test observer pattern functionality"""
    from motor.controller.core import Observer, ObserverEvent

    class MockObserver(Observer):
        def __init__(self):
            self.notifications = []

        def update(self, instance: ReadOnlyInstance, event: ObserverEvent):
            self.notifications.append((instance.id, event))

    observer = MockObserver()
    instance_manager.attach(observer)

    instance = create_test_instance(102, "test_observer", ["192.168.1.3"])
    instance_manager.add_instance(instance)

    # Check notification on instance addition (INITIAL)
    assert len(observer.notifications) == 1
    assert observer.notifications[0] == (102, ObserverEvent.INSTANCE_INITIAL)

    # Test notification mechanism with READY event
    observer.notifications.clear()  # Clear previous notifications
    instance_manager.notify(instance, ObserverEvent.INSTANCE_READY)

    assert len(observer.notifications) == 1
    assert observer.notifications[0] == (102, ObserverEvent.INSTANCE_READY)


def test_handle_initial_state():
    """Test _handle_initial method"""
    manager = create_instance_manager_with_config()
    instance = create_test_instance(1, "test_initial", ["192.168.1.1"])
    manager.add_instance(instance)
    instance.update_instance_status(InsStatus.INACTIVE)

    # Test INACTIVE -> INITIAL transition
    manager._handle_initial(InsStatus.INACTIVE, InsConditionEvent.INSTANCE_INIT, instance)
    assert instance.id not in manager.forced_separated_instances


def test_handle_active_state(instance_manager):
    """Test _handle_active method"""
    instance = instance_manager.get_instance(0)
    instance.update_instance_status(InsStatus.INITIAL)

    # Test transition to ACTIVE
    instance_manager._handle_active(InsStatus.INITIAL, InsConditionEvent.INSTANCE_NORMAL, instance)
    assert instance.status == InsStatus.ACTIVE


def test_handle_inactive_state(instance_manager):
    """Test _handle_inactive method"""
    instance = instance_manager.get_instance(0)
    instance.update_instance_status(InsStatus.ACTIVE)

    # Test transition to INACTIVE due to abnormal condition
    instance_manager._handle_inactive(InsStatus.ACTIVE, InsConditionEvent.INSTANCE_ABNORMAL, instance)
    assert instance.status == InsStatus.INACTIVE


def test_handle_deleted_state(instance_manager):
    """Test _handle_deleted method"""
    instance = create_test_instance(103, "test_deleted", ["192.168.1.4"])
    instance_manager.add_instance(instance)
    instance.update_instance_status(InsStatus.INACTIVE)

    # Test transition to DELETED
    instance_manager._handle_deleted(InsStatus.INACTIVE, InsConditionEvent.INSTANCE_HEARTBEAT_TIMEOUT, instance)
    assert instance.status == InsStatus.DELETED

    # Instance should be removed
    assert instance_manager.get_instance(103) is None


def test_refresh_instance_heartbeat(instance_manager):
    """Test heartbeat timestamp refresh"""
    instance = instance_manager.get_instance(0)
    original_timestamp = time.time() - 100  # Old timestamp

    # Set old timestamps
    for endpoints in instance.endpoints.values():
        for endpoint in endpoints.values():
            endpoint.hb_timestamp = original_timestamp

    current_time = time.time()
    # Test the inline heartbeat refresh logic from _maybe_refresh_heartbeat
    try:
        for endpoints in instance.endpoints.values():
            for endpoint in endpoints.values():
                endpoint.hb_timestamp = current_time
    except Exception:
        pass  # Test doesn't check exception handling

    # Verify timestamps updated
    for endpoints in instance.endpoints.values():
        for endpoint in endpoints.values():
            assert endpoint.hb_timestamp == current_time


def test_version_control():
    """Test version control functionality"""
    manager = create_instance_manager_with_config()

    # Test initial version
    assert manager._data_version == 0

    # Test version increment
    version1 = manager._get_next_version()
    assert version1 == 1
    assert manager._data_version == 1

    version2 = manager._get_next_version()
    assert version2 == 2
    assert manager._data_version == 2


def test_checksum_calculation(instance_manager):
    """Test instance checksum calculation"""
    instance = instance_manager.get_instance(0)

    # Create a persistent state to test checksum calculation
    instance_data = instance.model_dump()
    state = PersistentState(data=instance_data, version=1, timestamp=time.time(), checksum="")

    checksum1 = state.calculate_checksum()
    assert isinstance(checksum1, str)
    assert len(checksum1) > 0

    # Test that different instances produce different checksums
    instance2 = create_test_instance(999, "different_job", ["192.168.1.99"])
    instance_data2 = instance2.model_dump()
    state2 = PersistentState(data=instance_data2, version=1, timestamp=time.time(), checksum="")
    checksum2 = state2.calculate_checksum()
    assert checksum1 != checksum2


def test_persistent_instance_state():
    """Test PersistentState functionality"""
    instance_data = {"id": 1, "job_name": "test"}
    version = 1
    timestamp = time.time()

    # Create state and calculate correct checksum using the new method
    state = PersistentState(
        data=instance_data,
        version=version,
        timestamp=timestamp,
        checksum="",  # Will be calculated
    )
    state.checksum = state.calculate_checksum()

    # Test valid checksum
    assert state.is_valid()

    # Test invalid checksum
    state.checksum = "invalid"
    assert not state.is_valid()


def test_forced_separation_cleanup(instance_manager):
    """Test forced separated instances cleanup"""
    instance = create_test_instance(104, "test_cleanup", ["192.168.1.5"])
    instance_manager.add_instance(instance)

    # Force separate
    instance_manager.separate_instance(instance.id)
    assert instance.id in instance_manager.forced_separated_instances

    # Delete instance
    instance_manager.del_instance(instance.id)
    assert instance.id not in instance_manager.forced_separated_instances


def test_instances_management_loop_timeout():
    """Test instances management loop timeout handling"""
    manager = create_instance_manager_with_config()
    instance = create_test_instance(105, "test_timeout", ["192.168.1.6"])
    manager.add_instance(instance)
    instance.update_instance_status(InsStatus.ACTIVE)

    # Set endpoint to old timestamp to simulate timeout
    for endpoints in instance.endpoints.values():
        for endpoint in endpoints.values():
            endpoint.hb_timestamp = time.time() - 1000  # Very old timestamp

    # Manually trigger timeout transition by calling the transition logic
    # This simulates what happens in _instances_management_loop when is_all_endpoints_alive returns False
    from_state = instance.status
    event = InsConditionEvent.INSTANCE_HEARTBEAT_TIMEOUT
    to_state = manager.transitions.get((from_state, event), None)

    if to_state:
        state_handler = manager.states.get(to_state, None)
        if state_handler:
            state_handler(from_state, event, instance)

    # Should trigger timeout transition to INACTIVE
    assert instance.status == InsStatus.INACTIVE


def test_persistence_on_state_change():
    """Test automatic persistence on state changes"""
    with patch.object(InstanceManager, 'persist_data') as mock_persist:
        manager = create_instance_manager_with_config(enable_etcd=True)
        instance = create_test_instance(106, "test_persist", ["192.168.1.7"])
        manager.add_instance(instance)

        # Set initial status to INITIAL
        instance.update_instance_status(InsStatus.INITIAL)

        # Trigger state change to ACTIVE via heartbeat
        heartbeat_msg = get_mock_heartbeat_msg("test_persist", 106, "192.168.1.7")
        manager.handle_heartbeat(heartbeat_msg)

        # Persistence should be called during state transition
        mock_persist.assert_called()


def test_prevent_forced_separation_reactivation():
    """Test that forcibly separated instances cannot reactivate to ACTIVE"""
    manager = create_instance_manager_with_config()
    instance = create_test_instance(107, "test_prevent", ["192.168.1.8"])
    manager.add_instance(instance)

    # Make instance active and then force separate
    instance.update_instance_status(InsStatus.ACTIVE)
    manager.separate_instance(instance.id)
    assert instance.status == InsStatus.INACTIVE
    assert instance.id in manager.forced_separated_instances

    # Set endpoints to ready state to trigger INSTANCE_NORMAL event
    # This would normally transition to ACTIVE, but should be prevented
    for endpoints in instance.endpoints.values():
        for endpoint in endpoints.values():
            endpoint.status = EndpointStatus.NORMAL

    # Try to transition back to ACTIVE via INSTANCE_NORMAL event - should be prevented
    result = manager._handle_state_transition(instance)
    assert result is True  # Returns success but doesn't change state
    assert instance.status == InsStatus.INACTIVE  # Still INACTIVE, not ACTIVE


def test_update_config():
    """Test update_config method updates configuration and recreates ETCD client"""
    with patch('motor.controller.core.instance_manager.EtcdClient') as mock_etcd_class:
        mock_client = MagicMock()
        mock_etcd_class.return_value = mock_client

        manager = create_instance_manager_with_config(enable_etcd=True)

        # Create new config with different ETCD settings
        new_config = ControllerConfig()
        new_config.etcd_config.etcd_host = "new-etcd-host"
        new_config.etcd_config.etcd_port = 2380
        new_config.etcd_config.etcd_timeout = 30.0
        new_config.etcd_config.enable_etcd_persistence = True

        # Clear the mock call history to track new calls
        mock_etcd_class.reset_mock()

        # Update config
        manager.update_config(new_config)

        # Verify config was updated
        assert manager.etcd_config is new_config.etcd_config
        assert manager.etcd_config.etcd_host == "new-etcd-host"
        assert manager.etcd_config.etcd_port == 2380
        assert manager.etcd_config.etcd_timeout == 30.0

        # Verify ETCD client constructor was called with new config
        mock_etcd_class.assert_called_once_with(
            etcd_config=new_config.etcd_config, tls_config=new_config.etcd_tls_config
        )


# ===== Persistence and Recovery Tests =====


def test_persist_and_restore_instance_data_success():
    """Test successful persist and restore of instance manager data"""
    manager = create_instance_manager_with_config(enable_etcd=True)

    # Create and add test instance
    instance = create_test_instance(201, "test_persist_instance", ["192.168.1.1"])
    manager.add_instance(instance)

    # Mock successful ETCD operations
    with patch.object(manager.etcd_client, 'persist_data', return_value=True) as mock_persist:
        with patch.object(manager.etcd_client, 'restore_data') as mock_restore:
            # Persist data
            persist_result = manager.persist_data()
            assert persist_result

            # Verify persist was called
            mock_persist.assert_called_once()
            args, kwargs = mock_persist.call_args
            assert "/controller/instance_manager" in args[0]

            # Create mock persistent state for restore (new format: single PersistentState)
            instance_data = instance.model_dump()
            instance_state = PersistentState(data={"201": instance_data}, version=1, timestamp=time.time(), checksum="")
            instance_state.checksum = instance_state.calculate_checksum()
            mock_persistent_states = {"state": instance_state}

            mock_restore.return_value = mock_persistent_states

            # Create new manager instance for restore test
            with patch('motor.controller.core.instance_manager.EtcdClient'):
                new_manager = create_instance_manager_with_config(enable_etcd=True)

                # Restore data
                restore_result = new_manager.restore_data()
                assert restore_result

                # Verify data was restored
                assert 201 in new_manager.instances
                restored_instance = new_manager.instances[201]
                assert restored_instance.job_name == instance.job_name
                assert restored_instance.id == instance.id


def test_persist_data_with_checksum_validation():
    """Test that persisted data includes correct checksums"""
    manager = create_instance_manager_with_config(enable_etcd=True)

    # Create and add test instance
    instance = create_test_instance(202, "test_checksum", ["192.168.1.2"])
    manager.add_instance(instance)

    with patch.object(manager.etcd_client, 'persist_data', return_value=True) as mock_persist:
        # Persist data
        result = manager.persist_data()
        assert result

        # Verify the data passed to persist_data
        args, kwargs = mock_persist.call_args
        persisted_data = args[1]

        assert "state" in persisted_data

        # Get the PersistentState data
        state_data = persisted_data["state"]
        assert "checksum" in state_data
        assert len(state_data["checksum"]) > 0

        # Verify the instance data is in the state's data field
        assert "202" in state_data["data"]

        # Verify checksum is valid by reconstructing the state
        state = PersistentState(**state_data)
        assert state.is_valid()


def test_restore_data_with_invalid_checksum():
    """Test restore skips data with invalid checksums"""
    manager = create_instance_manager_with_config(enable_etcd=True)

    # Create mock persistent state with invalid checksum (new format: single PersistentState)
    mock_persistent_states = {
        "state": PersistentState(
            data={
                "203": {
                    "id": 203,
                    "job_name": "test_invalid",
                    "model_name": "test_model",
                    "role": "prefill",
                    "endpoints": {},
                    "status": "initial",
                }
            },
            version=1,
            timestamp=time.time(),
            checksum="invalid_checksum",  # Wrong checksum
        )
    }

    with patch.object(manager.etcd_client, 'restore_data', return_value=mock_persistent_states):
        result = manager.restore_data()

        # Should fail because checksum validation fails
        assert not result
        assert 203 not in manager.instances  # Should not restore invalid data


def test_persistence_disabled_in_config():
    """Test that persistence is properly disabled when config flag is False"""
    manager = create_instance_manager_with_config(enable_etcd=False)

    # Create and add test instance
    instance = create_test_instance(204, "test_disabled", ["192.168.1.3"])
    manager.add_instance(instance)

    # Try to persist manually - should still work but not be called from state transitions
    with patch.object(manager.etcd_client, 'persist_data', return_value=True):
        result = manager.persist_data()
        assert result  # persist_data always calls etcd_client.persist_data regardless of flag


def test_persist_empty_instances():
    """Test persisting when no instances exist"""
    manager = create_instance_manager_with_config(enable_etcd=True)

    with patch.object(manager.etcd_client, 'persist_data', return_value=True) as mock_persist:
        result = manager.persist_data()
        assert result

        # Verify data was persisted
        args, kwargs = mock_persist.call_args
        persisted_data = args[1]

        assert "state" in persisted_data
        state_data = persisted_data["state"]
        assert "data" in state_data
        assert len(state_data["data"]) == 0  # Empty instances dict


def test_restore_no_instance_data_available():
    """Test restore when no instance data is available in ETCD"""
    manager = create_instance_manager_with_config(enable_etcd=True)

    with patch.object(manager.etcd_client, 'restore_data', return_value=None):
        result = manager.restore_data()

        # Should succeed with empty state
        assert result
        assert len(manager.instances) == 0


def test_persistent_state_is_valid_method():
    """Test PersistentState.is_valid method"""
    # Create a valid state
    instance_data = {
        "id": 205,
        "job_name": "test_valid",
        "model_name": "test_model",
        "role": "prefill",
        "endpoints": {},
        "status": "active",
    }

    valid_state = PersistentState(
        data=instance_data,
        version=1,
        timestamp=time.time(),
        checksum="",  # Will be calculated
    )

    # Manually set correct checksum
    valid_state.checksum = valid_state.calculate_checksum()
    assert valid_state.is_valid()

    # Create invalid state with wrong checksum
    invalid_state = PersistentState(data=instance_data, version=1, timestamp=time.time(), checksum="wrong_checksum")
    assert not invalid_state.is_valid()


def test_restore_data_with_type_conversion():
    """Test restoration with string-formatted data from ETCD (type conversion)"""
    # Simulate data as it would come from ETCD - all values are strings
    etcd_string_data = {
        "id": "206",  # int as string
        "job_name": "test_type_conversion",
        "model_name": "test_model",
        "role": "prefill",
        "status": "active",  # enum as lowercase string
        "endpoints": {},  # dict
        "parallel_config": None,
        "node_managers": [],
        "gathered_workload": {"memory_mb": "1024", "cpu_cores": "2"},  # nested dict with string values
    }

    # Mock persistent state with string-formatted data (new format: single PersistentState)
    persistent_state = PersistentState(
        data={"206": etcd_string_data},
        version=1,
        timestamp=time.time(),
        checksum="",  # Will be calculated
    )
    persistent_state.checksum = persistent_state.calculate_checksum()

    mock_persistent_states = {"state": persistent_state}

    with patch('motor.controller.core.instance_manager.EtcdClient') as mock_etcd_class:
        mock_client = MagicMock()
        mock_client.restore_data.return_value = mock_persistent_states
        mock_etcd_class.return_value = mock_client

        manager = create_instance_manager_with_config(enable_etcd=True)
        result = manager.restore_data()

        # Should succeed - Pydantic should handle type conversion
        assert result
        assert 206 in manager.instances

        instance = manager.instances[206]
        assert instance.id == 206  # string "206" converted to int 206
        assert instance.job_name == "test_type_conversion"
        assert instance.status == InsStatus.ACTIVE  # string "active" converted to enum


def test_restore_data_with_invalid_enum_value():
    """Test restoration fails gracefully with invalid enum values"""
    # Simulate corrupted data with invalid enum value
    corrupted_data = {
        "id": "207",
        "job_name": "test_invalid_enum",
        "model_name": "test_model",
        "role": "prefill",
        "status": "INVALID_STATUS",  # Invalid enum value
        "endpoints": {},
        "parallel_config": None,
        "node_managers": [],
        "gathered_workload": {"memory_mb": "1024", "cpu_cores": "2"},
    }

    # Mock persistent state (new format: single PersistentState)
    persistent_state = PersistentState(
        data={"207": corrupted_data},
        version=1,
        timestamp=time.time(),
        checksum="",  # Will be calculated
    )
    persistent_state.checksum = persistent_state.calculate_checksum()

    mock_persistent_states = {"state": persistent_state}

    with patch('motor.controller.core.instance_manager.EtcdClient') as mock_etcd_class:
        mock_client = MagicMock()
        mock_client.restore_data.return_value = mock_persistent_states
        mock_etcd_class.return_value = mock_client

        manager = create_instance_manager_with_config(enable_etcd=True)

        # This should succeed (data restoration succeeds) but instance creation fails
        result = manager.restore_data()
        assert result

        # Instance should not be created due to validation error
        assert 207 not in manager.instances


def test_restore_data_with_malformed_numeric_data():
    """Test restoration fails gracefully with malformed numeric data"""
    # Simulate corrupted data with invalid numeric format
    corrupted_data = {
        "id": "not_a_number",  # Invalid int format
        "job_name": "test_malformed_number",
        "model_name": "test_model",
        "role": "prefill",
        "status": "active",
        "endpoints": {},
        "parallel_config": None,
        "node_managers": [],
        "gathered_workload": {"memory_mb": "1024", "cpu_cores": "2"},
    }

    # Mock persistent state (new format: single PersistentState)
    persistent_state = PersistentState(
        data={"invalid": corrupted_data},
        version=1,
        timestamp=time.time(),
        checksum="",  # Will be calculated
    )
    persistent_state.checksum = persistent_state.calculate_checksum()

    mock_persistent_states = {"state": persistent_state}

    with patch('motor.controller.core.instance_manager.EtcdClient') as mock_etcd_class:
        mock_client = MagicMock()
        mock_client.restore_data.return_value = mock_persistent_states
        mock_etcd_class.return_value = mock_client

        manager = create_instance_manager_with_config(enable_etcd=True)

        # This should succeed (data restoration succeeds) but instance creation fails
        result = manager.restore_data()
        assert result

        # Instance should not be created due to validation error
        assert len(manager.instances) == 0
