# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
#
# MindIE is licensed under both the Mulan PSL v2 and the Apache License, Version 2.0.
# You may choose to use this software under the terms of either license.
#
# ---------------------------------------------------------------------------
# Mulan PSL v2:
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
#
# Apache License, Version 2.0:
# You may obtain a copy of the License at:
#         http://www.apache.org/licenses/LICENSE-2.0
# ---------------------------------------------------------------------------
#
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the respective licenses for more details.

import time
import threading
from typing import Any
from motor.common.logger import get_logger

logger = get_logger(__name__)

DEFAULT_REQ_CONGESTION_TRIGGER_RATIO = 0.85
DEFAULT_REQ_CONGESTION_CLEAR_RATIO = 0.75
# report_alarms is a synchronous HTTP call (timeout 5s) on the inference path.
# Retry a rejected report on a later request, but not on every request.
DEFAULT_REQ_CONGESTION_REPORT_RETRY_SECONDS = 1.0


class TokenBucket:
    def __init__(self, capacity: int, refill_rate: float):
        """

        Args:
            capacity: Bucket capacity (maximum number of tokens)
            refill_rate: Token refill rate (tokens per second)
        """
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.tokens = capacity
        self.last_refill = time.time()
        self._lock = threading.Lock()

    def try_consume(self, tokens: int = 1, floor: int = 0) -> bool:
        """

        Args:
            tokens: Number of tokens to consume
            floor: Minimum token level that must remain after consumption. Normal
                requests pass their reserve floor here; high-priority requests
                pass 0 so they may dip into the reserve.

        Returns:
            bool: Whether successfully consumed tokens
        """
        with self._lock:
            now = time.time()
            elapsed = now - self.last_refill
            tokens_to_add = elapsed * self.refill_rate
            self.tokens = min(self.capacity, self.tokens + tokens_to_add)
            self.last_refill = now

            # Check if there are enough tokens while honoring the floor
            if self.tokens - tokens >= floor:
                self.tokens -= tokens
                return True
            return False

    def get_available_tokens(self) -> int:
        """Get current available token count"""
        with self._lock:
            now = time.time()
            elapsed = now - self.last_refill
            tokens_to_add = elapsed * self.refill_rate
            available = min(self.capacity, self.tokens + tokens_to_add)
            return int(available)

    def update_params(self, capacity: int, refill_rate: float) -> None:
        """Thread-safely update bucket parameters (for hot reload).

        Update capacity and refill_rate. If current tokens exceed the new capacity,
        truncate to the new capacity. If capacity increases, add the delta to current
        tokens so that additional quota is immediately available after expansion.

        Negative values are rejected: capacity must be >= 0, refill_rate must be >= 0.
        On invalid input the current parameters are kept unchanged and a warning is logged.

        Args:
            capacity: New bucket capacity.
            refill_rate: New token refill rate (tokens per second).
        """
        if capacity < 0 or refill_rate < 0:
            logger.warning(
                "Reject token bucket update due to invalid params: capacity=%s (expected >= 0), "
                "refill_rate=%s (expected >= 0); keep current capacity=%s, refill_rate=%s",
                capacity,
                refill_rate,
                self.capacity,
                self.refill_rate,
            )
            return

        with self._lock:
            now = time.time()
            old_capacity = self.capacity
            # 按旧速率结算已累积的 Token
            elapsed = now - self.last_refill
            if elapsed > 0:
                self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last_refill = now

            self.capacity = capacity
            self.refill_rate = refill_rate

            if self.tokens > capacity:
                self.tokens = capacity
            elif capacity > old_capacity:
                added_capacity = capacity - old_capacity
                self.tokens = min(capacity, self.tokens + added_capacity)


class SimpleRateLimiter:
    def __init__(
        self,
        max_requests: int = 100,
        window_size: int = 60,
        priority_reserve_ratio: float = 0.0,
        priority_admission_level: int = 3,
    ):
        """

        Args:
            max_requests: Maximum number of requests in time window
            window_size: Time window size (seconds)
            priority_reserve_ratio: Fraction of capacity reserved for high-priority
                requests (Agent Hint `priority` >= priority_admission_level); normal
                requests stop consuming at the reserve floor. 0 disables the reserve.
            priority_admission_level: Hint priority at/above which a request is
                high-priority (shared with the scheduler soft-load gate).
        """
        self.max_requests = max_requests
        self.window_size = window_size
        self.priority_admission_level = priority_admission_level

        # Calculate token bucket parameters
        self.capacity = max_requests  # Bucket capacity equals maximum requests
        self.refill_rate = max_requests / window_size  # Tokens added per second
        self._congestion_alarm_sent = False
        self._congestion_alarm_retry_at = 0.0

        self.priority_reserve_ratio = priority_reserve_ratio
        self._reserve = self._compute_reserve()

        # Use single global token bucket
        self._bucket = TokenBucket(capacity=self.capacity, refill_rate=self.refill_rate)

        logger.info(
            "Initialized global rate limiter: max_requests=%s, window_size=%ss, priority_reserve=%s",
            max_requests,
            window_size,
            self._reserve,
        )

    def _compute_reserve(self) -> int:
        """Reserve token count from the ratio; clamped so normal requests keep a slice."""
        if self.max_requests <= 0:
            return 0
        return min(int(self.max_requests * self.priority_reserve_ratio), self.max_requests - 1)

    @staticmethod
    def _extract_priority(request_data: dict[str, Any] | None) -> int | None:
        """Priority level carried by the middleware's request data, or None."""
        if not isinstance(request_data, dict):
            return None
        priority = request_data.get("priority")
        return priority if isinstance(priority, int) else None

    def is_allowed(self, request_data: dict[str, Any] | None = None) -> tuple[bool, dict[str, Any]]:
        """

        Args:
            request_data: Request data (optional); may carry "priority" (int hint level)

        Returns:
            tuple: (whether allowed, rate limiting info)
        """
        try:
            # WP-E2 hard priority: a trusted strict_priority bypasses the global
            # token bucket entirely (never 429 here); it is not consumed so
            # strict traffic does not deplete normal capacity. Capacity governance
            # for strict traffic stays with the scheduler layer.
            strict_level = request_data.get("strict_priority") if isinstance(request_data, dict) else None
            if strict_level is not None:
                logger.info("Strict-priority request bypasses rate limit: level=%s", strict_level)
                return True, {
                    "allowed": True,
                    "available": self._bucket.get_available_tokens(),
                    "limit": self.max_requests,
                    "window_size": self.window_size,
                    "scope": "global",
                    "priority": self._extract_priority(request_data),
                    "reserve": self._reserve,
                    "strict": True,
                    "timestamp": time.time(),
                }
            priority = self._extract_priority(request_data)
            high_priority = priority is not None and priority >= self.priority_admission_level
            # High-priority requests may dip into the reserve (floor 0); normal
            # requests stop at the reserve floor (0 when the reserve is disabled).
            floor = 0 if high_priority else self._reserve
            # Try to consume one token
            allowed = self._bucket.try_consume(floor=floor)
            available = self._bucket.get_available_tokens()
            # Remaining tokens are high when the bucket is idle. Congestion follows used capacity.
            used = self.max_requests - available

            self._sync_congestion_alarm(used)

            if allowed and high_priority and available < self._reserve + 1:
                logger.info(
                    "Priority request admitted from reserve: priority=%s available=%s/%s",
                    priority,
                    available,
                    self.max_requests,
                )

            # Build rate limiting info
            limit_info = {
                "allowed": allowed,
                "available": available,
                "limit": self.max_requests,
                "window_size": self.window_size,
                "scope": "global",
                "priority": priority,
                "reserve": self._reserve,
                "timestamp": time.time(),
            }

            if not allowed:
                logger.warning(f"Request globally rate limited: available: {available}/{self.max_requests}")

            return allowed, limit_info

        except Exception as e:
            logger.error(f"Rate limiting check failed: {e}")
            # Allow request by default when error occurs
            return True, {"error": str(e), "allowed": True, "timestamp": time.time()}

    def update_config(
        self,
        max_requests: int | None = None,
        window_size: int | None = None,
        priority_reserve_ratio: float | None = None,
    ) -> None:
        """Update rate limiting parameters at runtime (for hot reload).

        Update max_requests, window_size and/or priority_reserve_ratio, and sync
        the TokenBucket's capacity and refill_rate accordingly. Passing
        priority_reserve_ratio (not just max_requests/window_size) is required:
        the reserve is derived from the ratio, so a config change to the ratio
        would otherwise never take effect on hot reload (PR #1196 review).

        Invalid values are rejected: max_requests must be >= 0, window_size must be > 0
        (a zero window_size would cause a division by zero when computing refill_rate),
        priority_reserve_ratio must be in [0, 0.9] (the config-validation range).
        On invalid input the current parameters are kept unchanged and a warning is logged.

        Args:
            max_requests: New maximum number of requests; None means no change.
            window_size: New time window in seconds; None means no change.
            priority_reserve_ratio: New reserve fraction; None means no change.
        """
        if max_requests is None and window_size is None and priority_reserve_ratio is None:
            logger.info("Skip rate limiter update: no parameter provided")
            return

        if max_requests is not None and max_requests < 0:
            logger.warning(
                "Reject rate limiter update due to invalid max_requests=%s (expected >= 0); "
                "keep current max_requests=%s, window_size=%s",
                max_requests,
                self.max_requests,
                self.window_size,
            )
            return
        if window_size is not None and window_size <= 0:
            logger.warning(
                "Reject rate limiter update due to invalid window_size=%s (expected > 0); "
                "keep current max_requests=%s, window_size=%s",
                window_size,
                self.max_requests,
                self.window_size,
            )
            return
        if priority_reserve_ratio is not None and not 0 <= priority_reserve_ratio <= 0.9:
            logger.warning(
                "Reject rate limiter update due to invalid priority_reserve_ratio=%s (expected in [0, 0.9]); "
                "keep current priority_reserve_ratio=%s",
                priority_reserve_ratio,
                self.priority_reserve_ratio,
            )
            return

        if max_requests is not None:
            self.max_requests = max_requests
        if window_size is not None:
            self.window_size = window_size
        if priority_reserve_ratio is not None:
            self.priority_reserve_ratio = priority_reserve_ratio

        # Recalculate and update TokenBucket parameters
        new_capacity = self.max_requests
        new_refill_rate = self.max_requests / self.window_size
        self._reserve = self._compute_reserve()
        self._bucket.update_params(capacity=new_capacity, refill_rate=new_refill_rate)

        logger.info(
            "Updated rate limiter: max_requests=%s, window_size=%ss, priority_reserve=%s",
            self.max_requests,
            self.window_size,
            self._reserve,
        )

    def _sync_congestion_alarm(self, used: int) -> None:
        """Report a congestion crossing, and latch state only after Controller accepts it.

        ``report_alarms`` swallows transport errors and returns ``{"ok": False}``. Setting the
        latch before that return drops the event until the opposite threshold is crossed.
        A failed report keeps the previous latch and is retried on a later request, with a
        short gap so a down Controller does not add an HTTP timeout to every inference request.
        """
        trigger = int(self.max_requests * DEFAULT_REQ_CONGESTION_TRIGGER_RATIO)
        clear = int(self.max_requests * DEFAULT_REQ_CONGESTION_CLEAR_RATIO)
        if not self._congestion_alarm_sent and used >= trigger:
            active = True
            additional_information = (
                f"The current number of inference requests in the system is {used}, "
                f"which is greater than or equal to the configured maximum number of requests "
                f"{self.max_requests}*85%."
            )
        elif self._congestion_alarm_sent and used < clear:
            active = False
            additional_information = (
                f"The current number of inference requests in the system is {used}, "
                f"which is less than the configured maximum number of requests "
                f"{self.max_requests}*75%."
            )
        else:
            return

        now = time.monotonic()
        if now < self._congestion_alarm_retry_at:
            return

        from motor.common.alarm.req_congestion_event import ReqCongestionEvent, RequestCongestionReason
        from motor.coordinator.api_client.controller_api_client import ControllerApiClient

        event = ReqCongestionEvent(
            reason_id=RequestCongestionReason.DEALING_WITH_CONGESTION,
            additional_information=additional_information,
        )
        try:
            outcome = ControllerApiClient.report_alarms(event.model_dump())
            accepted = isinstance(outcome, dict) and bool(outcome.get("ok"))
        except Exception as exc:
            accepted = False
            logger.warning(
                "Congestion alarm report failed, state unchanged, retry after %.1fs: %s",
                DEFAULT_REQ_CONGESTION_REPORT_RETRY_SECONDS,
                exc,
            )
        else:
            if not accepted:
                logger.warning(
                    "Congestion alarm report was not accepted, state unchanged, retry after %.1fs",
                    DEFAULT_REQ_CONGESTION_REPORT_RETRY_SECONDS,
                )

        if not accepted:
            self._congestion_alarm_retry_at = time.monotonic() + DEFAULT_REQ_CONGESTION_REPORT_RETRY_SECONDS
            return

        self._congestion_alarm_sent = active
        self._congestion_alarm_retry_at = 0.0
