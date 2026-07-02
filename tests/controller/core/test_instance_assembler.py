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
import hashlib
import pytest
from unittest.mock import MagicMock, patch

from motor.common.resources import Instance, InsStatus, ParallelConfig, Endpoint, ReadOnlyInstance
from motor.common.resources.http_msg_spec import RegisterMsg, ReregisterMsg, Ranktable, ServerInfo, DeviceInfo
from motor.controller.core.instance_assembler import (
    InstanceAssembler,
    AssembleInstanceMetadata,
    RegisterStatus,
)
from motor.common.etcd.persistent_state import PersistentState
from motor.common.utils.singleton import ThreadSafeSingleton
from motor.controller.core import InstanceManager
from motor.config.controller import ControllerConfig


def build_pod_ranktable(
    pod_ip: str,
    pod_device_num: int,
    rank_offset: int = 0,
    is_supperpod: bool = True,
) -> Ranktable:
    """
    Build pod level ranktable, it only have on server, so server_list size is 1.
    This function is mainly for test case to build ranktable.
    """
    ranktable = Ranktable(
        version="1.2",
        status="completed",
        server_count="1",
        server_list=[
            ServerInfo(
                server_id=pod_ip,
                container_ip=pod_ip,
                device=[
                    DeviceInfo(
                        device_ip=pod_ip,
                        device_id=str(i),
                        rank_id=str(rank_offset + i),
                        super_device_id="0" if is_supperpod else None,
                    )
                    for i in range(pod_device_num)
                ],
            )
        ],
    )
    return ranktable


@pytest.fixture
def test_config():
    """Test configuration fixture"""
    dp = 4
    tp = 2
    role = "prefill"
    pod_ip1 = "127.0.0.1"
    pod_ip2 = "127.0.0.2"
    parallel_config = ParallelConfig(dp_size=dp, tp_size=tp, local_world_size=tp, world_size=dp * tp)
    return {
        'dp': dp,
        'tp': tp,
        'role': role,
        'pod_ip1': pod_ip1,
        'pod_ip2': pod_ip2,
        'parallel_config': parallel_config,
    }


def _cleanup_singletons():
    """Clean up singleton instances to ensure test isolation"""
    singletons_to_cleanup = [InstanceAssembler, InstanceManager]

    for singleton_cls in singletons_to_cleanup:
        if singleton_cls in ThreadSafeSingleton._instances:
            instance = ThreadSafeSingleton._instances[singleton_cls]
            try:
                if hasattr(instance, 'stop'):
                    instance.stop()
            except Exception:
                pass  # Ignore errors during cleanup
            del ThreadSafeSingleton._instances[singleton_cls]


@pytest.fixture(autouse=True)
def cleanup_singletons():
    """Auto cleanup singletons before and after each test"""
    _cleanup_singletons()
    yield
    _cleanup_singletons()


@pytest.fixture
def mock_config():
    """Mock controller config"""
    config = ControllerConfig()
    # Disable ETCD persistence for most tests to avoid complexity
    config.etcd_config.enable_etcd_persistence = False
    config.instance_config.instance_assemble_timeout = 1.0  # Fast timeout for tests
    config.instance_config.instance_assembler_check_interval = 0.1
    config.instance_config.instance_assembler_cmd_send_interval = 0.1
    config.instance_config.send_cmd_retry_times = 3
    return config


@pytest.fixture
def instance_assembler(mock_config):
    """Setup mock assembler with threading mocked to prevent actual thread starts"""
    with patch('threading.Thread') as mock_thread_class:
        mock_thread = MagicMock()
        mock_thread_class.return_value = mock_thread

        with patch('motor.controller.core.instance_assembler.EtcdClient') as mock_etcd_class:
            mock_etcd = MagicMock()
            mock_etcd_class.return_value = mock_etcd

            assembler = InstanceAssembler(mock_config)
            yield assembler


# Helper functions for test data creation
def create_register_msg(job_name: str, pod_ip: str, config: dict, **kwargs) -> RegisterMsg:
    """Create a RegisterMsg with common defaults"""
    defaults = {
        'model_name': "test_model",
        'role': config['role'],
        'business_port': ["8080", "8084"],
        'mgmt_port': ["9090", "9094"],
        'nm_port': "8088",
        'parallel_config': config['parallel_config'],
        'enable_multi_endpoints': True,
        'device_num': 2 * config['tp'],  # device count based on tp size
    }
    defaults.update(kwargs)

    return RegisterMsg(job_name=job_name, pod_ip=pod_ip, **defaults)


def create_reregister_msg(job_name: str, pod_ip: str, instance_id: int, config: dict, endpoints: list) -> ReregisterMsg:
    """Create a ReregisterMsg with common defaults"""
    # Convert endpoints dict to list if needed
    if isinstance(endpoints, dict):
        endpoints_list = list(endpoints.values())
    else:
        endpoints_list = endpoints

    return ReregisterMsg(
        job_name=job_name,
        model_name="test_model",
        instance_id=instance_id,
        role=config['role'],
        pod_ip=pod_ip,
        nm_port="8088",
        parallel_config=config['parallel_config'],
        endpoints=endpoints_list,
        enable_multi_endpoints=True,
    )


def register_instance_with_pods(assembler: InstanceAssembler, job_name: str, config: dict, pod_count: int = 2) -> bool:
    """Register pods for an instance and return whether assembly is complete"""
    pod_ips = [f"127.0.0.{i + 1}" for i in range(pod_count)]

    for i, pod_ip in enumerate(pod_ips):
        rank_offset = i * 2 * config['tp']
        msg = create_register_msg(
            job_name,
            pod_ip,
            config,
            ranktable=build_pod_ranktable(pod_ip=pod_ip, pod_device_num=2 * config['tp'], rank_offset=rank_offset),
        )
        result = assembler.register(msg)
        assert result == 0

    # Try to assemble with mocked endpoint health check
    if job_name in assembler.instances:
        metadata = assembler.instances[job_name]
        with patch.object(assembler, '_filter_abnormal_endpoints'):
            assembler._assemble_instance(metadata)
        return metadata.register_status == RegisterStatus.ASSEMBLED

    return False


def create_assembled_instance(assembler: InstanceAssembler, job_name: str, config: dict) -> AssembleInstanceMetadata:
    """Create and assemble a complete instance"""
    success = register_instance_with_pods(assembler, job_name, config)
    assert success, f"Failed to assemble instance {job_name}"
    return assembler.instances[job_name]


# ===== Basic Functionality Tests =====


def test_initialization(mock_config):
    """Test InstanceAssembler initialization"""
    with patch('threading.Thread'):
        with patch('motor.controller.core.instance_assembler.EtcdClient'):
            assembler = InstanceAssembler(mock_config)

            assert assembler.etcd_config is mock_config.etcd_config
            assert assembler.ins_id_cnt == 1
            assert len(assembler.instances) == 0
            assert not assembler.stop_event.is_set()
            assert assembler._data_version == 0


def test_singleton_behavior(mock_config):
    """Test singleton pattern prevents re-initialization"""
    with patch('threading.Thread'), patch('motor.controller.core.instance_assembler.EtcdClient'):
        assembler1 = InstanceAssembler(mock_config)
        original_timeout = assembler1.instance_assemble_timeout

        # Create a different config and try to create another instance
        different_config = ControllerConfig()
        different_config.instance_config.instance_assemble_timeout = 999
        assembler2 = InstanceAssembler(different_config)

        # Should return the same instance
        assert assembler1 is assembler2
        # Config should not be changed by second initialization
        assert assembler1.instance_assemble_timeout == original_timeout


def test_init_with_none_config():
    """Test initialization with None config uses default"""
    with patch('threading.Thread'), patch('motor.controller.core.instance_assembler.EtcdClient'):
        assembler = InstanceAssembler(config=None)
        assert assembler.instance_assemble_timeout is not None
        assert hasattr(assembler, 'instance_assemble_timeout')


def test_register_new_instance(instance_assembler, test_config):
    """Test registering a new instance"""
    job_name = "test_job"
    msg = create_register_msg(job_name, test_config['pod_ip1'], test_config)

    result = instance_assembler.register(msg)

    assert result == 0
    assert job_name in instance_assembler.instances
    metadata = instance_assembler.instances[job_name]
    assert metadata.register_status == RegisterStatus.NOT_REGISTERED  # Initial state
    assert metadata.instance.job_name == job_name
    assert metadata.instance.id == 1  # First instance
    assert instance_assembler.ins_id_cnt == 2
    # Verify endpoints and node managers were added
    assert len(metadata.instance.endpoints) == 1
    assert len(metadata.instance.node_managers) == 1


def test_register_existing_instance(instance_assembler, test_config):
    """Test registering additional pods to existing instance"""
    job_name = "test_job"

    # First registration
    msg1 = create_register_msg(job_name, test_config['pod_ip1'], test_config)
    result1 = instance_assembler.register(msg1)
    assert result1 == 0
    assert len(instance_assembler.instances) == 1

    # Second registration to same instance
    msg2 = create_register_msg(
        job_name,
        test_config['pod_ip2'],
        test_config,
        ranktable=build_pod_ranktable(
            pod_ip=test_config['pod_ip2'], pod_device_num=2 * test_config['tp'], rank_offset=2 * test_config['tp']
        ),
    )
    result2 = instance_assembler.register(msg2)
    assert result2 == 0

    # Should still be only one instance entry
    assert len(instance_assembler.instances) == 1
    metadata = instance_assembler.instances[job_name]
    assert len(metadata.instance.endpoints) == 2  # Two pods registered


def test_register_already_assembled_instance(instance_assembler, test_config):
    """Test registering to an already assembled instance returns -1"""
    job_name = "test_job"

    # Create and assemble complete instance
    metadata = create_assembled_instance(instance_assembler, job_name, test_config)

    # For new registration, instance stays in assembler with ASSEMBLED status waiting for start command
    # Only when start command is sent successfully, it gets removed
    assert job_name in instance_assembler.instances
    assert metadata.register_status == RegisterStatus.ASSEMBLED

    # Mock successful start command to remove it from assembler
    def stop_sleep(*args, **kwargs):
        raise RuntimeError("Stop iteration")

    with patch(
        'motor.controller.api_client.node_manager_api_client.NodeManagerApiClient.send_start_command', return_value=True
    ):
        with patch.object(instance_assembler.work_condition, 'wait', side_effect=stop_sleep):
            try:
                instance_assembler._start_commmand_sender()
            except RuntimeError as e:
                if "Stop iteration" not in str(e):
                    raise

    # Now instance should be removed from assembler
    assert job_name not in instance_assembler.instances

    # Now try to register again - should return -1 since instance is fully managed
    with patch.object(InstanceManager(), 'has_active_instance_by_job_name', return_value=True):
        msg = create_register_msg(job_name, "127.0.0.3", test_config)
        result = instance_assembler.register(msg)
        assert result == -1


def test_reregister_new_instance(instance_assembler, test_config):
    """Test reregistering a new instance"""
    job_name = "test_reregister"

    # Build endpoints for reregister
    reg_msg = create_register_msg(job_name, test_config['pod_ip1'], test_config)
    endpoints = instance_assembler._build_single_endpoint(reg_msg, 0)

    msg = create_reregister_msg(
        job_name, test_config['pod_ip1'], instance_id=5, config=test_config, endpoints=endpoints
    )
    result = instance_assembler.reregister(msg)

    assert result == 0
    assert job_name in instance_assembler.instances
    metadata = instance_assembler.instances[job_name]
    assert metadata.register_status == RegisterStatus.NOT_REGISTERED  # Initial state
    assert metadata.is_reregister is True
    assert metadata.instance.id == 5
    assert instance_assembler.ins_id_cnt == 6  # instance_id + 1


def test_reregister_already_assembled_instance(instance_assembler, test_config):
    """Test reregistering to an already assembled instance returns -1"""
    job_name = "test_reregister"

    # First reregister and assemble (multi-endpoint mode: 2 pods × 2 endpoints = 4 endpoints)
    reg_msg = create_register_msg(job_name, test_config['pod_ip1'], test_config)
    endpoints = instance_assembler._build_multi_endpoints(reg_msg, 0)
    msg = create_reregister_msg(
        job_name, test_config['pod_ip1'], instance_id=0, config=test_config, endpoints=endpoints
    )
    result = instance_assembler.reregister(msg)
    assert result == 0

    # Register second pod to complete assembly
    reg_msg2 = create_register_msg(
        job_name,
        test_config['pod_ip2'],
        test_config,
        ranktable=build_pod_ranktable(
            pod_ip=test_config['pod_ip2'], pod_device_num=2 * test_config['tp'], rank_offset=2 * test_config['tp']
        ),
    )
    endpoints2 = instance_assembler._build_multi_endpoints(reg_msg2, 2)
    msg2 = create_reregister_msg(
        job_name, test_config['pod_ip2'], instance_id=0, config=test_config, endpoints=endpoints2
    )
    result2 = instance_assembler.reregister(msg2)
    assert result2 == 0

    # Assemble the instance (mock endpoint filtering for reregistration)
    metadata = instance_assembler.instances[job_name]
    with patch.object(instance_assembler, '_filter_abnormal_endpoints'):
        instance_assembler._assemble_instance(metadata)

    # Verify instance is assembled and moved to InstanceManager
    assert job_name not in instance_assembler.instances

    # Try to reregister again
    with patch.object(InstanceManager(), 'has_active_instance_by_job_name', return_value=True):
        msg3 = create_reregister_msg(job_name, "127.0.0.3", instance_id=0, config=test_config, endpoints=endpoints)
        result3 = instance_assembler.reregister(msg3)
        assert result3 == -1


def test_eval_register_status(instance_assembler, test_config):
    """Test _eval_register_status for different scenarios"""
    job_name_new = "test_new"
    job_name_assembling = "test_assembling"
    job_name_assembled = "test_assembled"

    # Test NOT_REGISTERED
    status = instance_assembler._eval_register_status(job_name_new)
    assert status == RegisterStatus.NOT_REGISTERED

    # Test ASSEMBLING
    msg = create_register_msg(job_name_assembling, test_config['pod_ip1'], test_config)
    instance_assembler.register(msg)
    status = instance_assembler._eval_register_status(job_name_assembling)
    assert status == RegisterStatus.ASSEMBLING

    # Test ASSEMBLED (instance managed by InstanceManager)
    with patch.object(InstanceManager(), 'has_active_instance_by_job_name', return_value=True):
        status = instance_assembler._eval_register_status(job_name_assembled)
        assert status == RegisterStatus.ASSEMBLED


def test_assembly_incomplete_instance(instance_assembler, test_config):
    """Test assembly of incomplete instance (not enough endpoints)"""
    job_name = "test_incomplete"

    # Register only one pod
    msg = create_register_msg(job_name, test_config['pod_ip1'], test_config, business_port=["8080"])
    instance_assembler.register(msg)

    metadata = instance_assembler.instances[job_name]
    original_status = metadata.register_status

    # Try to assemble (mock endpoint filtering)
    with patch.object(instance_assembler, '_filter_abnormal_endpoints'):
        instance_assembler._assemble_instance(metadata)

    # Should remain in assembling state
    assert metadata.register_status == original_status
    assert job_name in instance_assembler.instances


def test_assembly_complete_instance_new_registration(instance_assembler, test_config):
    """Test assembly of complete instance (new registration)"""
    job_name = "test_complete_new"

    # Create assembled instance
    metadata = create_assembled_instance(instance_assembler, job_name, test_config)

    # Should be assembled but still in instances (waiting for start command)
    assert metadata.register_status == RegisterStatus.ASSEMBLED
    assert job_name in instance_assembler.instances

    # Verify instance was added to InstanceManager
    instance_manager = InstanceManager()
    assert instance_manager.has_instance_by_job_name(job_name)


def test_assembly_complete_instance_reregistration(instance_assembler, test_config):
    """Test assembly of complete instance (reregistration)"""
    job_name = "test_complete_reregister"

    # Build endpoints for reregistration (multi-endpoint mode: 2 pods × 2 endpoints = 4 endpoints)
    reg_msg1 = create_register_msg(job_name, test_config['pod_ip1'], test_config)
    reg_msg2 = create_register_msg(
        job_name,
        test_config['pod_ip2'],
        test_config,
        ranktable=build_pod_ranktable(
            pod_ip=test_config['pod_ip2'], pod_device_num=2 * test_config['tp'], rank_offset=2 * test_config['tp']
        ),
    )

    endpoints1 = instance_assembler._build_multi_endpoints(reg_msg1, 0)
    endpoints2 = instance_assembler._build_multi_endpoints(reg_msg2, 2)

    # Reregister both pods
    msg1 = create_reregister_msg(job_name, test_config['pod_ip1'], 0, config=test_config, endpoints=endpoints1)
    msg2 = create_reregister_msg(job_name, test_config['pod_ip2'], 0, config=test_config, endpoints=endpoints2)

    instance_assembler.reregister(msg1)
    instance_assembler.reregister(msg2)

    metadata = instance_assembler.instances[job_name]
    assert metadata.is_reregister is True

    # Assemble (mock endpoint filtering for reregistration)
    with patch.object(instance_assembler, '_filter_abnormal_endpoints'):
        instance_assembler._assemble_instance(metadata)

    # For reregistration, instance should be removed from assembler after assembly
    assert job_name not in instance_assembler.instances

    # Verify instance was added to InstanceManager
    instance_manager = InstanceManager()
    assert instance_manager.has_instance_by_job_name(job_name)


@patch('motor.controller.core.instance_assembler.NodeManagerApiClient.query_status')
def test_assembly_timeout(mock_query_status, instance_assembler, test_config):
    """Test instance assembly timeout"""
    job_name = "test_timeout"

    # Mock NodeManagerApiClient.query_status to avoid network calls
    mock_query_status.return_value = {"status": True}

    # Set short timeout for faster test execution
    instance_assembler.instance_assemble_timeout = 0.05

    # Register incomplete instance
    msg = create_register_msg(job_name, test_config['pod_ip1'], test_config, business_port=["8080"])
    instance_assembler.register(msg)

    time.sleep(0.06)

    # Try to assemble - should remove timed out instance
    metadata = instance_assembler.instances[job_name]
    instance_assembler._assemble_instance(metadata)

    # Instance should be removed due to timeout
    assert job_name not in instance_assembler.instances


def test_send_start_command_success(instance_assembler, test_config):
    """Test successful start command sending"""
    job_name = "test_start_success"

    # Create assembled instance
    metadata = create_assembled_instance(instance_assembler, job_name, test_config)

    # Mock successful API calls
    with patch(
        'motor.controller.api_client.node_manager_api_client.NodeManagerApiClient.send_start_command'
    ) as mock_send:
        mock_send.return_value = True

        result = instance_assembler._send_start_command(metadata)

        assert result is True
        # Should be called for each node manager
        assert mock_send.call_count == len(metadata.instance.node_managers)


def test_send_start_command_partial_failure(instance_assembler, test_config):
    """Test start command with partial failure"""
    job_name = "test_start_partial_failure"

    # Create assembled instance
    metadata = create_assembled_instance(instance_assembler, job_name, test_config)

    # Mock partial failure
    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return call_count == 1  # First call succeeds, second fails

    with patch(
        'motor.controller.api_client.node_manager_api_client.NodeManagerApiClient.send_start_command'
    ) as mock_send:
        mock_send.side_effect = side_effect

        result = instance_assembler._send_start_command(metadata)

        assert result is False  # Should return False if any fails
        assert mock_send.call_count == len(metadata.instance.node_managers)


def test_send_start_command_no_endpoints(instance_assembler, test_config):
    """Test start command when some node managers have no endpoints"""
    # Create instance with node managers but only one has endpoints
    instance = Instance(
        job_name="test_no_endpoints",
        model_name="test_model",
        id=1,
        role=test_config['role'],
        parallel_config=test_config['parallel_config'],
    )

    # Add node managers
    instance.add_node_mgr("127.0.0.1", "8088")
    instance.add_node_mgr("127.0.0.2", "8089")

    # Only add endpoints for first node manager
    reg_msg = create_register_msg("test", "127.0.0.1", test_config)
    pod_endpoints = instance_assembler._build_single_endpoint(reg_msg, 0)
    instance.add_endpoints("127.0.0.1", pod_endpoints)

    metadata = AssembleInstanceMetadata(instance=instance)

    with patch(
        'motor.controller.api_client.node_manager_api_client.NodeManagerApiClient.send_start_command'
    ) as mock_send:
        mock_send.return_value = True

        result = instance_assembler._send_start_command(metadata)

        assert result is True
        # Should only be called for node manager with endpoints
        assert mock_send.call_count == 1


def test_start_command_sender_success(instance_assembler, test_config):
    """Test _start_command_sender removes instance after successful start"""
    job_name = "test_sender_success"

    # Create assembled instance
    create_assembled_instance(instance_assembler, job_name, test_config)

    # Mock successful send
    def stop_sleep(*args, **kwargs):
        raise RuntimeError("Stop iteration")

    with patch(
        'motor.controller.api_client.node_manager_api_client.NodeManagerApiClient.send_start_command'
    ) as mock_send:
        mock_send.return_value = True

        # Mock work_condition.wait to stop after one iteration
        with patch.object(instance_assembler.work_condition, 'wait', side_effect=stop_sleep):
            try:
                instance_assembler._start_commmand_sender()
            except RuntimeError as e:
                if "Stop iteration" not in str(e):
                    raise

        # Instance should be removed after successful start command
        assert job_name not in instance_assembler.instances


def test_start_command_sender_retry(instance_assembler, test_config):
    """Test _start_command_sender retries on failure"""
    job_name = "test_sender_retry"

    # Create assembled instance
    create_assembled_instance(instance_assembler, job_name, test_config)

    # Mock failed send
    def stop_sleep(*args, **kwargs):
        raise RuntimeError("Stop iteration")

    with patch(
        'motor.controller.api_client.node_manager_api_client.NodeManagerApiClient.send_start_command'
    ) as mock_send:
        mock_send.return_value = False

        # Mock work_condition.wait to stop after one iteration
        with patch.object(instance_assembler.work_condition, 'wait', side_effect=stop_sleep):
            try:
                instance_assembler._start_commmand_sender()
            except RuntimeError as e:
                if "Stop iteration" not in str(e):
                    raise

        # Instance should still be there with incremented retry count
        assert job_name in instance_assembler.instances
        assert instance_assembler.instances[job_name].start_command_send_times == 1


def test_start_command_sender_max_retries(instance_assembler, test_config):
    """Test _start_command_sender removes instance after max retries"""
    job_name = "test_sender_max_retries"

    # Set max retries to 2 (so we can see the retry count increment)
    instance_assembler.send_cmd_retry_times = 2

    # Create assembled instance
    create_assembled_instance(instance_assembler, job_name, test_config)

    # Mock failed sends
    def stop_sleep(*args, **kwargs):
        raise RuntimeError("Stop iteration")

    with patch(
        'motor.controller.api_client.node_manager_api_client.NodeManagerApiClient.send_start_command'
    ) as mock_send:
        mock_send.return_value = False

        # First attempt - should increment retry count
        with patch.object(instance_assembler.work_condition, 'wait', side_effect=stop_sleep):
            try:
                instance_assembler._start_commmand_sender()
            except RuntimeError as e:
                if "Stop iteration" not in str(e):
                    raise

        # Should still be there after first failure, retry count incremented
        assert job_name in instance_assembler.instances
        assert instance_assembler.instances[job_name].start_command_send_times == 1

        # Second attempt - should remove instance since max retries (2) reached
        with patch.object(instance_assembler.work_condition, 'wait', side_effect=stop_sleep):
            try:
                instance_assembler._start_commmand_sender()
            except RuntimeError as e:
                if "Stop iteration" not in str(e):
                    raise

        # Instance should be removed after max retries
        assert job_name not in instance_assembler.instances


def test_persist_data_disabled(mock_config):
    """Test persist_data when ETCD persistence is disabled"""
    # Create assembler with persistence disabled
    mock_config.etcd_config.enable_etcd_persistence = False

    with patch('threading.Thread'), patch('motor.controller.core.instance_assembler.EtcdClient') as mock_etcd_class:
        mock_etcd = MagicMock()
        mock_etcd.persist_data.return_value = True
        mock_etcd_class.return_value = mock_etcd

        assembler = InstanceAssembler(mock_config)

        result = assembler.persist_data()
        # persist_data always calls etcd_client.persist_data regardless of enable_etcd_persistence flag
        assert result is True


def test_persist_data_enabled(instance_assembler, test_config):
    """Test persist_data when ETCD persistence is enabled"""
    # etcd persistence is already enabled in the config used for initialization

    # Create some test data
    create_assembled_instance(instance_assembler, "test_job", test_config)

    # Reset mock to clear previous calls
    instance_assembler.etcd_client.persist_data.reset_mock()

    instance_assembler.persist_data()

    # Verify persist was called on etcd_client
    instance_assembler.etcd_client.persist_data.assert_called_once()
    args, kwargs = instance_assembler.etcd_client.persist_data.call_args
    assert "/controller/instance_assembler" in args[0]
    assert "state" in args[1]


def test_restore_data_disabled(instance_assembler, test_config):
    """Test restore_data when ETCD persistence is disabled"""
    # Disable persistence
    instance_assembler.etcd_config.enable_etcd_persistence = False

    # Mock ETCD to return None (no data when persistence is disabled)
    with patch.object(instance_assembler.etcd_client, 'restore_data', return_value=None):
        result = instance_assembler.restore_data()
        assert result is True


def test_restore_data_enabled(instance_assembler, test_config):
    """Test restore_data when ETCD persistence is enabled"""
    # etcd persistence is already enabled in the config used for initialization

    # Mock ETCD returning some data (new format: single PersistentState)
    state = PersistentState(
        data={"ins_id_cnt": 5, "instances": {}},
        version=1,
        timestamp=time.time(),
        checksum="",  # Will be calculated
    )
    state.checksum = state.calculate_checksum()

    mock_persistent_states = {"state": state}

    with patch.object(instance_assembler.etcd_client, 'restore_data', return_value=mock_persistent_states):
        result = instance_assembler.restore_data()

        assert result is True
        assert instance_assembler.ins_id_cnt == 5


def test_checksum_calculation(instance_assembler, test_config):
    """Test checksum calculation for data integrity"""
    # Create test metadata
    metadata = create_assembled_instance(instance_assembler, "test_checksum", test_config)

    # Create a persistent state to test checksum calculation
    metadata_data = metadata.model_dump(mode='json')

    state = PersistentState(data=metadata_data, version=1, timestamp=time.time(), checksum="")

    checksum = state.calculate_checksum()

    assert isinstance(checksum, str)
    assert len(checksum) > 0

    # Same data should produce same checksum
    checksum2 = state.calculate_checksum()
    assert checksum == checksum2


def test_ins_id_cnt_checksum(instance_assembler):
    """Test checksum calculation for ins_id_cnt"""
    instance_assembler.ins_id_cnt = 42

    # Create a persistent state for ins_id_cnt
    ins_id_cnt_data = {"ins_id_cnt": instance_assembler.ins_id_cnt}
    state = PersistentState(data=ins_id_cnt_data, version=1, timestamp=time.time(), checksum="")

    checksum = state.calculate_checksum()

    assert isinstance(checksum, str)
    assert len(checksum) > 0

    # Same value should produce same checksum
    checksum2 = state.calculate_checksum()
    assert checksum == checksum2


def test_persist_data_exception_handling(instance_assembler, test_config):
    """Test persist_data exception handling"""
    # Create test data
    create_assembled_instance(instance_assembler, "test_persist_exception", test_config)

    # Mock etcd_client.persist_data to raise an exception
    with patch.object(instance_assembler.etcd_client, 'persist_data', side_effect=Exception("ETCD connection failed")):
        result = instance_assembler.persist_data()

        assert result is False


def test_restore_data_exception_handling(instance_assembler):
    """Test restore_data exception handling"""
    # Mock etcd_client.restore_data to raise an exception
    with patch.object(instance_assembler.etcd_client, 'restore_data', side_effect=Exception("ETCD connection failed")):
        result = instance_assembler.restore_data()

        assert result is False


def test_restore_data_invalid_checksum(instance_assembler):
    """Test restore_data with invalid checksum (corrupted data)"""
    # Create mock persistent state with invalid checksum (new format: single PersistentState)
    mock_persistent_states = {
        "state": PersistentState(
            data={"ins_id_cnt": 5, "instances": {}},
            version=1,
            timestamp=time.time(),
            checksum="invalid_checksum",  # Wrong checksum
        )
    }

    with patch.object(instance_assembler.etcd_client, 'restore_data', return_value=mock_persistent_states):
        result = instance_assembler.restore_data()

        assert result is False  # Should fail because checksum validation fails
        assert instance_assembler.ins_id_cnt == 1  # Should not restore invalid data


def test_restore_data_reconstruction_exception(instance_assembler):
    """Test restore_data with reconstruction exception"""
    # Mock AssembleInstanceMetadata.model_validate to raise an exception
    with patch('motor.controller.core.instance_assembler.AssembleInstanceMetadata.model_validate') as mock_validate:
        mock_validate.side_effect = Exception("Metadata validation failed")

        # Mock persistent state (new format: single PersistentState)
        metadata_data = {
            "instance": {
                "job_name": "test_instance",
                "model_name": "test_model",
                "id": 0,
                "role": "prefill",
                "parallel_config": {
                    "dp_size": 1,
                    "pcp_size": 1,
                    "tp_size": 1,
                    "ep_size": 1,
                    "pp_size": 1,
                    "world_size": 1,
                },
                "endpoints": {},
                "node_managers": [],
            },
            "register_status": "NOT_REGISTERED",
            "start_command_send_times": 0,
            "register_timestamp": time.time(),
            "is_reregister": False,
        }

        state = PersistentState(
            data={"ins_id_cnt": 1, "instances": {"test_instance": metadata_data}},
            version=1,
            timestamp=time.time(),
            checksum="",  # Will be calculated
        )
        state.checksum = state.calculate_checksum()

        mock_persistent_states = {"state": state}

        with patch.object(instance_assembler.etcd_client, 'restore_data', return_value=mock_persistent_states):
            result = instance_assembler.restore_data()

            assert result is True  # Should succeed but skip problematic instance
            assert len(instance_assembler.instances) == 0  # Should not restore invalid instance


def test_checksum_calculation_exception_handling(instance_assembler, test_config):
    """Test checksum calculation exception handling"""
    # Create test metadata
    metadata = create_assembled_instance(instance_assembler, "test_checksum_exception", test_config)

    # Create a persistent state to test checksum calculation
    metadata_data = metadata.model_dump(mode='json')

    state = PersistentState(data=metadata_data, version=1, timestamp=time.time(), checksum="")

    # Mock hashlib.sha256 to raise an exception
    with patch.object(hashlib, 'sha256', side_effect=Exception("Hash calculation failed")):
        checksum = state.calculate_checksum()

        assert checksum == ""  # Should return empty string on exception


def test_ins_id_cnt_checksum_exception_handling(instance_assembler):
    """Test ins_id_cnt checksum calculation exception handling"""
    instance_assembler.ins_id_cnt = 42

    # Create a persistent state for ins_id_cnt
    ins_id_cnt_data = {"ins_id_cnt": instance_assembler.ins_id_cnt}
    state = PersistentState(data=ins_id_cnt_data, version=1, timestamp=time.time(), checksum="")

    # Mock hashlib.sha256 to raise an exception
    with patch.object(hashlib, 'sha256', side_effect=Exception("Hash calculation failed")):
        checksum = state.calculate_checksum()

        assert checksum == ""  # Should return empty string on exception


def test_persistent_state_is_valid_method():
    """Test PersistentState.is_valid method"""
    # Create a valid state
    valid_state = PersistentState(
        data={"test": "data"},
        version=1,
        timestamp=time.time(),
        checksum="",  # Will be calculated
    )

    # Manually set correct checksum
    valid_state.checksum = valid_state.calculate_checksum()
    assert valid_state.is_valid() is True

    # Create invalid state with wrong checksum
    invalid_state = PersistentState(data={"test": "data"}, version=1, timestamp=time.time(), checksum="wrong_checksum")
    assert invalid_state.is_valid() is False


def test_restore_data_with_type_conversion():
    """Test restoration with string-formatted data from ETCD (type conversion)"""
    # Simulate metadata data as it would come from ETCD - nested format with all values as strings
    etcd_string_metadata = {
        "instance": {
            "job_name": "test_type_conversion",
            "model_name": "test_model",
            "id": "208",  # int as string
            "role": "prefill",
            "parallel_config": None,
            "endpoints": {},  # dict
            "node_managers": [],  # list
        },
        "register_status": "ASSEMBLED",  # enum value as string from ETCD
        "start_command_send_times": "0",  # int as string
        "register_timestamp": str(time.time()),  # float as string
        "is_reregister": "False",  # bool as string
    }

    # Mock persistent state with string-formatted metadata (new format: single PersistentState)
    persistent_state = PersistentState(
        data={"ins_id_cnt": 1, "instances": {"test_type_conversion": etcd_string_metadata}},
        version=1,
        timestamp=time.time(),
        checksum="",  # Will be calculated
    )
    persistent_state.checksum = persistent_state.calculate_checksum()

    mock_persistent_states = {"state": persistent_state}

    with patch('motor.controller.core.instance_assembler.EtcdClient') as mock_etcd_class:
        mock_client = MagicMock()
        mock_client.restore_data.return_value = mock_persistent_states
        mock_etcd_class.return_value = mock_client

        # Create assembler with ETCD enabled
        config = ControllerConfig()
        config.etcd_config.enable_etcd_persistence = True

        with patch('threading.Thread'):
            assembler = InstanceAssembler(config)
            result = assembler.restore_data()

        # Should succeed - Pydantic should handle type conversion
        assert result is True
        assert "test_type_conversion" in assembler.instances

        metadata = assembler.instances["test_type_conversion"]
        assert metadata.instance.id == 208  # string "208" converted to int 208
        assert metadata.register_status == RegisterStatus.ASSEMBLED  # string "ASSEMBLED" converted to enum
        assert metadata.start_command_send_times == 0  # string "0" converted to int 0
        assert metadata.is_reregister is True  # restore_data overrides to True for Controller restart recovery


def test_restore_data_with_invalid_enum_value():
    """Test restoration fails gracefully with invalid enum values in metadata"""
    # Simulate corrupted metadata with invalid enum value (nested format)
    corrupted_metadata = {
        "instance": {
            "job_name": "test_invalid_enum",
            "model_name": "test_model",
            "id": "209",
            "role": "prefill",
            "parallel_config": None,
            "endpoints": {},
            "node_managers": [],
        },
        "register_status": "999",  # Invalid enum value as string
        "start_command_send_times": "0",
        "register_timestamp": str(time.time()),
        "is_reregister": "False",
    }

    # Mock persistent state (new format: single PersistentState)
    persistent_state = PersistentState(
        data={"ins_id_cnt": 1, "instances": {"test_invalid_enum": corrupted_metadata}},
        version=1,
        timestamp=time.time(),
        checksum="",  # Will be calculated
    )
    persistent_state.checksum = persistent_state.calculate_checksum()

    mock_persistent_states = {"state": persistent_state}

    with patch('motor.controller.core.instance_assembler.EtcdClient') as mock_etcd_class:
        mock_client = MagicMock()
        mock_client.restore_data.return_value = mock_persistent_states
        mock_etcd_class.return_value = mock_client

        # Create assembler with ETCD enabled
        config = ControllerConfig()
        config.etcd_config.enable_etcd_persistence = True

        with patch('threading.Thread'):
            assembler = InstanceAssembler(config)

            # This should succeed (data restoration succeeds) but metadata reconstruction fails
            result = assembler.restore_data()
            assert result is True

            # Instance should not be restored due to validation error
            assert "test_invalid_enum" not in assembler.instances


def test_start_method(mock_config):
    """Test start method starts threads"""
    with patch('threading.Thread') as mock_thread_class:
        with patch('motor.controller.core.instance_assembler.EtcdClient'):
            assembler = InstanceAssembler(mock_config)

            assembler.start()

            # Verify two threads were created and started
            assert mock_thread_class.call_count == 2
            assert mock_thread_class.return_value.start.call_count == 2


def test_stop_method(mock_config):
    """Test stop method sets stop event and joins threads"""
    with patch('threading.Thread') as mock_thread_class:
        with patch('motor.controller.core.instance_assembler.EtcdClient'):
            mock_thread1 = MagicMock()
            mock_thread2 = MagicMock()
            mock_thread1.is_alive.return_value = True
            mock_thread2.is_alive.return_value = True
            mock_thread_class.side_effect = [mock_thread1, mock_thread2]

            assembler = InstanceAssembler(mock_config)
            assembler.start()  # Start to initialize threads

            assembler.stop()

            # Verify stop event is set
            assert assembler.stop_event.is_set()

            # Verify threads were joined
            mock_thread1.join.assert_called_once()
            mock_thread2.join.assert_called_once()


def test_instances_assembler_loop_stop_event(instance_assembler, test_config):
    """Test _instances_assembler_loop respects stop event"""
    # Set stop event
    instance_assembler.stop_event.set()

    # Mock work_condition.wait to raise RuntimeError when stop event is set
    def stop_sleep(*args, **kwargs):
        raise RuntimeError("Stop iteration")

    with patch.object(instance_assembler.work_condition, 'wait', side_effect=stop_sleep):
        try:
            instance_assembler._instances_assembler_loop()
        except RuntimeError as e:
            if "Stop iteration" not in str(e):
                raise

    # Should exit without processing


def test_multiple_instances_registration(instance_assembler, test_config):
    """Test registering multiple instances"""
    num_instances = 5

    for i in range(num_instances):
        job_name = f"perf_test_{i}"
        success = register_instance_with_pods(instance_assembler, job_name, test_config)
        assert success

    assert len(instance_assembler.instances) == num_instances

    # Verify all instances have unique IDs
    ids = [metadata.instance.id for metadata in instance_assembler.instances.values()]
    assert len(set(ids)) == num_instances


def test_ins_id_cnt_increment(instance_assembler, test_config):
    """Test ins_id_cnt increments correctly"""
    initial_cnt = instance_assembler.ins_id_cnt

    # Register first instance
    register_instance_with_pods(instance_assembler, "job1", test_config)
    assert instance_assembler.ins_id_cnt == initial_cnt + 1

    # Register second instance
    register_instance_with_pods(instance_assembler, "job2", test_config)
    assert instance_assembler.ins_id_cnt == initial_cnt + 2


def test_update_config(instance_assembler):
    """Test update_config method updates configuration and recreates ETCD client"""
    # Create new config with different ETCD settings
    new_config = ControllerConfig()
    new_config.etcd_config.etcd_host = "new-etcd-host"
    new_config.etcd_config.etcd_port = 2380
    new_config.etcd_config.etcd_timeout = 30.0
    new_config.etcd_config.enable_etcd_persistence = True

    with patch('motor.controller.core.instance_assembler.EtcdClient') as mock_etcd_class:
        mock_client = MagicMock()
        mock_etcd_class.return_value = mock_client

        # Clear the mock call history to track new calls
        mock_etcd_class.reset_mock()

        # Update config
        instance_assembler.update_config(new_config)

        # Verify config was updated
        assert instance_assembler.etcd_config is new_config.etcd_config
        assert instance_assembler.etcd_config.etcd_host == "new-etcd-host"
        assert instance_assembler.etcd_config.etcd_port == 2380
        assert instance_assembler.etcd_config.etcd_timeout == 30.0

        # Verify ETCD client constructor was called with new config
        mock_etcd_class.assert_called_once_with(
            etcd_config=new_config.etcd_config, tls_config=new_config.etcd_tls_config
        )


# ===== Persistence and Recovery Tests =====


def test_persist_and_restore_data_success(instance_assembler, test_config):
    """Test successful persist and restore of instance assembler data"""
    # Create test instance
    metadata = create_assembled_instance(instance_assembler, "test_persist", test_config)

    # Enable persistence in config
    instance_assembler.etcd_config.enable_etcd_persistence = True

    # Mock successful ETCD operations
    with patch.object(instance_assembler.etcd_client, 'persist_data', return_value=True) as mock_persist:
        with patch.object(instance_assembler.etcd_client, 'restore_data') as mock_restore:
            # Persist data
            persist_result = instance_assembler.persist_data()
            assert persist_result is True

            # Verify persist was called
            mock_persist.assert_called_once()
            args, kwargs = mock_persist.call_args
            assert "/controller/instance_assembler" in args[0]

            # Create mock persistent state for restore (new format: single PersistentState)
            metadata_data = metadata.model_dump(mode='json')
            assembler_data = {"ins_id_cnt": instance_assembler.ins_id_cnt, "instances": {"test_persist": metadata_data}}

            assembler_state = PersistentState(data=assembler_data, version=1, timestamp=time.time(), checksum="")
            assembler_state.checksum = assembler_state.calculate_checksum()
            mock_persistent_states = {"state": assembler_state}

            mock_restore.return_value = mock_persistent_states

            # Create new assembler instance for restore test
            with patch('threading.Thread'), patch('motor.controller.core.instance_assembler.EtcdClient'):
                # Create a mock config similar to the original
                new_config = ControllerConfig()
                new_config.etcd_config.enable_etcd_persistence = True
                new_config.instance_config.instance_assemble_timeout = 1.0
                new_config.instance_config.instance_assembler_check_interval = 0.1
                new_config.instance_config.instance_assembler_cmd_send_interval = 0.1
                new_config.instance_config.send_cmd_retry_times = 3
                new_assembler = InstanceAssembler(new_config)

                # Restore data
                restore_result = new_assembler.restore_data()
                assert restore_result is True

                # Verify data was restored
                assert new_assembler.ins_id_cnt == instance_assembler.ins_id_cnt
                assert "test_persist" in new_assembler.instances
                restored_metadata = new_assembler.instances["test_persist"]
                assert restored_metadata.instance.job_name == metadata.instance.job_name
                assert restored_metadata.register_status == metadata.register_status


def test_persist_data_with_checksum_validation(instance_assembler, test_config):
    """Test that persisted data includes correct checksums"""
    # Create test instance
    create_assembled_instance(instance_assembler, "test_checksum", test_config)

    # Enable persistence
    instance_assembler.etcd_config.enable_etcd_persistence = True

    with patch.object(instance_assembler.etcd_client, 'persist_data', return_value=True) as mock_persist:
        # Persist data
        result = instance_assembler.persist_data()
        assert result is True

        # Verify the data passed to persist_data
        args, kwargs = mock_persist.call_args
        persisted_data = args[1]

        assert "state" in persisted_data

        # Get the PersistentState data
        state_data = persisted_data["state"]
        assert "checksum" in state_data
        assert len(state_data["checksum"]) > 0

        # Verify the data structure
        assert "data" in state_data
        assert "ins_id_cnt" in state_data["data"]
        assert "instances" in state_data["data"]
        assert "test_checksum" in state_data["data"]["instances"]

        # Verify checksum is valid by reconstructing the state
        state = PersistentState(**state_data)
        assert state.is_valid()


def test_restore_data_with_invalid_checksum(instance_assembler, test_config):
    """Test restore skips data with invalid checksums"""
    # Create mock persistent state with invalid checksum (new format: single PersistentState)
    mock_persistent_states = {
        "state": PersistentState(
            data={"ins_id_cnt": 5, "instances": {}},
            version=1,
            timestamp=time.time(),
            checksum="invalid_checksum",  # Wrong checksum
        )
    }

    with patch.object(instance_assembler.etcd_client, 'restore_data', return_value=mock_persistent_states):
        result = instance_assembler.restore_data()

        # Should fail because checksum validation fails
        assert result is False
        assert instance_assembler.ins_id_cnt == 1  # Should not restore invalid data


def test_persistence_disabled_in_config(instance_assembler, test_config):
    """Test that persistence is properly disabled when config flag is False"""
    # Ensure persistence is disabled
    instance_assembler.etcd_config.enable_etcd_persistence = False

    # Create test instance
    create_assembled_instance(instance_assembler, "test_disabled", test_config)

    # Register should not call persist (only called when enable_persistence is True)
    with patch.object(instance_assembler.etcd_client, 'persist_data', return_value=True) as mock_persist:
        # Try to persist manually - should still work but not be called from register
        result = instance_assembler.persist_data()
        assert result is True  # persist_data always calls etcd_client.persist_data regardless of flag

        # But register should not call persist when disabled
        msg = create_register_msg("test_register_disabled", test_config['pod_ip1'], test_config)
        instance_assembler.register(msg)

        # persist_data should not have been called again (only once from manual call above)
        assert mock_persist.call_count == 1


def test_persist_empty_state(instance_assembler):
    """Test persisting when no instances exist"""
    # Enable persistence
    instance_assembler.etcd_config.enable_etcd_persistence = True

    with patch.object(instance_assembler.etcd_client, 'persist_data', return_value=True) as mock_persist:
        result = instance_assembler.persist_data()
        assert result is True

        # Verify data was persisted
        args, kwargs = mock_persist.call_args
        persisted_data = args[1]

        assert "state" in persisted_data

        # Verify state data
        assembler_data = persisted_data["state"]
        assert assembler_data["data"]["ins_id_cnt"] == instance_assembler.ins_id_cnt
        assert len(assembler_data["data"]["instances"]) == 0  # Empty instances
        assert assembler_data["version"] >= 1
        assert assembler_data["timestamp"] > 0
        assert len(assembler_data["checksum"]) > 0


def test_restore_no_data_available(instance_assembler):
    """Test restore when no data is available in ETCD"""
    with patch.object(instance_assembler.etcd_client, 'restore_data', return_value=None):
        result = instance_assembler.restore_data()

        # Should succeed with empty state
        assert result is True
        assert len(instance_assembler.instances) == 0
        assert instance_assembler.ins_id_cnt == 1  # Default value


def test_filter_abnormal_endpoints_all_normal(instance_assembler, test_config):
    """Test _filter_abnormal_endpoints filters endpoints when all node managers report normal status"""
    # Create instance with node managers
    instance = Instance(
        job_name="test_filter_normal",
        model_name="test_model",
        id=1,
        role=test_config['role'],
        parallel_config=test_config['parallel_config'],
    )

    # Add node managers
    instance.add_node_mgr("127.0.0.1", "8088")
    instance.add_node_mgr("127.0.0.2", "8088")

    # Mock NodeManagerApiClient.query_status to return normal status
    with patch('motor.controller.core.instance_assembler.NodeManagerApiClient.query_status') as mock_query_status:
        mock_query_status.return_value = {"status": True}

        instance_assembler._filter_abnormal_endpoints(instance)

        # Verify query_status was called for each node manager
        assert mock_query_status.call_count == 2


def test_filter_abnormal_endpoints_with_abnormal(instance_assembler, test_config):
    """Test _filter_abnormal_endpoints does not filter endpoints when node managers are reachable"""
    # Create instance with node managers
    instance = Instance(
        job_name="test_filter_abnormal",
        model_name="test_model",
        id=1,
        role=test_config['role'],
        parallel_config=test_config['parallel_config'],
    )

    # Add node managers and endpoints
    instance.add_node_mgr("127.0.0.1", "8088")
    instance.add_node_mgr("127.0.0.2", "8088")

    # Add endpoints for both nodes
    endpoints1 = {1: Endpoint(id=1, ip="127.0.0.1", business_port="1001", mgmt_port="9001")}
    endpoints2 = {2: Endpoint(id=2, ip="127.0.0.2", business_port="1002", mgmt_port="9002")}
    instance.add_endpoints("127.0.0.1", endpoints1)
    instance.add_endpoints("127.0.0.2", endpoints2)

    # Mock NodeManagerApiClient.query_status - both are reachable (no exceptions)
    with patch('motor.controller.core.instance_assembler.NodeManagerApiClient.query_status') as mock_query_status:
        # Both calls succeed, regardless of status content
        mock_query_status.side_effect = [{"status": True}, {"status": False}]

        instance_assembler._filter_abnormal_endpoints(instance)

        # No endpoints should be removed since both node managers are reachable
        assert instance.get_endpoints_num() == 2  # Both endpoints remain
        assert "127.0.0.1" in instance.endpoints
        assert "127.0.0.2" in instance.endpoints
        assert len(instance.node_managers) == 2  # Both node managers remain


def test_filter_abnormal_endpoints_invalid_response(instance_assembler, test_config):
    """Test _filter_abnormal_endpoints does not filter endpoints when node manager responds (even with invalid response)"""
    # Create instance with node managers
    instance = Instance(
        job_name="test_filter_invalid",
        model_name="test_model",
        id=1,
        role=test_config['role'],
        parallel_config=test_config['parallel_config'],
    )

    # Add node manager and endpoints
    instance.add_node_mgr("127.0.0.1", "8088")
    endpoints = {1: Endpoint(id=1, ip="127.0.0.1", business_port="1001", mgmt_port="9001")}
    instance.add_endpoints("127.0.0.1", endpoints)

    # Mock NodeManagerApiClient.query_status to return invalid response but no exception
    with patch('motor.controller.core.instance_assembler.NodeManagerApiClient.query_status') as mock_query_status:
        mock_query_status.return_value = {"invalid": "response"}  # No 'status' field, but call succeeds

        instance_assembler._filter_abnormal_endpoints(instance)

        # No endpoints should be removed since node manager is reachable
        assert instance.get_endpoints_num() == 1
        assert len(instance.node_managers) == 1


def test_filter_abnormal_endpoints_connection_error(instance_assembler, test_config):
    """Test _filter_abnormal_endpoints filters endpoints when connection to node manager fails"""
    # Create instance with node managers
    instance = Instance(
        job_name="test_filter_error",
        model_name="test_model",
        id=1,
        role=test_config['role'],
        parallel_config=test_config['parallel_config'],
    )

    # Add node manager and endpoints
    instance.add_node_mgr("127.0.0.1", "8088")
    endpoints = {1: Endpoint(id=1, ip="127.0.0.1", business_port="1001", mgmt_port="9001")}
    instance.add_endpoints("127.0.0.1", endpoints)

    # Mock NodeManagerApiClient.query_status to raise exception
    with patch('motor.controller.core.instance_assembler.NodeManagerApiClient.query_status') as mock_query_status:
        mock_query_status.side_effect = Exception("Connection failed")

        instance_assembler._filter_abnormal_endpoints(instance)

        # Verify endpoints were removed due to connection failure
        assert instance.get_endpoints_num() == 0
        assert len(instance.node_managers) == 0


def test_filter_abnormal_endpoints_mixed_scenarios(instance_assembler, test_config):
    """Test _filter_abnormal_endpoints with mixed reachable/unreachable node managers"""
    # Create instance with multiple node managers
    instance = Instance(
        job_name="test_filter_mixed",
        model_name="test_model",
        id=1,
        role=test_config['role'],
        parallel_config=test_config['parallel_config'],
    )

    # Add node managers and endpoints
    instance.add_node_mgr("127.0.0.1", "8088")  # Will be reachable
    instance.add_node_mgr("127.0.0.2", "8088")  # Will fail connection

    endpoints1 = {1: Endpoint(id=1, ip="127.0.0.1", business_port="1001", mgmt_port="9001")}
    endpoints2 = {2: Endpoint(id=2, ip="127.0.0.2", business_port="1002", mgmt_port="9002")}
    instance.add_endpoints("127.0.0.1", endpoints1)
    instance.add_endpoints("127.0.0.2", endpoints2)

    # Mock NodeManagerApiClient.query_status - first succeeds, second fails
    with patch('motor.controller.core.instance_assembler.NodeManagerApiClient.query_status') as mock_query_status:
        mock_query_status.side_effect = [{"status": True}, Exception("Connection failed")]

        instance_assembler._filter_abnormal_endpoints(instance)

        # Only unreachable node manager's endpoints should be removed
        assert instance.get_endpoints_num() == 1
        assert "127.0.0.1" in instance.endpoints  # Reachable node manager's endpoints remain
        assert "127.0.0.2" not in instance.endpoints  # Unreachable node manager's endpoints removed
        assert len(instance.node_managers) == 1  # Only unreachable node manager removed


def test_filter_abnormal_endpoints_no_node_managers(instance_assembler, test_config, caplog):
    """Test _filter_abnormal_endpoints handles case when instance has no node managers"""
    # Create instance without node managers
    instance = Instance(
        job_name="test_filter_no_managers",
        model_name="test_model",
        id=1,
        role=test_config['role'],
        parallel_config=test_config['parallel_config'],
    )

    with caplog.at_level('WARNING'):
        instance_assembler._filter_abnormal_endpoints(instance)

    # Method should complete without error when no node managers
    assert "No node managers found for instance test_filter_no_managers(id:1), cannot filter endpoints" in caplog.text


def test_assemble_instance_with_abnormal_endpoints(instance_assembler, test_config):
    """Test _assemble_instance when abnormal endpoints are removed leaving insufficient endpoints"""
    # Create instance with only enough endpoints (= dp_size)
    instance = Instance(
        job_name="test_assemble_abnormal",
        model_name="test_model",
        id=1,
        role=test_config['role'],
        parallel_config=test_config['parallel_config'],  # dp=4
    )

    # Add exactly dp_size endpoints
    for i in range(1, 5):
        pod_ip = f"127.0.0.{i}"
        endpoints = {i: Endpoint(id=i, ip=pod_ip, business_port=f"100{i}", mgmt_port=f"900{i}")}
        instance.add_endpoints(pod_ip, endpoints)
        instance.add_node_mgr(pod_ip, "8088")

    # Create metadata
    metadata = AssembleInstanceMetadata(instance=instance, register_timestamp=time.time())

    # Mock _filter_abnormal_endpoints to remove some endpoints (simulate abnormal detection)
    def mock_filter(instance_to_filter):
        # Remove 2 endpoints, leaving only 2 which is less than dp_size=4
        if "127.0.0.1" in instance_to_filter.endpoints:
            instance_to_filter.del_endpoints("127.0.0.1")
        if "127.0.0.2" in instance_to_filter.endpoints:
            instance_to_filter.del_endpoints("127.0.0.2")

    with patch.object(instance_assembler, '_filter_abnormal_endpoints', side_effect=mock_filter):
        instance_assembler._assemble_instance(metadata)

        # Should not be assembled because not enough endpoints remain after filtering
        assert metadata.register_status != RegisterStatus.ASSEMBLED


def test_assemble_instance_with_healthy_endpoints(instance_assembler, test_config):
    """Test _assemble_instance when endpoints are enough and all healthy"""
    # Create instance with enough endpoints (>= dp_size)
    instance = Instance(
        job_name="test_assemble_healthy",
        model_name="test_model",
        id=1,
        role=test_config['role'],
        parallel_config=test_config['parallel_config'],  # dp=4
    )

    # Add exactly dp_size endpoints
    for i in range(1, 5):
        pod_ip = f"127.0.0.{i}"
        endpoints = {i: Endpoint(id=i, ip=pod_ip, business_port=f"100{i}", mgmt_port=f"900{i}")}
        instance.add_endpoints(pod_ip, endpoints)
        instance.add_node_mgr(pod_ip, "8088")

    # Create metadata
    metadata = AssembleInstanceMetadata(instance=instance, register_timestamp=time.time())

    # Mock _filter_abnormal_endpoints (no return value needed)
    # Mock InstanceManager.add_instance
    with (
        patch.object(instance_assembler, '_filter_abnormal_endpoints'),
        patch('motor.controller.core.instance_assembler.InstanceManager') as mock_im_class,
    ):
        mock_im = MagicMock()
        mock_im_class.return_value = mock_im

        instance_assembler._assemble_instance(metadata)

        # Should be assembled because all endpoints are healthy
        assert metadata.register_status == RegisterStatus.ASSEMBLED
        # InstanceManager.add_instance should be called
        mock_im.add_instance.assert_called_once_with(instance)


def test_is_endpoints_enough_multi_endpoint_disabled():
    """Test is_endpoints_enough when enable_multi_endpoints is False"""
    # Test case 1: Not enough node managers
    instance1 = Instance(
        job_name="test_not_enough_nodes",
        model_name="test_model",
        id=1,
        role="both",
        parallel_config=ParallelConfig(world_size=16),  # world_size=16
        enable_multi_endpoints=False,
    )
    instance1.add_node_mgr("127.0.0.1", "8080", device_num=8)  # 1 node with 8 devices
    assert instance1.is_endpoints_enough() is False  # Need 2 nodes (16/8=2)

    # Test case 2: Enough node managers
    instance2 = Instance(
        job_name="test_enough_nodes",
        model_name="test_model",
        id=2,
        role="both",
        parallel_config=ParallelConfig(world_size=16),  # world_size=16
        enable_multi_endpoints=False,
    )
    instance2.add_node_mgr("127.0.0.1", "8080", device_num=8)
    instance2.add_node_mgr("127.0.0.2", "8081", device_num=8)  # 2 nodes with 8 devices each
    assert instance2.is_endpoints_enough() is True  # Have 2 nodes (16/8=2)

    # Test case 3: World size not divisible by device_num (should use ceiling)
    instance3 = Instance(
        job_name="test_ceiling_nodes",
        model_name="test_model",
        id=3,
        role="both",
        parallel_config=ParallelConfig(world_size=20),  # world_size=20
        enable_multi_endpoints=False,
    )
    instance3.add_node_mgr("127.0.0.1", "8080", device_num=8)
    instance3.add_node_mgr("127.0.0.2", "8081", device_num=8)
    instance3.add_node_mgr("127.0.0.3", "8082", device_num=8)  # 3 nodes with 8 devices each
    assert instance3.is_endpoints_enough() is True  # Need 3 nodes (ceil(20/8)=3)

    # Test case 4: Multi-endpoint enabled (should check dp_size)
    instance4 = Instance(
        job_name="test_multi_endpoint",
        model_name="test_model",
        id=4,
        role="both",
        parallel_config=ParallelConfig(dp_size=4, tp_size=1, pp_size=1),
        enable_multi_endpoints=True,
    )
    # Add 2 endpoints (less than dp_size=4)
    endpoints = {
        0: Endpoint(id=0, ip="127.0.0.1", business_port="8000", mgmt_port="9000"),
        1: Endpoint(id=1, ip="127.0.0.1", business_port="8001", mgmt_port="9001"),
    }
    instance4.add_endpoints("127.0.0.1", endpoints)
    assert instance4.is_endpoints_enough() is False  # Need 4 endpoints

    # Add more endpoints to reach dp_size
    endpoints2 = {
        2: Endpoint(id=2, ip="127.0.0.2", business_port="8002", mgmt_port="9002"),
        3: Endpoint(id=3, ip="127.0.0.2", business_port="8003", mgmt_port="9003"),
    }
    instance4.add_endpoints("127.0.0.2", endpoints2)
    assert instance4.is_endpoints_enough() is True  # Have 4 endpoints


def test_get_all_endpoints_multi_endpoint_disabled():
    """Test get_all_endpoints when enable_multi_endpoints is False"""
    # Test case 1: Multi-endpoint disabled - should only return endpoint with id=0
    instance1 = Instance(
        job_name="test_single_endpoint",
        model_name="test_model",
        id=1,
        role="both",
        parallel_config=ParallelConfig(dp_size=2, tp_size=1, pp_size=1),
        enable_multi_endpoints=False,
    )

    # Add multiple endpoints
    endpoints = {
        0: Endpoint(id=0, ip="127.0.0.1", business_port="8000", mgmt_port="9000"),
        1: Endpoint(id=1, ip="127.0.0.1", business_port="8001", mgmt_port="9001"),
        2: Endpoint(id=2, ip="127.0.0.1", business_port="8002", mgmt_port="9002"),
    }
    instance1.add_endpoints("127.0.0.1", endpoints)

    all_eps = instance1.get_all_endpoints()
    assert len(all_eps) == 1  # Only 1 endpoint
    assert all_eps[0].id == 0  # Only endpoint with id=0

    # Test case 2: Multi-endpoint enabled - should return all endpoints
    instance2 = Instance(
        job_name="test_all_endpoints",
        model_name="test_model",
        id=2,
        role="both",
        parallel_config=ParallelConfig(dp_size=3, tp_size=1, pp_size=1),
        enable_multi_endpoints=True,
    )

    instance2.add_endpoints("127.0.0.1", endpoints)
    all_eps2 = instance2.get_all_endpoints()
    assert len(all_eps2) == 3  # All 3 endpoints
    assert {ep.id for ep in all_eps2} == {0, 1, 2}

    # Test case 3: Multiple pods with multi-endpoint disabled
    instance3 = Instance(
        job_name="test_multi_pods",
        model_name="test_model",
        id=3,
        role="both",
        parallel_config=ParallelConfig(dp_size=2, tp_size=1, pp_size=1),
        enable_multi_endpoints=False,
    )

    # Add endpoints from multiple pods
    endpoints_pod1 = {
        0: Endpoint(id=0, ip="127.0.0.1", business_port="8000", mgmt_port="9000"),
        1: Endpoint(id=1, ip="127.0.0.1", business_port="8001", mgmt_port="9001"),
    }
    endpoints_pod2 = {
        2: Endpoint(id=2, ip="127.0.0.2", business_port="8002", mgmt_port="9002"),
        3: Endpoint(id=3, ip="127.0.0.2", business_port="8003", mgmt_port="9003"),
    }
    instance3.add_endpoints("127.0.0.1", endpoints_pod1)
    instance3.add_endpoints("127.0.0.2", endpoints_pod2)

    all_eps3 = instance3.get_all_endpoints()
    assert len(all_eps3) == 1  # Only 1 endpoint (id=0)
    assert all_eps3[0].id == 0


def test_assemble_instance_multi_endpoint_disabled(instance_assembler):
    """Test _assemble_instance when enable_multi_endpoints is False"""
    # Create instance with enable_multi_endpoints=False
    instance = Instance(
        job_name="test_assemble_multi_disabled",
        model_name="test_model",
        id=1,
        role="both",
        parallel_config=ParallelConfig(world_size=16),  # world_size=16
        enable_multi_endpoints=False,
    )

    # Add node managers with device_num
    instance.add_node_mgr("127.0.0.1", "8080", device_num=8)
    instance.add_node_mgr("127.0.0.2", "8081", device_num=8)

    # Add endpoints (only id=0 for each pod)
    endpoints1 = {0: Endpoint(id=0, ip="127.0.0.1", business_port="8000", mgmt_port="9000")}
    endpoints2 = {0: Endpoint(id=0, ip="127.0.0.2", business_port="8000", mgmt_port="9000")}
    instance.add_endpoints("127.0.0.1", endpoints1)
    instance.add_endpoints("127.0.0.2", endpoints2)

    # Create metadata
    metadata = AssembleInstanceMetadata(instance=instance, register_timestamp=time.time())

    # Mock _filter_abnormal_endpoints and InstanceManager
    with (
        patch.object(instance_assembler, '_filter_abnormal_endpoints'),
        patch('motor.controller.core.instance_assembler.InstanceManager') as mock_im_class,
    ):
        mock_im = MagicMock()
        mock_im_class.return_value = mock_im

        instance_assembler._assemble_instance(metadata)

        # Should be assembled because we have enough node managers
        assert metadata.register_status == RegisterStatus.ASSEMBLED
        mock_im.add_instance.assert_called_once_with(instance)


def test_assemble_instance_multi_endpoint_disabled_not_enough_nodes(instance_assembler):
    """Test _assemble_instance when enable_multi_endpoints is False but not enough nodes"""
    # Create instance with enable_multi_endpoints=False
    instance = Instance(
        job_name="test_assemble_not_enough",
        model_name="test_model",
        id=1,
        role="both",
        parallel_config=ParallelConfig(world_size=16),  # world_size=16
        enable_multi_endpoints=False,
    )

    # Add only 1 node manager (need 2 for world_size=16 with device_num=8)
    instance.add_node_mgr("127.0.0.1", "8080", device_num=8)

    # Add endpoint
    endpoints = {0: Endpoint(id=0, ip="127.0.0.1", business_port="8000", mgmt_port="9000")}
    instance.add_endpoints("127.0.0.1", endpoints)

    # Create metadata
    metadata = AssembleInstanceMetadata(instance=instance, register_timestamp=time.time())

    # Mock _filter_abnormal_endpoints
    with patch.object(instance_assembler, '_filter_abnormal_endpoints'):
        instance_assembler._assemble_instance(metadata)

        # Should NOT be assembled because not enough node managers
        assert metadata.register_status != RegisterStatus.ASSEMBLED


# ===== D2D Weight Transfer Tests =====


def _make_mock_readonly_instance(
    job_name: str,
    role: str,
    ips: list[str],
    *,
    endpoint_id: int = 0,
    endpoint_ids: list[int] | None = None,
):
    """Create a ReadOnlyInstance wrapping a real Instance (required by upstream merge)."""
    inst = Instance(
        job_name=job_name,
        model_name="test",
        id=abs(hash(job_name)) % 1_000_000,
        role=role,
        parallel_config=ParallelConfig(),
    )
    for idx, ip in enumerate(ips):
        ep_id = endpoint_ids[idx] if endpoint_ids is not None else endpoint_id
        inst.add_endpoints(
            f"pod-{job_name}-{idx}",
            {0: Endpoint(id=ep_id, ip=ip, business_port="8000", mgmt_port="9000")},
        )
    return ReadOnlyInstance(inst)


def _make_mock_peer_instance_cross_node(job_name: str, role: str, ip_by_rank: dict[int, str]):
    """Peer instance with one pod per DP rank (cross-node DP topology)."""
    inst = Instance(
        job_name=job_name,
        model_name="test",
        id=abs(hash(job_name)) % 1_000_000,
        role=role,
        parallel_config=ParallelConfig(),
    )
    for dp_rank, ip in ip_by_rank.items():
        inst.add_endpoints(
            f"pod-{job_name}-{dp_rank}",
            {0: Endpoint(id=dp_rank, ip=ip, business_port="8000", mgmt_port="9000")},
        )
    return ReadOnlyInstance(inst)


def test_collect_d2d_peer_ips_queries_active_only(instance_assembler, test_config):
    """_collect_d2d_peer_ips only queries ACTIVE instances from InstanceManager."""
    instance = Instance(
        job_name="current_job",
        model_name="test",
        id=99,
        role=test_config['role'],
        parallel_config=test_config['parallel_config'],
    )
    metadata = AssembleInstanceMetadata(instance=instance)

    with patch.object(InstanceManager(), 'get_instances', return_value=[]) as mock_get:
        instance_assembler._collect_d2d_peer_ips(
            metadata, [Endpoint(id=0, ip="127.0.0.1", business_port="8000", mgmt_port="9000")]
        )
        mock_get.assert_called_once_with({InsStatus.ACTIVE})


def test_collect_d2d_peer_ips_matches_dp_rank(instance_assembler, test_config):
    """_collect_d2d_peer_ips collects only peer endpoints with matching id."""
    instance = Instance(
        job_name="current_job",
        model_name="test",
        id=99,
        role=test_config['role'],
        parallel_config=test_config['parallel_config'],
    )
    metadata = AssembleInstanceMetadata(instance=instance)

    mock_peer = _make_mock_peer_instance_cross_node(
        "peer_job",
        test_config['role'],
        {0: "10.0.0.1", 1: "10.0.0.2", 2: "10.0.0.3", 3: "10.0.0.4"},
    )
    ep0 = Endpoint(id=0, ip="127.0.0.1", business_port="8000", mgmt_port="9000")
    ep2 = Endpoint(id=2, ip="127.0.0.1", business_port="8000", mgmt_port="9000")
    ep5 = Endpoint(id=5, ip="127.0.0.1", business_port="8000", mgmt_port="9000")

    with patch.object(InstanceManager(), 'get_instances', return_value=[mock_peer]):
        assert instance_assembler._collect_d2d_peer_ips(metadata, [ep0]) == ["0:10.0.0.1"]
        assert instance_assembler._collect_d2d_peer_ips(metadata, [ep2]) == ["2:10.0.0.3"]
        assert instance_assembler._collect_d2d_peer_ips(metadata, [ep5]) is None


def test_collect_d2d_peer_ips_active_same_role(instance_assembler, test_config):
    """_collect_d2d_peer_ips collects IPs from same-role ACTIVE peer instances."""
    instance = Instance(
        job_name="current_job",
        model_name="test",
        id=99,
        role=test_config['role'],
        parallel_config=test_config['parallel_config'],
    )
    metadata = AssembleInstanceMetadata(instance=instance)

    mock_peer = _make_mock_readonly_instance("peer_job", test_config['role'], ["10.0.0.1", "10.0.0.2"])
    ep0 = Endpoint(id=0, ip="127.0.0.1", business_port="8000", mgmt_port="9000")

    with patch.object(InstanceManager(), 'get_instances', return_value=[mock_peer]):
        result = instance_assembler._collect_d2d_peer_ips(metadata, [ep0])
        assert set(result) == {"0:10.0.0.1", "0:10.0.0.2"}


def test_collect_d2d_peer_ips_excludes_own_job_name(instance_assembler, test_config):
    """_collect_d2d_peer_ips excludes instances with the same job_name (self)."""
    instance = Instance(
        job_name="my_job",
        model_name="test",
        id=99,
        role=test_config['role'],
        parallel_config=test_config['parallel_config'],
    )
    metadata = AssembleInstanceMetadata(instance=instance)

    mock_self = _make_mock_readonly_instance("my_job", test_config['role'], ["10.0.0.1"])
    mock_peer = _make_mock_readonly_instance("other_job", test_config['role'], ["10.0.0.2"])
    ep0 = Endpoint(id=0, ip="127.0.0.1", business_port="8000", mgmt_port="9000")

    with patch.object(InstanceManager(), 'get_instances', return_value=[mock_self, mock_peer]):
        result = instance_assembler._collect_d2d_peer_ips(metadata, [ep0])
        assert result == ["0:10.0.0.2"]


def test_collect_d2d_peer_ips_excludes_different_role(instance_assembler, test_config):
    """_collect_d2d_peer_ips excludes instances with a different role."""
    instance = Instance(
        job_name="current_job",
        model_name="test",
        id=99,
        role="prefill",
        parallel_config=test_config['parallel_config'],
    )
    metadata = AssembleInstanceMetadata(instance=instance)

    mock_same = _make_mock_readonly_instance("peer_prefill", "prefill", ["10.0.0.1"])
    mock_diff = _make_mock_readonly_instance("peer_decode", "decode", ["10.0.0.2"])
    ep0 = Endpoint(id=0, ip="127.0.0.1", business_port="8000", mgmt_port="9000")

    with patch.object(InstanceManager(), 'get_instances', return_value=[mock_same, mock_diff]):
        result = instance_assembler._collect_d2d_peer_ips(metadata, [ep0])
        assert result == ["0:10.0.0.1"]


def test_collect_d2d_peer_ips_deduplicates(instance_assembler, test_config):
    """_collect_d2d_peer_ips deduplicates IPs across multiple peer instances."""
    instance = Instance(
        job_name="current_job",
        model_name="test",
        id=99,
        role=test_config['role'],
        parallel_config=test_config['parallel_config'],
    )
    metadata = AssembleInstanceMetadata(instance=instance)

    mock_peer1 = _make_mock_readonly_instance("peer1", test_config['role'], ["10.0.0.1", "10.0.0.2"])
    mock_peer2 = _make_mock_readonly_instance("peer2", test_config['role'], ["10.0.0.2", "10.0.0.3"])
    ep0 = Endpoint(id=0, ip="127.0.0.1", business_port="8000", mgmt_port="9000")

    with patch.object(InstanceManager(), 'get_instances', return_value=[mock_peer1, mock_peer2]):
        result = instance_assembler._collect_d2d_peer_ips(metadata, [ep0])
        assert set(result) == {"0:10.0.0.1", "0:10.0.0.2", "0:10.0.0.3"}


def test_collect_d2d_peer_ips_no_peers(instance_assembler, test_config):
    """_collect_d2d_peer_ips returns None when no peer instances exist."""
    instance = Instance(
        job_name="current_job",
        model_name="test",
        id=99,
        role=test_config['role'],
        parallel_config=test_config['parallel_config'],
    )
    metadata = AssembleInstanceMetadata(instance=instance)
    ep0 = Endpoint(id=0, ip="127.0.0.1", business_port="8000", mgmt_port="9000")

    with patch.object(InstanceManager(), 'get_instances', return_value=[]):
        result = instance_assembler._collect_d2d_peer_ips(metadata, [ep0])
        assert result is None


def test_send_start_command_with_d2d_enabled(instance_assembler, test_config):
    """_send_start_command includes rank-aligned d2d_peer_ips when D2D is enabled."""
    instance = Instance(
        job_name="d2d_job",
        model_name="test",
        id=99,
        role=test_config['role'],
        parallel_config=test_config['parallel_config'],
    )
    instance.add_node_mgr("127.0.0.1", "8088")
    instance.add_node_mgr("127.0.0.2", "8088")
    reg_msg = create_register_msg("d2d_job", "127.0.0.1", test_config)
    pod_endpoints = instance_assembler._build_single_endpoint(reg_msg, 0)
    instance.add_endpoints("127.0.0.1", pod_endpoints)
    reg_msg2 = create_register_msg("d2d_job", "127.0.0.2", test_config)
    pod_endpoints2 = instance_assembler._build_single_endpoint(reg_msg2, 1)
    instance.add_endpoints("127.0.0.2", pod_endpoints2)

    metadata = AssembleInstanceMetadata(instance=instance)

    mock_peer = _make_mock_peer_instance_cross_node(
        "peer_job",
        test_config['role'],
        {0: "10.0.0.1", 1: "10.0.0.2"},
    )

    with (
        patch.object(instance_assembler, '_is_d2d_enabled_for_role', return_value=True),
        patch.object(InstanceManager(), 'get_instances', return_value=[mock_peer]),
        patch(
            'motor.controller.api_client.node_manager_api_client.NodeManagerApiClient.send_start_command'
        ) as mock_send,
    ):
        mock_send.return_value = True

        result = instance_assembler._send_start_command(metadata)

        assert result is True
        assert mock_send.call_count == 2
        first_msg = mock_send.call_args_list[0][0][1]
        second_msg = mock_send.call_args_list[1][0][1]
        assert first_msg.d2d_peer_ips == ["0:10.0.0.1"]
        assert second_msg.d2d_peer_ips == ["1:10.0.0.2"]


def test_send_start_command_with_d2d_disabled(instance_assembler, test_config):
    """_send_start_command does not populate d2d_peer_ips when D2D is disabled."""
    instance = Instance(
        job_name="d2d_job",
        model_name="test",
        id=99,
        role=test_config['role'],
        parallel_config=test_config['parallel_config'],
    )
    instance.add_node_mgr("127.0.0.1", "8088")
    reg_msg = create_register_msg("d2d_job", "127.0.0.1", test_config)
    pod_endpoints = instance_assembler._build_single_endpoint(reg_msg, 0)
    instance.add_endpoints("127.0.0.1", pod_endpoints)

    metadata = AssembleInstanceMetadata(instance=instance)

    mock_peer = _make_mock_readonly_instance("peer_job", test_config['role'], ["10.0.0.1"])

    with (
        patch.object(instance_assembler, '_is_d2d_enabled_for_role', return_value=False),
        patch.object(InstanceManager(), 'get_instances', return_value=[mock_peer]),
        patch(
            'motor.controller.api_client.node_manager_api_client.NodeManagerApiClient.send_start_command'
        ) as mock_send,
    ):
        mock_send.return_value = True

        instance_assembler._send_start_command(metadata)

        called_msg = mock_send.call_args[0][1]
        assert called_msg.d2d_peer_ips is None


def test_send_start_command_with_d2d_enabled_no_peers(instance_assembler, test_config):
    """_send_start_command omits d2d_peer_ips when D2D is enabled but no peers found."""
    instance = Instance(
        job_name="d2d_job",
        model_name="test",
        id=99,
        role=test_config['role'],
        parallel_config=test_config['parallel_config'],
    )
    instance.add_node_mgr("127.0.0.1", "8088")
    reg_msg = create_register_msg("d2d_job", "127.0.0.1", test_config)
    pod_endpoints = instance_assembler._build_single_endpoint(reg_msg, 0)
    instance.add_endpoints("127.0.0.1", pod_endpoints)

    metadata = AssembleInstanceMetadata(instance=instance)

    with (
        patch.object(instance_assembler, '_is_d2d_enabled_for_role', return_value=True),
        patch.object(InstanceManager(), 'get_instances', return_value=[]),
        patch(
            'motor.controller.api_client.node_manager_api_client.NodeManagerApiClient.send_start_command'
        ) as mock_send,
    ):
        mock_send.return_value = True

        instance_assembler._send_start_command(metadata)

        called_msg = mock_send.call_args[0][1]
        assert called_msg.d2d_peer_ips is None


def test_collect_d2d_peer_ips_includes_headless(instance_assembler, test_config):
    """_collect_d2d_peer_ips includes headless peer endpoints for CP cross-node."""
    instance = Instance(
        job_name="current_job",
        model_name="test",
        id=99,
        role=test_config['role'],
        parallel_config=test_config['parallel_config'],
        enable_multi_endpoints=True,
    )
    metadata = AssembleInstanceMetadata(instance=instance)

    # Peer with both master (id=0) and slave (id=1, headless) endpoints
    mock_peer = Instance(
        job_name="peer_job",
        model_name="test",
        id=88,
        role=test_config['role'],
        parallel_config=ParallelConfig(),
        enable_multi_endpoints=True,
    )
    mock_peer.add_endpoints(
        "10.0.0.1",
        {0: Endpoint(id=0, ip="10.0.0.1", business_port="8000", mgmt_port="9000")},
    )
    mock_peer.add_endpoints(
        "10.0.0.2",
        {0: Endpoint(id=1, ip="10.0.0.2", business_port="8000", mgmt_port="9000", headless=True)},
    )
    ro_peer = ReadOnlyInstance(mock_peer)

    ep0 = Endpoint(id=0, ip="127.0.0.1", business_port="8000", mgmt_port="9000")
    ep1 = Endpoint(id=1, ip="127.0.0.1", business_port="8000", mgmt_port="9000")

    with patch.object(InstanceManager(), 'get_instances', return_value=[ro_peer]):
        # Master endpoint (id=0) matches peer's non-headless endpoint
        assert instance_assembler._collect_d2d_peer_ips(metadata, [ep0]) == ["0:10.0.0.1"]
        # Slave endpoint (id=1) matches peer's headless endpoint
        assert instance_assembler._collect_d2d_peer_ips(metadata, [ep1]) == ["1:10.0.0.2"]


def test_cross_node_pcp_assembly_waits_for_all_nodes(instance_assembler):
    """Test that nnodes > 1 waits for all nodes before assembling"""
    instance = Instance(
        job_name="test_pcp_wait",
        model_name="test_model",
        id=1,
        role="prefill",
        parallel_config=ParallelConfig(dp_size=1, tp_size=4),
        enable_multi_endpoints=True,
    )

    # Add only 1 node manager (nnodes=2, need 2)
    instance.add_node_mgr("127.0.0.1", "8080", device_num=8)
    endpoints = {0: Endpoint(id=0, ip="127.0.0.1", business_port="8000", mgmt_port="9000")}
    instance.add_endpoints("127.0.0.1", endpoints)

    metadata = AssembleInstanceMetadata(
        instance=instance,
        register_timestamp=time.time(),
        nnodes=2,
    )

    with patch.object(instance_assembler, '_filter_abnormal_endpoints'):
        instance_assembler._assemble_instance(metadata)

        # Should NOT be assembled because only 1/2 nodes registered
        assert metadata.register_status != RegisterStatus.ASSEMBLED

    # Add second node manager
    instance.add_node_mgr("127.0.0.2", "8080", device_num=8)
    endpoints2 = {0: Endpoint(id=1, ip="127.0.0.2", business_port="8000", mgmt_port="9000")}
    instance.add_endpoints("127.0.0.2", endpoints2)

    with patch.object(instance_assembler, '_filter_abnormal_endpoints'):
        instance_assembler._assemble_instance(metadata)

        # Should be assembled now with 2/2 nodes
        assert metadata.register_status == RegisterStatus.ASSEMBLED


def test_nnodes_default_backward_compatible(instance_assembler):
    """Test that nnodes=1 (default) uses existing is_endpoints_enough logic"""
    instance = Instance(
        job_name="test_nnodes_default",
        model_name="test_model",
        id=1,
        role="prefill",
        parallel_config=ParallelConfig(dp_size=2, tp_size=4),
        enable_multi_endpoints=True,
    )

    # Add 1 node manager with 1 endpoint
    instance.add_node_mgr("127.0.0.1", "8080", device_num=8)
    endpoints = {0: Endpoint(id=0, ip="127.0.0.1", business_port="8000", mgmt_port="9000")}
    instance.add_endpoints("127.0.0.1", endpoints)

    # nnodes=1 (default) — should use is_endpoints_enough()
    # With dp_size=2 and only 1 endpoint, is_endpoints_enough returns False
    metadata = AssembleInstanceMetadata(
        instance=instance,
        register_timestamp=time.time(),
        nnodes=1,
    )

    with patch.object(instance_assembler, '_filter_abnormal_endpoints'):
        instance_assembler._assemble_instance(metadata)
        # Should NOT be assembled: dp_size=2, only 1 endpoint
        assert metadata.register_status != RegisterStatus.ASSEMBLED


def test_cross_node_pcp_assembly_extra_nodes(instance_assembler):
    """Test that node_managers > nnodes still assembles (tolerant)"""
    instance = Instance(
        job_name="test_pcp_extra",
        model_name="test_model",
        id=1,
        role="prefill",
        parallel_config=ParallelConfig(dp_size=1, tp_size=4),
        enable_multi_endpoints=True,
    )

    # Add 3 node managers when nnodes=2
    for ip_suffix in ["1", "2", "3"]:
        instance.add_node_mgr(f"127.0.0.{ip_suffix}", "8080", device_num=8)
        endpoints = {
            0: Endpoint(id=int(ip_suffix) - 1, ip=f"127.0.0.{ip_suffix}", business_port="8000", mgmt_port="9000")
        }
        instance.add_endpoints(f"127.0.0.{ip_suffix}", endpoints)

    metadata = AssembleInstanceMetadata(
        instance=instance,
        register_timestamp=time.time(),
        nnodes=2,
    )

    with patch.object(instance_assembler, '_filter_abnormal_endpoints'):
        instance_assembler._assemble_instance(metadata)
        # Should be assembled: 3 >= 2
        assert metadata.register_status == RegisterStatus.ASSEMBLED


def test_cross_node_pcp_with_dp_waits_for_all_groups(instance_assembler):
    """DP=4, PCP nnodes=2: needs dp*nnodes=8 nodes, not just 2."""
    instance = Instance(
        job_name="test_pcp_dp_combo",
        model_name="test_model",
        id=1,
        role="prefill",
        parallel_config=ParallelConfig(dp_size=4, tp_size=16, pcp_size=2),
        enable_multi_endpoints=True,
    )

    # Add 7 node managers (1 short of 8)
    for i in range(7):
        instance.add_node_mgr(f"10.0.0.{i + 1}", "8080", device_num=16)
        instance.add_endpoints(
            f"10.0.0.{i + 1}",
            {0: Endpoint(id=i, ip=f"10.0.0.{i + 1}", business_port="8000", mgmt_port="9000")},
        )

    metadata = AssembleInstanceMetadata(instance=instance, nnodes=2)

    with patch.object(instance_assembler, "_filter_abnormal_endpoints"):
        instance_assembler._assemble_instance(metadata)
        # 7 < dp(4)*nnodes(2)=8 → not ready
        assert metadata.register_status != RegisterStatus.ASSEMBLED

    # Add the 8th node
    instance.add_node_mgr("10.0.0.8", "8080", device_num=16)
    instance.add_endpoints("10.0.0.8", {0: Endpoint(id=7, ip="10.0.0.8", business_port="8000", mgmt_port="9000")})

    with patch.object(instance_assembler, "_filter_abnormal_endpoints"):
        instance_assembler._assemble_instance(metadata)
        # 8 >= 8 → ready
        assert metadata.register_status == RegisterStatus.ASSEMBLED


def test_send_start_command_assigns_node_rank(instance_assembler):
    """Test that _send_start_command assigns node_rank by registration order"""
    instance = Instance(
        job_name="test_node_rank",
        model_name="test_model",
        id=1,
        role="prefill",
        parallel_config=ParallelConfig(dp_size=1, tp_size=4),
        enable_multi_endpoints=True,
    )

    # Add 3 node managers in registration order
    instance.add_node_mgr("10.0.0.2", "8080", device_num=8)
    instance.add_node_mgr("10.0.0.1", "8080", device_num=8)
    instance.add_node_mgr("10.0.0.3", "8080", device_num=8)

    # Add endpoints for each
    instance.add_endpoints("10.0.0.2", {0: Endpoint(id=0, ip="10.0.0.2", business_port="8000", mgmt_port="9000")})
    instance.add_endpoints("10.0.0.1", {0: Endpoint(id=0, ip="10.0.0.1", business_port="8000", mgmt_port="9000")})
    instance.add_endpoints("10.0.0.3", {0: Endpoint(id=0, ip="10.0.0.3", business_port="8000", mgmt_port="9000")})

    metadata = AssembleInstanceMetadata(instance=instance, nnodes=3)

    sent_msgs = []

    def capture_call(node_mgr, start_cmd_msg):
        sent_msgs.append((node_mgr.pod_ip, start_cmd_msg.node_rank))
        return True

    with patch(
        'motor.controller.api_client.node_manager_api_client.NodeManagerApiClient.send_start_command',
        side_effect=capture_call,
    ):
        instance_assembler._send_start_command(metadata)

    # Verify 3 calls
    assert len(sent_msgs) == 3

    # Registration order: 10.0.0.2→rank 0 (first), 10.0.0.1→rank 1, 10.0.0.3→rank 2
    assert sent_msgs[0] == ("10.0.0.2", 0)
    assert sent_msgs[1] == ("10.0.0.1", 1)
    assert sent_msgs[2] == ("10.0.0.3", 2)


def test_send_start_command_node_rank_modulo_for_dp_pcp(instance_assembler):
    """DP=2, nnodes=2, 4 nodes: node_rank = registration_index % nnodes (0,1,0,1)."""
    instance = Instance(
        job_name="test_node_rank_mod",
        model_name="test_model",
        id=1,
        role="prefill",
        parallel_config=ParallelConfig(dp_size=2, tp_size=4, pcp_size=2),
        enable_multi_endpoints=True,
    )

    for i in range(4):
        instance.add_node_mgr(f"10.0.0.{i + 1}", "8080", device_num=8)
        instance.add_endpoints(
            f"10.0.0.{i + 1}",
            {0: Endpoint(id=i, ip=f"10.0.0.{i + 1}", business_port="8000", mgmt_port="9000")},
        )

    metadata = AssembleInstanceMetadata(instance=instance, nnodes=2)

    sent_ranks = {}

    def capture_call(node_mgr, start_cmd_msg):
        sent_ranks[node_mgr.pod_ip] = start_cmd_msg.node_rank
        return True

    with patch(
        "motor.controller.api_client.node_manager_api_client.NodeManagerApiClient.send_start_command",
        side_effect=capture_call,
    ):
        instance_assembler._send_start_command(metadata)

    # node_rank = registration_index % 2 → 0,1,0,1
    assert sent_ranks["10.0.0.1"] == 0
    assert sent_ranks["10.0.0.2"] == 1
    assert sent_ranks["10.0.0.3"] == 0
    assert sent_ranks["10.0.0.4"] == 1


def test_register_msg_nnodes_stored_in_metadata(instance_assembler, test_config):
    """Test that nnodes from RegisterMsg is stored in AssembleInstanceMetadata"""
    job_name = "test_nnodes_stored"
    msg = create_register_msg(
        job_name,
        test_config['pod_ip1'],
        test_config,
        nnodes=3,
    )
    result = instance_assembler.register(msg)
    assert result == 0
    metadata = instance_assembler.instances[job_name]
    assert metadata.nnodes == 3, f"Expected nnodes=3, got {metadata.nnodes}"


def test_register_msg_nnodes_default(instance_assembler, test_config):
    """Test that nnodes defaults to 1 when not specified"""
    job_name = "test_nnodes_default_reg"
    msg = create_register_msg(job_name, test_config['pod_ip1'], test_config)
    result = instance_assembler.register(msg)
    assert result == 0
    metadata = instance_assembler.instances[job_name]
    assert metadata.nnodes == 1, f"Expected default nnodes=1, got {metadata.nnodes}"


def test_reregister_preserves_nnodes(instance_assembler, test_config):
    """Test that reregister NOT_REGISTERED path preserves nnodes from ReregisterMsg."""
    job_name = "test_reregister_nnodes"
    config = test_config.copy()

    # Simulate first registration
    register_msg = create_register_msg(job_name, config['pod_ip1'], config, nnodes=2)
    result = instance_assembler.register(register_msg)
    assert result == 0
    assert instance_assembler.instances[job_name].nnodes == 2

    # Clear instances to simulate Controller restart
    instance_assembler.instances.clear()

    # Reregister with nnodes
    endpoint = Endpoint(id=0, ip=config['pod_ip1'], business_port="8000", mgmt_port="9000")
    reregister_msg = ReregisterMsg(
        job_name=job_name,
        model_name="test_model",
        instance_id=1,
        role=config['role'],
        pod_ip=config['pod_ip1'],
        nm_port="8088",
        parallel_config=config['parallel_config'],
        endpoints=[endpoint],
        enable_multi_endpoints=True,
        nnodes=2,
    )
    result = instance_assembler.reregister(reregister_msg)
    assert result == 0
    assert instance_assembler.instances[job_name].nnodes == 2


# ===== Headless Endpoint Marking Tests (Cross-Node PCP) =====


def test_cross_node_pcp_marks_slave_endpoints_headless(instance_assembler):
    """When nnodes > 1, slave node endpoints (node_rank > 0) are marked headless."""
    instance = Instance(
        job_name="test_headless_marking",
        model_name="test_model",
        id=1,
        role="prefill",
        parallel_config=ParallelConfig(dp_size=1, tp_size=4),
        enable_multi_endpoints=True,
    )

    # First registered = master (10.0.0.10), second = slave (10.0.0.2)
    instance.add_node_mgr("10.0.0.10", "8080", device_num=8)
    instance.add_node_mgr("10.0.0.2", "8080", device_num=8)

    # Add endpoints for both
    instance.add_endpoints("10.0.0.10", {0: Endpoint(id=0, ip="10.0.0.10", business_port="8000", mgmt_port="9000")})
    instance.add_endpoints("10.0.0.2", {0: Endpoint(id=0, ip="10.0.0.2", business_port="8000", mgmt_port="9000")})

    metadata = AssembleInstanceMetadata(instance=instance, nnodes=2)

    with patch.object(instance_assembler, "_filter_abnormal_endpoints"):
        instance_assembler._assemble_instance(metadata)

    assert metadata.register_status == RegisterStatus.ASSEMBLED

    # First registered (10.0.0.10) = master, should NOT be headless
    eps_master = instance.get_endpoints("10.0.0.10")
    for ep in eps_master.values():
        assert ep.headless is False, f"Master endpoint {ep.ip} should not be headless"

    # Second registered (10.0.0.2) = slave, should be headless
    eps_slave = instance.get_endpoints("10.0.0.2")
    for ep in eps_slave.values():
        assert ep.headless is True, f"Slave endpoint {ep.ip} should be headless"

    # get_all_endpoints should only return the master
    all_eps = instance.get_all_endpoints()
    assert len(all_eps) == 1
    assert all_eps[0].ip == "10.0.0.10"


def test_cross_node_pcp_reregister_preserves_headless(instance_assembler):
    """Re-registration uses node_rank from ReregisterMsg, not registration order."""
    # Simulate slave re-registering first after Controller restart
    slave_endpoint = Endpoint(id=0, ip="10.0.0.200", business_port="8000", mgmt_port="9000")
    master_endpoint = Endpoint(id=1, ip="10.0.0.1", business_port="8000", mgmt_port="9000")

    # Slave (node_rank=1) re-registers first
    slave_msg = ReregisterMsg(
        job_name="test_reregister_headless",
        model_name="test_model",
        instance_id=1,
        role="prefill",
        pod_ip="10.0.0.200",
        nm_port="8088",
        parallel_config=ParallelConfig(dp_size=1, tp_size=4),
        endpoints=[slave_endpoint],
        nnodes=2,
        node_rank=1,
    )
    instance_assembler.reregister(slave_msg)

    # Master (node_rank=0) re-registers second
    master_msg = ReregisterMsg(
        job_name="test_reregister_headless",
        model_name="test_model",
        instance_id=1,
        role="prefill",
        pod_ip="10.0.0.1",
        nm_port="8088",
        parallel_config=ParallelConfig(dp_size=1, tp_size=4),
        endpoints=[master_endpoint],
        nnodes=2,
        node_rank=0,
    )
    instance_assembler.reregister(master_msg)

    metadata = instance_assembler.instances["test_reregister_headless"]
    assert metadata.nnodes == 2
    assert metadata.is_reregister is True

    # Slave endpoint should be headless (node_rank=1), master should not
    eps_slave = metadata.instance.get_endpoints("10.0.0.200")
    for ep in eps_slave.values():
        assert ep.headless is True

    eps_master = metadata.instance.get_endpoints("10.0.0.1")
    for ep in eps_master.values():
        assert ep.headless is False


def test_cross_node_pcp_no_headless_when_nnodes_is_one(instance_assembler):
    """When nnodes=1, no endpoints are marked headless."""
    instance = Instance(
        job_name="test_no_headless_nnodes1",
        model_name="test_model",
        id=1,
        role="prefill",
        parallel_config=ParallelConfig(dp_size=2, tp_size=4),
        enable_multi_endpoints=True,
    )

    instance.add_node_mgr("10.0.0.1", "8080", device_num=8)
    instance.add_node_mgr("10.0.0.2", "8080", device_num=8)
    instance.add_endpoints("10.0.0.1", {0: Endpoint(id=0, ip="10.0.0.1", business_port="8000", mgmt_port="9000")})
    instance.add_endpoints("10.0.0.2", {0: Endpoint(id=1, ip="10.0.0.2", business_port="8000", mgmt_port="9000")})

    metadata = AssembleInstanceMetadata(instance=instance, nnodes=1)

    with patch.object(instance_assembler, "_filter_abnormal_endpoints"):
        instance_assembler._assemble_instance(metadata)

    assert metadata.register_status == RegisterStatus.ASSEMBLED

    # No endpoints should be headless
    for ep in instance.get_all_endpoints():
        assert ep.headless is False


def test_register_records_snapshot_dp_master_ip_when_is_master(instance_assembler, test_config):
    """Register with is_master=True records snapshot_dp_master_ip on instance metadata."""
    job_name = "test_snapshot_master_register"
    slave_msg = create_register_msg(job_name, "10.0.0.2", test_config, is_master=False)
    master_msg = create_register_msg(job_name, "10.0.0.1", test_config, is_master=True)

    assert instance_assembler.register(slave_msg) == 0
    assert instance_assembler.register(master_msg) == 0

    metadata = instance_assembler.instances[job_name]
    assert metadata.snapshot_dp_master_ip == "10.0.0.1"


def test_send_start_command_uses_snapshot_dp_master_ip(instance_assembler, test_config):
    """Start command uses snapshot_dp_master_ip instead of first registered node."""
    job_name = "test_snapshot_master_start"
    metadata = create_assembled_instance(instance_assembler, job_name, test_config)
    metadata.snapshot_dp_master_ip = "10.0.0.99"

    with patch(
        "motor.controller.api_client.node_manager_api_client.NodeManagerApiClient.send_start_command"
    ) as mock_send:
        mock_send.return_value = True
        assert instance_assembler._send_start_command(metadata) is True

        master_ips = {call.args[1].master_dp_ip for call in mock_send.call_args_list}
        assert master_ips == {"10.0.0.99"}


# ===== Register/Reregister Isolation Tests =====


def test_register_rejected_when_reregister_assembling(instance_assembler, test_config):
    """register should be rejected when an ASSEMBLING instance was created by reregister.

    Scenario: one pod already called reregister (creating an ASSEMBLING instance with
    is_reregister=True), then another pod calls register. The register should be
    rejected because register and reregister pods must not be assembled together.
    """
    job_name = "test_register_rejected"

    # First, create an ASSEMBLING instance via reregister
    reg_msg = create_register_msg(job_name, test_config['pod_ip1'], test_config)
    endpoints = instance_assembler._build_multi_endpoints(reg_msg, 0)
    rereg_msg = create_reregister_msg(
        job_name, test_config['pod_ip1'], instance_id=10, config=test_config, endpoints=endpoints
    )
    result = instance_assembler.reregister(rereg_msg)
    assert result == 0
    assert job_name in instance_assembler.instances
    assert instance_assembler.instances[job_name].is_reregister is True

    # Now try to register a second pod — should be rejected
    msg = create_register_msg(
        job_name,
        test_config['pod_ip2'],
        test_config,
        ranktable=build_pod_ranktable(
            pod_ip=test_config['pod_ip2'], pod_device_num=2 * test_config['tp'], rank_offset=2 * test_config['tp']
        ),
    )
    result = instance_assembler.register(msg)
    assert result == -1

    # Verify the instance is still there with only one pod
    assert job_name in instance_assembler.instances
    metadata = instance_assembler.instances[job_name]
    assert metadata.is_reregister is True
    assert len(metadata.instance.endpoints) == 1  # Only the reregister pod


def test_reregister_rejected_when_register_assembling(instance_assembler, test_config):
    """reregister should be rejected when an ASSEMBLING instance was created by register.

    Scenario: one pod already called register (creating an ASSEMBLING instance with
    is_reregister=False), then another pod calls reregister (e.g. because it got 503
    and thinks Controller restarted). The reregister should be rejected because
    register and reregister pods must not be assembled together.
    """
    job_name = "test_reregister_rejected"

    # First, create an ASSEMBLING instance via register
    msg = create_register_msg(job_name, test_config['pod_ip1'], test_config)
    result = instance_assembler.register(msg)
    assert result == 0
    assert job_name in instance_assembler.instances
    assert instance_assembler.instances[job_name].is_reregister is False

    # Now try to reregister a second pod — should be rejected
    reg_msg2 = create_register_msg(
        job_name,
        test_config['pod_ip2'],
        test_config,
        ranktable=build_pod_ranktable(
            pod_ip=test_config['pod_ip2'], pod_device_num=2 * test_config['tp'], rank_offset=2 * test_config['tp']
        ),
    )
    endpoints = instance_assembler._build_multi_endpoints(reg_msg2, 2)
    rereg_msg = create_reregister_msg(
        job_name, test_config['pod_ip2'], instance_id=0, config=test_config, endpoints=endpoints
    )
    result = instance_assembler.reregister(rereg_msg)
    assert result == -1

    # Verify the instance is still there with only one pod
    assert job_name in instance_assembler.instances
    metadata = instance_assembler.instances[job_name]
    assert metadata.is_reregister is False
    assert len(metadata.instance.endpoints) == 1  # Only the register pod


def test_register_rejected_when_reregister_assembling_new_instance(instance_assembler, test_config):
    """register creates a new instance ID when rejected by reregister assembly.

    When register is rejected due to an ongoing reregister assembly, the register
    pod should receive -1. When the reregister assembly completes or times out,
    a subsequent register for the same job_name should be able to create a fresh
    instance.
    """
    job_name = "test_register_after_reject"

    # Create an ASSEMBLING instance via reregister
    reg_msg = create_register_msg(job_name, test_config['pod_ip1'], test_config)
    endpoints = instance_assembler._build_multi_endpoints(reg_msg, 0)
    rereg_msg = create_reregister_msg(
        job_name, test_config['pod_ip1'], instance_id=10, config=test_config, endpoints=endpoints
    )
    result = instance_assembler.reregister(rereg_msg)
    assert result == 0

    # register from another pod should be rejected
    msg = create_register_msg(job_name, test_config['pod_ip2'], test_config)
    result = instance_assembler.register(msg)
    assert result == -1

    # Remove the reregister instance (simulating timeout/cleanup)
    del instance_assembler.instances[job_name]

    # Now register should succeed — fresh instance, new ID
    msg2 = create_register_msg(job_name, test_config['pod_ip1'], test_config)
    result = instance_assembler.register(msg2)
    assert result == 0
    assert job_name in instance_assembler.instances
    # ins_id_cnt was 10+1 from reregister, then incremented by this register
    assert instance_assembler.instances[job_name].is_reregister is False


# ===== Controller Restart Recovery Tests =====


def test_restore_data_sets_is_reregister_true(instance_assembler, test_config):
    """After Controller restart, restore_data marks all instances as is_reregister=True.

    Before restart, the instance was created by register (is_reregister=False).
    After restart, all pods will call reregister to rejoin, so restored instances
    must have is_reregister=True to avoid being rejected by the reregister check.
    """
    job_name = "test_restore_sets_reregister"

    # Create an instance via register (is_reregister=False)
    metadata = create_assembled_instance(instance_assembler, job_name, test_config)
    assert metadata.is_reregister is False

    # Enable persistence and mock restore
    instance_assembler.etcd_config.enable_etcd_persistence = True

    with patch.object(instance_assembler.etcd_client, 'restore_data') as mock_restore:
        metadata_data = metadata.model_dump(mode='json')
        assembler_data = {
            "ins_id_cnt": instance_assembler.ins_id_cnt,
            "instances": {job_name: metadata_data},
        }
        state = PersistentState(data=assembler_data, version=1, timestamp=time.time(), checksum="")
        state.checksum = state.calculate_checksum()
        mock_restore.return_value = {"state": state}

        # Simulate Controller restart: create new assembler, restore
        with patch('threading.Thread'), patch('motor.controller.core.instance_assembler.EtcdClient'):
            new_config = ControllerConfig()
            new_config.etcd_config.enable_etcd_persistence = True
            new_config.instance_config.instance_assemble_timeout = 1.0
            new_config.instance_config.instance_assembler_check_interval = 0.1
            new_config.instance_config.instance_assembler_cmd_send_interval = 0.1
            new_config.instance_config.send_cmd_retry_times = 3
            new_assembler = InstanceAssembler(new_config)

            # Restore data — should mark is_reregister=True
            restore_result = new_assembler.restore_data()
            assert restore_result is True

            # Verify restored instance has is_reregister=True
            assert job_name in new_assembler.instances
            restored_metadata = new_assembler.instances[job_name]
            assert restored_metadata.is_reregister is True


def test_reregister_succeeds_after_controller_restart(instance_assembler, test_config):
    """Controller restart: restore + reregister + assemble completes successfully.

    Full recovery path:
    1. Pre-restart: instance created by register, ASSEMBLING (1 pod, not enough)
    2. Controller restarts → restore_data → is_reregister=True
    3. Pod calls reregister → accepted (is_reregister matches)
    4. A second pod also calls reregister → instance completes assembly
    """
    job_name = "test_reregister_after_restart"

    # Step 1: Pre-restart — one pod registered via register, instance is ASSEMBLING
    msg = create_register_msg(job_name, test_config['pod_ip1'], test_config)
    result = instance_assembler.register(msg)
    assert result == 0
    assert instance_assembler.instances[job_name].is_reregister is False

    # Persist the ASSEMBLING instance (not yet assembled)
    metadata = instance_assembler.instances[job_name]
    instance_assembler.etcd_config.enable_etcd_persistence = True

    with patch.object(instance_assembler.etcd_client, 'restore_data') as mock_restore:
        metadata_data = metadata.model_dump(mode='json')
        assembler_data = {
            "ins_id_cnt": instance_assembler.ins_id_cnt,
            "instances": {job_name: metadata_data},
        }
        state = PersistentState(data=assembler_data, version=1, timestamp=time.time(), checksum="")
        state.checksum = state.calculate_checksum()
        mock_restore.return_value = {"state": state}

        # Step 2: Simulate Controller restart — new assembler, restore data
        with patch('threading.Thread'), patch('motor.controller.core.instance_assembler.EtcdClient'):
            new_config = ControllerConfig()
            new_config.etcd_config.enable_etcd_persistence = True
            new_config.instance_config.instance_assemble_timeout = 1.0
            new_config.instance_config.instance_assembler_check_interval = 0.1
            new_config.instance_config.instance_assembler_cmd_send_interval = 0.1
            new_config.instance_config.send_cmd_retry_times = 3
            new_assembler = InstanceAssembler(new_config)

            assert new_assembler.restore_data() is True
            assert new_assembler.instances[job_name].is_reregister is True

            # Step 3: Pod 1 calls reregister → should succeed (returns 0, not -1)
            reg_msg = create_register_msg(job_name, test_config['pod_ip1'], test_config)
            endpoints = new_assembler._build_multi_endpoints(reg_msg, 0)
            rereg_msg1 = create_reregister_msg(
                job_name,
                test_config['pod_ip1'],
                instance_id=1,
                config=test_config,
                endpoints=endpoints,
            )
            result1 = new_assembler.reregister(rereg_msg1)
            assert result1 == 0

            # Step 4: Pod 2 calls reregister → instance gets enough endpoints → assembly completes
            reg_msg2 = create_register_msg(
                job_name,
                test_config['pod_ip2'],
                test_config,
                ranktable=build_pod_ranktable(
                    pod_ip=test_config['pod_ip2'],
                    pod_device_num=2 * test_config['tp'],
                    rank_offset=2 * test_config['tp'],
                ),
            )
            endpoints2 = new_assembler._build_multi_endpoints(reg_msg2, 2)
            rereg_msg2 = create_reregister_msg(
                job_name,
                test_config['pod_ip2'],
                instance_id=1,
                config=test_config,
                endpoints=endpoints2,
            )
            result2 = new_assembler.reregister(rereg_msg2)
            assert result2 == 0

            # Assembly should complete (instance moved to InstanceManager)
            metadata_after = new_assembler.instances[job_name]
            with patch.object(new_assembler, '_filter_abnormal_endpoints'):
                new_assembler._assemble_instance(metadata_after)

            # For reregister, instance is popped from assembler after assembly
            assert job_name not in new_assembler.instances

            # Verify instance is in InstanceManager
            im = InstanceManager()
            assert im.has_instance_by_job_name(job_name)
