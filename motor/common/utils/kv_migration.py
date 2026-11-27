# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Submit a directed Memcache drain and collect its native result."""

import threading
import time
from collections.abc import Callable

from motor.common.logger import get_logger

logger = get_logger(__name__)


def _cancel_after_timeout(drain_id: str, cancel: Callable[[str, float], dict]) -> None:
    try:
        cancel(drain_id, 1.0)
    except Exception as exc:
        logger.warning("Could not cancel expired KV drain %s: %s", drain_id, exc)


def run_directed_drain(
    submit: Callable[[], dict],
    deadline: float,
    *,
    query: Callable[[str, float], dict],
    cancel: Callable[[str, float], dict],
) -> dict:
    """Submit or reuse one job. Both exit paths wait for its terminal result within the same budget."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("KV drain budget exhausted before submission")
    submitted = submit()
    drain_id = submitted.get("drain_id") if isinstance(submitted, dict) else None
    if not isinstance(drain_id, str) or not drain_id:
        raise RuntimeError("Directed drain has no acknowledged UUID")
    logger.info("Polling accepted Memcache drain %s", drain_id)
    last_error = None
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            result = query(drain_id, remaining)
            if result.get("drain_id", drain_id) != drain_id:
                raise ValueError("Memcache returned a different drain UUID")
            state = result["state"]
            if state in ("migration_completed", "migration_failed", "migration_cancelled"):
                return {**result, "drain_id": drain_id}
            if state != "migrating":
                raise ValueError("Unexpected drain state: %s" % state)
        except Exception as exc:
            # Losing a status response does not establish the outcome and must not release the source.
            last_error = exc
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))

    # The entire budget is available for migration. Cancellation must not delay Pod shutdown.
    threading.Thread(target=_cancel_after_timeout, args=(drain_id, cancel), daemon=True).start()
    raise TimeoutError("Drain %s exceeded its deadline" % drain_id) from last_error
