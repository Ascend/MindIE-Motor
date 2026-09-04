# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 license for more details.

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from motor.common.resources.endpoint import (
    Endpoint,
    EndpointStatus,
    Workload,
)
from motor.common.resources.http_msg_spec import EventType
from motor.common.resources.instance import Instance, InsStatus, PDRole, ParallelConfig
from motor.config.coordinator import CoordinatorConfig, SchedulerType
from motor.coordinator.domain.instance_manager import InstanceManager
from motor.coordinator.scheduler.runtime.scheduler_client import _SchedulerInstanceCache
from motor.coordinator.scheduler.runtime.scheduler_server import (
    _SchedulerFrontendTransport,
    _SchedulerRequestDispatcher,
    _instance_from_dict,
    AsyncSchedulerServer,
)
from motor.coordinator.scheduler.runtime.zmq_protocol import (
    SchedulerRequest,
    SchedulerRequestType,
    SchedulerResponseType,
)
from motor.coordinator.scheduler.scheduler import Scheduler


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------


class _DummyWorkloadWriter:
    """Minimal workload-writer stub reused across dispatcher tests."""

    def __init__(
        self,
        sequence: int = 0,
        instance_version: int = 1,
        role_sequences: dict[PDRole, int] | None = None,
    ):
        self.sequence = sequence
        self.instance_version = instance_version
        self._role_sequences = role_sequences
        self.writes: list[tuple[int, int]] = []
        self.snapshots: int = 0
        self.heartbeats: int = 0
        self.shm_name = "test_shm"
        self.blocked: list[tuple[int, bool]] = []

    def role_sequence(self, role: PDRole) -> int | None:
        if self._role_sequences is None:
            return None
        return self._role_sequences.get(role)

    async def write_single_entry(self, instance_id: int, endpoint_id: int) -> None:
        self.write_single_entry_sync(instance_id, endpoint_id)

    def write_single_entry_sync(self, instance_id: int, endpoint_id: int) -> None:
        self.sequence += 2
        self.writes.append((instance_id, endpoint_id))

    def write_single_entry_from_workload(self, instance_id, endpoint_id, role, workload) -> None:
        self.write_single_entry_sync(instance_id, endpoint_id)

    def write_snapshot(self) -> None:
        self.snapshots += 1

    def write_heartbeat(self) -> None:
        self.heartbeats += 1

    def set_blocked(self, instance_id: int, blocked: bool) -> int:
        self.blocked.append((instance_id, blocked))
        return 0

    def release(self) -> None:
        pass


def _make_instance(
    instance_id: int,
    endpoint_ids: tuple[int, ...],
    role: PDRole = PDRole.ROLE_P,
    engine_type: str | None = None,
) -> Instance:
    inst = Instance(
        job_name=f"{role.value}-{instance_id}",
        model_name="test_model",
        id=instance_id,
        role=role,
        engine_type=engine_type,
        status=InsStatus.ACTIVE,
        parallel_config=ParallelConfig(dp_size=len(endpoint_ids)),
    )
    inst.add_endpoints(
        f"pod-{instance_id}",
        {
            idx: Endpoint(
                id=ep_id,
                ip=f"10.0.0.{instance_id}",
                business_port=f"80{idx}",
                status=EndpointStatus.NORMAL,
                workload=Workload(),
            )
            for idx, ep_id in enumerate(endpoint_ids)
        },
    )
    return inst


def _make_dispatcher(
    scheduler_type: SchedulerType = SchedulerType.LOAD_BALANCE,
    workload_writer=None,
    on_refresh_done=None,
) -> tuple[_SchedulerRequestDispatcher, InstanceManager, Scheduler, CoordinatorConfig]:
    config = CoordinatorConfig()
    config.scheduler_config.scheduler_type = scheduler_type
    config.scheduler_config.endpoint_instance_score_weight = 0.0
    instance_manager = InstanceManager(config)
    scheduler = Scheduler(instance_provider=instance_manager, config=config)
    dispatcher = _SchedulerRequestDispatcher(
        instance_manager,
        scheduler,
        config,
        workload_writer=workload_writer,
        on_instance_refresh_done=on_refresh_done,
    )
    return dispatcher, instance_manager, scheduler, config


class TestInstanceFromDict:
    def test_invalid_data_returns_none(self):
        """model_validate fails on missing required fields → _instance_from_dict returns None."""
        result = _instance_from_dict({"completely": "wrong", "data": 42})
        assert result is None

    def test_valid_dict_returns_instance(self):
        inst = _make_instance(5, (50,))
        data = inst.model_dump(mode="json")
        result = _instance_from_dict(data)
        assert result is not None
        assert result.id == 5


class TestControlPlaneProtocol:
    def test_hot_path_rpcs_removed_from_protocol(self):
        """P3 gate: ALLOCATE / UPDATE / REFRESH must not exist on the control-plane enum."""
        names = {member.name for member in SchedulerRequestType}
        assert "ALLOCATE_ONLY" not in names
        assert "UPDATE_WORKLOAD" not in names
        assert "REFRESH_INSTANCES" not in names
        assert "GET_AVAILABLE_INSTANCES" in names
        assert "CIRCUIT_BREAKER_REPORT" in names


class TestDispatchUnknownType:
    @pytest.mark.asyncio
    async def test_returns_error_for_unknown_request_type(self):
        dispatcher, *_ = _make_dispatcher()
        request = SchedulerRequest(
            request_type="totally_unknown_type",
            request_id="req-unknown",
            data={},
        )
        response = await dispatcher.dispatch(request)
        assert response.response_type == SchedulerResponseType.ERROR
        assert "Unknown request type" in (response.error or "")


class TestHandleGetAvailableInstances:
    @pytest.mark.asyncio
    async def test_returns_instances_without_shm_name(self):
        """Without workload_writer, response should not contain workload_shm_name."""
        dispatcher, instance_manager, *_ = _make_dispatcher()
        inst = _make_instance(1, (10,))
        await instance_manager.refresh_instances(EventType.ADD, [inst])

        request = SchedulerRequest(
            request_type=SchedulerRequestType.GET_AVAILABLE_INSTANCES,
            request_id="req-g1",
            data={},
        )
        response = await dispatcher.dispatch(request)
        assert response.response_type == SchedulerResponseType.SUCCESS
        assert "instances" in response.data
        assert "workload_shm_name" not in response.data

    @pytest.mark.asyncio
    async def test_returns_shm_name_when_writer_present(self):
        writer = _DummyWorkloadWriter()
        dispatcher, instance_manager, *_ = _make_dispatcher(workload_writer=writer)
        inst = _make_instance(1, (10,))
        await instance_manager.refresh_instances(EventType.ADD, [inst])

        request = SchedulerRequest(
            request_type=SchedulerRequestType.GET_AVAILABLE_INSTANCES,
            request_id="req-g2",
            data={},
        )
        response = await dispatcher.dispatch(request)
        assert response.response_type == SchedulerResponseType.SUCCESS
        assert response.data["workload_shm_name"] == writer.shm_name

    @pytest.mark.asyncio
    async def test_role_filter_limits_returned_instances(self):
        dispatcher, instance_manager, *_ = _make_dispatcher()
        inst_p = _make_instance(1, (10,), PDRole.ROLE_P)
        inst_d = _make_instance(2, (20,), PDRole.ROLE_D)
        await instance_manager.refresh_instances(EventType.ADD, [inst_p, inst_d])

        request = SchedulerRequest(
            request_type=SchedulerRequestType.GET_AVAILABLE_INSTANCES,
            request_id="req-g3",
            data={"role": PDRole.ROLE_P.value},
        )
        response = await dispatcher.dispatch(request)
        assert response.response_type == SchedulerResponseType.SUCCESS
        ids = [inst["id"] for inst in response.data["instances"]]
        assert 1 in ids
        assert 2 not in ids

    @pytest.mark.asyncio
    async def test_get_payload_keeps_dispatch_capabilities(self):
        """GET model_dump must keep caps so Worker can select TRIGGER."""
        dispatcher, instance_manager, *_ = _make_dispatcher()
        inst = _make_instance(8, (80,), role=PDRole.ROLE_D, engine_type="vllm")
        inst.dispatch_capabilities = ["concurrent_engine_sync"]
        await instance_manager.refresh_instances(EventType.ADD, [inst])

        request = SchedulerRequest(
            request_type=SchedulerRequestType.GET_AVAILABLE_INSTANCES,
            request_id="req-g-caps",
            data={},
        )
        response = await dispatcher.dispatch(request)
        assert response.response_type == SchedulerResponseType.SUCCESS
        restored = _instance_from_dict(response.data["instances"][0])
        assert restored is not None
        assert restored.dispatch_capabilities == ["concurrent_engine_sync"]


class TestApplyRefresh:
    @pytest.mark.asyncio
    async def test_no_change_skips_snapshot_and_callback(self):
        callback_called = [False]

        async def sync_cb(event_type, instances):
            callback_called[0] = True

        writer = _DummyWorkloadWriter()
        dispatcher, instance_manager, *_ = _make_dispatcher(
            workload_writer=writer,
            on_refresh_done=sync_cb,
        )
        instance_manager.refresh_instances = AsyncMock(return_value=False)
        changed = await dispatcher.apply_refresh(EventType.ADD, [])
        assert changed is False
        assert writer.snapshots == 0
        assert callback_called[0] is False

    @pytest.mark.asyncio
    async def test_changed_writes_snapshot_and_publishes(self):
        published = []

        async def on_done(event_type, instances):
            published.append((event_type, instances))

        writer = _DummyWorkloadWriter()
        dispatcher, instance_manager, *_ = _make_dispatcher(
            workload_writer=writer,
            on_refresh_done=on_done,
        )
        inst = _make_instance(1, (10,))
        changed = await dispatcher.apply_refresh(EventType.ADD, [inst])
        assert changed is True
        assert writer.snapshots == 1
        assert published and published[0][0] == EventType.ADD

    @pytest.mark.asyncio
    async def test_add_delta_applies_to_worker_cache(self):
        """apply_refresh ADD -> PUB delta payload -> Worker cache.apply_add (no REFRESH RPC)."""
        published = []

        async def on_done(event_type, instances):
            published.append((event_type, instances))

        dispatcher, instance_manager, *_ = _make_dispatcher(on_refresh_done=on_done)
        inst = _make_instance(7, (70,))
        changed = await dispatcher.apply_refresh(EventType.ADD, [inst])
        assert changed is True
        assert published and published[0][0] == EventType.ADD

        server = AsyncSchedulerServer(CoordinatorConfig(), instance_manager=instance_manager)
        delta = server._build_instance_delta(EventType.ADD, published[0][1])
        assert delta is not None
        assert delta["event"] == "add"
        rebuilt = [x for x in (_instance_from_dict(d) for d in delta["instances"]) if x is not None]
        cache = _SchedulerInstanceCache()
        assert await cache.apply_add(rebuilt) is True
        assert [i.id for i in cache.get_instances(PDRole.ROLE_P)] == [7]

    @pytest.mark.asyncio
    async def test_del_clears_blocked_flag(self):
        writer = _DummyWorkloadWriter()
        dispatcher, instance_manager, *_ = _make_dispatcher(workload_writer=writer)
        inst = _make_instance(4, (40,))
        await instance_manager.refresh_instances(EventType.ADD, [inst])
        changed = await dispatcher.apply_refresh(EventType.DEL, [inst])
        assert changed is True
        assert (4, False) in writer.blocked

    @pytest.mark.asyncio
    async def test_dirty_snapshot_is_retried_even_when_next_refresh_is_a_noop(self):
        """A write_snapshot failure leaves _snapshot_dirty set; a later apply_refresh whose own IM
        delta is a no-op (idempotent retry) must still force write_snapshot while dirty, so IM/SHM
        cannot stay diverged forever just because no further real change ever arrives (P0 fix).
        """
        writer = _DummyWorkloadWriter()
        writer.write_snapshot = MagicMock(side_effect=RuntimeError("shm write failed"))
        dispatcher, instance_manager, *_ = _make_dispatcher(workload_writer=writer)
        inst = _make_instance(1, (10,))

        with pytest.raises(RuntimeError):
            await dispatcher.apply_refresh(EventType.ADD, [inst])
        assert dispatcher._snapshot_dirty is True

        writer.write_snapshot = MagicMock()  # recovers
        instance_manager.refresh_instances = AsyncMock(return_value=False)  # idempotent no-op
        changed = await dispatcher.apply_refresh(EventType.ADD, [inst])

        assert changed is False
        writer.write_snapshot.assert_called_once()
        assert dispatcher._snapshot_dirty is False

    @pytest.mark.asyncio
    async def test_retry_dirty_snapshot_converges_from_heartbeat_loop(self):
        """The heartbeat-driven retry (no new instance-list event at all) must also clear dirty."""
        writer = _DummyWorkloadWriter()
        dispatcher, *_ = _make_dispatcher(workload_writer=writer)
        dispatcher._snapshot_dirty = True

        dispatcher._retry_dirty_snapshot()

        assert writer.snapshots == 1
        assert dispatcher._snapshot_dirty is False


class TestSchedulerFrontendTransport:
    @pytest.mark.asyncio
    async def test_recv_returns_none_when_no_socket(self):
        transport = _SchedulerFrontendTransport(MagicMock())
        client_id, frames = await transport.recv()
        assert client_id is None
        assert frames == []

    @pytest.mark.asyncio
    async def test_recv_valid_message(self):
        mock_socket = AsyncMock()
        mock_socket.recv_multipart = AsyncMock(return_value=[b"client-id", b"", b"payload-frame"])
        transport = _SchedulerFrontendTransport(MagicMock())
        transport._socket = mock_socket

        client_id, frames = await transport.recv()
        assert client_id == b"client-id"
        assert frames == [b"payload-frame"]

    @pytest.mark.asyncio
    async def test_recv_too_few_parts_returns_none(self):
        """Message with fewer than 3 parts is considered malformed."""
        mock_socket = AsyncMock()
        mock_socket.recv_multipart = AsyncMock(return_value=[b"client-id", b""])
        transport = _SchedulerFrontendTransport(MagicMock())
        transport._socket = mock_socket

        client_id, frames = await transport.recv()
        assert client_id is None
        assert frames == []

    @pytest.mark.asyncio
    async def test_send_noop_when_no_socket(self):
        transport = _SchedulerFrontendTransport(MagicMock())
        # Should not raise even when socket is None
        await transport.send(b"client", [b"response"])

    @pytest.mark.asyncio
    async def test_send_calls_socket_send_multipart(self):
        mock_socket = AsyncMock()
        transport = _SchedulerFrontendTransport(MagicMock())
        transport._socket = mock_socket

        await transport.send(b"client-id", [b"response-frame"])
        mock_socket.send_multipart.assert_called_once()
        sent_frames = mock_socket.send_multipart.call_args[0][0]
        assert b"client-id" in sent_frames

    @pytest.mark.asyncio
    async def test_bind_creates_router_socket_and_binds(self):
        import zmq

        mock_socket = MagicMock()
        mock_context = MagicMock()
        mock_context.socket.return_value = mock_socket

        transport = _SchedulerFrontendTransport(mock_context)
        await transport.bind("ipc:///tmp/test_scheduler")

        mock_context.socket.assert_called_once_with(zmq.ROUTER)
        mock_socket.bind.assert_called_once_with("ipc:///tmp/test_scheduler")
        assert transport._socket is mock_socket

    @pytest.mark.asyncio
    async def test_disconnect_closes_socket_and_sets_none(self):
        mock_socket = MagicMock()
        transport = _SchedulerFrontendTransport(MagicMock())
        transport._socket = mock_socket

        await transport.disconnect()
        mock_socket.close.assert_called_once()
        assert transport._socket is None

    @pytest.mark.asyncio
    async def test_disconnect_handles_close_exception(self):
        """close() error should be swallowed; socket still set to None."""
        mock_socket = MagicMock()
        mock_socket.close.side_effect = Exception("zmq close error")
        transport = _SchedulerFrontendTransport(MagicMock())
        transport._socket = mock_socket

        await transport.disconnect()
        assert transport._socket is None


def _make_server() -> AsyncSchedulerServer:
    """Create an AsyncSchedulerServer without calling start() (no real ZMQ)."""
    config = CoordinatorConfig()
    return AsyncSchedulerServer(config)


class TestAsyncSchedulerServerStop:
    @pytest.mark.asyncio
    async def test_stop_releases_all_resources(self):
        server = _make_server()

        # Save references BEFORE stop() nullifies them
        mock_writer = MagicMock()
        mock_pub = MagicMock()
        mock_disconnect = AsyncMock()
        mock_transport = AsyncMock()
        mock_transport.disconnect = mock_disconnect
        mock_context = MagicMock()

        server._workload_writer = mock_writer
        server._pub_socket = mock_pub
        server._transport = mock_transport
        server.context = mock_context

        await server.stop()

        mock_writer.release.assert_called_once()
        mock_pub.close.assert_called_once()
        mock_disconnect.assert_called_once()
        mock_context.term.assert_called_once()
        assert server._workload_writer is None
        assert server._pub_socket is None

    @pytest.mark.asyncio
    async def test_stop_with_empty_state_does_not_raise(self):
        """stop() with all attributes None must complete without error."""
        server = _make_server()
        await server.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_active_tasks(self):
        server = _make_server()

        async def long_running():
            await asyncio.sleep(100)

        task = asyncio.create_task(long_running())
        server._active_tasks.add(task)
        task.add_done_callback(server._active_tasks.discard)

        await server.stop()

        assert task.cancelled() or task.done()
        assert len(server._active_tasks) == 0

    @pytest.mark.asyncio
    async def test_stop_handles_heartbeat_task_cancellation(self):
        server = _make_server()

        async def heartbeat_stub():
            await asyncio.sleep(100)

        server._heartbeat_task = asyncio.create_task(heartbeat_stub())
        await server.stop()
        assert server._heartbeat_task is None

    @pytest.mark.asyncio
    async def test_stop_swallows_writer_release_error(self):
        server = _make_server()
        mock_writer = MagicMock()
        mock_writer.release.side_effect = Exception("release error")
        server._workload_writer = mock_writer
        server._transport = AsyncMock()

        # Must not raise
        await server.stop()


class TestAsyncSchedulerServerPublishInstanceChanged:
    @pytest.mark.asyncio
    async def test_noop_when_no_pub_socket(self):
        server = _make_server()
        server._pub_socket = None
        # Should complete silently
        await server._publish_instance_changed()

    @pytest.mark.asyncio
    async def test_sends_topic_and_version(self):
        from motor.coordinator.scheduler.runtime.zmq_protocol import (
            INSTANCE_CHANGE_TOPIC,
        )

        server = _make_server()
        mock_pub = AsyncMock()
        server._pub_socket = mock_pub
        server._workload_writer = _DummyWorkloadWriter(instance_version=42)

        await server._publish_instance_changed()

        mock_pub.send_multipart.assert_called_once()
        call_args = mock_pub.send_multipart.call_args[0][0]
        assert call_args[0] == INSTANCE_CHANGE_TOPIC
        assert call_args[1] == b"42"

    @pytest.mark.asyncio
    async def test_send_exception_is_swallowed(self):
        server = _make_server()
        mock_pub = AsyncMock()
        mock_pub.send_multipart.side_effect = Exception("zmq send error")
        server._pub_socket = mock_pub
        server._workload_writer = _DummyWorkloadWriter()

        # Must not raise
        await server._publish_instance_changed()

    @pytest.mark.asyncio
    async def test_add_event_appends_delta_frame(self):
        from motor.coordinator.scheduler.runtime.zmq_protocol import INSTANCE_CHANGE_TOPIC
        import msgspec

        server = _make_server()
        mock_pub = AsyncMock()
        server._pub_socket = mock_pub
        server._workload_writer = _DummyWorkloadWriter(instance_version=9)
        inst = _make_instance(7, (70,), role=PDRole.ROLE_P)

        await server._publish_instance_changed(EventType.ADD, [inst])

        frames = mock_pub.send_multipart.call_args[0][0]
        assert len(frames) == 3  # topic, version, delta
        assert frames[0] == INSTANCE_CHANGE_TOPIC
        assert frames[1] == b"9"
        delta = msgspec.msgpack.decode(frames[2])
        assert delta["event"] == "add"
        assert [i["id"] for i in delta["instances"]] == [7]

    @pytest.mark.asyncio
    async def test_set_event_sends_version_only(self):
        server = _make_server()
        mock_pub = AsyncMock()
        server._pub_socket = mock_pub
        server._workload_writer = _DummyWorkloadWriter(instance_version=9)
        inst = _make_instance(7, (70,), role=PDRole.ROLE_P)

        # SET is not delta-patched by workers; publish version only so they full-pull.
        await server._publish_instance_changed(EventType.SET, [inst])

        frames = mock_pub.send_multipart.call_args[0][0]
        assert len(frames) == 2
