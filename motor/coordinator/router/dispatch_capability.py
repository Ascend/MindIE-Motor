# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from motor.common.resources.dispatch import DispatchPlan
from motor.common.logger import get_logger
from motor.coordinator.domain import ScheduledResource


logger = get_logger(__name__)

_VLLM = "vllm"
_SGLANG = "sglang"

_ENGINE_ALIASES = {
    "vllm": _VLLM,
    "vllm_engine": _VLLM,
    "sglang": _SGLANG,
    "srt": _SGLANG,
}

_DEFAULT_ENGINE_PLANS = {
    _VLLM: {
        "pd_separate": DispatchPlan.CONCURRENT_ENGINE_SYNC,
        "cdp_separate": DispatchPlan.CONCURRENT_ENGINE_SYNC,
        "cpcd_separate": DispatchPlan.PREFILL_HANDOFF_DECODE,
        "pd_dual_dispatch": DispatchPlan.CONCURRENT_ENGINE_SYNC,
        "pd_disaggregation_single_container": DispatchPlan.CONCURRENT_ENGINE_SYNC,
    },
    _SGLANG: {
        "pd_separate": DispatchPlan.CONCURRENT_ENGINE_SYNC,
        "cdp_separate": DispatchPlan.CONCURRENT_ENGINE_SYNC,
        "cpcd_separate": DispatchPlan.CONCURRENT_ENGINE_SYNC,
        "pd_dual_dispatch": DispatchPlan.CONCURRENT_ENGINE_SYNC,
        "pd_disaggregation_single_container": DispatchPlan.CONCURRENT_ENGINE_SYNC,
    },
}


class DispatchPlanNotSupported(RuntimeError):
    pass


def select_dispatch_plan_for_pair(
    *,
    deploy_mode: str,
    prefill: ScheduledResource | None,
    decode: ScheduledResource | None,
    default_engine_type: str | None = None,
) -> DispatchPlan:
    explicit_plan = _select_explicit_plan(deploy_mode, prefill, decode)
    if explicit_plan is not None:
        return explicit_plan

    logger.warning(
        "dispatch_capabilities missing for selected P/D pair; "
        "fallback to engine_type/deploy_mode compatibility path. deploy_mode=%s",
        deploy_mode,
    )
    prefill_engine = resolve_engine_family(prefill, default_engine_type)
    decode_engine = resolve_engine_family(decode, default_engine_type)
    if prefill_engine != decode_engine:
        raise DispatchPlanNotSupported(
            f"P/D engine family mismatch: prefill={prefill_engine or 'unknown'}, decode={decode_engine or 'unknown'}"
        )

    engine = prefill_engine or _VLLM
    try:
        return _DEFAULT_ENGINE_PLANS[engine][deploy_mode]
    except KeyError as e:
        raise DispatchPlanNotSupported(
            f"Dispatch plan not supported for engine={engine}, deploy_mode={deploy_mode}"
        ) from e


def resolve_engine_family(
    resource: ScheduledResource | None,
    default_engine_type: str | None = None,
) -> str | None:
    instance = resource.instance if resource is not None else None
    endpoint = resource.endpoint if resource is not None else None
    candidates = [
        getattr(instance, "engine_type", None),
        getattr(endpoint, "engine_type", None),
        default_engine_type,
    ]
    for candidate in candidates:
        engine = _normalize_engine_family(candidate)
        if engine is not None:
            return engine
    return None


def _select_explicit_plan(
    deploy_mode: str,
    prefill: ScheduledResource | None,
    decode: ScheduledResource | None,
) -> DispatchPlan | None:
    prefill_plans = _explicit_plans(prefill)
    decode_plans = _explicit_plans(decode)
    if not prefill_plans and not decode_plans:
        return None

    supported = prefill_plans or set(DispatchPlan)
    supported &= decode_plans or set(DispatchPlan)

    preferred = (
        [
            DispatchPlan.PREFILL_HANDOFF_DECODE,
            DispatchPlan.CONCURRENT_ENGINE_SYNC,
        ]
        if deploy_mode == "cpcd_separate"
        else [
            DispatchPlan.CONCURRENT_ENGINE_SYNC,
            DispatchPlan.PREFILL_HANDOFF_DECODE,
        ]
    )
    for plan in preferred:
        if plan in supported:
            return plan
    raise DispatchPlanNotSupported(f"No shared dispatch capability for deploy_mode={deploy_mode}")


def _explicit_plans(resource: ScheduledResource | None) -> set[DispatchPlan]:
    instance = resource.instance if resource is not None else None
    raw_values = getattr(instance, "dispatch_capabilities", None) or []
    plans = set()
    for raw in raw_values:
        try:
            plans.add(DispatchPlan(str(raw)))
        except ValueError:
            continue
    return plans


def _normalize_engine_family(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    return _ENGINE_ALIASES.get(text)
