# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Dispatch attempts own independent state and drain their asynchronous work on cancellation."""

import asyncio
from unittest.mock import Mock, call

import pytest

from motor.common.resources.endpoint import Endpoint
from motor.common.resources.instance import Instance
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.domain.scheduling import ScheduledResource
from motor.coordinator.router import dispatch_session
from motor.coordinator.router.dispatch_session import AttemptContext, AttemptState, AttemptStopReason, PDDispatchSession


@pytest.fixture
def attempt() -> AttemptContext:
    """Use actual resource models; only the external HTTP pool is mocked by tests."""
    prefill = ScheduledResource(
        instance=Instance(id=1, job_name="prefill", model_name="test", role="prefill"),
        endpoint=Endpoint(id=0, ip="192.0.2.1", business_port="8000"),
    )
    decode = ScheduledResource(
        instance=Instance(id=2, job_name="decode", model_name="test", role="decode"),
        endpoint=Endpoint(id=0, ip="192.0.2.2", business_port="8001"),
    )
    return PDDispatchSession("request-1").new_attempt(prefill, decode, CoordinatorConfig())


def test_new_attempt_isolates_retry_state(attempt: AttemptContext) -> None:
    session = PDDispatchSession("request-1")
    first = session.new_attempt(attempt.prefill_resource, None, attempt.config)
    second = session.new_attempt(None, attempt.decode_resource, attempt.config)
    assert (first.attempt_seq, second.attempt_seq) == (1, 2)
    assert first.root_request_id == second.root_request_id == "request-1"
    assert first.pair_id != second.pair_id
    assert len(first.pair_id) == len(second.pair_id) == 32
    first.release_flags.prefill_tokens = True
    first.prefill_completed_event.set()
    first.trigger_prompt(0).mark_dispatched()
    assert second.release_flags.prefill_tokens is False
    assert not second.prefill_completed_event.is_set()
    assert second.trigger_prompts == {}
    assert first.stop_lock is not second.stop_lock
    assert second.prefill_resource is None
    assert second.decode_resource is attempt.decode_resource


@pytest.mark.parametrize("terminal", [AttemptState.DONE, AttemptState.STOPPED])
def test_terminal_attempt_cannot_be_reactivated(attempt: AttemptContext, terminal: AttemptState) -> None:
    for state in (AttemptState.DISPATCHING, AttemptState.ACTIVE, AttemptState.FIRST_VISIBLE, terminal):
        assert attempt.transition(state) is True
        assert attempt.state is state
    for state in AttemptState:
        assert attempt.transition(state) is (state is terminal)
        assert attempt.state is terminal
    attempt.stop()
    assert attempt.state is terminal


def test_stopping_attempt_accepts_only_stop_completion(attempt: AttemptContext) -> None:
    attempt.stop()
    assert attempt.state is AttemptState.STOPPING
    assert attempt.transition(AttemptState.STOPPING) is True
    assert attempt.transition(AttemptState.ACTIVE) is False
    assert attempt.transition(AttemptState.DONE) is False
    assert attempt.state is AttemptState.STOPPING
    assert attempt.transition(AttemptState.STOPPED) is True


@pytest.mark.parametrize("role", ["prefill", "decode"])
def test_only_dispatched_unfinished_roles_need_abort(
    attempt: AttemptContext, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    monkeypatch.setattr(dispatch_session.time, "perf_counter", lambda: 123.0)
    assert attempt.needs_abort(role) is False
    attempt.mark_dispatched(role)
    assert attempt.needs_abort(role) is True
    attempt.mark_completed(role)
    assert attempt.needs_abort(role) is False
    if role == "prefill":
        assert attempt.prefill_completed_at == 123.0
        assert attempt.prefill_completed_event.is_set()
    else:
        assert not attempt.prefill_completed_event.is_set()


def test_trigger_prompts_track_completion_independently(
    attempt: AttemptContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dispatch_session.time, "perf_counter", lambda: 456.0)
    first, second = attempt.trigger_prompt(None), attempt.trigger_prompt(1)
    assert attempt.trigger_prompt(None) is first
    assert first.prefill_completed_event is not second.prefill_completed_event
    assert first.lock is not second.lock
    assert not attempt.needs_abort("prefill")
    first.mark_dispatched()
    second.mark_dispatched()
    first.mark_completed()
    assert first.prefill_completed_at == 456.0
    assert first.prefill_completed_event.is_set()
    assert not second.prefill_completed_event.is_set()
    assert attempt.needs_abort("prefill")
    second.mark_completed()
    assert not attempt.needs_abort("prefill")
    attempt.reset_trigger_prompts()
    assert attempt.trigger_prompts == {}
    fresh = attempt.trigger_prompt(None)
    assert fresh is not first
    assert not fresh.prefill_dispatched
    assert not fresh.prefill_completed_event.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [reason.value for reason in AttemptStopReason])
async def test_cancel_drains_prefill_decode_and_trigger_tasks(attempt: AttemptContext, reason: str) -> None:
    started = [asyncio.Event() for _ in range(3)]
    cancelled = {}

    async def pending(index: int) -> None:
        started[index].set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            cancelled[index] = exc.args
            raise

    tasks = [asyncio.create_task(pending(index)) for index in range(3)]
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), timeout=1)
        assert attempt.register_prefill_task(tasks[0]) is tasks[0]
        assert attempt.register_decode_task(tasks[1]) is tasks[1]
        attempt.trigger_prompt(0).prefill_task = tasks[0]
        attempt.trigger_prompt(1).prefill_task = tasks[2]
        attempt.trigger_prompt(2)  # An unstarted prompt must not prevent draining the others.
        await asyncio.wait_for(attempt.cancel(reason), timeout=1)
        assert all(task.cancelled() for task in tasks)
        assert cancelled == {index: (reason,) for index in range(3)}
        await attempt.cancel(reason)  # Cancellation of already drained tasks is idempotent.
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_does_not_cancel_calling_task_or_finished_result(attempt: AttemptContext) -> None:
    async def completed() -> str:
        return "response"

    finished = asyncio.create_task(completed())
    assert await finished == "response"
    attempt.register_prefill_task(asyncio.current_task())
    attempt.register_decode_task(finished)
    attempt.trigger_prompt(0).prefill_task = asyncio.current_task()
    attempt.trigger_prompt(1).prefill_task = finished
    await attempt.cancel(AttemptStopReason.PEER_FAILED.value)
    assert not asyncio.current_task().cancelling()
    assert finished.result() == "response"


@pytest.mark.asyncio
async def test_cancel_drains_tasks_even_when_cleanup_raises(attempt: AttemptContext) -> None:
    started = asyncio.Event()

    async def failing_cleanup() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            raise RuntimeError("cleanup failed")

    task = attempt.register_decode_task(asyncio.create_task(failing_cleanup()))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.wait_for(attempt.cancel("timeout"), timeout=1)
        assert task.done()
        assert isinstance(task.exception(), RuntimeError)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("has_prefill,has_decode", [(False, False), (True, False), (False, True), (True, True)])
def test_cancellers_follow_allocated_resources(
    attempt: AttemptContext, monkeypatch: pytest.MonkeyPatch, has_prefill: bool, has_decode: bool
) -> None:
    pool = Mock()
    pool._get_pool_key.side_effect = lambda ip, port, tls: (ip, port)
    monkeypatch.setattr(dispatch_session, "HTTPClientPool", lambda: pool)
    if not has_prefill:
        attempt.prefill_resource = None
    if not has_decode:
        attempt.decode_resource = None
    keys = []
    if has_prefill:
        keys.append(("192.0.2.1", "8000"))
    if has_decode:
        keys.append(("192.0.2.2", "8001"))
    attempt.register_canceller()
    assert pool.register_canceller.call_args_list == [call(key, attempt.pair_id, attempt.cancel) for key in keys]
    attempt.unregister_canceller()
    assert pool.unregister_canceller.call_args_list == [call(key, attempt.pair_id) for key in keys]
    for key_call in pool._get_pool_key.call_args_list:
        assert key_call.args[2] is attempt.config.infer_tls_config
    pool.reset_mock()
    attempt.unregister_prefill_canceller()
    assert pool.unregister_canceller.call_args_list == (
        [call(("192.0.2.1", "8000"), attempt.pair_id)] if has_prefill else []
    )
    attempt.register_decode_canceller()
    assert pool.register_canceller.call_args_list == (
        [call(("192.0.2.2", "8001"), attempt.pair_id, attempt.cancel)] if has_decode else []
    )


@pytest.mark.parametrize("method", ["unregister_canceller", "unregister_prefill_canceller"])
def test_unregister_is_best_effort_when_pool_is_unavailable(
    attempt: AttemptContext, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    pool_factory = Mock(side_effect=RuntimeError("pool closed"))
    monkeypatch.setattr(dispatch_session, "HTTPClientPool", pool_factory)
    getattr(attempt, method)()
    pool_factory.assert_called_once_with()
    assert attempt.state is AttemptState.CREATED
