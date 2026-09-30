# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Tests for TokenBucket and SimpleRateLimiter hot reload and congestion alarm."""

import threading
import time
from unittest.mock import patch

from motor.coordinator.middleware.rate_limiter import TokenBucket, SimpleRateLimiter
from motor.coordinator.middleware.fastapi_middleware import RateLimitConfigHolder


def test_token_bucket_update_params_expands_capacity():
    """When expanding capacity, current tokens should not exceed new capacity and can be consumed normally."""
    bucket = TokenBucket(capacity=5, refill_rate=1.0)
    for _ in range(3):
        assert bucket.try_consume()
    assert bucket.get_available_tokens() == 2

    bucket.update_params(capacity=10, refill_rate=2.0)
    assert bucket.capacity == 10
    assert bucket.refill_rate == 2.0
    for _ in range(7):
        assert bucket.try_consume()
    assert not bucket.try_consume()


def test_token_bucket_update_params_shrinks_capacity_and_clamps():
    """When shrinking capacity, excess tokens should be clamped."""
    bucket = TokenBucket(capacity=10, refill_rate=1.0)
    assert bucket.get_available_tokens() == 10

    bucket.update_params(capacity=3, refill_rate=0.5)
    assert bucket.capacity == 3
    assert bucket.refill_rate == 0.5
    assert bucket.get_available_tokens() == 3

    for _ in range(3):
        assert bucket.try_consume()
    assert not bucket.try_consume()


def test_token_bucket_update_params_only_one_param():
    """Only update one parameter, the other should remain unchanged."""
    bucket = TokenBucket(capacity=10, refill_rate=2.0)

    bucket.update_params(capacity=20, refill_rate=2.0)
    assert bucket.capacity == 20
    assert bucket.refill_rate == 2.0

    bucket.update_params(capacity=20, refill_rate=5.0)
    assert bucket.capacity == 20
    assert bucket.refill_rate == 5.0


def test_token_bucket_update_params_concurrent_safety():
    """In concurrent scenarios, update_params and try_consume should not conflict."""
    bucket = TokenBucket(capacity=100, refill_rate=100.0)
    consumed_count = [0]

    def consume_tokens():
        for _ in range(50):
            if bucket.try_consume():
                consumed_count[0] += 1
            time.sleep(0.001)

    def update_params():
        for i in range(10):
            bucket.update_params(capacity=50 + i * 10, refill_rate=100.0)
            time.sleep(0.005)

    t1 = threading.Thread(target=consume_tokens)
    t2 = threading.Thread(target=consume_tokens)
    t3 = threading.Thread(target=update_params)

    t1.start()
    t2.start()
    t3.start()

    t1.join()
    t2.join()
    t3.join()

    assert consumed_count[0] >= 0
    assert bucket.capacity == 140


def test_simple_rate_limiter_update_config():
    """SimpleRateLimiter.update_config should sync bucket parameters."""
    limiter = SimpleRateLimiter(max_requests=100, window_size=60)
    assert limiter.max_requests == 100
    assert limiter.window_size == 60

    limiter.update_config(max_requests=50, window_size=30)

    assert limiter.max_requests == 50
    assert limiter.window_size == 30
    assert limiter._bucket.capacity == 50
    assert limiter._bucket.refill_rate == 50 / 30

    _, info = limiter.is_allowed()
    assert info["limit"] == 50
    assert info["window_size"] == 30


def test_simple_rate_limiter_update_only_max_requests():
    """Only update max_requests, window_size should remain unchanged."""
    limiter = SimpleRateLimiter(max_requests=100, window_size=60)

    limiter.update_config(max_requests=200)

    assert limiter.max_requests == 200
    assert limiter.window_size == 60
    assert limiter._bucket.capacity == 200
    assert limiter._bucket.refill_rate == 200 / 60


def test_simple_rate_limiter_update_only_window_size():
    """Only update window_size, max_requests should remain unchanged."""
    limiter = SimpleRateLimiter(max_requests=100, window_size=60)

    limiter.update_config(window_size=30)

    assert limiter.max_requests == 100
    assert limiter.window_size == 30
    assert limiter._bucket.capacity == 100
    assert limiter._bucket.refill_rate == 100 / 30


def test_simple_rate_limiter_update_config_with_small_window():
    """When shrinking window_size, refill_rate should increase."""
    limiter = SimpleRateLimiter(max_requests=60, window_size=60)
    assert limiter._bucket.refill_rate == 1.0

    limiter.update_config(window_size=10)
    assert limiter._bucket.refill_rate == 6.0


def test_simple_rate_limiter_update_config_reflects_in_response():
    """After update, limit_info returned by is_allowed should immediately reflect new values."""
    limiter = SimpleRateLimiter(max_requests=100, window_size=60)

    _, info1 = limiter.is_allowed()
    assert info1["limit"] == 100
    assert info1["window_size"] == 60

    limiter.update_config(max_requests=50, window_size=30)

    _, info2 = limiter.is_allowed()
    assert info2["limit"] == 50
    assert info2["window_size"] == 30


def test_rate_limit_config_holder_with_rate_limiter():
    """RateLimitConfigHolder should correctly hold the rate_limiter reference."""
    limiter = SimpleRateLimiter(max_requests=50, window_size=30)
    holder = RateLimitConfigHolder(rate_limiter=limiter)

    assert holder.rate_limiter is limiter
    assert holder.rate_limiter.max_requests == 50
    assert holder.rate_limiter.window_size == 30


def test_rate_limit_config_holder_update_rate_limiter_config():
    """Updating config via holder.rate_limiter.update_config should sync rate limiter parameters."""
    limiter = SimpleRateLimiter(max_requests=10, window_size=60)
    holder = RateLimitConfigHolder(rate_limiter=limiter)

    assert holder.rate_limiter.max_requests == 10
    assert holder.rate_limiter.window_size == 60

    holder.rate_limiter.update_config(max_requests=20, window_size=30)

    assert holder.rate_limiter.max_requests == 20
    assert holder.rate_limiter.window_size == 30
    assert holder.rate_limiter._bucket.capacity == 20
    assert holder.rate_limiter._bucket.refill_rate == 20 / 30


def _frozen_clock(start: float = 1_000_000.0):
    """Return a mutable clock and a ``time.time`` replacement that does not refill on its own."""
    now = {"t": start}

    def _time() -> float:
        return now["t"]

    return now, _time


@patch("motor.coordinator.api_client.controller_api_client.ControllerApiClient.report_alarms")
def test_idle_full_bucket_does_not_report_congestion(mock_report):
    """A full bucket means low load; remaining tokens must not trigger congestion."""
    mock_report.return_value = {"ok": True}
    limiter = SimpleRateLimiter(max_requests=100, window_size=60)

    allowed, _ = limiter.is_allowed()

    assert allowed is True
    mock_report.assert_not_called()


@patch("motor.coordinator.api_client.controller_api_client.ControllerApiClient.report_alarms")
def test_congestion_alarm_fires_when_used_capacity_reaches_85_percent(mock_report):
    """Alarm uses used=max_requests-available, so it fires only after most tokens are consumed."""
    mock_report.return_value = {"ok": True}
    _now, clock = _frozen_clock()
    with patch("motor.coordinator.middleware.rate_limiter.time.time", clock):
        limiter = SimpleRateLimiter(max_requests=100, window_size=3600)

        for _ in range(84):
            allowed, _ = limiter.is_allowed()
            assert allowed is True
        assert mock_report.call_count == 0

        allowed, info = limiter.is_allowed()

        assert allowed is True
        used = limiter.max_requests - info["available"]
        assert used >= 85
        assert mock_report.call_count == 1
        payload = mock_report.call_args[0][0]
        assert f"is {used}," in payload["additional_information"]

        limiter.is_allowed()
        assert mock_report.call_count == 1


@patch("motor.coordinator.api_client.controller_api_client.ControllerApiClient.report_alarms")
def test_congestion_clears_when_used_capacity_falls_below_75_percent(mock_report):
    """Recovery follows used capacity dropping below 75%, not remaining-token fullness."""
    mock_report.return_value = {"ok": True}
    now, clock = _frozen_clock()
    with patch("motor.coordinator.middleware.rate_limiter.time.time", clock):
        limiter = SimpleRateLimiter(max_requests=100, window_size=3600)
        for _ in range(85):
            limiter.is_allowed()
        assert mock_report.call_count == 1

        # 85 consumes leave 15 tokens. Refill 12, then the next request consumes 1,
        # so available is 26 and used is 74, below the 75% clear line.
        refill_rate = limiter.max_requests / limiter.window_size
        now["t"] += 12 / refill_rate + 1
        allowed, info = limiter.is_allowed()

    assert allowed is True
    assert mock_report.call_count == 2
    used = limiter.max_requests - info["available"]
    assert used < 75
    payload = mock_report.call_args[0][0]
    assert f"is {used}," in payload["additional_information"]


@patch("motor.coordinator.middleware.rate_limiter.DEFAULT_REQ_CONGESTION_REPORT_RETRY_SECONDS", 0)
@patch("motor.coordinator.api_client.controller_api_client.ControllerApiClient.report_alarms")
def test_congestion_report_error_does_not_block_request(mock_report):
    """A raised report must not reject a request that still has tokens, and must not latch."""
    mock_report.side_effect = [RuntimeError("controller unavailable"), {"ok": True}]
    _now, clock = _frozen_clock()
    with patch("motor.coordinator.middleware.rate_limiter.time.time", clock):
        limiter = SimpleRateLimiter(max_requests=100, window_size=3600)
        for _ in range(84):
            limiter.is_allowed()
        assert mock_report.call_count == 0

        allowed, info = limiter.is_allowed()

        assert allowed is True
        assert info["allowed"] is True
        assert mock_report.call_count == 1

        limiter.is_allowed()
        assert mock_report.call_count == 2
        limiter.is_allowed()
        assert mock_report.call_count == 2


@patch("motor.coordinator.middleware.rate_limiter.DEFAULT_REQ_CONGESTION_REPORT_RETRY_SECONDS", 0)
@patch("motor.coordinator.api_client.controller_api_client.ControllerApiClient.report_alarms")
def test_congestion_report_not_accepted_does_not_latch(mock_report):
    """``report_alarms`` returns ok=False instead of raising; that must not stick the sent flag."""
    mock_report.side_effect = [{"ok": False}, {"ok": True}]
    _now, clock = _frozen_clock()
    with patch("motor.coordinator.middleware.rate_limiter.time.time", clock):
        limiter = SimpleRateLimiter(max_requests=100, window_size=3600)
        for _ in range(85):
            limiter.is_allowed()
        assert mock_report.call_count == 1

        limiter.is_allowed()
        assert mock_report.call_count == 2
        limiter.is_allowed()
        assert mock_report.call_count == 2


@patch("motor.coordinator.api_client.controller_api_client.ControllerApiClient.report_alarms")
def test_rejected_congestion_report_is_not_retried_on_the_next_request(mock_report):
    """A down Controller must not add another synchronous report to every following request."""
    mock_report.return_value = {"ok": False}
    mono = {"t": 10.0}
    _now, clock = _frozen_clock()
    with (
        patch("motor.coordinator.middleware.rate_limiter.time.time", clock),
        patch("motor.coordinator.middleware.rate_limiter.time.monotonic", lambda: mono["t"]),
    ):
        limiter = SimpleRateLimiter(max_requests=100, window_size=3600)
        for _ in range(85):
            limiter.is_allowed()
        assert mock_report.call_count == 1

        limiter.is_allowed()
        assert mock_report.call_count == 1

        mono["t"] += 1.0
        limiter.is_allowed()
        assert mock_report.call_count == 2


@patch("motor.coordinator.api_client.controller_api_client.ControllerApiClient.report_alarms")
def test_congestion_report_error_does_not_allow_a_rejected_request(mock_report):
    """Report failure must not turn a token rejection into an allow."""
    mock_report.side_effect = RuntimeError("controller unavailable")
    _now, clock = _frozen_clock()
    with patch("motor.coordinator.middleware.rate_limiter.time.time", clock):
        limiter = SimpleRateLimiter(max_requests=1, window_size=3600)
        assert limiter.is_allowed()[0] is True

        allowed, info = limiter.is_allowed()

    assert allowed is False
    assert info["allowed"] is False
