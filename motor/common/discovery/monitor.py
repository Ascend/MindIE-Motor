# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Periodic readiness probes; business requests read immutable in-memory states."""

import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace

from requests import HTTPError

from motor.common.discovery.errors import DiscoveryUnavailable
from motor.common.http.http_client import HttpStatusError, SafeHTTPSClient
from motor.common.logger import get_logger
from motor.config.service_discovery import ServiceDiscoveryConfig, ServiceEndpoint
from motor.config.tls_config import TLSConfig

logger = get_logger(__name__)


@dataclass(frozen=True)
class EndpointSnapshot:
    endpoint: ServiceEndpoint
    kind: str
    ready: bool = False

    @property
    def host(self) -> str:
        return self.endpoint.host

    @property
    def port(self) -> int:
        return self.endpoint.ports[self.kind]

    @property
    def address(self) -> str:
        return self.endpoint.address(self.kind)


@dataclass(frozen=True)
class EndpointState:
    endpoint: ServiceEndpoint
    eligible: bool = False
    ready: bool = False
    observed_at: float = 0.0

    def snapshot(self, kind: str) -> EndpointSnapshot:
        return EndpointSnapshot(self.endpoint, kind, self.ready)


@dataclass(frozen=True)
class ProbeResult:
    eligible: bool
    ready: bool


def probe_endpoint(
    service: str, endpoint: ServiceEndpoint, tls_config: TLSConfig | None, timeout: float
) -> ProbeResult:
    """Use HTTP 200 for routing; retain topology readiness for Controller recovery."""
    kind = "api" if service == "controller" else "management"
    try:
        with SafeHTTPSClient(endpoint.address(kind), tls_config=tls_config, timeout=timeout) as client:
            response = client.do_get("/readiness")
            eligible = response.status_code == 200
            ready = eligible
            if service == "coordinator" and eligible:
                try:
                    body = response.json()
                    ready = isinstance(body, dict) and body.get("ready") is True
                except ValueError:
                    ready = False
            return ProbeResult(eligible, ready)
    except (HttpStatusError, HTTPError):
        return ProbeResult(False, False)
    except (RuntimeError, OSError, ValueError) as exc:
        logger.debug("Readiness probe failed for %s: %s", endpoint.address(kind), exc)
        return ProbeResult(False, False)


class EndpointMonitor:
    """One process's bounded concurrent probes of one service's static members.

    Service clients keep the task for their process lifetime. The scheduler alone
    publishes states; business calls only select a cached target and keep their
    existing request and retry behavior.
    """

    def __init__(
        self,
        service: str,
        config: ServiceDiscoveryConfig,
        tls_config: TLSConfig | None = None,
        *,
        probe: Callable[[ServiceEndpoint], ProbeResult] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not config.uses_probe(service):
            raise ValueError("Probe discovery requires configured " + service + " endpoints")
        self.members = config.endpoints[service]
        self._config = config
        self._service = service
        self._clock = clock
        self._probe = probe or (
            lambda endpoint: probe_endpoint(service, endpoint, tls_config, config.probe_timeout_seconds)
        )
        self._states = {member.identity: EndpointState(member) for member in self.members}
        self._selected: str | None = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._closed = False
        self._thread: threading.Thread | None = None
        self._executor: ThreadPoolExecutor | None = None

    def start(self) -> None:
        """Begin the first probes asynchronously; startup does not wait for readiness."""
        with self._lock:
            if self._closed:
                raise RuntimeError("Cannot restart a stopped endpoint monitor")
            if self._thread is not None:
                return
            self._executor = ThreadPoolExecutor(max_workers=min(8, len(self.members)), thread_name_prefix="MotorProbe")
            self._thread = threading.Thread(target=self._run, name="MotorEndpointMonitor", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Release an individually owned monitor, e.g. between in-process tests."""
        with self._lock:
            self._closed = True
            thread, executor = self._thread, self._executor
        self._wake.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def states(self) -> tuple[EndpointState, ...]:
        """Return every member, including standby/unobserved/stale members."""
        with self._lock:
            return self._current_states(self._clock())

    def select(self, kind: str) -> EndpointSnapshot:
        """Keep the selected eligible endpoint until it becomes unavailable."""
        with self._lock:
            states = self._current_states(self._clock())
            available = [state for state in states if kind in state.endpoint.ports and state.eligible]
            if not available:
                raise DiscoveryUnavailable("No fresh " + self._service + " endpoint for " + kind)
            selected = next((state for state in available if state.endpoint.identity == self._selected), available[0])
            self._selected = selected.endpoint.identity
            return selected.snapshot(kind)

    def _current_states(self, now: float) -> tuple[EndpointState, ...]:
        result = []
        for state in self._states.values():
            if self._closed or now - state.observed_at >= self._config.max_staleness_seconds:
                state = replace(state, eligible=False, ready=False)
            result.append(state)
        return tuple(result)

    def _publish(self, member: ServiceEndpoint, started_at: float, result: ProbeResult) -> None:
        with self._lock:
            if self._closed:
                return
            self._states[member.identity] = EndpointState(member, result.eligible, result.ready, observed_at=started_at)

    def _run(self) -> None:
        pending: dict[str, tuple[Future[ProbeResult], float]] = {}
        due = {member.identity: 0.0 for member in self.members}
        while True:
            self._wake.clear()
            with self._lock:
                if self._closed:
                    return
            for member in self.members:
                key = member.identity
                if key in pending and pending[key][0].done():
                    future, started_at = pending.pop(key)
                    try:
                        result = future.result()
                    except Exception as exc:  # noqa: BLE001 -- one worker must not terminate other members' monitoring.
                        logger.debug("Readiness worker failed for %s: %s", member.host, exc)
                        result = ProbeResult(False, False)
                    self._publish(member, started_at, result)
                    due[key] = self._clock() + self._config.probe_interval_seconds
            with self._lock:
                if self._closed:
                    return
                now = self._clock()
                for member in self.members:
                    key = member.identity
                    if key not in pending and due[key] <= now:
                        try:
                            future = self._executor.submit(self._probe, member)
                        except RuntimeError as exc:
                            # The executor rejects submissions during interpreter shutdown.
                            logger.debug("Endpoint probe scheduler stopped: %s", exc)
                            return
                        pending[key] = future, now
                        future.add_done_callback(lambda _future: self._wake.set())
                delay = min(
                    (max(0.0, when - now) for key, when in due.items() if key not in pending),
                    default=self._config.probe_interval_seconds,
                )
            self._wake.wait(delay)
