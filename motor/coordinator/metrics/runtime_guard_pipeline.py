# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.

"""Runtime Guard detection feeding the scheduler's precision alarm state."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from motor.common.alarm.precision_issue_alarm import build_precision_issue_alarm, build_precision_issue_clear_alarm
from motor.common.logger import get_logger
from motor.coordinator.api_client.controller_api_client import ControllerApiClient
from motor.coordinator.metrics.runtime_guard_monitor import _read_anomaly_samples

logger = get_logger(__name__)


def _format_samples(samples: dict[str, float]) -> str:
    return ",".join(f"{name}={value:g}" for name, value in sorted(samples.items()))


@dataclass
class _EndpointState:
    successes: int = 0
    values: dict[str, float] = field(default_factory=dict)
    generation: float | None = None
    normal_ready: bool = False


class RuntimeGuardPipeline:
    """Process scrape batches off the metrics thread and share master alarm state."""

    def __init__(
        self,
        *,
        parser: Callable,
        scheduler_provider: Callable,
        probe_provider: Callable,
        config_provider: Callable,
        report: Callable = ControllerApiClient.report_alarms,
        interval: int = 10,
    ) -> None:
        self._parser = parser
        self._scheduler_provider = scheduler_provider
        self._probe_provider = probe_provider
        self._config_provider = config_provider
        self._report = report
        self._interval = interval
        self._states: dict[tuple[int, int], _EndpointState] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    async def ingest(self, instance_id: int, model: str, scrapes: list[tuple[int, str, str | None]]) -> None:
        missing = sum(1 for _, _, text in scrapes if not text)
        logger.debug(
            "[PrecisionDetection] ingest instance=%s model=%s endpoints=%d missing=%d",
            instance_id,
            model,
            len(scrapes),
            missing,
        )
        lock = self._locks.setdefault(instance_id, asyncio.Lock())
        async with lock:
            await self._ingest_locked(instance_id, model, scrapes)

    async def _ingest_locked(self, instance_id: int, model: str, scrapes: list[tuple[int, str, str | None]]) -> None:
        endpoint_ids = {endpoint_id for endpoint_id, _, _ in scrapes}
        known_ids = {endpoint_id for iid, endpoint_id in self._states if iid == instance_id}
        disappeared = sorted(known_ids - endpoint_ids)
        for eid in disappeared:
            self._states[(instance_id, eid)].normal_ready = False
        if disappeared:
            logger.info("[PrecisionDetection] instance=%s disappeared endpoints=%s", instance_id, disappeared)
        changes: list[tuple[int, str, int, dict[str, float], float | None]] = []
        evaluated: list[tuple[_EndpointState, dict[str, float], float | None]] = []
        for endpoint_id, port, text in scrapes:
            state = self._states.setdefault((instance_id, endpoint_id), _EndpointState())
            if not text:
                state.normal_ready = False
                logger.info(
                    "[PrecisionDetection] scrape empty instance=%s endpoint=%s port=%s",
                    instance_id,
                    endpoint_id,
                    port,
                )
                continue
            samples, generation, readable = _read_anomaly_samples(text, self._parser)
            if not readable:
                state.normal_ready = False
                logger.info(
                    "[PrecisionDetection] scrape unreadable instance=%s endpoint=%s port=%s",
                    instance_id,
                    endpoint_id,
                    port,
                )
                continue
            state.successes += 1
            if state.successes < self._interval:
                logger.debug(
                    "[PrecisionDetection] collecting instance=%s endpoint=%s port=%s progress=%d/%d",
                    instance_id,
                    endpoint_id,
                    port,
                    state.successes,
                    self._interval,
                )
                continue
            state.successes = 0
            # A missing series is zero, but a page without any Runtime Guard samples
            # cannot prove a normal detector observation.
            if not samples:
                state.normal_ready = False
                state.values = {}
                state.generation = generation
                logger.debug(
                    "[PrecisionDetection] window instance=%s endpoint=%s port=%s no runtime guard series",
                    instance_id,
                    endpoint_id,
                    port,
                )
                continue
            if any(not math.isfinite(value) or value < 0 for value in samples.values()):
                state.normal_ready = False
                logger.info(
                    "[PrecisionDetection] window instance=%s endpoint=%s port=%s invalid counter samples=%s",
                    instance_id,
                    endpoint_id,
                    port,
                    _format_samples(samples),
                )
                continue
            previous = state.values
            generation_reset = (
                state.generation is not None and generation is not None and state.generation != generation
            )
            if generation_reset:
                previous = {}
            delta = 0.0
            for incident, value in samples.items():
                old = previous.get(incident, 0.0)
                if value >= old:
                    delta += value - old
            evaluated.append((state, samples, generation))
            (logger.info if delta > 0 else logger.debug)(
                "[PrecisionDetection] window instance=%s endpoint=%s port=%s delta=%s samples=%s generation_reset=%s",
                instance_id,
                endpoint_id,
                port,
                int(delta),
                _format_samples(samples),
                generation_reset,
            )
            if delta > 0:
                state.normal_ready = False
                changes.append((endpoint_id, port, int(delta), samples, generation))
            else:
                state.normal_ready = True
        if changes:
            for eid in endpoint_ids:
                self._states[(instance_id, eid)].normal_ready = False
        if not changes and (
            not scrapes
            or not known_ids.issubset(endpoint_ids)
            or not all(self._states[(instance_id, eid)].normal_ready for eid in endpoint_ids)
        ):
            for state, samples, generation in evaluated:
                state.values = samples
                state.generation = generation
            logger.debug(
                "[PrecisionDetection] instance=%s hold evaluated=%d disappeared=%s",
                instance_id,
                len(evaluated),
                disappeared,
            )
            return
        scheduler = self._scheduler_provider()
        if scheduler is None:
            logger.warning("[PrecisionDetection] instance=%s scheduler unavailable", instance_id)
            return
        config = self._config_provider().precision_detection_config
        key = (None, instance_id)
        logger.debug(
            "[PrecisionDetection] instance=%s record has_issue=%s changes=%d clear_threshold=%s",
            instance_id,
            bool(changes),
            len(changes),
            config.precision_clear_threshold,
        )
        streak = await scheduler.record_precision_result(
            key, bool(changes), 1, clear_threshold=config.precision_clear_threshold, check_valid=True
        )
        if streak is None or streak.skip:
            logger.debug(
                "[PrecisionDetection] instance=%s scheduler result skipped empty=%s skip=%s",
                instance_id,
                streak is None,
                getattr(streak, "skip", None),
            )
            return
        action = "raise" if streak.threshold_hit else "clear" if streak.clear_threshold_hit else ""
        (logger.info if action else logger.debug)(
            "[PrecisionDetection] instance=%s streak action=%s consecutive=%s threshold_hit=%s clear_hit=%s",
            instance_id,
            action or "none",
            getattr(streak, "consecutive", None),
            streak.threshold_hit,
            streak.clear_threshold_hit,
        )
        success = True
        if action:
            success = await self._execute(scheduler, key, action, streak, changes, model, config)
        if success:
            for state, samples, generation in evaluated:
                state.values = samples
                state.generation = generation
            if not changes:
                for eid in endpoint_ids:
                    self._states[(instance_id, eid)].normal_ready = False

    async def _execute(
        self, scheduler: Any, key: tuple, action: str, streak: Any, changes: list, model: str, config: Any
    ) -> bool:
        success = False
        moi = streak.alarm_moi or ""
        cleared = False
        try:
            if action == "clear":
                if not moi:
                    logger.warning("[PrecisionDetection] clear skipped instance=%s missing moi", key[1])
                    return False
                logger.info("[PrecisionDetection] clear instance=%s moi=%s", key[1], moi)
                payload = build_precision_issue_clear_alarm(
                    p_instance_id=None, d_instance_id=key[1], alarm_moi=moi, model_id=model
                )
            else:
                probe = self._probe_provider()
                if probe is None:
                    logger.warning("[PrecisionDetection] probe unavailable instance=%s", key[1])
                    return False
                failures = 0
                for endpoint_id, port, delta, _, _ in changes:
                    logger.info(
                        "[PrecisionDetection] probe start instance=%s endpoint=%s port=%s delta=%s",
                        key[1],
                        endpoint_id,
                        port,
                        delta,
                    )
                    try:
                        result = await probe.run(
                            p_instance_id=None,
                            d_instance_id=key[1],
                            d_endpoint_id=endpoint_id,
                            model=model,
                            max_attempts=1,
                            timeout_seconds=config.probe_timeout_seconds,
                        )
                        failures += result.failures
                        logger.info(
                            "[PrecisionDetection] probe done instance=%s endpoint=%s failures=%s",
                            key[1],
                            endpoint_id,
                            result.failures,
                        )
                    except Exception:
                        logger.exception(
                            "[PrecisionDetection] probe failed instance=%s endpoint=%s", key[1], endpoint_id
                        )
                        failures += 1
                payload = build_precision_issue_alarm(
                    p_instance_id=None,
                    d_instance_id=key[1],
                    precision_issue_count=sum(change[2] for change in changes),
                    probe_failure_count=failures,
                    model_id=model,
                )
                ports = sorted({port for _, port, _, _, _ in changes})
                payload["additional_information"] += f", abnormal_ports=[{','.join(ports)}]"
                moi = payload["moi"]
            if asyncio.iscoroutinefunction(self._report):
                outcome = await self._report(payload)
            else:
                outcome = await asyncio.to_thread(self._report, payload)
            success = bool(outcome.get("ok"))
            logger.info(
                "[PrecisionDetection] %s reported instance=%s ok=%s auto_recovery_cleared=%s",
                action,
                key[1],
                success,
                bool(outcome.get("precision_alarm_cleared_by_auto_recovery")),
            )
            cleared = bool(
                outcome.get("precision_alarm_cleared") or outcome.get("precision_alarm_cleared_by_auto_recovery")
            )
        except Exception:
            logger.exception("[PrecisionDetection] %s action failed instance=%s", action, key[1])
        finally:
            if streak.action_token:
                finished = await scheduler.finish_precision_action(
                    key,
                    streak.action_token,
                    action_type=action,
                    success=success,
                    alarm_moi=moi if action == "raise" else None,
                    auto_recovery_cleared=cleared,
                )
                if not finished:
                    logger.warning("[PrecisionDetection] failed to finish precision action instance=%s", key[1])
                    success = False
        return success
