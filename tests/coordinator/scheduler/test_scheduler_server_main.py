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
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from motor.common.resources.endpoint import (
    Endpoint,
    EndpointStatus,
    Workload,
    WorkloadAction,
)
from motor.common.resources.http_msg_spec import EventType
from motor.common.resources.instance import Instance, InsStatus, PDRole, ParallelConfig
from motor.config.coordinator import CoordinatorConfig, SchedulerType
from motor.coordinator.domain.instance_manager import InstanceManager
from motor.coordinator.scheduler.runtime.scheduler_server import (
    _SCHEDULING_LOG_SAMPLE_RATE,
    _SchedulerFrontendTransport,
    _SchedulerRequestDispatcher,
    _instance_from_dict,
    _serialize_endpoint_minimal,
    _serialize_instance_minimal,
    _should_log_scheduling_sample,
    AsyncSchedulerServer,
)
from motor.coordinator.scheduler.runtime.zmq_protocol import (
    CANDIDATE_POLICY_KV_CACHE_AFFINITY,
    CANDIDATE_POLICY_LOAD_BALANCE,
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

    def release(self) -> None:
        pass


def _make_instance(
    instance_id: int,
    endpoint_ids: tuple[int, ...],
    role: PDRole = PDRole.ROLE_P,
) -> Instance:
    inst = Instance(
        job_name=f"{role.value}-{instance_id}",
        model_name="test_model",
        id=instance_id,
        role=role,
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
                mgmt_port=f"90{idx}",
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


class TestShouldLogSchedulingSample:
    def test_empty_string_returns_false(self):
        assert _should_log_scheduling_sample("") is False

    def test_none_returns_false(self):
        assert _should_log_scheduling_sample(None) is False  # type: ignore[arg-type]

    def test_matching_key_returns_true(self):
        """Find a key whose hash % _SCHEDULING_LOG_SAMPLE_RATE == 0."""
        hit = None
        for i in range(10_000):
            key = str(i)
            if hash(key) % _SCHEDULING_LOG_SAMPLE_RATE == 0:
                hit = key
                break
        assert hit is not None, "No sample-rate hit found in range(10000)"
        assert _should_log_scheduling_sample(hit) is True

    def test_non_matching_key_returns_false(self):
        """Find a key that does NOT hit the sample rate."""
        miss = None
        for i in range(10_000):
            key = str(i)
            if hash(key) % _SCHEDULING_LOG_SAMPLE_RATE != 0:
                miss = key
                break
        assert miss is not None
        assert _should_log_scheduling_sample(miss) is False


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


class TestSerializeInstanceMinimal:
    def test_none_returns_empty_dict(self):
        assert _serialize_instance_minimal(None) == {}

    def test_valid_instance_returns_minimal_fields(self):
        inst = _make_instance(7, (70,), role=PDRole.ROLE_D)
        result = _serialize_instance_minimal(inst)
        assert result["id"] == 7
        assert result["role"] == PDRole.ROLE_D
        assert result["job_name"] == inst.job_name
        assert result["model_name"] == "test_model"
        assert result["engine_type"] is None
        assert result["dispatch_capabilities"] == []
        assert len(result) == 6


class TestSerializeEndpointMinimal:
    def test_none_returns_empty_dict(self):
        assert _serialize_endpoint_minimal(None) == {}

    def test_endpoint_without_status(self):
        ep = Endpoint(id=11, ip="1.2.3.4", business_port="8080", mgmt_port="9090")
        result = _serialize_endpoint_minimal(ep)
        assert result["id"] == 11
        assert result["ip"] == "1.2.3.4"
        assert result["business_port"] == "8080"
        assert result["mgmt_port"] == "9090"

    def test_endpoint_with_status_serializes_value(self):
        ep = Endpoint(
            id=12,
            ip="5.6.7.8",
            business_port="8081",
            mgmt_port="9090",
            status=EndpointStatus.NORMAL,
        )
        result = _serialize_endpoint_minimal(ep)
        assert result["status"] == EndpointStatus.NORMAL.value

    def test_endpoint_with_empty_mgmt_port_defaults_empty_string(self):
        """mgmt_port='' → serializer returns empty string (falsy → or '' branch)."""
        ep = Endpoint(id=13, ip="9.9.9.9", business_port="9000", mgmt_port="")
        result = _serialize_endpoint_minimal(ep)
        assert result["mgmt_port"] == ""


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


class TestHandleUpdateWorkload:
    @pytest.mark.asyncio
    async def test_missing_instance_id_returns_error(self):
        dispatcher, *_ = _make_dispatcher()
        request = SchedulerRequest(
            request_type=SchedulerRequestType.UPDATE_WORKLOAD,
            request_id="req-1",
            data={
                "endpoint_id": 10,
                "workload_change": Workload().model_dump(mode="json"),
            },
        )
        response = await dispatcher.dispatch(request)
        assert response.response_type == SchedulerResponseType.ERROR
        assert "Missing" in (response.error or "")

    @pytest.mark.asyncio
    async def test_missing_endpoint_id_returns_error(self):
        dispatcher, *_ = _make_dispatcher()
        request = SchedulerRequest(
            request_type=SchedulerRequestType.UPDATE_WORKLOAD,
            request_id="req-2",
            data={
                "instance_id": 1,
                "workload_change": Workload().model_dump(mode="json"),
            },
        )
        response = await dispatcher.dispatch(request)
        assert response.response_type == SchedulerResponseType.ERROR
        assert "Missing" in (response.error or "")

    @pytest.mark.asyncio
    async def test_missing_workload_change_returns_error(self):
        dispatcher, *_ = _make_dispatcher()
        request = SchedulerRequest(
            request_type=SchedulerRequestType.UPDATE_WORKLOAD,
            request_id="req-3",
            data={"instance_id": 1, "endpoint_id": 10},
        )
        response = await dispatcher.dispatch(request)
        assert response.response_type == SchedulerResponseType.ERROR
        assert "Missing workload_change" in (response.error or "")

    @pytest.mark.asyncio
    async def test_invalid_workload_format_returns_error(self):
        dispatcher, *_ = _make_dispatcher()
        # Use a non-dict to force model_validate to fail
        request = SchedulerRequest(
            request_type=SchedulerRequestType.UPDATE_WORKLOAD,
            request_id="req-4b",
            data={
                "instance_id": 1,
                "endpoint_id": 10,
                "workload_action": WorkloadAction.ALLOCATION.value,
                "workload_change": "this_is_not_a_dict",
            },
        )
        response = await dispatcher.dispatch(request)
        assert response.response_type == SchedulerResponseType.ERROR
        assert "Invalid workload_change format" in (response.error or "")

    @pytest.mark.asyncio
    async def test_success_without_writer_returns_success(self):
        dispatcher, instance_manager, scheduler, _ = _make_dispatcher()
        inst = _make_instance(1, (10,))
        await instance_manager.refresh_instances(EventType.ADD, [inst])

        scheduler.update_workload = AsyncMock(return_value=True)

        request = SchedulerRequest(
            request_type=SchedulerRequestType.UPDATE_WORKLOAD,
            request_id="req-5",
            data={
                "instance_id": 1,
                "endpoint_id": 10,
                "role": PDRole.ROLE_P.value,
                "req_id": "r5",
                "workload_action": WorkloadAction.ALLOCATION.value,
                "workload_change": Workload(active_tokens=8).model_dump(mode="json"),
            },
        )
        response = await dispatcher.dispatch(request)
        assert response.response_type == SchedulerResponseType.SUCCESS
        assert response.data["success"] is True

    @pytest.mark.asyncio
    async def test_success_with_writer_records_write(self):
        writer = _DummyWorkloadWriter()
        dispatcher, instance_manager, scheduler, _ = _make_dispatcher(workload_writer=writer)
        inst = _make_instance(2, (20,))
        await instance_manager.refresh_instances(EventType.ADD, [inst])

        scheduler.update_workload = AsyncMock(return_value=True)

        request = SchedulerRequest(
            request_type=SchedulerRequestType.UPDATE_WORKLOAD,
            request_id="req-6",
            data={
                "instance_id": 2,
                "endpoint_id": 20,
                "role": PDRole.ROLE_P.value,
                "req_id": "r6",
                "workload_action": WorkloadAction.ALLOCATION.value,
                "workload_change": Workload(active_tokens=3).model_dump(mode="json"),
            },
        )
        response = await dispatcher.dispatch(request)
        assert response.response_type == SchedulerResponseType.SUCCESS
        assert response.data["success"] is True
        assert (2, 20) in writer.writes

    @pytest.mark.asyncio
    async def test_operation_id_commits_update_workload_once(self, caplog):
        caplog.set_level(logging.INFO)
        writer = _DummyWorkloadWriter()
        dispatcher, instance_manager, scheduler, _ = _make_dispatcher(workload_writer=writer)
        inst = _make_instance(3, (30,))
        await instance_manager.refresh_instances(EventType.ADD, [inst])
        # Seed a positive allocation so the release below stays non-negative and the ledger value can
        # distinguish "applied once" from "applied twice" (a release-without-allocation floors to 0).
        await instance_manager.update_instance_workload(3, 30, Workload(active_tokens=10))

        data = {
            "instance_id": 3,
            "endpoint_id": 30,
            "role": PDRole.ROLE_P.value,
            "req_id": "r7",
            "operation_id": "op-r7-release-tokens",
            "workload_action": WorkloadAction.RELEASE_TOKENS.value,
            "workload_change": Workload(active_tokens=-3).model_dump(mode="json"),
        }

        first = await dispatcher.dispatch(
            SchedulerRequest(
                request_type=SchedulerRequestType.UPDATE_WORKLOAD,
                request_id="req-7a",
                data=data,
            )
        )
        second = await dispatcher.dispatch(
            SchedulerRequest(
                request_type=SchedulerRequestType.UPDATE_WORKLOAD,
                request_id="req-7b",
                data=dict(data),
            )
        )

        assert first.response_type == SchedulerResponseType.SUCCESS
        assert first.data["success"] is True
        assert "idempotent" not in first.data
        assert second.response_type == SchedulerResponseType.SUCCESS
        assert second.data["success"] is True
        assert second.data["idempotent"] is True
        assert "UPDATE_WORKLOAD idempotent replay" in caplog.text
        assert "operation_id=op-r7-release-tokens" in caplog.text
        assert "scheduler_request_id=req-7b" in caplog.text
        _role, workload = await instance_manager.get_endpoint_workload(3, 30)
        assert workload.active_tokens == 7  # 10 seeded - 3 released, applied exactly once
        assert writer.writes == [(3, 30), (3, 30)]

    def test_committed_operation_store_is_bounded_fifo(self):
        """Retry-dedup store keeps memory bounded by evicting the oldest id once the cap is hit."""
        dispatcher, *_ = _make_dispatcher()
        with patch(
            "motor.coordinator.scheduler.runtime.scheduler_server._MAX_COMMITTED_UPDATE_WORKLOAD_OPERATIONS",
            3,
        ):
            for i in range(5):
                dispatcher._remember_committed_operation(f"op-{i}")

        store = dispatcher._committed_update_workload_operations
        assert len(store) == 3
        assert list(store) == [
            "op-2",
            "op-3",
            "op-4",
        ]  # oldest (op-0, op-1) evicted first
        assert "op-0" not in store  # an evicted retry would be applied again
        assert "op-4" in store  # a recent retry is still de-duplicated

    @pytest.mark.asyncio
    async def test_no_sync_policy_writes_absolute_not_delta(self):
        """A policy without update_workload_sync must publish the ledger absolute, not the delta."""
        dispatcher, instance_manager, _scheduler, _ = _make_dispatcher(scheduler_type=SchedulerType.ROUND_ROBIN)
        inst = _make_instance(3, (30,))
        await instance_manager.refresh_instances(EventType.ADD, [inst])
        # The endpoint's authoritative absolute load is 7 active tokens.
        await instance_manager.update_instance_workload(3, 30, Workload(active_tokens=7))

        class _RecordingWriter:
            def __init__(self, im):
                self._im = im
                self.written: list[tuple[int, int, float | None]] = []

            def role_sequence(self, role):
                return None

            def write_single_entry_from_workload(self, instance_id, endpoint_id, role, workload):
                self.written.append(
                    (
                        instance_id,
                        endpoint_id,
                        None if workload is None else workload.active_tokens,
                    )
                )

            def write_single_entry_sync(self, instance_id, endpoint_id):
                _role, workload = self._im.get_endpoint_workload_sync(instance_id, endpoint_id)
                self.written.append(
                    (
                        instance_id,
                        endpoint_id,
                        None if workload is None else workload.active_tokens,
                    )
                )

            def write_snapshot(self):
                pass

        writer = _RecordingWriter(instance_manager)
        dispatcher._workload_writer = writer

        request = SchedulerRequest(
            request_type=SchedulerRequestType.UPDATE_WORKLOAD,
            request_id="req-nosync",
            data={
                "instance_id": 3,
                "endpoint_id": 30,
                "role": PDRole.ROLE_P.value,
                "req_id": "r-nosync",
                "workload_action": WorkloadAction.RELEASE_TOKENS.value,
                "workload_change": Workload(active_tokens=-3).model_dump(mode="json"),
            },
        )

        response = await dispatcher.dispatch(request)

        assert response.response_type == SchedulerResponseType.SUCCESS
        assert response.data["success"] is True
        # Must be the ledger absolute (7), never the delta (-3).
        assert writer.written == [(3, 30, 7.0)]


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


class TestHandleRefreshInstances:
    @pytest.mark.asyncio
    async def test_invalid_event_type_returns_error(self):
        dispatcher, *_ = _make_dispatcher()
        request = SchedulerRequest(
            request_type=SchedulerRequestType.REFRESH_INSTANCES,
            request_id="req-r1",
            data={"event_type": None, "instances": []},
        )
        response = await dispatcher.dispatch(request)
        assert response.response_type == SchedulerResponseType.ERROR
        assert "Invalid event type" in (response.error or "")

    @pytest.mark.asyncio
    async def test_no_change_skips_snapshot_and_callback(self):
        callback_called = [False]

        def sync_cb():
            callback_called[0] = True

        dispatcher, instance_manager, *_ = _make_dispatcher(
            workload_writer=_DummyWorkloadWriter(),
            on_refresh_done=sync_cb,
        )
        # Mock refresh_instances to simulate "no change"
        instance_manager.refresh_instances = AsyncMock(return_value=False)

        request = SchedulerRequest(
            request_type=SchedulerRequestType.REFRESH_INSTANCES,
            request_id="req-r2",
            data={"event_type": EventType.ADD.value, "instances": []},
        )
        response = await dispatcher.dispatch(request)
        assert response.response_type == SchedulerResponseType.SUCCESS
        assert response.data["changed"] is False
        assert callback_called[0] is False

    @pytest.mark.asyncio
    async def test_changed_writes_snapshot(self):
        writer = _DummyWorkloadWriter()
        dispatcher, instance_manager, *_ = _make_dispatcher(workload_writer=writer)
        instance_manager.refresh_instances = AsyncMock(return_value=True)

        request = SchedulerRequest(
            request_type=SchedulerRequestType.REFRESH_INSTANCES,
            request_id="req-r3",
            data={"event_type": EventType.ADD.value, "instances": []},
        )
        response = await dispatcher.dispatch(request)
        assert response.response_type == SchedulerResponseType.SUCCESS
        assert response.data["changed"] is True
        assert writer.snapshots >= 1

    @pytest.mark.asyncio
    async def test_changed_calls_sync_callback(self):
        received = []

        def sync_cb(event_type=None, instances=None):
            received.append((event_type, instances))

        dispatcher, instance_manager, *_ = _make_dispatcher(on_refresh_done=sync_cb)
        instance_manager.refresh_instances = AsyncMock(return_value=True)

        request = SchedulerRequest(
            request_type=SchedulerRequestType.REFRESH_INSTANCES,
            request_id="req-r4",
            data={"event_type": EventType.ADD.value, "instances": []},
        )
        await dispatcher.dispatch(request)
        # The refresh callback now receives the event type + changed instances (for delta PUB).
        assert len(received) == 1
        assert received[0][0] == EventType.ADD

    @pytest.mark.asyncio
    async def test_changed_calls_async_callback(self):
        callback_called = [False]

        async def async_cb(event_type=None, instances=None):
            callback_called[0] = True

        dispatcher, instance_manager, *_ = _make_dispatcher(on_refresh_done=async_cb)
        instance_manager.refresh_instances = AsyncMock(return_value=True)

        request = SchedulerRequest(
            request_type=SchedulerRequestType.REFRESH_INSTANCES,
            request_id="req-r5",
            data={"event_type": EventType.ADD.value, "instances": []},
        )
        await dispatcher.dispatch(request)
        assert callback_called[0] is True

    @pytest.mark.asyncio
    async def test_callback_exception_is_logged_not_raised(self):
        """Callback exception must not propagate; dispatcher returns SUCCESS."""

        def bad_cb(event_type=None, instances=None):
            raise RuntimeError("callback error")

        dispatcher, instance_manager, *_ = _make_dispatcher(on_refresh_done=bad_cb)
        instance_manager.refresh_instances = AsyncMock(return_value=True)

        request = SchedulerRequest(
            request_type=SchedulerRequestType.REFRESH_INSTANCES,
            request_id="req-r6",
            data={"event_type": EventType.ADD.value, "instances": []},
        )
        response = await dispatcher.dispatch(request)
        assert response.response_type == SchedulerResponseType.SUCCESS


class TestHandleAllocateOnlyEdgeCases:
    @pytest.mark.asyncio
    async def test_missing_instance_id_returns_error(self):
        dispatcher, *_ = _make_dispatcher()
        request = SchedulerRequest(
            request_type=SchedulerRequestType.ALLOCATE_ONLY,
            request_id="req-a1",
            data={
                "endpoint_id": 10,
                "role": PDRole.ROLE_P.value,
                "workload": Workload().model_dump(mode="json"),
            },
        )
        response = await dispatcher.dispatch(request)
        assert response.response_type == SchedulerResponseType.ERROR
        assert "Missing" in (response.error or "")

    @pytest.mark.asyncio
    async def test_missing_workload_returns_error(self):
        dispatcher, *_ = _make_dispatcher()
        request = SchedulerRequest(
            request_type=SchedulerRequestType.ALLOCATE_ONLY,
            request_id="req-a2",
            data={
                "instance_id": 1,
                "endpoint_id": 10,
                "role": PDRole.ROLE_P.value,
            },
        )
        response = await dispatcher.dispatch(request)
        assert response.response_type == SchedulerResponseType.ERROR
        assert "Missing workload" in (response.error or "")

    @pytest.mark.asyncio
    async def test_invalid_workload_format_returns_error(self):
        dispatcher, *_ = _make_dispatcher()
        request = SchedulerRequest(
            request_type=SchedulerRequestType.ALLOCATE_ONLY,
            request_id="req-a3",
            data={
                "instance_id": 1,
                "endpoint_id": 10,
                "role": PDRole.ROLE_P.value,
                "workload": "not_a_dict",
            },
        )
        response = await dispatcher.dispatch(request)
        assert response.response_type == SchedulerResponseType.ERROR
        assert "Invalid workload format" in (response.error or "")

    @pytest.mark.asyncio
    async def test_candidate_parse_fails_returns_success_with_none(self):
        """instance_id present but non-int-castable → selected_candidate is None."""
        dispatcher, *_ = _make_dispatcher()
        request = SchedulerRequest(
            request_type=SchedulerRequestType.ALLOCATE_ONLY,
            request_id="req-a4",
            data={
                "instance_id": "not_an_int",
                "endpoint_id": "also_not_an_int",
                "role": PDRole.ROLE_P.value,
                "workload": Workload().model_dump(mode="json"),
            },
        )
        response = await dispatcher.dispatch(request)
        assert response.response_type == SchedulerResponseType.SUCCESS
        assert response.data["instance"] is None
        assert response.data["endpoint"] is None

    @pytest.mark.asyncio
    async def test_instance_not_in_pool_returns_success_with_none(self):
        """Candidate IDs valid but not in the instance pool → authoritative select returns None."""
        dispatcher, *_ = _make_dispatcher(scheduler_type=SchedulerType.LOAD_BALANCE)
        # Do NOT add any instances to the pool
        request = SchedulerRequest(
            request_type=SchedulerRequestType.ALLOCATE_ONLY,
            request_id="req-a5",
            data={
                "instance_id": 999,
                "endpoint_id": 9999,
                "role": PDRole.ROLE_P.value,
                "workload": Workload(active_tokens=1).model_dump(mode="json"),
            },
        )
        response = await dispatcher.dispatch(request)
        assert response.response_type == SchedulerResponseType.SUCCESS
        assert response.data["instance"] is None
        assert response.data["endpoint"] is None

    @pytest.mark.asyncio
    async def test_update_workload_false_returns_success_with_none(self):
        """When scheduler.update_workload returns False, response data has None instance/endpoint."""
        writer = _DummyWorkloadWriter(sequence=0, instance_version=1)
        dispatcher, instance_manager, scheduler, _ = _make_dispatcher(
            scheduler_type=SchedulerType.LOAD_BALANCE,
            workload_writer=writer,
        )
        inst = _make_instance(1, (10,), PDRole.ROLE_P)
        await instance_manager.refresh_instances(EventType.ADD, [inst])

        # Force the synchronous commit path to return False.
        scheduler.update_workload_sync = MagicMock(return_value=(False, None, None))

        request = SchedulerRequest(
            request_type=SchedulerRequestType.ALLOCATE_ONLY,
            request_id="req-a6",
            data={
                "instance_id": 1,
                "endpoint_id": 10,
                "role": PDRole.ROLE_P.value,
                "req_id": "r-a6",
                "workload_sequence": writer.sequence,
                "instance_version": writer.instance_version,
                "workload": Workload(active_tokens=2).model_dump(mode="json"),
            },
        )
        response = await dispatcher.dispatch(request)
        assert response.response_type == SchedulerResponseType.SUCCESS
        assert response.data["instance"] is None
        assert response.data["endpoint"] is None


class TestParseOptionalInt:
    def test_none_returns_none(self):
        assert _SchedulerRequestDispatcher._parse_optional_int(None) is None

    def test_valid_int_returns_int(self):
        assert _SchedulerRequestDispatcher._parse_optional_int(42) == 42

    def test_valid_string_returns_int(self):
        assert _SchedulerRequestDispatcher._parse_optional_int("7") == 7

    def test_invalid_string_returns_none(self):
        assert _SchedulerRequestDispatcher._parse_optional_int("abc") is None

    def test_float_truncated(self):
        assert _SchedulerRequestDispatcher._parse_optional_int(3.9) == 3


class TestExtractAllocateCandidate:
    def test_valid_fields_returns_tuple(self):
        result = _SchedulerRequestDispatcher._extract_allocate_candidate({"instance_id": "1", "endpoint_id": "10"})
        assert result == (1, 10)

    def test_missing_instance_id_returns_none(self):
        result = _SchedulerRequestDispatcher._extract_allocate_candidate({"endpoint_id": 10})
        assert result is None

    def test_missing_endpoint_id_returns_none(self):
        result = _SchedulerRequestDispatcher._extract_allocate_candidate({"instance_id": 1})
        assert result is None

    def test_non_castable_ids_returns_none(self):
        result = _SchedulerRequestDispatcher._extract_allocate_candidate({"instance_id": "bad", "endpoint_id": "val"})
        assert result is None


class TestExtractAllocateCandidates:
    def test_not_a_list_returns_empty(self):
        result = _SchedulerRequestDispatcher._extract_allocate_candidates({"candidates": "not_a_list"})
        assert result == []

    def test_missing_key_returns_empty(self):
        result = _SchedulerRequestDispatcher._extract_allocate_candidates({})
        assert result == []

    def test_valid_entries_parsed(self):
        result = _SchedulerRequestDispatcher._extract_allocate_candidates(
            {
                "candidates": [
                    {"instance_id": 1, "endpoint_id": 10},
                    {"instance_id": 2, "endpoint_id": 20},
                ]
            }
        )
        assert result == [(1, 10), (2, 20)]

    def test_invalid_entries_skipped(self):
        result = _SchedulerRequestDispatcher._extract_allocate_candidates(
            {
                "candidates": [
                    {"instance_id": 1, "endpoint_id": 10},
                    "not_a_dict",
                    {"instance_id": None, "endpoint_id": 20},
                    {"instance_id": "bad", "endpoint_id": "val"},
                ]
            }
        )
        assert result == [(1, 10)]


class TestCanUseWorkerTop1FastPath:
    def test_no_workload_writer_returns_false(self):
        dispatcher, *_ = _make_dispatcher(workload_writer=None)
        assert dispatcher._can_use_worker_top1_fast_path(0, None, 1, PDRole.ROLE_P) is False

    def test_none_sequence_returns_false(self):
        writer = _DummyWorkloadWriter(sequence=5, instance_version=3)
        dispatcher, *_ = _make_dispatcher(workload_writer=writer)
        assert dispatcher._can_use_worker_top1_fast_path(None, None, 3, PDRole.ROLE_P) is False

    def test_none_version_returns_false(self):
        writer = _DummyWorkloadWriter(sequence=5, instance_version=3)
        dispatcher, *_ = _make_dispatcher(workload_writer=writer)
        assert dispatcher._can_use_worker_top1_fast_path(5, None, None, PDRole.ROLE_P) is False

    def test_matching_sequence_and_version_returns_true(self):
        writer = _DummyWorkloadWriter(sequence=5, instance_version=3)
        dispatcher, *_ = _make_dispatcher(workload_writer=writer)
        assert dispatcher._can_use_worker_top1_fast_path(5, None, 3, PDRole.ROLE_P) is True

    def test_mismatched_sequence_returns_false(self):
        writer = _DummyWorkloadWriter(sequence=5, instance_version=3)
        dispatcher, *_ = _make_dispatcher(workload_writer=writer)
        assert dispatcher._can_use_worker_top1_fast_path(4, None, 3, PDRole.ROLE_P) is False

    def test_mismatched_version_returns_false(self):
        writer = _DummyWorkloadWriter(sequence=5, instance_version=3)
        dispatcher, *_ = _make_dispatcher(workload_writer=writer)
        assert dispatcher._can_use_worker_top1_fast_path(5, None, 2, PDRole.ROLE_P) is False

    def test_matching_role_sequence_ignores_global_sequence_mismatch(self):
        writer = _DummyWorkloadWriter(
            sequence=99,
            instance_version=3,
            role_sequences={PDRole.ROLE_P: 7, PDRole.ROLE_D: 11},
        )
        dispatcher, *_ = _make_dispatcher(workload_writer=writer)
        assert dispatcher._can_use_worker_top1_fast_path(5, 7, 3, PDRole.ROLE_P) is True

    def test_mismatched_role_sequence_returns_false(self):
        writer = _DummyWorkloadWriter(
            sequence=5,
            instance_version=3,
            role_sequences={PDRole.ROLE_P: 7, PDRole.ROLE_D: 11},
        )
        dispatcher, *_ = _make_dispatcher(workload_writer=writer)
        assert dispatcher._can_use_worker_top1_fast_path(5, 11, 3, PDRole.ROLE_P) is False


class TestShouldScanGlobalLoadBalance:
    def test_lb_policy_returns_true(self):
        dispatcher, *_ = _make_dispatcher(scheduler_type=SchedulerType.LOAD_BALANCE)
        assert dispatcher._should_scan_global_load_balance(CANDIDATE_POLICY_LOAD_BALANCE) is True

    def test_kv_affinity_policy_returns_false(self):
        dispatcher, *_ = _make_dispatcher(scheduler_type=SchedulerType.LOAD_BALANCE)
        assert dispatcher._should_scan_global_load_balance(CANDIDATE_POLICY_KV_CACHE_AFFINITY) is False

    def test_none_policy_lb_scheduler_returns_true(self):
        """No candidate_policy specified → fall back to scheduler_type (LB → True)."""
        dispatcher, *_ = _make_dispatcher(scheduler_type=SchedulerType.LOAD_BALANCE)
        assert dispatcher._should_scan_global_load_balance(None) is True

    def test_none_policy_kv_scheduler_returns_false(self):
        """No candidate_policy specified → fall back to scheduler_type (KVA → False)."""
        dispatcher, *_ = _make_dispatcher(scheduler_type=SchedulerType.KV_CACHE_AFFINITY)
        assert dispatcher._should_scan_global_load_balance(None) is False

    def test_unknown_policy_lb_scheduler_returns_true(self):
        """Unknown candidate_policy falls back to scheduler_type (LB → True)."""
        dispatcher, *_ = _make_dispatcher(scheduler_type=SchedulerType.LOAD_BALANCE)
        assert dispatcher._should_scan_global_load_balance("unknown_policy_xyz") is True

    def test_unknown_policy_kv_scheduler_returns_false(self):
        """Unknown candidate_policy falls back to scheduler_type (KVA → False)."""
        dispatcher, *_ = _make_dispatcher(scheduler_type=SchedulerType.KV_CACHE_AFFINITY)
        assert dispatcher._should_scan_global_load_balance("unknown_policy_xyz") is False


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
        mock_shm = MagicMock()
        mock_pub = MagicMock()
        mock_disconnect = AsyncMock()
        mock_transport = AsyncMock()
        mock_transport.disconnect = mock_disconnect
        mock_context = MagicMock()

        server._workload_writer = mock_writer
        server._workload_shm = mock_shm
        server._pub_socket = mock_pub
        server._transport = mock_transport
        server.context = mock_context

        await server.stop()

        mock_writer.release.assert_called_once()
        mock_shm.close.assert_called_once()
        mock_shm.unlink.assert_called_once()
        mock_pub.close.assert_called_once()
        mock_disconnect.assert_called_once()
        mock_context.term.assert_called_once()
        assert server._workload_writer is None
        assert server._workload_shm is None
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
    async def test_stop_swallows_shm_close_error(self):
        server = _make_server()

        mock_shm = MagicMock()
        mock_shm.close.side_effect = Exception("close error")
        mock_shm.unlink.side_effect = Exception("unlink error")
        server._workload_writer = MagicMock()
        server._workload_shm = mock_shm
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
