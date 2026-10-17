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
import time
from http import HTTPStatus

import requests

from motor.common.http.http_client import SafeHTTPSClient
from motor.common.logger import get_logger
from motor.common.utils.net import format_address
from motor.node_manager.core.services.native_engine.models import CommandSpec, ProbeSpec, RuntimeProcess, RuntimeState

logger = get_logger(__name__)


class ProcessSupervisor:
    """Own native engine process groups and their node-local runtime state."""

    def __init__(self, stop_grace_seconds: float = 10.0) -> None:
        self._processes: dict[int, RuntimeProcess] = {}
        self._lock = threading.Lock()
        self._stop_grace_seconds = stop_grace_seconds

    def start(self, endpoint_id: int, command: CommandSpec, probe: ProbeSpec) -> bool:
        """Start one endpoint atomically; return False when it is already running."""
        with self._lock:
            existing = self._processes.get(endpoint_id)
            if existing is not None and existing.process.poll() is None:
                if existing.command == command and existing.probe == probe:
                    return False
                raise RuntimeError(f"Engine endpoint {endpoint_id} is already running with a different launch spec")
            self._processes.pop(endpoint_id, None)

            process = subprocess.Popen(  # pylint: disable=consider-using-with
                list(command.argv),
                shell=False,
                env=dict(command.env),
                cwd=command.cwd,
                start_new_session=True,
            )
            if process.poll() is not None:
                raise RuntimeError(f"Engine process exited immediately with code {process.returncode}")

            self._processes[endpoint_id] = RuntimeProcess(
                endpoint_id=endpoint_id,
                process=process,
                command=command,
                probe=probe,
                started_at=time.monotonic(),
                process_group_id=process.pid if os.name == "posix" else None,
            )
            return True

    def rebind_endpoint_ids(self, new_endpoint_ids: list[int]) -> dict[int, int]:
        """Re-key local runtimes when restore start_cmd remaps endpoint ids.

        Registration order can assign a different contiguous id block to this
        node (e.g. 0..3 -> 4..7). Pair sorted existing keys with sorted
        ``new_endpoint_ids``. Returns ``{old_id: new_id}``.
        """
        with self._lock:
            old_ids = sorted(self._processes.keys())
            new_ids = sorted(new_endpoint_ids)
            if not old_ids:
                logger.warning("[snapshot] No local engine runtimes to rebind after restore")
                return {}
            if len(old_ids) != len(new_ids):
                raise RuntimeError(
                    "Cannot rebind engine endpoints after restore: local runtime "
                    f"count {len(old_ids)} ({old_ids}) != start_cmd endpoint count "
                    f"{len(new_ids)} ({new_ids})"
                )
            if old_ids == new_ids:
                return {endpoint_id: endpoint_id for endpoint_id in old_ids}

            moved: dict[int, RuntimeProcess] = {}
            mapping: dict[int, int] = {}
            for old_id, new_id in zip(old_ids, new_ids):
                runtime = self._processes.pop(old_id)
                runtime.endpoint_id = new_id
                # Resume may still be in progress on the new pod IP; allow startup grace.
                if runtime.state not in (RuntimeState.STOPPING, RuntimeState.STOPPED):
                    runtime.state = RuntimeState.STARTING
                    runtime.ready_at = None
                    runtime.started_at = time.monotonic()
                moved[new_id] = runtime
                mapping[old_id] = new_id
            self._processes.update(moved)
            logger.info(
                "[snapshot] Rebound local engine endpoint ids after restore "
                "(old_endpoint_id -> new_endpoint_id from start_cmd): %s",
                ", ".join(f"{old_id} -> {new_id}" for old_id, new_id in mapping.items()),
            )
            return mapping

    def pid_list(self) -> list[int]:
        with self._lock:
            return [runtime.process.pid for runtime in self._processes.values()]

    def probe_spec(self, endpoint_id: int) -> ProbeSpec | None:
        with self._lock:
            runtime = self._processes.get(endpoint_id)
            return runtime.probe if runtime is not None else None

    def state(self, endpoint_id: int, host: str, port: int) -> RuntimeState:
        with self._lock:
            runtime = self._processes.get(endpoint_id)
            if runtime is None:
                return RuntimeState.STOPPED
            if runtime.state == RuntimeState.STOPPING:
                return RuntimeState.STOPPING

        process = runtime.process
        if process.poll() is not None:
            return self._commit_state(endpoint_id, runtime, RuntimeState.STOPPED)
        if runtime.probe.process_only:
            return self._commit_state(endpoint_id, runtime, RuntimeState.RUNNING)
        try:
            address = format_address(host, port)
            with SafeHTTPSClient(
                address=address,
                tls_config=runtime.probe.tls_config,
                timeout=runtime.probe.timeout_seconds,
            ) as client:
                for attempt in range(1, runtime.probe.max_attempts + 1):
                    try:
                        response = client.do_get(runtime.probe.path)
                        break
                    except Exception as err:
                        if not self._is_timeout_error(err) or attempt == runtime.probe.max_attempts:
                            raise
                        logger.debug(
                            "Native health probe timed out for endpoint %s (attempt %s/%s), retrying",
                            endpoint_id,
                            attempt,
                            runtime.probe.max_attempts,
                        )
            if runtime.probe.path == "/snapshot/health":
                if response.status_code == HTTPStatus.ACCEPTED:
                    return self._commit_state(endpoint_id, runtime, RuntimeState.RUNNING)
                if response.status_code != HTTPStatus.OK:
                    raise RuntimeError("Unexpected snapshot health status: %s" % response.status_code)
            return self._commit_state(endpoint_id, runtime, RuntimeState.READY, ready=True)
        except Exception as err:
            elapsed = time.monotonic() - runtime.started_at
            if runtime.state == RuntimeState.STARTING and elapsed < runtime.probe.startup_timeout_seconds:
                logger.debug(
                    "Engine endpoint %s is still starting after %.1fs: %s",
                    endpoint_id,
                    elapsed,
                    err,
                )
                return self._current_state(endpoint_id, runtime)
            logger.warning("Native health probe failed for endpoint %s: %s", endpoint_id, err)
            return self._commit_state(endpoint_id, runtime, RuntimeState.UNHEALTHY)

    def dead_pids(self) -> list[tuple[int, int]]:
        """Return ``(pid, endpoint_id)`` for processes that died.

        The endpoint id is included so the caller can report the death per
        endpoint (the Daemon's engine-relaunch flow dedups and reports by
        pid/endpoint).
        """
        with self._lock:
            dead = []
            dead_runtimes = []
            for endpoint_id, runtime in list(self._processes.items()):
                if runtime.state == RuntimeState.STOPPING or runtime.process.poll() is None:
                    continue
                runtime.state = RuntimeState.STOPPED
                dead.append((runtime.process.pid, endpoint_id))
                dead_runtimes.append(runtime)
                self._processes.pop(endpoint_id, None)

        # The launcher may exit before its workers. Clean the cached process group
        # after removing the record so the same death is reported only once.
        for runtime in dead_runtimes:
            self._kill_group(runtime)
        return dead

    def stop_all(self) -> list[int]:
        with self._lock:
            runtimes = list(self._processes.values())
            for runtime in runtimes:
                runtime.state = RuntimeState.STOPPING

        stopped = self._stop_runtimes(runtimes)
        self._remove_stopped(runtimes)
        return stopped

    def stop(self, endpoint_id: int) -> int | None:
        """Stop one endpoint without affecting unrelated engine processes."""
        with self._lock:
            runtime = self._processes.get(endpoint_id)
            if runtime is not None:
                runtime.state = RuntimeState.STOPPING
        if runtime is None:
            return None
        stopped = self._stop_runtimes([runtime])[0]
        self._remove_stopped([runtime])
        return stopped

    def kill_workers(self, endpoint_ids: list[int] | None = None) -> dict[str, list[int]]:
        """SIGKILL ``VLLM::Worker`` children only; leave API/EngineCore alive.

        Used by Controller user-recover first shrink: empty a problem Pod by
        killing workers so FT ``scale_down`` can retire ranks without
        ``/scale_elastic_ep``. Never calls ``_kill_group`` on the endpoint.
        """
        with self._lock:
            if endpoint_ids:
                wanted = {int(endpoint_id) for endpoint_id in endpoint_ids}
                targets = [runtime for endpoint_id, runtime in self._processes.items() if endpoint_id in wanted]
            else:
                targets = list(self._processes.values())

        killed: list[int] = []
        for runtime in targets:
            for pid in self._find_worker_pids(runtime):
                try:
                    os.kill(pid, signal.SIGKILL)
                    killed.append(pid)
                    logger.info(
                        "Killed VLLM::Worker pid=%s for endpoint %s",
                        pid,
                        runtime.endpoint_id,
                    )
                except ProcessLookupError:
                    pass
                except (OSError, PermissionError) as err:
                    logger.warning(
                        "Failed to kill worker pid=%s endpoint=%s: %s",
                        pid,
                        runtime.endpoint_id,
                        err,
                    )
        return {
            "endpoint_ids": [runtime.endpoint_id for runtime in targets],
            "killed_pids": killed,
        }

    def _stop_runtimes(self, runtimes: list[RuntimeProcess]) -> list[int]:
        for runtime in runtimes:
            runtime.state = RuntimeState.STOPPING
            self._terminate_group(runtime)

        deadline = time.monotonic() + self._stop_grace_seconds
        for runtime in runtimes:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                runtime.process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                self._kill_group(runtime)
            else:
                # The launcher can exit while child workers still hold the same PGID.
                # Reap the remaining group instead of treating a leader exit as cleanup.
                if self._group_exists(runtime):
                    self._kill_group(runtime)
        return [runtime.process.pid for runtime in runtimes]

    def _current_state(self, endpoint_id: int, runtime: RuntimeProcess) -> RuntimeState:
        with self._lock:
            current = self._processes.get(endpoint_id)
            return runtime.state if current is runtime else RuntimeState.STOPPED

    def _commit_state(
        self,
        endpoint_id: int,
        runtime: RuntimeProcess,
        state: RuntimeState,
        *,
        ready: bool = False,
    ) -> RuntimeState:
        with self._lock:
            current = self._processes.get(endpoint_id)
            if current is not runtime:
                return RuntimeState.STOPPED
            if runtime.state == RuntimeState.STOPPING:
                return RuntimeState.STOPPING
            runtime.state = state
            if ready:
                runtime.ready_at = runtime.ready_at or time.monotonic()
            return runtime.state

    def _remove_stopped(self, runtimes: list[RuntimeProcess]) -> None:
        with self._lock:
            for runtime in runtimes:
                if self._processes.get(runtime.endpoint_id) is runtime:
                    runtime.state = RuntimeState.STOPPED
                    self._processes.pop(runtime.endpoint_id, None)

    @staticmethod
    def _is_timeout_error(error: Exception) -> bool:
        """Return whether SafeHTTPSClient surfaced a requests timeout."""
        current: BaseException | None = error
        while current is not None:
            if isinstance(current, requests.exceptions.Timeout):
                return True
            current = current.__cause__
        return False

    @staticmethod
    def _terminate_group(runtime: RuntimeProcess) -> None:
        process = runtime.process
        try:
            if os.name == "posix" and runtime.process_group_id is not None:
                getattr(os, "killpg")(runtime.process_group_id, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            pass
        except (OSError, PermissionError) as err:
            logger.warning("Failed to terminate engine process group %s: %s", process.pid, err)

    @staticmethod
    def _kill_group(runtime: RuntimeProcess) -> None:
        process = runtime.process
        try:
            if os.name == "posix" and runtime.process_group_id is not None:
                getattr(os, "killpg")(runtime.process_group_id, getattr(signal, "SIGKILL"))
            else:
                process.kill()
        except ProcessLookupError:
            pass
        except (OSError, PermissionError) as err:
            logger.error("Failed to kill engine process group %s: %s", process.pid, err)

    @staticmethod
    def _group_exists(runtime: RuntimeProcess) -> bool:
        """Return whether a cached POSIX process group still has a member."""
        if os.name != "posix" or runtime.process_group_id is None:
            return False
        try:
            getattr(os, "killpg")(runtime.process_group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError as err:
            logger.warning("Failed to inspect engine process group %s: %s", runtime.process_group_id, err)
            return True
        return True

    @staticmethod
    def _find_worker_pids(runtime: RuntimeProcess) -> list[int]:
        """Return PIDs in the endpoint process group whose cmdline is a Worker."""
        if os.name != "posix":
            return []
        pgid = runtime.process_group_id
        if pgid is None:
            return []
        leader_pid = runtime.process.pid
        workers: list[int] = []
        try:
            proc_entries = os.listdir("/proc")
        except OSError as err:
            logger.warning("Cannot list /proc to find workers: %s", err)
            return []
        for name in proc_entries:
            if not name.isdigit():
                continue
            pid = int(name)
            if pid == leader_pid:
                continue
            if ProcessSupervisor._proc_pgrp(pid) != pgid:
                continue
            if not ProcessSupervisor._proc_cmdline_is_worker(pid):
                continue
            workers.append(pid)
        return workers

    @staticmethod
    def _proc_pgrp(pid: int) -> int | None:
        """Read process group id from ``/proc/<pid>/stat``."""
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            return None
        close = text.rfind(")")
        if close < 0:
            return None
        fields = text[close + 2 :].split()
        # After ``(comm)``: state ppid pgrp ...
        if len(fields) < 3:
            return None
        try:
            return int(fields[2])
        except ValueError:
            return None

    @staticmethod
    def _proc_cmdline_is_worker(pid: int) -> bool:
        """True when cmdline/comm looks like a vLLM Worker process title."""
        markers = ("VLLM::Worker", "Worker_TP", "Worker_PP", "Worker_DP")
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                raw = handle.read().replace(b"\x00", b" ")
            cmdline = raw.decode("utf-8", errors="replace")
        except OSError:
            cmdline = ""
        if any(marker in cmdline for marker in markers):
            return True
        try:
            with open(f"/proc/{pid}/comm", encoding="utf-8") as handle:
                comm = handle.read().strip()
        except OSError:
            return False
        return any(marker in comm for marker in markers)
