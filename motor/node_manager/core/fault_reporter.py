# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""FaultReporter – polls each engine's FT status endpoint and forwards
software fault status updates to the Controller over HTTP.
"""

import json
import threading
import time
from pathlib import Path

from motor.common.constants import (
    ENGINE_STATUS_DEAD,
    ENGINE_STATUS_HEALTHY,
    ENGINE_STATUS_UNHEALTHY,
)
from motor.common.http.engine_ft_client import query_engine_ft_status
from motor.common.logger import get_logger
from motor.common.logger.rate_limited_logger import RateLimitedLogger
from motor.common.resources.endpoint import Endpoint
from motor.config.config_utils import (
    ENGINE_CONFIG,
    MOTOR_ENGINE_PREFILL_CONFIG,
    MOTOR_ENGINE_UNION_CONFIG,
    ConfigKey,
)
from motor.config.node_manager import NodeManagerConfig
from motor.node_manager.api_client.controller_api_client import ControllerApiClient

logger = get_logger(__name__)
_rl = RateLimitedLogger(logger)

# Engine FT enable keys accepted in the user config's engine_config sections
# (vLLM style: --enable-fault-tolerance / --enable_fault_tolerance).
_ENGINE_FT_ENABLE_KEYS = frozenset({"enable-fault-tolerance", "enable_fault_tolerance"})

# Engine sections of the user config that may carry an engine_config.
_ENGINE_SECTION_KEYS = (
    MOTOR_ENGINE_PREFILL_CONFIG,
    ConfigKey.MOTOR_ENGINE_DECODE.value,
    MOTOR_ENGINE_UNION_CONFIG,
)

# Map engine status names (from the vLLM /fault_tolerance/status response)
# to int values consumed by the Controller.
_ENGINE_STATUS_NAME_TO_INT = {
    ENGINE_STATUS_HEALTHY: 0,
    ENGINE_STATUS_DEAD: 1,
    ENGINE_STATUS_UNHEALTHY: 2,
}

# Engines take minutes to load a model; poll failures during this startup
# window must not be reported as dead.
_STARTUP_GRACE_SEC = 300.0


def _engine_ft_enabled(user_config_path: str | None) -> bool:
    """Detect whether any engine section of the user config enables fault tolerance.

    Scans the known engine sections (``motor_engine_*_config``) of the user
    config for an FT enable key (``enable-fault-tolerance`` /
    ``enable_fault_tolerance``) set to true (JSON ``true`` or ``1``).
    """
    if not user_config_path:
        return False
    path = Path(user_config_path)
    if not path.exists():
        return False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to read user config %s for FT detection: %s", user_config_path, e)
        return False
    if not isinstance(raw, dict):
        return False

    for section_key in _ENGINE_SECTION_KEYS:
        section = raw.get(section_key)
        if not isinstance(section, dict):
            continue
        engine_config = section.get(ENGINE_CONFIG)
        if not isinstance(engine_config, dict):
            continue
        if any(engine_config.get(ft_key) in (True, 1) for ft_key in _ENGINE_FT_ENABLE_KEYS):
            return True
    return False


class FaultReporter:
    """Polls per-engine FT status endpoints and reports non-healthy engines
    to the Controller.

    Each engine (a vLLM API server on the endpoint's business port) exposes
    ``GET /fault_tolerance/status`` returning
    ``{"engines": [{"id", "status", "fault_info"?}]}``. Status is polled in a
    background thread every ``poll_interval_sec``. An engine that cannot be
    reached for ``max_poll_failures`` consecutive polls is reported as dead
    (after the startup grace period).

    Reporting is enabled when the NodeManager config explicitly enables fault
    tolerance OR any engine section of the user config does (auto-detection).
    """

    def __init__(self, config: NodeManagerConfig):
        self._config = config
        self._config_lock = threading.RLock()
        self._enabled = self._compute_enabled(config)
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._endpoints: list[Endpoint] = []

    @staticmethod
    def _compute_enabled(config: NodeManagerConfig) -> bool:
        """Explicit config flag OR engine user config auto-detection."""
        if config.fault_tolerance_config.enable_fault_tolerance:
            return True
        return _engine_ft_enabled(config.config_path)

    def start(self, endpoints: list[Endpoint] | None = None) -> None:
        """Start the background polling thread (no-op when fault tolerance is disabled)."""
        if not self._enabled:
            return
        if self._thread is not None and self._thread.is_alive():
            logger.debug("FaultReporter thread already running")
            return
        if endpoints is not None:
            self._endpoints = endpoints
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._main_loop,
            daemon=True,
            name="fault_reporter",
        )
        self._thread.start()
        logger.info("FaultReporter started.")

    def stop(self) -> None:
        """Stop the polling thread, waiting through a full poll round for it to exit."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            # One loop round can take len(endpoints) x poll_timeout when engines
            # are unreachable; wait through a full round so the loop observes
            # the stop event instead of the join timing out mid-round.
            with self._config_lock:
                poll_timeout = self._config.fault_tolerance_config.poll_timeout_sec
                round_time = len(self._endpoints) * poll_timeout
            join_timeout = max(5.0, round_time + 1.0)
            self._thread.join(timeout=join_timeout)
            if self._thread.is_alive():
                # Keep the reference so start() refuses to spawn a second
                # polling thread while the old one is still alive.
                logger.warning(
                    "FaultReporter thread did not stop within timeout; %s",
                    "keeping reference to avoid duplicate threads",
                )
                return
        self._thread = None
        logger.info("FaultReporter stopped.")

    def update_config(self, config: NodeManagerConfig, endpoints: list[Endpoint]) -> None:
        """Apply a new config and endpoint set, (re)starting or stopping the reporter as needed."""
        with self._config_lock:
            old_enable = self._enabled
            old_endpoint_ids = {ep.id for ep in self._endpoints}
            self._config = config
            self._endpoints = endpoints
            self._enabled = self._compute_enabled(config)

        new_endpoint_ids = {ep.id for ep in endpoints}
        endpoints_changed = old_endpoint_ids != new_endpoint_ids

        if self._enabled != old_enable:
            if self._enabled:
                self.start()
                logger.info("FaultReporter enabled, started thread")
            else:
                self.stop()
                logger.info("FaultReporter disabled, stopped thread")
        elif self._enabled and endpoints_changed:
            # Polling targets changed while enabled — restart to poll the new engines.
            logger.info(
                "FaultReporter endpoints changed (%d -> %d), restarting",
                len(old_endpoint_ids),
                len(new_endpoint_ids),
            )
            self.stop()
            self.start()

    def _poll_interval_sec(self) -> float:
        with self._config_lock:
            return self._config.fault_tolerance_config.poll_interval_sec

    def _max_poll_failures(self) -> int:
        with self._config_lock:
            return self._config.fault_tolerance_config.max_poll_failures

    def _query_engine_status(self, ep: Endpoint) -> dict:
        """GET one engine's FT status payload from its business (API) port."""
        with self._config_lock:
            timeout = self._config.fault_tolerance_config.poll_timeout_sec
        return query_engine_ft_status(ep, timeout)

    def _main_loop(self) -> None:
        """Poll every engine's FT status and forward faults to Controller."""
        logger.info("FaultReporter loop started.")
        known_statuses: dict[int, str] = {}
        consecutive_failures: dict[int, int] = {}
        first_poll_time: dict[int, float] = {}

        while not self._stop_event.is_set():
            with self._config_lock:
                endpoints = list(self._endpoints)
            now = time.time()
            for ep in endpoints:
                first_poll_time.setdefault(ep.id, now)
                self._poll_engine(ep, known_statuses, consecutive_failures, first_poll_time)
            if self._stop_event.wait(self._poll_interval_sec()):
                break

        logger.info("FaultReporter loop stopped.")

    def _poll_engine(
        self,
        ep: Endpoint,
        known_statuses: dict[int, str],
        consecutive_failures: dict[int, int],
        first_poll_time: dict[int, float],
    ) -> None:
        """Poll a single engine: forward new non-healthy statuses, or count
        poll failures and report dead once the threshold is exceeded.
        """
        try:
            payload = self._query_engine_status(ep)
        except Exception as e:
            # A poll failure must never kill the polling thread — count it
            # and let the consecutive-failures threshold decide the engine's fate.
            failures = consecutive_failures.get(ep.id, 0) + 1
            consecutive_failures[ep.id] = failures
            _rl.error_window(
                f"node_manager.fault_reporter.poll.{ep.id}",
                f"Failed to poll engine {ep.id} FT status: {e}",
            )
            if failures >= self._max_poll_failures():
                self._report_unreachable_dead(ep, failures, known_statuses, first_poll_time)
            return

        consecutive_failures[ep.id] = 0
        try:
            if not isinstance(payload, dict):
                raise TypeError(f"unexpected FT status payload type: {type(payload).__name__}")
            for engine in payload.get("engines", []):
                self._process_engine_status(ep.id, engine, known_statuses)
        except Exception as e:
            # A malformed payload must never kill the polling thread — log and
            # continue with the next round.
            _rl.error_window(
                f"node_manager.fault_reporter.parse.{ep.id}",
                f"Failed to parse engine {ep.id} FT status: {e}",
            )

    def _process_engine_status(
        self,
        ep_id: int,
        engine: dict,
        known_statuses: dict[int, str],
    ) -> None:
        """Report a single engine's status if it is non-healthy and new.

        The dedup key is the managed endpoint id (``ep_id``) — the same
        namespace used by ``_report_unreachable_dead`` — not the payload's
        engine id, which is rank-local per API server and collides across
        endpoints.
        """
        if not isinstance(engine, dict):
            raise TypeError(f"engine entry is not a dict: {engine!r}")
        status = engine.get("status")
        if not isinstance(status, str):
            raise TypeError(f"engine entry of endpoint {ep_id} has no valid status")

        if status == ENGINE_STATUS_HEALTHY:
            known_statuses[ep_id] = status
            return

        if known_statuses.get(ep_id) == status:
            return  # already reported

        engine_status = _ENGINE_STATUS_NAME_TO_INT.get(status)
        if engine_status is None:
            logger.warning("Unknown engine status '%s' for engine %d", status, ep_id)
            return

        fault_info = engine.get("fault_info") or ""
        if status == "unhealthy" and fault_info:
            exception_type = fault_info
            exception_message = f"Engine unhealthy: {fault_info}"
        elif status == ENGINE_STATUS_DEAD:
            exception_type = "EngineDeadError"
            exception_message = "Engine process died"
        else:
            exception_type = "EngineUnhealthyError"
            exception_message = "Engine unhealthy"

        fault_data = {
            "exception_type": exception_type,
            "exception_message": exception_message,
            "engine_id": ep_id,
            "engine_status": engine_status,
        }
        # Only mark as reported after successful delivery to Controller
        if self._send_fault_to_controller(fault_data):
            known_statuses[ep_id] = status

    def _report_unreachable_dead(
        self,
        ep: Endpoint,
        failures: int,
        known_statuses: dict[int, str],
        first_poll_time: dict[int, float],
    ) -> None:
        """Report an engine as dead after repeated poll failures (deduped).

        Poll failures during the startup grace period (engine model load)
        are not reported as dead.
        """
        if known_statuses.get(ep.id) == ENGINE_STATUS_DEAD:
            return
        first_poll = first_poll_time.get(ep.id, time.time())
        if time.time() - first_poll < _STARTUP_GRACE_SEC:
            logger.debug(
                "Engine %d unreachable but within startup grace period, not reporting dead",
                ep.id,
            )
            return
        fault_data = {
            "exception_type": "EngineDeadError",
            "exception_message": f"Engine unreachable after {failures} consecutive polls",
            "engine_id": ep.id,
            "engine_status": 1,
        }
        if self._send_fault_to_controller(fault_data):
            known_statuses[ep.id] = ENGINE_STATUS_DEAD

    def _send_fault_to_controller(self, fault_data: dict) -> bool:
        """Inject pod_ip and forward a single fault to Controller.

        Returns True if the fault was successfully reported, False otherwise.
        """
        with self._config_lock:
            fault_data["pod_ip"] = self._config.api_config.pod_ip

        logger.debug(
            "Forwarding software fault to Controller: engine_id=%s, type=%s",
            fault_data.get("engine_id"),
            fault_data.get("exception_type"),
        )
        return ControllerApiClient.report_software_fault(fault_data)
