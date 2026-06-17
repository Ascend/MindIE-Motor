# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from motor.config.coordinator import CoordinatorConfig, TokenSamplingConfig
from motor.config.tls_config import TLSConfig
from motor.coordinator.domain import InstanceReadiness
from motor.coordinator.domain.request_manager import RequestManager
from motor.coordinator.fault_tolerance.precision import build_precision_reporter
from motor.coordinator.fault_tolerance.precision.checker import CheckResult, PrecisionChecker
from motor.coordinator.fault_tolerance.precision.sample_controller import DecodeSample

# ---------------------------------------------------------------------------
# StubChecker — inline test double (no production code coupling)
# ---------------------------------------------------------------------------
_PRECISION_DEBUG_RESULTS: list[bool] = [True, True, True, True, True, True, True, True, True, True, True, False]
_precision_debug_index: int = 0


class StubChecker(PrecisionChecker):
    """Sequential bool stub for integration tests; fail-open when exhausted."""

    def __init__(
        self,
        results: list[bool] | None = None,
        *,
        use_module_debug_sequence: bool = False,
    ) -> None:
        self._results = list(results) if results is not None else []
        self._use_module = use_module_debug_sequence
        self._index = 0

    async def check(
        self,
        prompt_token_ids: list[int],
        output_token_ids: list[int],
        logprobs: list[float],
        *,
        topk_logprobs: list[dict[int, float]] | None = None,
        model: str | None = None,
    ) -> CheckResult:
        global _precision_debug_index
        _ = (prompt_token_ids, output_token_ids, logprobs, topk_logprobs, model)
        try:
            if self._use_module:
                n = len(_PRECISION_DEBUG_RESULTS)
                if n == 0 or _precision_debug_index >= n:
                    return CheckResult(has_issue=False)

                has_issue = bool(_PRECISION_DEBUG_RESULTS[_precision_debug_index])
                _precision_debug_index += 1
                return CheckResult(has_issue=has_issue)

            n = len(self._results)
            if n == 0 or self._index >= n:
                return CheckResult(has_issue=False)
            has_issue = bool(self._results[self._index])
            self._index += 1
            return CheckResult(has_issue=has_issue)
        except Exception:
            return CheckResult(has_issue=False)


def reset_stub_debug_sequence(results: list[bool] | None = None) -> None:
    """Reset module-level debug sequence (for tests)."""
    global _precision_debug_index, _PRECISION_DEBUG_RESULTS
    if results is not None:
        _PRECISION_DEBUG_RESULTS = list(results)
    _precision_debug_index = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_rep(cfg: TokenSamplingConfig, **kwargs):
    sched = AsyncMock()
    sched.has_required_instances = AsyncMock(return_value=InstanceReadiness.REQUIRED_MET)
    return build_precision_reporter(
        cfg,
        TLSConfig(),
        config=CoordinatorConfig(),
        scheduler=sched,
        request_manager=RequestManager(CoordinatorConfig()),
        **kwargs,
    )


def _sample(**kwargs) -> DecodeSample:
    base = dict(
        p_instance_id=1,
        d_instance_id=2,
        prompt_token_ids=[],
        output_token_ids=[],
        logprobs=[],
        req_id="req",
        extra={"d_infer_base_url": "http://127.0.0.1:9", "model": "m"},
    )
    base.update(kwargs)
    return DecodeSample(**base)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stub_sequential_consumption_and_exhausted_fail_open() -> None:
    stub = StubChecker(results=[True, False, True])
    assert (await stub.check([], [], [])).has_issue is True
    assert (await stub.check([], [], [])).has_issue is False
    assert (await stub.check([], [], [])).has_issue is True
    assert (await stub.check([], [], [])).has_issue is False
    assert (await stub.check([], [], [])).has_issue is False


@pytest.mark.asyncio
async def test_false_resets_consecutive_streak() -> None:
    reset_stub_debug_sequence([True, True, False, True])
    cfg = TokenSamplingConfig(
        precision_check_enabled=True,
        precision_issue_threshold=3,
        probe_max_attempts=1,
        probe_timeout_seconds=1.0,
    )
    rep = _build_rep(cfg, checker=StubChecker(use_module_debug_sequence=True))
    rep._action.execute = AsyncMock()

    await rep.handle(_sample(req_id="a"))
    await rep.handle(_sample(req_id="b"))
    assert rep._counter.get_count((1, 2)) == 2
    await rep.handle(_sample(req_id="c"))
    assert rep._counter.get_count((1, 2)) == 0
    rep._action.execute.assert_not_called()


@pytest.mark.asyncio
async def test_ten_trues_triggers_probe_once() -> None:
    reset_stub_debug_sequence([True] * 10)
    cfg = TokenSamplingConfig(
        precision_check_enabled=True,
        precision_issue_threshold=10,
        probe_max_attempts=3,
        probe_timeout_seconds=1.0,
    )
    rep = _build_rep(cfg, checker=StubChecker(use_module_debug_sequence=True))
    rep._action.execute = AsyncMock()

    for i in range(9):
        await rep.handle(_sample(req_id=str(i)))
    assert rep._action.execute.await_count == 0

    await rep.handle(_sample(req_id="9"))
    for _ in range(50):
        if rep._action.execute.await_count:
            break
        await asyncio.sleep(0.01)
    assert rep._action.execute.await_count == 1


def test_no_top_level_controller_client_import_in_precision_alarm_module() -> None:
    """Controller client is lazy-imported inside PrecisionAlarm.execute."""
    import motor.coordinator.fault_tolerance.alarm.precision_alarm as pa_mod

    assert "ControllerApiClient" not in pa_mod.__dict__


@pytest.mark.asyncio
async def test_module_stub_debug_sequence() -> None:
    reset_stub_debug_sequence([True, False])
    stub = StubChecker(use_module_debug_sequence=True)
    assert (await stub.check([], [], [])).has_issue is True
    assert (await stub.check([], [], [])).has_issue is False
    assert _precision_debug_index == 2
