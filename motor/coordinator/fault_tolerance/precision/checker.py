# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
#
# MindIE is licensed under Mulan PSL v2.
# You may use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of the Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Precision check algorithms (ABC, WHL integration, and msprobe)."""

from __future__ import annotations

import asyncio
import os
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from motor.common.logger import get_logger

logger = get_logger(__name__)

# Lazy singleton for ILLDetector (msprobe). ``None`` means not initialised;
# the first MsprobeChecker construction imports the module and caches it here.
_msprobe_detector: Any = None

# ILLDetector keeps per-instance mutable state (``_garbled_count``,
# ``self.topk``, etc.) that the upstream implementation does not protect
# against concurrent calls. ``PrecisionReporter`` only locks per-PD-group,
# so different PD groups can call ``detector.run`` simultaneously and
# race on that state. We serialise msprobe invocations process-wide via
# this lock; it is held only inside ``MsprobeChecker.check`` and never
# blocks the event loop (``asyncio.to_thread`` moves the work off-loop).
_msprobe_lock = threading.Lock()


def _normalize_ill_type(v: Any) -> int:
    """Coerce msprobe ``DetectionResult.ill_type`` to a plain int.

    msprobe's dataclass currently uses ``ill_type: int = int`` (the type
    object itself) as a default; before/after that bug is fixed upstream,
    this function keeps the precision framework resilient.
    """
    if isinstance(v, type):
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


@dataclass
class CheckResult:
    has_issue: bool
    issue_type: int = 0
    confidence: float = 0.0
    detail: dict = field(default_factory=dict)


class PrecisionChecker(ABC):
    @abstractmethod
    async def check(
        self,
        prompt_token_ids: list[int],
        output_token_ids: list[int],
        logprobs: list[float],
        *,
        topk_logprobs: list[dict[int, float]] | None = None,
        model: str | None = None,
    ) -> CheckResult: ...


def _build_topk_fallback(
    output_token_ids: list[int],
    logprobs: list[float],
) -> list[dict[int, float]] | None:
    """Build a single-key topk list from token_ids + float logprobs.

    Used when topk_logprobs is empty (e.g. Completion path, or upstream
    collection gap). Returns ``None`` if lengths disagree — caller should
    fail-open in that case.
    """
    if not output_token_ids or not logprobs:
        return None
    if len(output_token_ids) != len(logprobs):
        return None
    return [{int(tid): float(lp)} for tid, lp in zip(output_token_ids, logprobs)]


def _get_msprobe_detector() -> Any:
    """Return the lazily-initialised ``ILLDetector`` instance, or raise.

    Importing msprobe at import time would make the framework depend on
    msprobe for every process; the constructor is the right place to fail.
    """
    global _msprobe_detector
    if _msprobe_detector is not None:
        return _msprobe_detector
    import msprobe.response_anomaly.detector as detector_module

    base = os.path.dirname(os.path.realpath(detector_module.__file__))
    # ILLDetector's defaults load configs from CWD; the package ships them
    # next to detector.py. Resolve to the package's bundled configs to keep
    # invocation independent of CWD.
    config_path = os.path.join(base, "configs", "config.yaml")
    mtype_path = os.path.join(base, "configs", "mtype_config.json")
    tk2cat_path = os.path.join(base, "token2category")
    if os.path.isfile(config_path) and os.path.isdir(tk2cat_path):
        from msprobe.response_anomaly.detector import ILLDetector

        _msprobe_detector = ILLDetector(config_path, mtype_path, tk2cat_path)
    else:
        # Fall back to defaults (CWD-relative) if bundled files move.
        from msprobe.response_anomaly.detector import ILLDetector

        _msprobe_detector = ILLDetector()
    return _msprobe_detector


def reset_msprobe_detector() -> None:
    """Drop the cached ILLDetector; for tests only."""
    global _msprobe_detector
    _msprobe_detector = None


class MsprobeChecker(PrecisionChecker):
    """Production precision checker backed by msprobe ``ILLDetector``.

    Uses a process-wide ``ILLDetector`` singleton (created on first use) to
    avoid reloading yaml/json/tk2cat on every sample.
    """

    async def check(
        self,
        prompt_token_ids: list[int],
        output_token_ids: list[int],
        logprobs: list[float],
        *,
        topk_logprobs: list[dict[int, float]] | None = None,
        model: str | None = None,
    ) -> CheckResult:
        _ = prompt_token_ids  # msprobe's signature takes prompt only via tk2cat lookup
        if not output_token_ids:
            return CheckResult(has_issue=False)

        topk = topk_logprobs if topk_logprobs else None
        if not topk:
            topk = _build_topk_fallback(output_token_ids, logprobs)
        if not topk or len(topk) != len(output_token_ids):
            if not topk:
                reason = "no_logprobs"
                detail = "logprobs empty"
            else:
                reason = "length_mismatch"
                detail = f"topk={len(topk)} tokens={len(output_token_ids)}"
            logger.warning(
                "MsprobeChecker: fail-open reason=%s %s; msprobe did NOT run detector.run for this sample",
                reason,
                detail,
            )
            return CheckResult(has_issue=False)

        try:
            detector = _get_msprobe_detector()
        except ImportError as e:
            logger.error(
                "MsprobeChecker: msprobe not installed (%s); production precision check requires it",
                e,
            )
            raise

        model_config = model or None
        try:

            def _run_with_lock() -> Any:
                # ILLDetector state is not thread-safe; serialise here.
                with _msprobe_lock:
                    return detector.run(
                        [topk],
                        [output_token_ids],
                        [model_config],
                    )

            results = await asyncio.to_thread(_run_with_lock)
        except Exception as e:
            logger.warning("MsprobeChecker: detector.run failed (fail-open): %s", e)
            return CheckResult(has_issue=False)

        if not results or not results[0]:
            logger.info(
                "MsprobeChecker: result is_ill=False ill_type=0 tokens=%d model=%s",
                len(output_token_ids),
                model_config,
            )
            return CheckResult(has_issue=False)
        is_ill, ill_type = results[0]
        normalized = _normalize_ill_type(ill_type)
        logger.info(
            "MsprobeChecker: result is_ill=%s ill_type=%s tokens=%d model=%s",
            bool(is_ill),
            normalized,
            len(output_token_ids),
            model_config,
        )
        return CheckResult(
            has_issue=bool(is_ill),
            issue_type=normalized,
        )
