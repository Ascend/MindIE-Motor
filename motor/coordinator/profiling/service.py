# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Resolve live profiling targets and control their native profilers."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from motor.common.resources.instance import Instance
from motor.config.coordinator import ProfilingConfig
from motor.config.tls_config import TLSConfig
from motor.coordinator.api_client.native_engine_api_client import NativeEngineApiClient
from motor.coordinator.profiling.models import (
    ProfilingAction,
    ProfilingResponse,
    ProfilingStartRequest,
    ProfilingTarget,
    ProfilingTargetResult,
    ProfilingTargetSelector,
    ProfilingTargetStatus,
)
from motor.coordinator.profiling.targets import (
    ProfilingTargetError,
    build_profile_prefix,
    resolve_targets,
)

InstancesProvider = Callable[[], Awaitable[dict[int, Instance]]]
EngineProfileCall = Callable[[ProfilingAction, str, "TLSConfig | None", float, dict[str, Any] | None], None]

_DETAIL_LIMIT = 256
_MAX_CONCURRENT_ENGINE_CALLS = 8


class ProfilingError(Exception):
    """Profiling request failed; ``status_code`` maps to the HTTP response."""

    status_code = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ProfilingUnavailableError(ProfilingError):
    """The Coordinator cannot access the live inference topology."""

    status_code = 503


def _truncate(detail: str) -> str:
    return detail if len(detail) <= _DETAIL_LIMIT else detail[:_DETAIL_LIMIT] + "..."


def _default_engine_call(
    action: ProfilingAction,
    address: str,
    tls_config: TLSConfig | None,
    timeout: float,
    body: dict[str, Any] | None,
) -> None:
    """Call the native engine profiling control endpoint."""
    if action == ProfilingAction.START:
        NativeEngineApiClient.start_profile(address, tls_config, timeout=timeout, body=body)
    else:
        NativeEngineApiClient.stop_profile(address, tls_config, timeout=timeout)


def _target_profile_prefix(target: ProfilingTarget, profiler_options: dict[str, Any] | None) -> str:
    """Build a per-target prefix; a caller prefix still gets the DP dimensions appended."""
    override = (profiler_options or {}).get("profile_prefix")
    if override is not None and not isinstance(override, str):
        raise ProfilingTargetError("profile_prefix must be a string")
    return build_profile_prefix(target, override)


class ProfilingService:
    """Execute stateless profiling operations against the current topology."""

    def __init__(
        self,
        config: ProfilingConfig,
        tls_config: TLSConfig | None,
        instances_provider: InstancesProvider,
        engine_call: EngineProfileCall | None = None,
    ) -> None:
        self._config = config
        self._tls_config = tls_config
        self._instances_provider = instances_provider
        self._engine_call = engine_call or _default_engine_call
        self._operation_lock = asyncio.Lock()

    def update_config(self, config: ProfilingConfig, tls_config: TLSConfig | None) -> None:
        """Apply a hot-reloaded profiling configuration."""
        self._config = config
        self._tls_config = tls_config

    async def start(self, request: ProfilingStartRequest) -> ProfilingResponse:
        """Start profiling on the selected live targets."""
        return await self._execute(ProfilingAction.START, request, request.profiler_options)

    async def stop(self, request: ProfilingTargetSelector) -> ProfilingResponse:
        """Stop the profiler currently active on each selected live target."""
        return await self._execute(ProfilingAction.STOP, request)

    async def _execute(
        self,
        action: ProfilingAction,
        selector: ProfilingTargetSelector,
        profiler_options: dict[str, Any] | None = None,
    ) -> ProfilingResponse:
        async with self._operation_lock:
            instances = await self._get_instances()
            config, tls_config = self._config, self._tls_config
            try:
                service, targets = resolve_targets(instances, selector, config.max_targets)
                # Validate every start prefix before making the first engine call.
                prefixes = [
                    _target_profile_prefix(target, profiler_options) if action == ProfilingAction.START else ""
                    for target in targets
                ]
            except ProfilingTargetError as err:
                raise ProfilingError(_truncate(str(err))) from err

            pending = iter(zip(targets, prefixes, strict=True))
            results: dict[str, ProfilingTargetResult] = {}
            cancelled = False

            async def call_targets() -> None:
                while not cancelled:
                    item = next(pending, None)
                    if item is None:
                        return
                    target, prefix = item
                    results[target.address] = await asyncio.to_thread(
                        self._call_engine,
                        action,
                        target,
                        prefix,
                        profiler_options,
                        timeout=config.request_timeout_seconds,
                        tls_config=tls_config,
                    )

            worker_count = min(len(targets), _MAX_CONCURRENT_ENGINE_CALLS)
            operation = asyncio.gather(*(call_targets() for _ in range(worker_count)), return_exceptions=True)
            try:
                outcomes = await asyncio.shield(operation)
            except asyncio.CancelledError:
                # Threads already running must finish before another control request can start.
                cancelled = True
                while not operation.done():
                    try:
                        await asyncio.wait({operation})
                    except asyncio.CancelledError:
                        continue
                raise
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    raise outcome
            return ProfilingResponse(action=action, service=service, targets=[results[t.address] for t in targets])

    def _call_engine(
        self,
        action: ProfilingAction,
        target: ProfilingTarget,
        prefix: str,
        profiler_options: dict[str, Any] | None,
        *,
        timeout: float,
        tls_config: TLSConfig | None,
    ) -> ProfilingTargetResult:
        body: dict[str, Any] | None = None
        if action == ProfilingAction.START:
            body = dict(profiler_options or {})
            body["profile_prefix"] = prefix
        try:
            self._engine_call(action, target.address, tls_config, timeout, body)
            status = ProfilingTargetStatus.STARTED if action == ProfilingAction.START else ProfilingTargetStatus.STOPPED
            detail = ""
        except Exception as err:  # pylint: disable=broad-except
            status = ProfilingTargetStatus.FAILED
            detail = _truncate(str(err) or type(err).__name__)
        return ProfilingTargetResult(
            **target.model_dump(),
            status=status,
            detail=detail,
            profile_prefix=prefix,
        )

    async def _get_instances(self) -> dict[int, Instance]:
        try:
            instances = await self._instances_provider()
        except ProfilingError:
            raise
        except Exception as err:  # pylint: disable=broad-except
            raise ProfilingUnavailableError(f"coordinator instance view is unavailable: {_truncate(str(err))}") from err
        if not instances:
            raise ProfilingUnavailableError("no available inference instances to profile")
        return instances
