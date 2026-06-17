# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from motor.common.resources.dispatch import DispatchPlan, dispatch_plans_from_capabilities, shared_dispatch_plans
from motor.coordinator.domain import ScheduledResource


class DispatchPlanNotSupported(RuntimeError):
    pass


def select_dispatch_plan_for_pair(
    *,
    prefill: ScheduledResource | None,
    decode: ScheduledResource | None,
) -> DispatchPlan:
    explicit_plan = _select_explicit_plan(prefill, decode)
    if explicit_plan is not None:
        return explicit_plan
    raise DispatchPlanNotSupported(
        "Selected P/D instances do not advertise a shared dispatch capability; "
        "configure a supported engine connector or dispatch_profile"
    )


def _select_explicit_plan(
    prefill: ScheduledResource | None,
    decode: ScheduledResource | None,
) -> DispatchPlan | None:
    prefill_instance = prefill.instance if prefill is not None else None
    decode_instance = decode.instance if decode is not None else None
    prefill_plans = dispatch_plans_from_capabilities(getattr(prefill_instance, "dispatch_capabilities", None))
    decode_plans = dispatch_plans_from_capabilities(getattr(decode_instance, "dispatch_capabilities", None))
    if not prefill_plans or not decode_plans:
        return None

    supported = shared_dispatch_plans(prefill_instance, decode_instance)

    preferred = [
        DispatchPlan.CONCURRENT_ENGINE_SYNC,
        DispatchPlan.PREFILL_HANDOFF_DECODE,
    ]
    for plan in preferred:
        if plan in supported:
            return plan
    raise DispatchPlanNotSupported("Selected P/D instances have no shared dispatch capability")
