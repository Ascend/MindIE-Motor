# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Unified instance recovery: terminate instances and precision PD-group alarm clear."""

from __future__ import annotations

from dataclasses import dataclass

from motor.common.alarm.alarm import Alarm
from motor.common.alarm.enums import Category, Cleared
from motor.common.alarm.precision_issue_alarm import PRECISION_ISSUE_ALARM_ID
from motor.common.alarm.record import Record
from motor.common.logger import get_logger
from motor.controller.api_client import NodeManagerApiClient
from motor.controller.api_client.coordinator_api_client import CoordinatorApiClient
from motor.controller.core.instance_manager import InstanceManager
from motor.controller.observability.observability import Observability

logger = get_logger(__name__)


@dataclass(frozen=True)
class PrecisionRecoveryOutcome:
    terminated: bool
    cleared: bool
    moi: str = ""
    scheduler_state_cleared: bool = False


def terminate_instance_for_recovery(instance_id: int, reason: str) -> bool:
    """Isolate instance from scheduling, then request stop on all node managers.

    Used by manual terminate API, precision auto-recovery, and northbound (e.g. CCAE) callbacks.

    Returns:
        True if instance existed and stop was attempted for all node managers (all returned True).
        False if instance missing after separation or initially not found.
    """
    instance = InstanceManager().get_instance(instance_id)
    if instance is None:
        logger.error("Recovery: instance %s not found (reason=%s)", instance_id, reason)
        return False
    logger.warning("Recovery: separate_instance id=%s reason=%s", instance_id, reason)
    InstanceManager().separate_instance(instance_id)
    instance = InstanceManager().get_instance(instance_id)
    if instance is None:
        logger.error("Recovery: instance %s missing after separate_instance", instance_id)
        return False
    ok = True
    for node_mgr in instance.get_node_managers():
        ok = NodeManagerApiClient.stop(node_mgr) and ok
    return ok


def _normalize_p_id(p_instance_id: int | None) -> int | None:
    if p_instance_id is None or p_instance_id <= 0:
        return None
    return p_instance_id


def _normalize_d_id(d_instance_id: int) -> int:
    return max(d_instance_id, 0)


def is_precision_raise_alarm(record: Record) -> bool:
    """True when *record* is an uncleared precision ALARM (not CLEAR)."""
    return (
        record.alarm_id == PRECISION_ISSUE_ALARM_ID
        and record.category == Category.ALARM
        and record.cleared == Cleared.NO
    )


def terminate_pd_group(
    *,
    p_instance_id: int | None,
    d_instance_id: int,
    reason: str,
) -> bool:
    """Terminate P/D instances in a PD group. Returns False when nothing was terminated successfully."""
    p_id = _normalize_p_id(p_instance_id)
    d_id = _normalize_d_id(d_instance_id)
    all_ok = True
    attempted = False
    if d_id > 0:
        attempted = True
        logger.warning("Precision recovery: terminating D instance_id=%s reason=%s", d_id, reason)
        all_ok = terminate_instance_for_recovery(d_id, reason) and all_ok
    if p_id is not None:
        attempted = True
        logger.warning("Precision recovery: terminating P instance_id=%s reason=%s", p_id, reason)
        all_ok = terminate_instance_for_recovery(p_id, reason) and all_ok
    return attempted and all_ok if attempted else False


def complete_precision_pd_group_recovery(
    *,
    p_instance_id: int | None,
    d_instance_id: int,
    source: str,
    observability: Observability,
    raise_record: Record | None = None,
    terminate: bool = True,
    reason: str = "precision_recovery",
    report_to_om: bool = True,
) -> PrecisionRecoveryOutcome:
    """Terminate (optional), clear OM alarm for PD group, and notify Coordinator."""
    p_id = _normalize_p_id(p_instance_id)
    d_id = _normalize_d_id(d_instance_id)

    terminated = True
    if terminate:
        terminated = terminate_pd_group(
            p_instance_id=p_id,
            d_instance_id=d_id,
            reason=reason,
        )
        if not terminated:
            logger.error(
                "Precision recovery (%s): terminate failed pd_group=(%s,%s)",
                source,
                p_id,
                d_id,
            )
            return PrecisionRecoveryOutcome(terminated=False, cleared=False)

    raise_rec = raise_record
    if raise_rec is None:
        raise_rec = observability.alarm_store.find_active_precision_alarm(p_id, d_id)

    if raise_rec is None:
        logger.info(
            "Precision recovery (%s): no active precision alarm pd_group=(%s,%s), notify coordinator only",
            source,
            p_id,
            d_id,
        )
        scheduler_state_cleared = CoordinatorApiClient.notify_precision_alarm_cleared(p_id, d_id)
        return PrecisionRecoveryOutcome(
            terminated=terminated,
            cleared=False,
            scheduler_state_cleared=scheduler_state_cleared,
        )

    if not isinstance(raise_rec, Alarm):
        # Do not notify Coordinator here: OM clear is also skipped, so clearing
        # Scheduler state alone would desync AlarmStore vs Scheduler.
        logger.error("Precision recovery (%s): active precision record is not Alarm", source)
        return PrecisionRecoveryOutcome(
            terminated=terminated,
            cleared=False,
            scheduler_state_cleared=False,
        )

    if report_to_om:
        # Report both raise and clear so OM always has the raise context when
        # processing the clear; AlarmStore deduplicates the raise if already
        # present in _active_precision_alarms.
        clear_copy = raise_rec.model_copy(deep=True)
        clear_copy.clear()
        clear_copy.update_time()
        observability.add_alarms([raise_rec, clear_copy])

    moi = str(raise_rec.moi or "")
    logger.info(
        "Precision recovery (%s): cleared precision alarm moi=%s pd_group=(%s,%s)",
        source,
        moi,
        p_id,
        d_id,
    )
    scheduler_state_cleared = CoordinatorApiClient.notify_precision_alarm_cleared(p_id, d_id)
    return PrecisionRecoveryOutcome(
        terminated=terminated,
        cleared=True,
        moi=moi,
        scheduler_state_cleared=scheduler_state_cleared,
    )
