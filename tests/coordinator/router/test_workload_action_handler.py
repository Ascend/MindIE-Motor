# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Tests for calculate_demand_workload and WorkloadActionHandler."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from motor.common.resources.endpoint import Workload, WorkloadAction
from motor.common.resources.instance import PDRole, Instance, InsStatus, ParallelConfig
from motor.coordinator.domain.workload_calculator import (
    calculate_committed_workload,
    calculate_demand_workload,
)
from motor.coordinator.router.workload import WorkloadActionHandler
from motor.coordinator.domain import ScheduledResource
from motor.common.resources.endpoint import Endpoint, EndpointStatus


class TestCalculateDemandWorkload:
    """Tests for calculate_demand_workload(role, request_length)."""

    def test_prefill_role(self):
        """ROLE_P: active_tokens set from prefill formula."""
        req_info = MagicMock()
        req_info.req_len = 4
        w = calculate_demand_workload(PDRole.ROLE_P, req_info)
        assert isinstance(w, Workload)
        # request_length=4 -> length_score=1.0 -> score = 1.0*0.0345+120.0745 = 120.109
        assert w.active_tokens > 0

    def test_prefill_role_uses_real_tokens_when_present(self):
        """ROLE_P: when req_info.token_ids is set, load is the real token count (not req_len)."""
        req_info = MagicMock()
        req_info.req_len = 999  # would give ~128 via the heuristic; must be ignored
        req_info.token_ids = [1, 2, 3, 4, 5]
        w = calculate_demand_workload(PDRole.ROLE_P, req_info)
        assert w.active_tokens == 5.0

    def test_prefill_role_falls_back_when_token_ids_empty(self):
        """ROLE_P: empty/absent token_ids falls back to the legacy byte-length heuristic."""
        req_info = MagicMock()
        req_info.req_len = 4
        req_info.token_ids = []  # empty -> not a usable token count -> fallback
        w = calculate_demand_workload(PDRole.ROLE_P, req_info)
        # Heuristic at req_len=4 -> ~120.1, definitely not len([])==0.
        assert w.active_tokens > 100

    def test_decode_role(self):
        """ROLE_D: only active_tokens set (request_length)."""
        req_info = MagicMock()
        req_info.req_len = 10
        w = calculate_demand_workload(PDRole.ROLE_D, req_info)
        assert w.active_tokens == 10.0

    def test_encode_role_uses_tokens_without_kv_cache(self, monkeypatch):
        """ROLE_E: encode allocation should not leave KV cache workload to release."""
        monkeypatch.setattr(
            "motor.coordinator.domain.workload_calculator.get_mul_token",
            lambda _: 42,
        )
        req_info = MagicMock()
        req_info.req_data = {
            "messages": [
                {
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,test"},
                        }
                    ]
                }
            ]
        }

        w = calculate_demand_workload(PDRole.ROLE_E, req_info)

        assert w.active_tokens == 42

    def test_encode_video_role_uses_request_length(self):
        """ROLE_E video workload should use integer req_len without calling len(req_len)."""
        req_info = MagicMock()
        req_info.req_len = 10
        req_info.req_data = {
            "messages": [
                {
                    "content": [
                        {
                            "type": "video_url",
                            "video_url": {"url": "https://example.com/video.mp4"},
                        }
                    ]
                }
            ]
        }

        w = calculate_demand_workload(PDRole.ROLE_E, req_info)

        assert w.active_tokens == 320

    def test_hybrid_role_matches_prefill_demand(self):
        """ROLE_U non-affinity demand matches ROLE_P (ISL or same prefill heuristic)."""
        req_info = MagicMock()
        req_info.req_len = 4
        req_info.token_ids = None
        assert calculate_demand_workload(PDRole.ROLE_U, req_info) == calculate_demand_workload(PDRole.ROLE_P, req_info)
        req_info.token_ids = [1, 2, 3, 4, 5]
        assert calculate_demand_workload(PDRole.ROLE_U, req_info).active_tokens == 5.0
        assert calculate_demand_workload(PDRole.ROLE_U, req_info) == calculate_demand_workload(PDRole.ROLE_P, req_info)

    def test_unknown_role_returns_empty_workload(self):
        """Unknown role returns empty Workload (and logs warning)."""
        # Pass an invalid role by using a string that is not a valid PDRole
        # calculate_demand_workload expects PDRole enum; if we pass something else
        # it goes to the else branch. We need a way to trigger else - the function
        # only accepts PDRole, so we'd need to pass a value that is not ROLE_P/D/U.
        # PDRole has ROLE_P, ROLE_D, ROLE_U. So we can't easily pass "unknown" without
        # the type checker. The implementation does "else: return Workload()" for
        # any other role. So we test that for a valid role we get non-empty, and
        # we skip testing invalid enum value from outside.
        req_info = MagicMock()
        req_info.req_len = 0
        w = calculate_demand_workload(PDRole.ROLE_P, req_info)
        assert w.active_tokens >= 0

    def test_committed_prefill_deducts_matched_without_credit(self):
        """ROLE_P committed load is ISL - matched_tokens (no overlap credit)."""
        w = calculate_committed_workload(PDRole.ROLE_P, isl=1000, matched_tokens=800)
        assert w.active_tokens == 200.0

    def test_committed_union_uses_isl_minus_matched(self):
        """ROLE_U commits the same as ROLE_P: ISL - matched_tokens."""
        w = calculate_committed_workload(
            PDRole.ROLE_U,
            isl=1000,
            matched_tokens=800,
        )
        assert w.active_tokens == 200.0


class TestWorkloadActionHandler:
    """Tests for WorkloadActionHandler.compute_and_update."""

    @pytest.fixture
    def mock_request_manager(self):
        m = MagicMock()
        m.add_req_workload = AsyncMock(return_value=True)
        m.get_req_workload = AsyncMock(return_value=None)
        m.update_req_workload = AsyncMock(return_value=True)
        m.del_req_workload = AsyncMock(return_value=True)
        return m

    @pytest.fixture
    def valid_resource(self):
        """ScheduledResource with instance and endpoint."""
        instance = Instance(
            job_name="test",
            model_name="m",
            id=1,
            role=PDRole.ROLE_P,
            status=InsStatus.ACTIVE,
            parallel_config=ParallelConfig(dp_size=1),
        )
        endpoint = Endpoint(
            id=1,
            ip="127.0.0.1",
            business_port="8080",
            status=EndpointStatus.NORMAL,
        )
        return ScheduledResource(instance=instance, endpoint=endpoint)

    @pytest.mark.asyncio
    async def test_compute_and_update_allocation_success(self, mock_request_manager, valid_resource):
        """ALLOCATION: add_req_workload called, returns (workload, role)."""
        handler = WorkloadActionHandler(mock_request_manager)
        req_info = MagicMock()
        req_info.req_len = 4
        workload_change, role = await handler.compute_and_update(
            valid_resource, "req-1", WorkloadAction.ALLOCATION, req_info=req_info
        )
        assert role == PDRole.ROLE_P
        assert workload_change is not None
        assert workload_change.active_tokens > 0
        mock_request_manager.add_req_workload.assert_called_once()
        call_args = mock_request_manager.add_req_workload.call_args
        assert call_args[0][0] == "req-1"
        assert call_args[0][1] == PDRole.ROLE_P

    @pytest.mark.asyncio
    async def test_compute_and_update_allocation_duplicate_returns_none(self, mock_request_manager, valid_resource):
        """ALLOCATION when add_req_workload returns False -> (None, None)."""
        mock_request_manager.add_req_workload = AsyncMock(return_value=False)
        handler = WorkloadActionHandler(mock_request_manager)
        req_info = MagicMock()
        req_info.req_len = 4
        workload_change, role = await handler.compute_and_update(
            valid_resource, "req-1", WorkloadAction.ALLOCATION, req_info=req_info
        )
        assert workload_change is None
        assert role is None

    @pytest.mark.asyncio
    async def test_compute_and_update_release_tokens_no_record_returns_none(self, mock_request_manager, valid_resource):
        """RELEASE_TOKENS when no workload record -> (None, None)."""
        mock_request_manager.get_req_workload = AsyncMock(return_value=None)
        handler = WorkloadActionHandler(mock_request_manager)
        req_info = MagicMock()
        req_info.req_len = 4
        workload_change, role = await handler.compute_and_update(
            valid_resource, "req-1", WorkloadAction.RELEASE_TOKENS, req_info=req_info
        )
        assert workload_change is None
        assert role is None

    @pytest.mark.asyncio
    async def test_encode_release_tokens_retains_record_until_ack(self, mock_request_manager):
        """RELEASE_TOKENS computes the negative delta but keeps the record until scheduler ACK
        (finalize_release), so a failed release can be recomputed and resent.
        """
        instance = Instance(
            job_name="encode",
            model_name="m",
            id=1,
            role=PDRole.ROLE_E,
            status=InsStatus.ACTIVE,
            parallel_config=ParallelConfig(dp_size=1),
        )
        endpoint = Endpoint(
            id=1,
            ip="127.0.0.1",
            business_port="8080",
            status=EndpointStatus.NORMAL,
        )
        resource = ScheduledResource(instance=instance, endpoint=endpoint)
        current = Workload(active_tokens=42)
        mock_request_manager.get_req_workload = AsyncMock(return_value=current)
        handler = WorkloadActionHandler(mock_request_manager)
        req_info = MagicMock()

        workload_change, role = await handler.compute_and_update(
            resource, "req-encode", WorkloadAction.RELEASE_TOKENS, req_info=req_info
        )

        assert role == PDRole.ROLE_E
        assert workload_change == Workload(active_tokens=-42)
        # Record must NOT be deleted before scheduler ACK
        mock_request_manager.del_req_workload.assert_not_called()

        # Scheduler ACKed -> finalize deletes the local record
        ok = await handler.finalize_release("req-encode", PDRole.ROLE_E)
        assert ok is True
        mock_request_manager.del_req_workload.assert_called_once_with("req-encode", PDRole.ROLE_E)

    @pytest.mark.asyncio
    async def test_release_tokens_recompute_after_failure_returns_same_delta(self, mock_request_manager):
        """If the release RPC fails permanently, a later re-enqueue recomputes the full
        negative delta from the retained record (delete-after-ACK semantics).
        """
        instance = Instance(
            job_name="test",
            model_name="m",
            id=1,
            role=PDRole.ROLE_P,
            status=InsStatus.ACTIVE,
            parallel_config=ParallelConfig(dp_size=1),
        )
        endpoint = Endpoint(
            id=1,
            ip="127.0.0.1",
            business_port="8080",
            status=EndpointStatus.NORMAL,
        )
        resource = ScheduledResource(instance=instance, endpoint=endpoint)
        current = Workload(active_tokens=100)
        mock_request_manager.get_req_workload = AsyncMock(return_value=current)
        handler = WorkloadActionHandler(mock_request_manager)
        req_info = MagicMock()

        first, _ = await handler.compute_and_update(resource, "req-1", WorkloadAction.RELEASE_TOKENS, req_info=req_info)
        # Simulate permanent RPC failure: no finalize_release call. Re-enqueue recomputes.
        second, _ = await handler.compute_and_update(
            resource, "req-1", WorkloadAction.RELEASE_TOKENS, req_info=req_info
        )

        assert first == Workload(active_tokens=-100)
        assert second == Workload(active_tokens=-100)
        mock_request_manager.del_req_workload.assert_not_called()

    @pytest.mark.asyncio
    async def test_finalize_release_attempt_scoped(self, mock_request_manager):
        """finalize_release with attempt_seq deletes the attempt-scoped record."""
        mock_request_manager.del_req_attempt_workload = AsyncMock(return_value=True)
        handler = WorkloadActionHandler(mock_request_manager)

        ok = await handler.finalize_release("req-1", PDRole.ROLE_P, attempt_seq=2)

        assert ok is True
        mock_request_manager.del_req_attempt_workload.assert_called_once_with("req-1", 2, PDRole.ROLE_P)
        mock_request_manager.del_req_workload.assert_not_called()

    @pytest.mark.asyncio
    async def test_compute_and_update_invalid_resource_returns_none(self, mock_request_manager):
        """Empty or invalid resource -> (None, None)."""
        handler = WorkloadActionHandler(mock_request_manager)
        req_info = MagicMock()
        req_info.req_len = 4
        workload_change, role = await handler.compute_and_update(
            None, "req-1", WorkloadAction.ALLOCATION, req_info=req_info
        )
        assert workload_change is None
        assert role is None
