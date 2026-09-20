# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Engine FT management for status collection, reporting, and recovery RPCs."""

import concurrent.futures
import threading
import time
from typing import Any, Callable

from motor.common.constants import (
    ENGINE_STATUS_DEAD,
    ENGINE_STATUS_HEALTHY,
    ENGINE_STATUS_UNHEALTHY,
)
from motor.common.http.engine_ft_client import (
    apply_engine_ft_instructions,
    EngineFtEndpointUnsupported,
    query_engine_ft_entry,
)
from motor.common.logger import get_logger
from motor.common.logger.rate_limited_logger import RateLimitedLogger
from motor.common.resources.endpoint import Endpoint
from motor.config.node_manager import NodeManagerConfig
from motor.node_manager.api_client.controller_api_client import ControllerApiClient

logger = get_logger(__name__)
_rl = RateLimitedLogger(logger)

# Map engine status names (from the vLLM /v1/fault_tolerance/status response)
# to int values consumed by the Controller.
_ENGINE_STATUS_NAME_TO_INT = {
    ENGINE_STATUS_HEALTHY: 0,
    ENGINE_STATUS_DEAD: 1,
    ENGINE_STATUS_UNHEALTHY: 2,
}
_REPORT_REJECT_BACKOFF_SEC = 60.0

# Maximum lease accepted while an FT transaction freezes suicide handling.
MAX_GUARD_LEASE_SEC = 300.0


class EngineFtManager:
    """Polls per-engine FT status endpoints and reports non-healthy engines
    to the Controller.

    Each engine (a vLLM API server on the endpoint's business port) exposes
    ``GET /v1/fault_tolerance/status``. The matching ``engines[]`` entry
    must use the endpoint's global DP rank. Status is polled in a background
    thread every ``poll_interval_sec``. An engine that cannot be reached
    after readiness is reported dead after ``max_poll_failures`` attempts.

    Reporting is enabled solely by the current role's normalized engine FT
    capability (``basic_config.ft_capability.enabled``), derived from the
    engine config's ``enable_fault_tolerance`` / ``enable-fault-tolerance``.
    There is no separate NodeManager-side switch.
    """

    def __init__(
        self,
        config: NodeManagerConfig,
        guard_callback: Callable[[str, float], None] | None = None,
        finalize_callback: Callable[[str, list[int], bool], None] | None = None,
    ):
        self._config = config
        self._config_lock = threading.RLock()
        self._enabled = self._compute_enabled(config)
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._endpoints: list[Endpoint] = []
        self._retired_endpoint_ids: set[int] = set()
        # Endpoints whose engine image does not implement the optional FT
        # status API (sustained HTTP 404) — skipped until the periodic
        # re-probe, an endpoint rebuild, or a config refresh.
        self._unsupported_endpoint_ids: set[int] = set()
        # Consecutive 404 count and last-reprobe timestamp per endpoint.
        self._consecutive_404: dict[int, int] = {}
        self._unsupported_since: dict[int, float] = {}
        # Per-endpoint poll state, kept across thread restarts so a restarted
        # loop does not re-report statuses that were already delivered.
        self._known_statuses: dict[int, tuple[Any, ...]] = {}
        self._consecutive_failures: dict[int, int] = {}
        # A Controller rejection is not delivery, so it must not be added to
        # _known_statuses. Keep a short signature-scoped cooldown instead to
        # avoid polling the Controller once per tick for a permanent rejection.
        self._rejected_statuses: dict[int, tuple[tuple[Any, ...], float]] = {}
        self._instance_id: int | None = None
        self._guard_callback = guard_callback
        self._finalize_callback = finalize_callback

    @staticmethod
    def _compute_enabled(config: NodeManagerConfig) -> bool:
        """Engine capability is the only switch for FT status polling."""
        return config.basic_config.ft_capability.enabled

    def start(self, endpoints: list[Endpoint] | None = None, instance_id: int | None = None) -> None:
        """Start the background polling thread (no-op when fault tolerance is disabled)."""
        if not self._enabled:
            return
        if self._thread is not None and self._thread.is_alive():
            logger.debug("EngineFtManager thread already running")
            return
        if endpoints is not None:
            self._endpoints = endpoints
        if instance_id is not None:
            self._instance_id = instance_id
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._main_loop,
            daemon=True,
            name="engine_ft_manager",
        )
        self._thread.start()
        logger.info("EngineFtManager started.")

    def stop(self) -> None:
        """Stop the polling thread, waiting through a full poll round for it to exit."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            # Endpoints are polled concurrently, so one round is bounded by one
            # poll timeout rather than endpoint count times poll timeout.
            with self._config_lock:
                poll_timeout = self._config.fault_tolerance_config.poll_timeout_sec
                round_time = poll_timeout
            join_timeout = max(5.0, round_time + 1.0)
            self._thread.join(timeout=join_timeout)
            if self._thread.is_alive():
                # Keep the reference so start() refuses to spawn a second
                # polling thread while the old one is still alive.
                logger.warning(
                    "EngineFtManager thread did not stop within timeout; %s",
                    "keeping reference to avoid duplicate threads",
                )
                return
        self._thread = None
        logger.info("EngineFtManager stopped.")

    def update_config(self, config: NodeManagerConfig, endpoints: list[Endpoint]) -> None:
        """Apply a new config and endpoint set, (re)starting or stopping the reporter as needed."""
        with self._config_lock:
            old_enable = self._enabled
            old_endpoint_ids = {ep.id for ep in self._endpoints}
            self._config = config
            self._endpoints = endpoints
            self._enabled = self._compute_enabled(config)
            # A new endpoint set means fresh engines/config — retry the FT API.
            self._clear_unsupported_endpoints()

        new_endpoint_ids = {ep.id for ep in endpoints}
        endpoints_changed = old_endpoint_ids != new_endpoint_ids

        if self._enabled != old_enable:
            if self._enabled:
                self.start()
                logger.info("EngineFtManager enabled, started thread")
            else:
                self.stop()
                logger.info("EngineFtManager disabled, stopped thread")
        elif self._enabled and endpoints_changed:
            # Polling targets changed while enabled — restart to poll the new engines.
            logger.info(
                "EngineFtManager endpoints changed (%d -> %d), restarting",
                len(old_endpoint_ids),
                len(new_endpoint_ids),
            )
            self.stop()
            self.start()

    def retire_endpoints(self, endpoint_ids: list[int]) -> None:
        """Stop polling DP ranks committed as retired by a scale-down transaction."""
        with self._config_lock:
            self._retired_endpoint_ids.update(endpoint_ids)
            for endpoint_id in endpoint_ids:
                self._known_statuses.pop(endpoint_id, None)
                self._consecutive_failures.pop(endpoint_id, None)

    def validate_retire_endpoints(self, endpoint_ids: list[int]) -> None:
        """Validate ranks before Daemon commits retirement to any subsystem."""
        if not isinstance(endpoint_ids, list) or any(
            not isinstance(endpoint_id, int) or isinstance(endpoint_id, bool) for endpoint_id in endpoint_ids
        ):
            raise ValueError("retired endpoint ids must be a list of DP ranks")
        with self._config_lock:
            managed_ids = {endpoint.id for endpoint in self._endpoints} | self._retired_endpoint_ids
        missing = set(endpoint_ids) - managed_ids
        if missing:
            raise ValueError("cannot retire unmanaged FT endpoint ids: %s" % sorted(missing))

    def _select_endpoints(self, endpoint_ids: list[int]) -> list[Endpoint]:
        if (
            not isinstance(endpoint_ids, list)
            or not endpoint_ids
            or any(not isinstance(endpoint_id, int) or isinstance(endpoint_id, bool) for endpoint_id in endpoint_ids)
            or len(endpoint_ids) != len(set(endpoint_ids))
        ):
            raise ValueError("endpoint_ids must be a non-empty list of unique DP ranks")
        with self._config_lock:
            endpoints = {
                endpoint.id: endpoint for endpoint in self._endpoints if endpoint.id not in self._retired_endpoint_ids
            }
        missing = set(endpoint_ids) - set(endpoints)
        if missing:
            raise ValueError("endpoint_ids are not managed by this NodeManager: %s" % sorted(missing))
        return [endpoints[endpoint_id] for endpoint_id in endpoint_ids]

    def query(self, endpoint_ids: list[int], timeout: float) -> dict[int, dict[str, Any]]:
        """Refresh selected ranks concurrently and return the unified FT snapshot.

        Transport failures are returned as unknown. Only an engine response
        declaring ``dead`` is authoritative evidence for irreversible removal.
        """
        endpoints = self._select_endpoints(endpoint_ids)

        def refresh(endpoint: Endpoint) -> tuple[int, dict[str, Any]]:
            try:
                entry = query_engine_ft_entry(endpoint, timeout)
            except Exception as error:
                entry = {
                    "id": endpoint.id,
                    "status": "unknown",
                    "fault_info": str(error),
                    "source": "poll_error",
                }
            if entry["status"] != "unknown":
                self._process_engine_status(endpoint.id, entry)
            return endpoint.id, entry

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(endpoints)) as executor:
            return dict(executor.map(refresh, endpoints))

    def apply(
        self,
        endpoint_ids: list[int],
        instruction: str,
        params: dict[str, Any],
        request_id: str,
        timeout: float,
        dp_store_port: int,
    ) -> None:
        if instruction not in {"retry", "scale_down"}:
            raise ValueError("unsupported engine FT instruction: %s" % instruction)
        endpoints = self._select_endpoints(endpoint_ids)
        engine_params = dict(params)
        if instruction == "scale_down":
            engine_params.setdefault("dp_store_port", dp_store_port)
        apply_engine_ft_instructions(endpoints, instruction, engine_params, request_id, timeout)

    def guard(self, request_id: str, freeze_seconds: float) -> None:
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("guard request_id must be a non-empty string")
        if (
            isinstance(freeze_seconds, bool)
            or not isinstance(freeze_seconds, (int, float))
            or not 0 < freeze_seconds <= MAX_GUARD_LEASE_SEC
        ):
            raise ValueError("guard lease_sec must be in range (0, 300]")
        if self._guard_callback is None:
            raise RuntimeError("engine FT guard callback is not configured")
        self._guard_callback(request_id, freeze_seconds)

    def finalize(self, request_id: str, retired_endpoint_ids: list[int], commit: bool) -> None:
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("finalize request_id must be a non-empty string")
        if self._finalize_callback is None:
            raise RuntimeError("engine FT finalize callback is not configured")
        self._finalize_callback(request_id, retired_endpoint_ids, commit)

    def reset_retired_endpoints(self) -> None:
        """Clear retirement state when a new engine topology is pulled."""
        with self._config_lock:
            self._retired_endpoint_ids.clear()

    def _is_retired(self, endpoint_id: int) -> bool:
        with self._config_lock:
            return endpoint_id in self._retired_endpoint_ids

    def _poll_interval_sec(self) -> float:
        with self._config_lock:
            return self._config.fault_tolerance_config.poll_interval_sec

    def _query_engine_status(self, ep: Endpoint) -> dict:
        """GET the FT status entry matching this endpoint's global DP rank."""
        with self._config_lock:
            timeout = self._config.fault_tolerance_config.poll_timeout_sec
        return query_engine_ft_entry(ep, timeout)

    def pause(self) -> None:
        """Suspend polling while the engines are being relaunched.

        During a relaunch the engines are killed and re-pulled — their FT
        endpoints are unreachable, so the poll failures would be reported as
        deaths while the relaunch is in progress. The loop idles on the pause
        event and resumes where it left off.
        """
        self._pause_event.set()
        logger.info("EngineFtManager paused (engine relaunch in progress)")

    def resume(self) -> None:
        """Resume polling after the relaunch; reset per-endpoint poll state.

        The engines are fresh processes, so their old statuses and
        poll-failure counts are no longer meaningful.
        """
        self._pause_event.clear()
        self._known_statuses.clear()
        self._consecutive_failures.clear()
        self._rejected_statuses.clear()
        # Re-pulled engines may run a new image — retry the FT API once.
        self._clear_unsupported_endpoints()
        logger.info("EngineFtManager resumed after engine relaunch")

    def _clear_unsupported_endpoints(self) -> None:
        """Drop all FT-API-unsupported classifications and their probe state."""
        self._unsupported_endpoint_ids.clear()
        self._consecutive_404.clear()
        self._unsupported_since.clear()

    def _main_loop(self) -> None:
        """Poll every engine's FT status and forward faults to Controller."""
        logger.info("EngineFtManager loop started.")

        while not self._stop_event.is_set():
            if self._pause_event.is_set():
                if self._stop_event.wait(self._poll_interval_sec()):
                    break
                continue
            with self._config_lock:
                endpoints = [endpoint for endpoint in self._endpoints if endpoint.id not in self._retired_endpoint_ids]
            if endpoints:
                with concurrent.futures.ThreadPoolExecutor(max_workers=len(endpoints)) as executor:
                    list(executor.map(self._poll_engine, endpoints))
            if self._stop_event.wait(self._poll_interval_sec()):
                break

        logger.info("EngineFtManager loop stopped.")

    def _poll_engine(self, ep: Endpoint) -> None:
        """Poll one engine and report it DEAD after the configured threshold."""
        if self._is_retired(ep.id):
            return
        if ep.id in self._unsupported_endpoint_ids:
            with self._config_lock:
                reprobe_interval = self._config.fault_tolerance_config.unsupported_reprobe_interval_sec
            last_probe = self._unsupported_since.get(ep.id, 0.0)
            if time.monotonic() - last_probe < reprobe_interval:
                return
            # Long-period re-probe: the engine image may have gained the FT
            # API since the classification, so it must not be skipped forever.
            logger.debug("Re-probing FT API support for engine %d", ep.id)
        try:
            engine = self._query_engine_status(ep)
        except EngineFtEndpointUnsupported:
            # A single HTTP 404 may be a transient routing/startup artifact;
            # only sustained 404s classify the endpoint as FT-API-unsupported.
            count = self._consecutive_404.get(ep.id, 0) + 1
            self._consecutive_404[ep.id] = count
            with self._config_lock:
                max_404 = self._config.fault_tolerance_config.max_consecutive_404
            if ep.id in self._unsupported_endpoint_ids:
                self._unsupported_since[ep.id] = time.monotonic()
                logger.debug(
                    "Engine %d still has no FT status API (HTTP 404); next re-probe later",
                    ep.id,
                )
            elif count >= max_404:
                self._unsupported_endpoint_ids.add(ep.id)
                self._unsupported_since[ep.id] = time.monotonic()
                self._consecutive_failures.pop(ep.id, None)
                logger.info(
                    "Engine %d does not implement the FT status API (%d consecutive HTTP 404); "
                    "skipping FT polling until the periodic re-probe",
                    ep.id,
                    count,
                )
            return
        except Exception as e:
            # A poll failure must never kill the polling thread. Require the
            # configured consecutive threshold before classifying it as DEAD.
            failures = self._consecutive_failures.get(ep.id, 0) + 1
            self._consecutive_failures[ep.id] = failures
            _rl.error_window(
                f"node_manager.engine_ft_manager.poll.{ep.id}",
                f"Failed to poll engine {ep.id} FT status: {e}",
            )
            with self._config_lock:
                max_failures = self._config.fault_tolerance_config.max_poll_failures
            if failures >= max_failures:
                self._report_unreachable_dead(ep, failures)
            return

        # Any non-404 answer proves the FT API exists on this endpoint.
        self._consecutive_404.pop(ep.id, None)
        self._unsupported_endpoint_ids.discard(ep.id)
        self._unsupported_since.pop(ep.id, None)
        self._consecutive_failures[ep.id] = 0
        try:
            self._process_engine_status(ep.id, engine)
        except Exception as e:
            # A malformed payload must never kill the polling thread — log and
            # continue with the next round.
            _rl.error_window(
                f"node_manager.engine_ft_manager.parse.{ep.id}",
                f"Failed to parse engine {ep.id} FT status: {e}",
            )

    def _process_engine_status(
        self,
        ep_id: int,
        engine: dict,
    ) -> None:
        """Report a single engine's status if it is non-healthy and new.

        The managed endpoint id and response id are both global DP ranks and
        must match. The same id is used as the report-dedup key.
        """
        if self._is_retired(ep_id):
            return
        if not isinstance(engine, dict):
            raise TypeError(f"engine entry is not a dict: {engine!r}")
        if engine.get("id") != ep_id:
            raise ValueError("engine FT status rank mismatch: expected=%s, actual=%s" % (ep_id, engine.get("id")))
        status = engine.get("status")
        if not isinstance(status, str):
            raise TypeError(f"engine entry of endpoint {ep_id} has no valid status")

        status_signature = self._status_signature(engine)
        if status == ENGINE_STATUS_HEALTHY:
            self._known_statuses[ep_id] = status_signature
            self._rejected_statuses.pop(ep_id, None)
            return

        if self._known_statuses.get(ep_id) in (status, status_signature):
            return  # already reported
        if self._is_rejection_backoff_active(ep_id, status_signature):
            return

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
            "additional_info": {
                "engine_ft_status": dict(engine),
                "ft_state": engine.get("ft_state"),
                "last_ft_request_id": engine.get("last_ft_request_id"),
                "ft_error": engine.get("ft_error"),
                "mask": engine.get("mask"),
            },
        }
        # Only mark as reported after successful delivery to Controller
        if self._send_fault_to_controller(fault_data):
            self._known_statuses[ep_id] = status_signature
            self._rejected_statuses.pop(ep_id, None)
        else:
            self._record_rejection(ep_id, status_signature)

    @staticmethod
    def _status_signature(engine: dict) -> tuple[Any, ...]:
        """Return the FT fields that define a meaningful status transition.

        In particular, ``recovering -> failed`` keeps the engine unhealthy
        but must still reach Controller. Comparing only the health string
        would silently lose that transition.
        """
        mask = engine.get("mask")
        if isinstance(mask, list):
            mask = tuple(mask)
        return (
            engine.get("status"),
            engine.get("fault_info"),
            engine.get("ft_state"),
            engine.get("last_ft_request_id"),
            engine.get("ft_error"),
            mask,
        )

    def _report_unreachable_dead(self, ep: Endpoint, failures: int) -> None:
        """Report an unreachable ready engine as dead (deduped)."""
        if self._is_retired(ep.id):
            return
        dead_signature = self._status_signature({"status": ENGINE_STATUS_DEAD})
        if self._known_statuses.get(ep.id) in (ENGINE_STATUS_DEAD, dead_signature):
            return
        if self._is_rejection_backoff_active(ep.id, dead_signature):
            return
        fault_data = {
            "exception_type": "EngineDeadError",
            "exception_message": f"Engine unreachable after {failures} consecutive polls",
            "engine_id": ep.id,
            "engine_status": 1,
        }
        if self._send_fault_to_controller(fault_data):
            self._known_statuses[ep.id] = dead_signature
            self._rejected_statuses.pop(ep.id, None)
        else:
            self._record_rejection(ep.id, dead_signature)

    def _is_rejection_backoff_active(self, ep_id: int, status_signature: tuple[Any, ...]) -> bool:
        """Return whether this unchanged rejected status is still cooling down."""
        rejected = self._rejected_statuses.get(ep_id)
        return bool(rejected and rejected[0] == status_signature and time.monotonic() < rejected[1])

    def _record_rejection(self, ep_id: int, status_signature: tuple[Any, ...]) -> None:
        """Rate-limit retries for one rejected status without marking it delivered."""
        self._rejected_statuses[ep_id] = (
            status_signature,
            time.monotonic() + _REPORT_REJECT_BACKOFF_SEC,
        )

    def _send_fault_to_controller(self, fault_data: dict) -> bool:
        """Inject reporter identity and forward a single fault to Controller.

        Returns True only when the Controller accepted the report — a
        rejected report must neither deduplicate the status nor freeze the
        suicide arbitration downstream.
        """
        with self._config_lock:
            fault_data["pod_ip"] = self._config.api_config.pod_ip
            fault_data["node_manager_port"] = str(self._config.api_config.node_manager_port)
            instance_id = self._instance_id
        if instance_id is None:
            logger.error("Cannot report software fault before instance start command")
            return False
        fault_data["instance_id"] = instance_id

        logger.debug(
            "Forwarding software fault to Controller: engine_id=%s, type=%s",
            fault_data.get("engine_id"),
            fault_data.get("exception_type"),
        )
        return ControllerApiClient.report_software_fault(fault_data)
