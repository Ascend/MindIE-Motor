# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.

"""Tests for BaseRouter._build_request_timeout (bounded TCP connect phase)."""

import time
from unittest.mock import MagicMock

import pytest

from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.domain import (
    ScheduledResource,  # noqa: F401 -- domain must import first (models.request <-> domain cycle)
)
from motor.coordinator.models.request import RequestInfo, ReqState
from motor.coordinator.router.strategies.base import BaseRouter


class _TestRouter(BaseRouter):
    async def handle_request(self):
        return None


def _make_router(config: CoordinatorConfig | None = None) -> _TestRouter:
    req_info = RequestInfo(
        req_id="req-1",
        req_data={"messages": []},
        req_len=2,
        api="/v1/chat/completions",
    )
    return _TestRouter(
        req_info,
        config or CoordinatorConfig(),
        scheduler=MagicMock(),
        request_manager=MagicMock(),
    )


class TestBuildRequestTimeout:
    """connect_timeout > 0 bounds only the connect phase; <= 0 keeps single-value behavior."""

    def test_connect_timeout_bounds_connect_phase(self):
        """connect=5s while the overall (read) timeout stays 30s."""
        router = _make_router()
        timeout = router._build_request_timeout(30)

        assert timeout.connect == 5.0
        assert timeout.read == 30.0

    def test_connect_timeout_zero_inherits_single_value(self):
        """connect_timeout=0 keeps the historical single-value timeout."""
        config = CoordinatorConfig()
        config.exception_config.connect_timeout = 0
        router = _make_router(config)

        timeout = router._build_request_timeout(30)

        assert timeout.connect == 30.0
        assert timeout.read == 30.0


class TestStreamOverallTimeout:
    """_stream_overall_timeout: remaining infer_timeout budget for streaming responses."""

    def test_full_budget_when_just_arrived(self):
        config = CoordinatorConfig()
        config.exception_config.infer_timeout = 3600
        router = _make_router(config)

        budget = router._stream_overall_timeout()

        assert 3599 < budget <= 3600

    def test_elapsed_time_deducted_from_budget(self):
        config = CoordinatorConfig()
        config.exception_config.infer_timeout = 100
        router = _make_router(config)
        router.req_info.status[ReqState.ARRIVE] = time.time() - 40

        assert router._stream_overall_timeout() == pytest.approx(60, abs=1)

    def test_budget_floors_at_zero_after_deadline(self):
        config = CoordinatorConfig()
        config.exception_config.infer_timeout = 100
        router = _make_router(config)
        router.req_info.status[ReqState.ARRIVE] = time.time() - 200

        assert router._stream_overall_timeout() == 0.0

    def test_missing_arrive_time_uses_now(self):
        config = CoordinatorConfig()
        config.exception_config.infer_timeout = 100
        router = _make_router(config)
        router.req_info.status.pop(ReqState.ARRIVE, None)

        assert router._stream_overall_timeout() == pytest.approx(100, abs=1)
