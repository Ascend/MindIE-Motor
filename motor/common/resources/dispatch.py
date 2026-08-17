# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from enum import Enum
from typing import Any


class DispatchPlan(str, Enum):
    """Coordinator dispatch execution plan for P/D separated inference."""

    CONCURRENT_ENGINE_SYNC = "concurrent_engine_sync"
    PREFILL_HANDOFF_DECODE = "prefill_handoff_decode"


DISPATCH_PROFILE_KEY = "dispatch_profile"
KV_TRANSFER_CONFIG_KEY = "kv_transfer_config"
KV_CONNECTOR_KEY = "kv_connector"
KV_CONNECTOR_EXTRA_CONFIG_KEY = "kv_connector_extra_config"
KV_CONNECTORS_KEY = "connectors"


class DispatchProfile(str, Enum):
    """Engine-side P/D coordination profile inferred from kv_transfer configuration."""

    TRIGGER = "trigger"
    HANDOFF = "handoff"
    BOOTSTRAP = "bootstrap"
    UNKNOWN = "unknown"


_VLLM_HANDOFF_CONNECTORS = frozenset(
    {
        "mooncakeconnectorv1",
        "mooncakehybridconnector",
        "nixlconnector",
    }
)
_VLLM_TRIGGER_CONNECTORS = frozenset({"mooncakelayerwiseconnector"})


def classify_vllm_dispatch_profile(
    engine_config: Any,
    explicit_profile: str | None = None,
) -> DispatchProfile:
    """Classify vLLM P/D coordination semantics from explicit config or whitelist."""
    profile = _parse_explicit_profile(explicit_profile or _config_get(engine_config, DISPATCH_PROFILE_KEY))
    if profile is not None:
        return profile

    kv_transfer_config = _config_get(engine_config, KV_TRANSFER_CONFIG_KEY, {})
    return _classify_vllm_kv_transfer_config(kv_transfer_config)


def dispatch_capabilities_for_profile(profile: DispatchProfile) -> list[str]:
    if profile == DispatchProfile.HANDOFF:
        return [DispatchPlan.PREFILL_HANDOFF_DECODE.value]
    if profile in (DispatchProfile.TRIGGER, DispatchProfile.BOOTSTRAP):
        return [DispatchPlan.CONCURRENT_ENGINE_SYNC.value]
    return []


def _classify_vllm_kv_transfer_config(kv_transfer_config: Any) -> DispatchProfile:
    if not isinstance(kv_transfer_config, dict):
        return DispatchProfile.UNKNOWN

    connector = _normalized(kv_transfer_config.get(KV_CONNECTOR_KEY))
    if connector == "multiconnector":
        return _classify_vllm_multi_connector(kv_transfer_config)

    if connector in _VLLM_HANDOFF_CONNECTORS:
        return DispatchProfile.HANDOFF
    if connector in _VLLM_TRIGGER_CONNECTORS:
        return DispatchProfile.TRIGGER
    return DispatchProfile.UNKNOWN


def _classify_vllm_multi_connector(kv_transfer_config: dict[str, Any]) -> DispatchProfile:
    extra_config = kv_transfer_config.get(KV_CONNECTOR_EXTRA_CONFIG_KEY, {})
    if not isinstance(extra_config, dict):
        return DispatchProfile.UNKNOWN
    connectors = extra_config.get(KV_CONNECTORS_KEY, [])
    if not isinstance(connectors, list) or len(connectors) < 2:
        return DispatchProfile.UNKNOWN

    transport_connector = connectors[0]
    if not isinstance(transport_connector, dict):
        return DispatchProfile.UNKNOWN
    return _classify_vllm_kv_transfer_config(transport_connector)


def _parse_explicit_profile(value: Any) -> DispatchProfile | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    if normalized == DispatchPlan.PREFILL_HANDOFF_DECODE.value:
        return DispatchProfile.HANDOFF
    if normalized == DispatchPlan.CONCURRENT_ENGINE_SYNC.value:
        return DispatchProfile.TRIGGER
    try:
        profile = DispatchProfile(normalized)
    except ValueError as exc:
        allowed = ", ".join(profile.value for profile in DispatchProfile if profile != DispatchProfile.UNKNOWN)
        raise ValueError(f"Unsupported dispatch_profile {value!r}. Allowed values: {allowed}.") from exc
    if profile == DispatchProfile.UNKNOWN:
        return None
    return profile


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    if isinstance(config, dict):
        return config.get(key, default)
    getter = getattr(config, "get", None)
    if getter is not None:
        return getter(key, default)
    return getattr(config, key, default)


def _normalized(value: Any) -> str:
    return str(value or "").strip().lower()
