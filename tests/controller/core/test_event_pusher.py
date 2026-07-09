# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import pytest
import queue
from unittest.mock import Mock, patch, MagicMock

from motor.config.controller import ControllerConfig
from motor.controller.core.event_pusher import EventPusher, Event
from motor.common.resources.instance import Instance, ReadOnlyInstance
from motor.controller.core.observer import ObserverEvent
from motor.common.resources.http_msg_spec import EventType


@pytest.fixture
def event_pusher():
    """create EventPusher object fixture"""
    with patch('threading.Thread') as mock_thread_class:
        mock_thread = MagicMock()
        mock_thread_class.return_value = mock_thread

        # Create EventPusher instance (threads are created in __init__)
        config = ControllerConfig()
        return EventPusher(config)


@pytest.fixture
def mock_instance():
    """mock Instance fixture"""
    instance = Mock(spec=Instance)
    instance.job_name = "test_job"
    return instance


@pytest.fixture
def mock_http_client():
    """mock HTTP client fixture"""
    with patch('motor.controller.core.event_pusher.CoordinatorApiClient.send_instance_refresh') as mock_send_method:
        mock_send_method.return_value = True
        yield mock_send_method


def test_init(event_pusher):
    """init test case"""
    assert not event_pusher.is_coordinator_reset
    assert isinstance(event_pusher.event_queue, queue.Queue)
    assert event_pusher.instances == {}

    # check threads are None before start() is called
    assert event_pusher.event_consumer_thread is None
    assert event_pusher.heartbeat_detector_thread is None


def test_start():
    """test start method creates and starts threads"""
    with patch('threading.Thread') as mock_thread_class:
        mock_thread = MagicMock()
        mock_thread_class.return_value = mock_thread

        # Create EventPusher instance
        config = ControllerConfig()
        event_pusher = EventPusher(config)

        # Before start, threads should be None
        assert event_pusher.event_consumer_thread is None
        assert event_pusher.heartbeat_detector_thread is None

        # Call start
        event_pusher.start()

        # After start, threads should be created and started
        assert event_pusher.event_consumer_thread is not None
        assert event_pusher.heartbeat_detector_thread is not None
        assert event_pusher.event_consumer_thread.daemon
        assert event_pusher.heartbeat_detector_thread.daemon

        # Verify threads were started
        mock_thread.start.assert_called()
        assert mock_thread.start.call_count == 2


def test_event_consumer_add_event(event_pusher, mock_http_client):
    """test event consumer add event"""
    # add instances
    test_instance = Instance(job_name="test_job", model_name="test_model", id=1, role="prefill")
    readonly_instance = ReadOnlyInstance(test_instance)
    event_pusher.instances["test_job"] = readonly_instance

    test_event = Event(event_type=EventType.ADD, instance=readonly_instance.to_instance())
    event_pusher.event_queue.put(test_event)
    # Put sentinel None to signal end of real events
    event_pusher.event_queue.put(None)

    # Save original get, then mock to drain the real queue and raise StopIteration
    _original_get = event_pusher.event_queue.get

    def mock_get(timeout=None):
        try:
            return _original_get(block=False)
        except queue.Empty:
            raise StopIteration

    with patch.object(event_pusher.event_queue, 'get', side_effect=mock_get):
        try:
            event_pusher._event_consumer()
        except StopIteration:
            pass

        # check send_instance_refresh is called
        mock_http_client.assert_called_once()


def test_event_consumer_del_event(event_pusher, mock_http_client):
    """test event consumer del event"""
    # add instances
    test_instance = Instance(job_name="test_job", model_name="test_model", id=1, role="prefill")
    readonly_instance = ReadOnlyInstance(test_instance)
    event_pusher.instances["test_job"] = readonly_instance

    test_event = Event(event_type=EventType.DEL, instance=readonly_instance.to_instance())
    event_pusher.event_queue.put(test_event)
    # Put sentinel None to signal end of real events
    event_pusher.event_queue.put(None)

    _original_get = event_pusher.event_queue.get

    def mock_get(timeout=None):
        try:
            return _original_get(block=False)
        except queue.Empty:
            raise StopIteration

    with patch.object(event_pusher.event_queue, 'get', side_effect=mock_get):
        try:
            event_pusher._event_consumer()
        except StopIteration:
            pass

        # check send_instance_refresh is called
        mock_http_client.assert_called_once()


def test_event_consumer_set_event(event_pusher, mock_http_client):
    """test event consumer set event"""
    # add multi instance - ensure we have both prefill and decode instances
    for i in range(2):
        # Add prefill instance
        job_name = "test_prefill_job" + str(i)
        test_instance = Instance(job_name=job_name, model_name="test_model", id=i, role="prefill")
        readonly_instance = ReadOnlyInstance(test_instance)
        event_pusher.instances[job_name] = readonly_instance

        # Add decode instance
        job_name = "test_decode_job" + str(i)
        test_instance = Instance(job_name=job_name, model_name="test_model", id=i + 10, role="decode")
        readonly_instance = ReadOnlyInstance(test_instance)
        event_pusher.instances[job_name] = readonly_instance

    test_event = Event(event_type=EventType.SET, instance=None)

    event_pusher.event_queue.put(test_event)
    event_pusher.event_queue.put(None)

    _original_get = event_pusher.event_queue.get

    def mock_get(timeout=None):
        try:
            return _original_get(block=False)
        except queue.Empty:
            raise StopIteration

    with patch.object(event_pusher.event_queue, 'get', side_effect=mock_get):
        try:
            event_pusher._event_consumer()
        except StopIteration:
            pass

        # check send_instance_refresh is called
        mock_http_client.assert_called_once()


def test_event_consumer_set_event_missing_decode(event_pusher, mock_http_client):
    """test event consumer set event is skipped when missing decode instance"""
    # add only prefill instances
    for i in range(2):
        job_name = "test_prefill_job" + str(i)
        test_instance = Instance(job_name=job_name, model_name="test_model", id=i, role="prefill")
        readonly_instance = ReadOnlyInstance(test_instance)
        event_pusher.instances[job_name] = readonly_instance

    test_event = Event(event_type=EventType.SET, instance=None)

    event_pusher.event_queue.put(test_event)
    event_pusher.event_queue.put(None)

    _original_get = event_pusher.event_queue.get

    def mock_get(timeout=None):
        try:
            return _original_get(block=False)
        except queue.Empty:
            raise StopIteration

    with patch.object(event_pusher.event_queue, 'get', side_effect=mock_get):
        try:
            event_pusher._event_consumer()
        except StopIteration:
            pass

        # check send_instance_refresh is called even missing decode instance
        mock_http_client.assert_called()


def test_event_consumer_exception_handling(event_pusher, mock_http_client):
    """test event consumer exception handling"""
    # set mock to return False (indicating failure)
    mock_http_client.return_value = False

    # add instances
    test_instance = Instance(job_name="test_job", model_name="test_model", id=1, role="prefill")
    readonly_instance = ReadOnlyInstance(test_instance)
    event_pusher.instances["test_job"] = readonly_instance

    test_event = Event(event_type=EventType.ADD, instance=readonly_instance.to_instance())

    event_pusher.event_queue.put(test_event)
    event_pusher.event_queue.put(None)

    _original_get = event_pusher.event_queue.get

    def mock_get(timeout=None):
        try:
            return _original_get(block=False)
        except queue.Empty:
            raise StopIteration

    with patch.object(event_pusher.event_queue, 'get', side_effect=mock_get):
        try:
            event_pusher._event_consumer()
        except StopIteration:
            pass

        # check send_instance_refresh is called
        mock_http_client.assert_called_once()


def test_heartbeat_detector_normal(event_pusher):
    """test heartbeat detector"""
    # Mock CoordinatorApiClient.query_status to return successful response
    with patch('motor.controller.core.event_pusher.CoordinatorApiClient.query_status') as mock_query_status:
        mock_query_status.return_value = {"ready": True}

        # mock reset flag，重置为 True 时应发送一次 SET 事件并清零标志
        event_pusher.is_coordinator_reset = True

        # set loop count
        call_count = 0

        def mock_wait(timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise StopIteration

        with patch.object(event_pusher.work_condition, 'wait', side_effect=mock_wait):
            try:
                event_pusher._coordinator_heartbeat_detector()
            except StopIteration:
                pass

            # check reset flag
            assert not event_pusher.is_coordinator_reset
            # 当检测到重置时，应推送一次 SET 事件
            assert not event_pusher.event_queue.empty()
            evt = event_pusher.event_queue.get()
            assert evt.event_type == EventType.SET
            assert evt.instance is None


def test_heartbeat_detector_failure(event_pusher):
    """test heartbeat detector failure"""
    # Mock CoordinatorApiClient.query_status to raise exception after first success
    call_count = 0

    def mock_query_status(params: dict = None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call succeeds to establish connection
            event_pusher.is_first_heartbeat_success = True
            return {"ready": True}
        else:
            # Subsequent calls fail
            raise RuntimeError("Connection failed")

    with patch('motor.controller.core.event_pusher.CoordinatorApiClient.query_status', side_effect=mock_query_status):
        sleep_count = 0

        def mock_wait(timeout=None):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count >= 5:  # Run enough iterations to trigger reset detection
                raise StopIteration

        with patch('motor.controller.core.event_pusher.logger') as mock_logger:
            with patch.object(event_pusher.work_condition, 'wait', side_effect=mock_wait):
                try:
                    event_pusher._coordinator_heartbeat_detector()
                except StopIteration:
                    pass

                # Check that coordinator reset detection was triggered at least once
                assert mock_logger.warning.call_count >= 1
                # Check that the warning message indicates restart detection
                warning_calls = [
                    call
                    for call in mock_logger.warning.call_args_list
                    if "Coordinator heartbeat lost. Possible restart detected" in str(call)
                ]
                assert len(warning_calls) >= 1


def test_heartbeat_detector_ready_false_not_repeated(event_pusher):
    """SET event should only fire once when ready=False persists across iterations."""
    ready_values = [
        {"ready": True},  # iteration 1: normal
        {"ready": False},  # iteration 2: transition → SET triggered
        {"ready": False},  # iteration 3: still not ready → no SET
        {"ready": False},  # iteration 4: still not ready → no SET
        {"ready": True},  # iteration 5: ready again → flag reset
        {"ready": False},  # iteration 6: transition again → SET triggered
    ]
    call_count = 0

    def mock_query_status(params: dict = None):
        nonlocal call_count
        if call_count < len(ready_values):
            val = ready_values[call_count]
        else:
            raise StopIteration
        call_count += 1
        return val

    with patch('motor.controller.core.event_pusher.CoordinatorApiClient.query_status', side_effect=mock_query_status):
        iter_count = 0

        def mock_wait(timeout=None):
            nonlocal iter_count
            iter_count += 1
            if iter_count > len(ready_values):
                raise StopIteration

        with patch.object(event_pusher.work_condition, 'wait', side_effect=mock_wait):
            try:
                event_pusher._coordinator_heartbeat_detector()
            except StopIteration:
                pass

    # Collect all SET events from the queue
    set_events = []
    while not event_pusher.event_queue.empty():
        evt = event_pusher.event_queue.get()
        if evt.event_type == EventType.SET:
            set_events.append(evt)

    # Should have exactly 2 SET events (iteration 2 and iteration 6), not 4
    assert len(set_events) == 2, f"Expected 2 SET events (one per ready transition), got {len(set_events)}"


def test_update_add_instance(event_pusher):
    """test update add instance"""
    test_instance = Instance(job_name="test_job", model_name="test_model", id=1, role="prefill")
    readonly_instance = ReadOnlyInstance(test_instance)
    event_pusher.update(readonly_instance, ObserverEvent.INSTANCE_READY)

    # Verify that the instance was added to the dictionary
    assert readonly_instance.job_name in event_pusher.instances
    assert event_pusher.instances[readonly_instance.job_name] == readonly_instance

    # Verify that the event has been placed in the queue
    assert not event_pusher.event_queue.empty()
    event = event_pusher.event_queue.get()
    assert event.event_type == EventType.ADD
    assert event.instance.job_name == readonly_instance.job_name


def test_update_remove_instance(event_pusher):
    """test update remove instance"""
    test_instance = Instance(job_name="test_job", model_name="test_model", id=1, role="prefill")
    readonly_instance = ReadOnlyInstance(test_instance)
    event_pusher.instances[readonly_instance.job_name] = readonly_instance

    event_pusher.update(readonly_instance, ObserverEvent.INSTANCE_REMOVED)

    # INSTANCE_REMOVED 应作为 DEL 事件通知 Coordinator 清理 paused_pool
    assert not event_pusher.event_queue.empty()
    event = event_pusher.event_queue.get()
    assert event.event_type == EventType.DEL
    assert event.instance.job_name == readonly_instance.job_name


def test_update_seperated_instance(event_pusher):
    """test update seperated instance"""
    test_instance = Instance(job_name="test_job_seperated", model_name="test_model", id=1, role="prefill")
    readonly_instance = ReadOnlyInstance(test_instance)
    event_pusher.instances[readonly_instance.job_name] = readonly_instance

    event_pusher.update(readonly_instance, ObserverEvent.INSTANCE_SEPERATED)

    # Verify that the event has been placed in the queue
    assert not event_pusher.event_queue.empty()
    event = event_pusher.event_queue.get()
    # INSTANCE_SEPERATED 应作为 DEL 事件通知
    assert event.event_type == EventType.DEL
    assert event.instance.job_name == readonly_instance.job_name


def test_update_seperated_instance_recovery(event_pusher):
    """test update seperated instance recovery"""
    test_instance = Instance(job_name="test_job_recovery", model_name="test_model", id=1, role="prefill")
    readonly_instance = ReadOnlyInstance(test_instance)
    event_pusher.instances[readonly_instance.job_name] = readonly_instance

    # First separate the instance
    event_pusher.update(readonly_instance, ObserverEvent.INSTANCE_SEPERATED)
    # Clear the queue
    while not event_pusher.event_queue.empty():
        event_pusher.event_queue.get()

    # Then recover the instance
    event_pusher.update(readonly_instance, ObserverEvent.INSTANCE_READY)

    # Verify that the recovery event has been placed in the queue
    assert not event_pusher.event_queue.empty()
    event = event_pusher.event_queue.get()
    assert event.event_type == EventType.ADD
    assert event.instance.job_name == readonly_instance.job_name


def test_update_deep_copy_instance(event_pusher):
    """test that update method performs deep copy of instance for data consistency"""
    # Create a test instance with some data
    original_job_name = "original_job"
    original_model_name = "original_model"
    test_instance = Instance(job_name=original_job_name, model_name=original_model_name, id=1, role="prefill")

    # Add some test data
    from motor.common.resources.instance import NodeManagerInfo

    test_instance.node_managers.append(NodeManagerInfo(pod_ip="192.168.1.1", host_ip="10.0.0.1", port="8080"))

    # Wrap in ReadOnlyInstance
    readonly_instance = ReadOnlyInstance(test_instance)

    # Call update method (this should perform deep copy)
    event_pusher.update(readonly_instance, ObserverEvent.INSTANCE_READY)

    # Get the event from queue
    assert not event_pusher.event_queue.empty()
    event = event_pusher.event_queue.get()
    assert event.event_type == EventType.ADD

    # Verify the event instance is a deep copy by checking it's not the same object
    assert event.instance is not readonly_instance
    assert event.instance.job_name == original_job_name
    assert event.instance.model_name == original_model_name

    # Verify nested objects are also deep copied
    assert event.instance.node_managers is not test_instance.node_managers
    assert len(event.instance.node_managers) == len(test_instance.node_managers)
    assert event.instance.node_managers[0].pod_ip == test_instance.node_managers[0].pod_ip

    # Modify the original instance after the event was created (through the underlying instance)
    test_instance.job_name = "modified_job"
    test_instance.model_name = "modified_model"
    test_instance.node_managers[0].pod_ip = "192.168.1.2"

    # Verify that the event instance is unaffected by the modifications
    assert event.instance.job_name == original_job_name
    assert event.instance.model_name == original_model_name
    assert event.instance.node_managers[0].pod_ip == "192.168.1.1"

    # Verify that the instance in the internal dictionary is still the original reference
    assert event_pusher.instances[original_job_name] is readonly_instance


def test_update_deep_copy_seperated_instance(event_pusher):
    """test that update method performs deep copy for seperated instance events"""

    # Create and add a test instance
    original_job_name = "seperated_job"
    test_instance = Instance(job_name=original_job_name, model_name="test_model", id=1, role="prefill")
    readonly_instance = ReadOnlyInstance(test_instance)
    event_pusher.instances[readonly_instance.job_name] = readonly_instance

    # Call update method for seperated event (this should perform deep copy)
    event_pusher.update(readonly_instance, ObserverEvent.INSTANCE_SEPERATED)

    # Get the event from queue
    assert not event_pusher.event_queue.empty()
    event = event_pusher.event_queue.get()
    assert event.event_type == EventType.DEL

    # Verify the event instance is a deep copy
    assert event.instance is not readonly_instance
    assert event.instance.job_name == original_job_name

    # Modify the original instance after the event was created
    test_instance.job_name = "modified_seperated_job"

    # Verify that the event instance is unaffected
    assert event.instance.job_name == original_job_name


def test_update_seperated_instance_initial_stage_abnormal(event_pusher):
    """test update seperated instance when instance abnormal in initial stage"""
    test_instance = Instance(job_name="test_job_initial_abnormal", model_name="test_model", id=1, role="prefill")
    readonly_instance = ReadOnlyInstance(test_instance)
    # Intentionally do not add the instance to event_pusher.instances
    # dict to simulate abnormal instance in initial stage

    event_pusher.update(readonly_instance, ObserverEvent.INSTANCE_SEPERATED)

    # When instance abnormal in initial stage, the event should be
    # ignored and no event should be pushed to the queue
    assert event_pusher.event_queue.empty()
    # Verify that the instance was not added to the dictionary
    assert readonly_instance.job_name not in event_pusher.instances


def test_update_config():
    """Test update_config method updates configuration"""
    with patch('threading.Thread') as mock_thread_class:
        mock_thread = MagicMock()
        mock_thread_class.return_value = mock_thread

        # Create EventPusher instance
        config = ControllerConfig()
        event_pusher = EventPusher(config)

        # Store original config field
        original_coordinator_heartbeat_interval = event_pusher.coordinator_heartbeat_interval

        # Create new config with different event settings
        new_config = ControllerConfig()
        new_config.event_config.coordinator_heartbeat_interval = 20.0

        # Update config
        event_pusher.update_config(new_config)

        # Verify config was updated
        assert event_pusher.coordinator_heartbeat_interval == 20.0
        assert event_pusher.coordinator_heartbeat_interval != original_coordinator_heartbeat_interval
