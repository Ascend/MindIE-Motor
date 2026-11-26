# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
import signal
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from unittest.mock import MagicMock, call, patch

import pytest
import requests

from motor.node_manager.core.services.native_engine.models import CommandSpec, ProbeSpec, RuntimeState
from motor.node_manager.core.services.native_engine.supervisor import ProcessSupervisor


def _command() -> CommandSpec:
    return CommandSpec(argv=("vllm", "serve"), env={"KEY": "value"})


def _probe(
    *,
    startup_timeout: float = 1800,
    max_attempts: int = 1,
    process_only: bool = False,
) -> ProbeSpec:
    return ProbeSpec(
        path="/health",
        timeout_seconds=5,
        startup_timeout_seconds=startup_timeout,
        max_attempts=max_attempts,
        process_only=process_only,
    )


def test_probe_rejects_non_positive_attempt_limit():
    with pytest.raises(ValueError, match="max_attempts must be a positive integer"):
        _probe(max_attempts=0)


@patch("motor.node_manager.core.services.native_engine.supervisor.subprocess.Popen")
def test_start_owns_native_process_group(mock_popen):
    process = MagicMock(pid=12345)
    process.poll.return_value = None
    mock_popen.return_value = process
    supervisor = ProcessSupervisor()

    started = supervisor.start(3, _command(), _probe())

    assert started is True
    assert supervisor.pid_list() == [12345]
    assert mock_popen.call_args.kwargs["start_new_session"] is True


@patch("motor.node_manager.core.services.native_engine.supervisor.subprocess.Popen")
def test_repeated_start_is_idempotent(mock_popen):
    process = MagicMock(pid=12345)
    process.poll.return_value = None
    mock_popen.return_value = process
    supervisor = ProcessSupervisor()

    assert supervisor.start(3, _command(), _probe()) is True
    assert supervisor.start(3, _command(), _probe()) is False

    mock_popen.assert_called_once()
    assert supervisor.pid_list() == [12345]


@patch("motor.node_manager.core.services.native_engine.supervisor.subprocess.Popen")
def test_repeated_start_rejects_different_launch_spec_without_stopping_existing(mock_popen):
    process = MagicMock(pid=12345)
    process.poll.return_value = None
    mock_popen.return_value = process
    supervisor = ProcessSupervisor()
    supervisor.start(3, _command(), _probe())

    different_command = CommandSpec(argv=("vllm", "serve", "--port", "9000"), env={"KEY": "value"})
    with pytest.raises(RuntimeError, match="different launch spec"):
        supervisor.start(3, different_command, _probe())

    mock_popen.assert_called_once()
    assert supervisor.pid_list() == [12345]


@patch("motor.node_manager.core.services.native_engine.supervisor.subprocess.Popen")
def test_concurrent_start_creates_only_one_process(mock_popen):
    process = MagicMock(pid=12345)
    process.poll.return_value = None
    second_popen_entered = threading.Event()
    popen_calls = 0
    calls_lock = threading.Lock()

    def popen(*_args, **_kwargs):
        nonlocal popen_calls
        with calls_lock:
            popen_calls += 1
            call_number = popen_calls
        if call_number == 1:
            second_popen_entered.wait(timeout=0.1)
        else:
            second_popen_entered.set()
        return process

    mock_popen.side_effect = popen
    supervisor = ProcessSupervisor()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: supervisor.start(3, _command(), _probe()), range(2)))

    assert sorted(results) == [False, True]
    assert mock_popen.call_count == 1
    assert supervisor.pid_list() == [12345]


@patch("motor.node_manager.core.services.native_engine.supervisor.SafeHTTPSClient")
@patch("motor.node_manager.core.services.native_engine.supervisor.subprocess.Popen")
def test_failed_probe_stays_starting_until_startup_timeout(mock_popen, mock_client):
    process = MagicMock(pid=12345)
    process.poll.return_value = None
    mock_popen.return_value = process
    mock_client.return_value.__enter__.return_value.do_get.side_effect = RuntimeError("not listening")
    supervisor = ProcessSupervisor()
    supervisor.start(3, _command(), _probe(startup_timeout=600))

    with patch("motor.node_manager.core.services.native_engine.supervisor.time.monotonic", return_value=100):
        supervisor._processes[3].started_at = 0
        state = supervisor.state(3, "10.0.0.1", 8000)

    assert state == RuntimeState.STARTING


@patch("motor.node_manager.core.services.native_engine.supervisor.SafeHTTPSClient")
@patch("motor.node_manager.core.services.native_engine.supervisor.subprocess.Popen")
def test_failed_probe_after_startup_timeout_is_unhealthy(mock_popen, mock_client):
    process = MagicMock(pid=12345)
    process.poll.return_value = None
    mock_popen.return_value = process
    mock_client.return_value.__enter__.return_value.do_get.side_effect = RuntimeError("not listening")
    supervisor = ProcessSupervisor()
    supervisor.start(3, _command(), _probe(startup_timeout=10))

    with patch("motor.node_manager.core.services.native_engine.supervisor.time.monotonic", return_value=11):
        supervisor._processes[3].started_at = 0
        state = supervisor.state(3, "10.0.0.1", 8000)

    assert state == RuntimeState.UNHEALTHY


@patch("motor.node_manager.core.services.native_engine.supervisor.SafeHTTPSClient")
@patch("motor.node_manager.core.services.native_engine.supervisor.subprocess.Popen")
def test_successful_native_probe_marks_ready(mock_popen, mock_client):
    process = MagicMock(pid=12345)
    process.poll.return_value = None
    mock_popen.return_value = process
    supervisor = ProcessSupervisor()
    supervisor.start(3, _command(), _probe())

    state = supervisor.state(3, "10.0.0.1", 8000)

    assert state == RuntimeState.READY
    mock_client.return_value.__enter__.return_value.do_get.assert_called_once_with("/health")


@patch("motor.node_manager.core.services.native_engine.supervisor.SafeHTTPSClient")
@patch("motor.node_manager.core.services.native_engine.supervisor.subprocess.Popen")
def test_snapshot_pending_probe_reports_running(mock_popen, mock_client):
    process = MagicMock(pid=12345)
    process.poll.return_value = None
    mock_popen.return_value = process
    response = mock_client.return_value.__enter__.return_value.do_get.return_value
    response.status_code = HTTPStatus.ACCEPTED
    supervisor = ProcessSupervisor()
    probe = ProbeSpec(
        path="/snapshot/health",
        timeout_seconds=5,
        startup_timeout_seconds=1800,
    )
    supervisor.start(3, _command(), probe)

    state = supervisor.state(3, "10.0.0.1", 8000)

    assert state == RuntimeState.RUNNING


@patch("motor.node_manager.core.services.native_engine.supervisor.SafeHTTPSClient")
@patch("motor.node_manager.core.services.native_engine.supervisor.subprocess.Popen")
def test_completed_snapshot_probe_marks_ready(mock_popen, mock_client):
    process = MagicMock(pid=12345)
    process.poll.return_value = None
    mock_popen.return_value = process
    response = mock_client.return_value.__enter__.return_value.do_get.return_value
    response.status_code = HTTPStatus.OK
    supervisor = ProcessSupervisor()
    probe = ProbeSpec(path="/snapshot/health", timeout_seconds=5, startup_timeout_seconds=1800)
    supervisor.start(3, _command(), probe)

    assert supervisor.state(3, "10.0.0.1", 8000) == RuntimeState.READY


@patch("motor.node_manager.core.services.native_engine.supervisor.SafeHTTPSClient")
@patch("motor.node_manager.core.services.native_engine.supervisor.subprocess.Popen")
def test_native_probe_retries_timeout_within_attempt_limit(mock_popen, mock_client):
    process = MagicMock(pid=12345)
    process.poll.return_value = None
    mock_popen.return_value = process
    timeout = requests.exceptions.ReadTimeout("timed out")
    wrapped_timeout = RuntimeError("send request failed")
    wrapped_timeout.__cause__ = timeout
    client = mock_client.return_value.__enter__.return_value
    client.do_get.side_effect = [wrapped_timeout, None]
    supervisor = ProcessSupervisor()
    supervisor.start(3, _command(), _probe(max_attempts=2))

    assert supervisor.state(3, "10.0.0.1", 8000) == RuntimeState.READY
    assert client.do_get.call_count == 2


@patch("motor.node_manager.core.services.native_engine.supervisor.SafeHTTPSClient")
@patch("motor.node_manager.core.services.native_engine.supervisor.subprocess.Popen")
def test_native_probe_does_not_retry_non_timeout_error(mock_popen, mock_client):
    process = MagicMock(pid=12345)
    process.poll.return_value = None
    mock_popen.return_value = process
    client = mock_client.return_value.__enter__.return_value
    client.do_get.side_effect = RuntimeError("connection refused")
    supervisor = ProcessSupervisor()
    supervisor.start(3, _command(), _probe(startup_timeout=10, max_attempts=3))

    with patch("motor.node_manager.core.services.native_engine.supervisor.time.monotonic", return_value=11):
        supervisor._processes[3].started_at = 0
        assert supervisor.state(3, "10.0.0.1", 8000) == RuntimeState.UNHEALTHY

    client.do_get.assert_called_once_with("/health")


@patch("motor.node_manager.core.services.native_engine.supervisor.SafeHTTPSClient")
@patch("motor.node_manager.core.services.native_engine.supervisor.subprocess.Popen")
def test_native_probe_marks_unhealthy_after_exhausting_timeout_retries(mock_popen, mock_client):
    process = MagicMock(pid=12345)
    process.poll.return_value = None
    mock_popen.return_value = process
    mock_client.return_value.__enter__.return_value.do_get.side_effect = requests.exceptions.ReadTimeout("timed out")
    supervisor = ProcessSupervisor()
    supervisor.start(3, _command(), _probe(startup_timeout=10, max_attempts=2))

    with patch("motor.node_manager.core.services.native_engine.supervisor.time.monotonic", return_value=11):
        supervisor._processes[3].started_at = 0
        assert supervisor.state(3, "10.0.0.1", 8000) == RuntimeState.UNHEALTHY

    assert mock_client.return_value.__enter__.return_value.do_get.call_count == 2


@patch("motor.node_manager.core.services.native_engine.supervisor.SafeHTTPSClient")
@patch("motor.node_manager.core.services.native_engine.supervisor.subprocess.Popen")
def test_headless_probe_reports_running_without_claiming_readiness(mock_popen, mock_client):
    process = MagicMock(pid=12345)
    process.poll.return_value = None
    mock_popen.return_value = process
    supervisor = ProcessSupervisor()
    supervisor.start(3, _command(), _probe(process_only=True))

    assert supervisor.state(3, "10.0.0.2", 8000) == RuntimeState.RUNNING
    assert supervisor._processes[3].ready_at is None
    mock_client.assert_not_called()


@patch("motor.node_manager.core.services.native_engine.supervisor.subprocess.Popen")
def test_dead_process_is_stopped_without_http_probe(mock_popen):
    process = MagicMock(pid=12345)
    process.poll.side_effect = [None, 1, 1]
    mock_popen.return_value = process
    supervisor = ProcessSupervisor()
    supervisor.start(3, _command(), _probe())

    assert supervisor.state(3, "10.0.0.1", 8000) == RuntimeState.STOPPED
    with patch.object(supervisor, "_kill_group") as kill_group:
        assert supervisor.dead_pids() == [(12345, 3)]
    kill_group.assert_called_once()
    assert supervisor.dead_pids() == []
    assert supervisor.pid_list() == []


@patch("motor.node_manager.core.services.native_engine.supervisor.subprocess.Popen")
def test_stop_uses_graceful_then_forced_process_group_cleanup(mock_popen):
    process = MagicMock(pid=12345)
    process.poll.return_value = None
    process.wait.side_effect = subprocess.TimeoutExpired(cmd="vllm", timeout=0)
    mock_popen.return_value = process
    supervisor = ProcessSupervisor(stop_grace_seconds=0)
    supervisor.start(3, _command(), _probe())

    with (
        patch.object(supervisor, "_terminate_group") as terminate_group,
        patch.object(supervisor, "_kill_group") as kill_group,
    ):
        stopped = supervisor.stop_all()

    assert stopped == [12345]
    runtime = terminate_group.call_args.args[0]
    assert runtime.process is process
    kill_group.assert_called_once_with(runtime)
    assert supervisor.pid_list() == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups only")
@patch("motor.node_manager.core.services.native_engine.supervisor.subprocess.Popen")
def test_stop_kills_remaining_group_after_leader_exits(mock_popen):
    process = MagicMock(pid=12345)
    process.poll.return_value = None
    process.wait.return_value = 0
    mock_popen.return_value = process
    supervisor = ProcessSupervisor()
    supervisor.start(3, _command(), _probe())

    with patch("motor.node_manager.core.services.native_engine.supervisor.os.killpg") as killpg:
        assert supervisor.stop_all() == [12345]

    assert killpg.call_args_list == [
        call(12345, signal.SIGTERM),
        call(12345, 0),
        call(12345, signal.SIGKILL),
    ]


@patch("motor.node_manager.core.services.native_engine.supervisor.subprocess.Popen")
def test_stop_one_endpoint_preserves_other_processes(mock_popen):
    first = MagicMock(pid=12345)
    second = MagicMock(pid=12346)
    first.poll.return_value = None
    second.poll.return_value = None
    mock_popen.side_effect = [first, second]
    supervisor = ProcessSupervisor()
    supervisor.start(3, _command(), _probe())
    supervisor.start(4, _command(), _probe())

    with patch.object(supervisor, "_terminate_group"):
        assert supervisor.stop(4) == 12346

    assert supervisor.pid_list() == [12345]


@patch("motor.node_manager.core.services.native_engine.supervisor.subprocess.Popen")
def test_stop_state_remains_visible_until_process_cleanup_finishes(mock_popen):
    process = MagicMock(pid=12345)
    process.poll.return_value = None
    entered_wait = threading.Event()
    release_wait = threading.Event()

    def wait(*_args, **_kwargs):
        entered_wait.set()
        release_wait.wait(timeout=1)
        return 0

    process.wait.side_effect = wait
    mock_popen.return_value = process
    supervisor = ProcessSupervisor()
    supervisor.start(3, _command(), _probe())

    with patch.object(supervisor, "_terminate_group"):
        thread = threading.Thread(target=supervisor.stop_all)
        thread.start()
        assert entered_wait.wait(timeout=1)
        assert supervisor.state(3, "10.0.0.1", 8000) == RuntimeState.STOPPING
        release_wait.set()
        thread.join(timeout=1)

    assert supervisor.state(3, "10.0.0.1", 8000) == RuntimeState.STOPPED


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups only")
@patch("motor.node_manager.core.services.native_engine.supervisor.subprocess.Popen")
def test_cleanup_uses_cached_process_group_when_leader_has_exited(mock_popen):
    process = MagicMock(pid=12345)
    process.poll.side_effect = [None, 1]
    mock_popen.return_value = process
    supervisor = ProcessSupervisor()
    supervisor.start(3, _command(), _probe())
    runtime = supervisor._processes[3]

    with patch("motor.node_manager.core.services.native_engine.supervisor.os.killpg") as killpg:
        supervisor._terminate_group(runtime)

    killpg.assert_called_once_with(12345, signal.SIGTERM)


@patch("motor.node_manager.core.services.native_engine.supervisor.subprocess.Popen")
def test_kill_workers_sigkills_workers_not_launcher(mock_popen):
    process = MagicMock(pid=100)
    process.poll.return_value = None
    mock_popen.return_value = process
    supervisor = ProcessSupervisor()
    supervisor.start(8, _command(), _probe())

    with (
        patch.object(ProcessSupervisor, "_find_worker_pids", return_value=[201, 202]),
        patch("motor.node_manager.core.services.native_engine.supervisor.os.kill") as kill,
        patch.object(ProcessSupervisor, "_kill_group") as kill_group,
    ):
        result = supervisor.kill_workers([8])

    assert result["endpoint_ids"] == [8]
    assert result["killed_pids"] == [201, 202]
    assert kill.call_args_list == [
        call(201, signal.SIGKILL),
        call(202, signal.SIGKILL),
    ]
    kill_group.assert_not_called()
    assert supervisor.pid_list() == [100]


@patch("motor.node_manager.core.services.native_engine.supervisor.SafeHTTPSClient")
@patch("motor.node_manager.core.services.native_engine.supervisor.subprocess.Popen")
def test_native_probe_flaps_ready_unhealthy_ready_within_startup_window(mock_popen, mock_client):
    process = MagicMock(pid=12345)
    process.poll.return_value = None
    mock_popen.return_value = process
    client = mock_client.return_value.__enter__.return_value
    ok_response = client.do_get.return_value
    ok_response.status_code = HTTPStatus.OK
    client.do_get.side_effect = [ok_response, RuntimeError("connection refused"), ok_response]
    supervisor = ProcessSupervisor()
    supervisor.start(3, _command(), _probe(startup_timeout=600))

    with patch("motor.node_manager.core.services.native_engine.supervisor.time.monotonic", return_value=100):
        supervisor._processes[3].started_at = 0
        assert supervisor.state(3, "10.0.0.1", 8000) == RuntimeState.READY
        # The runtime is READY now, so a probe failure must commit UNHEALTHY even though
        # elapsed (100s) is still below startup_timeout (600s).
        assert supervisor.state(3, "10.0.0.1", 8000) == RuntimeState.UNHEALTHY
        assert supervisor.state(3, "10.0.0.1", 8000) == RuntimeState.READY


@patch("motor.node_manager.core.services.native_engine.supervisor.SafeHTTPSClient")
@patch("motor.node_manager.core.services.native_engine.supervisor.subprocess.Popen")
def test_snapshot_health_no_content_is_treated_as_probe_failure(mock_popen, mock_client):
    process = MagicMock(pid=12345)
    process.poll.return_value = None
    mock_popen.return_value = process
    response = mock_client.return_value.__enter__.return_value.do_get.return_value
    response.status_code = HTTPStatus.NO_CONTENT
    supervisor = ProcessSupervisor()
    probe = ProbeSpec(path="/snapshot/health", timeout_seconds=5, startup_timeout_seconds=10)
    supervisor.start(3, _command(), probe)

    with patch("motor.node_manager.core.services.native_engine.supervisor.time.monotonic", return_value=11):
        supervisor._processes[3].started_at = 0
        state = supervisor.state(3, "10.0.0.1", 8000)

    assert state == RuntimeState.UNHEALTHY


@patch("motor.node_manager.core.services.native_engine.supervisor.subprocess.Popen")
def test_rebind_endpoint_ids_pairs_sorted_ids_and_preserves_processes(mock_popen):
    processes = []
    for pid in range(100, 104):
        process = MagicMock(pid=pid)
        process.poll.return_value = None
        processes.append(process)
    mock_popen.side_effect = processes
    supervisor = ProcessSupervisor()
    # Unordered start order (dict insertion order 3,1,0,2) and an unordered
    # new id list lock in the sorted-pairing behavior: dropping either
    # sorted() call breaks the mapping assertions below.
    start_order = [3, 1, 0, 2]
    for endpoint_id in start_order:
        supervisor.start(endpoint_id, _command(), _probe())
    supervisor._processes[0].state = RuntimeState.READY
    supervisor._processes[0].ready_at = 10.0
    supervisor._processes[1].state = RuntimeState.STOPPING
    supervisor._processes[1].started_at = 3.0
    supervisor._processes[3].state = RuntimeState.STOPPED
    supervisor._processes[3].started_at = 4.0

    with patch("motor.node_manager.core.services.native_engine.supervisor.time.monotonic", return_value=555.0):
        mapping = supervisor.rebind_endpoint_ids([5, 2, 4, 3])

    assert mapping == {0: 2, 1: 3, 2: 4, 3: 5}
    assert set(supervisor._processes) == {2, 3, 4, 5}
    assert sorted(supervisor.pid_list()) == [100, 101, 102, 103]
    # sorted(old)=[0,1,2,3] pairs position-wise with sorted(new)=[2,3,4,5]:
    # old id was started as start_order[processes.index(process)].
    for old_id, new_id in ((0, 2), (1, 3), (2, 4), (3, 5)):
        runtime = supervisor._processes[new_id]
        assert runtime.process is processes[start_order.index(old_id)]
        assert runtime.endpoint_id == new_id
    # The formerly READY endpoint restarts its lifecycle under the new id.
    rebound = supervisor._processes[2]
    assert rebound.state == RuntimeState.STARTING
    assert rebound.ready_at is None
    assert rebound.started_at == 555.0
    # Stopping/stopped runtimes keep their state and timestamps untouched.
    stopping = supervisor._processes[3]
    assert stopping.state == RuntimeState.STOPPING
    assert stopping.started_at == 3.0
    stopped = supervisor._processes[5]
    assert stopped.state == RuntimeState.STOPPED
    assert stopped.started_at == 4.0


@patch("motor.node_manager.core.services.native_engine.supervisor.subprocess.Popen")
def test_rebind_endpoint_ids_identity_mapping_keeps_runtime_states(mock_popen):
    first = MagicMock(pid=100)
    second = MagicMock(pid=101)
    first.poll.return_value = None
    second.poll.return_value = None
    mock_popen.side_effect = [first, second]
    supervisor = ProcessSupervisor()
    supervisor.start(0, _command(), _probe())
    supervisor.start(1, _command(), _probe())
    supervisor._processes[0].state = RuntimeState.READY
    supervisor._processes[0].ready_at = 10.0
    supervisor._processes[0].started_at = 7.0
    supervisor._processes[1].state = RuntimeState.READY
    supervisor._processes[1].ready_at = 20.0
    supervisor._processes[1].started_at = 9.0

    assert supervisor.rebind_endpoint_ids([1, 0]) == {0: 0, 1: 1}

    for endpoint_id, ready_at, started_at in ((0, 10.0, 7.0), (1, 20.0, 9.0)):
        runtime = supervisor._processes[endpoint_id]
        assert runtime.state == RuntimeState.READY
        assert runtime.ready_at == ready_at
        assert runtime.started_at == started_at
    assert set(supervisor._processes) == {0, 1}
    assert supervisor.pid_list() == [100, 101]


@patch("motor.node_manager.core.services.native_engine.supervisor.subprocess.Popen")
def test_rebind_endpoint_ids_count_mismatch_keeps_original_mapping(mock_popen):
    first = MagicMock(pid=100)
    second = MagicMock(pid=101)
    first.poll.return_value = None
    second.poll.return_value = None
    mock_popen.side_effect = [first, second]
    supervisor = ProcessSupervisor()
    supervisor.start(0, _command(), _probe())
    supervisor.start(1, _command(), _probe())

    with pytest.raises(RuntimeError, match="Cannot rebind engine endpoints"):
        supervisor.rebind_endpoint_ids([2])

    assert set(supervisor._processes) == {0, 1}
    assert supervisor.pid_list() == [100, 101]
    assert supervisor._processes[0].state == RuntimeState.STARTING
    assert supervisor._processes[1].state == RuntimeState.STARTING


def test_rebind_endpoint_ids_without_local_runtimes_returns_empty():
    supervisor = ProcessSupervisor()

    assert supervisor.rebind_endpoint_ids([4, 5]) == {}
