# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from __future__ import annotations

import hashlib
import os
from typing import Any

from motor.coordinator.domain import ScheduledResource
from motor.coordinator.router.dispatch_session import AttemptContext

_SGLANG_ENGINE_TYPE = "sglang"


def is_sglang_resource(resource: ScheduledResource | None) -> bool:
    """Return True if the scheduled resource reports engine_type=sglang."""
    if resource is None or resource.instance is None:
        return False
    engine_type = str(getattr(resource.instance, "engine_type", "") or "").strip().lower()
    return engine_type == _SGLANG_ENGINE_TYPE


def ensure_sglang_pd_pair(attempt: AttemptContext) -> None:
    """Require both P and D legs to be SGLang for native bootstrap PD."""
    prefill = attempt.prefill_resource
    decode = attempt.decode_resource
    if not is_sglang_resource(prefill) or not is_sglang_resource(decode):
        prefill_engine = prefill.instance.engine_type if prefill and prefill.instance else None
        decode_engine = decode.instance.engine_type if decode and decode.instance else None
        raise RuntimeError(
            "SGLang native PD requires both prefill and decode engine_type=sglang, "
            f"got prefill={prefill_engine!r} decode={decode_engine!r}."
        )


def _prefill_bootstrap_host(attempt: AttemptContext) -> str:
    """Return Prefill endpoint IP."""
    resource = attempt.prefill_resource
    if resource is None or resource.endpoint is None:
        raise RuntimeError("SGLang PD requires a scheduled prefill endpoint for bootstrap_host")
    return resource.endpoint.ip


def _bootstrap_port() -> str:
    """Read and validate DISAGGREGATION_BOOTSTRAP_PORT."""
    raw = os.getenv("DISAGGREGATION_BOOTSTRAP_PORT", "").strip()
    try:
        port = int(raw)
    except ValueError as e:
        raise RuntimeError(f"DISAGGREGATION_BOOTSTRAP_PORT must be an integer (e.g. 8998), got {raw!r}.") from e
    if not 1 <= port <= 65535:
        raise RuntimeError(f"DISAGGREGATION_BOOTSTRAP_PORT must be in range 1-65535, got {port}.")
    return str(port)


def _stable_bootstrap_room(pair_id: str, attempt_seq: int) -> int:
    """Derive a stable positive int63 bootstrap_room for a P/D attempt."""
    raw = f"{pair_id}:{attempt_seq}".encode("utf-8")
    digest = hashlib.blake2b(raw, digest_size=8).digest()
    return int.from_bytes(digest, "big") & ((1 << 63) - 1)


def inject_sglang_pd_fields(req: dict[str, Any], attempt: AttemptContext) -> None:
    """Mutate ``req`` in place with stock SGLang PD fields (no ``_motor_dispatch``)."""
    ensure_sglang_pd_pair(attempt)
    req["bootstrap_host"] = _prefill_bootstrap_host(attempt)
    req["bootstrap_port"] = _bootstrap_port()
    req["bootstrap_room"] = _stable_bootstrap_room(attempt.pair_id, attempt.attempt_seq)
    req.setdefault("request_id", f"{attempt.root_request_id}#a{attempt.attempt_seq}")
