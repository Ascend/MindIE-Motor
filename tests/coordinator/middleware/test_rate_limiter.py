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


# ---------------------------------------------------------------------------
# Phase 3: priority reserve (Agent Hint `priority` admission tiers)
# ---------------------------------------------------------------------------


def _drain_bucket(limiter: SimpleRateLimiter) -> None:
    """Consume until the bucket only has the reserve floor left for normal requests."""
    while limiter.is_allowed({"priority": None})[0]:
        pass


def test_priority_reserve_disabled_keeps_plain_bucket():
    limiter = SimpleRateLimiter(max_requests=4, window_size=60)
    assert limiter._reserve == 0
    for _ in range(4):
        assert limiter.is_allowed({})[0]
    assert not limiter.is_allowed({})[0]


def test_normal_requests_stop_at_reserve_floor():
    """Normal consumption stops at the reserve floor; the floor is not usable by normal requests."""
    limiter = SimpleRateLimiter(max_requests=4, window_size=60, priority_reserve_ratio=0.5)
    assert limiter._reserve == 2
    # Normal requests may consume capacity - reserve = 2 tokens.
    assert limiter.is_allowed({})[0]
    assert limiter.is_allowed({})[0]
    assert not limiter.is_allowed({})[0]


def test_high_priority_request_dips_into_reserve():
    """A high-priority request is admitted when only the reserve remains."""
    limiter = SimpleRateLimiter(max_requests=4, window_size=60, priority_reserve_ratio=0.5)
    assert limiter.is_allowed({})[0]
    assert limiter.is_allowed({})[0]
    assert not limiter.is_allowed({})[0]

    allowed, info = limiter.is_allowed({"priority": 3})
    assert allowed
    assert info["priority"] == 3
    assert info["reserve"] == 2


def test_high_priority_below_admission_level_gets_no_reserve():
    """Priority below the admission level is treated as a normal request."""
    limiter = SimpleRateLimiter(max_requests=4, window_size=60, priority_reserve_ratio=0.5, priority_admission_level=3)
    assert limiter.is_allowed({})[0]
    assert limiter.is_allowed({})[0]
    assert not limiter.is_allowed({"priority": 2})[0]
    assert limiter.is_allowed({"priority": 3})[0]


def test_reserve_clamped_to_keep_normal_requests_possible():
    """A 90% reserve on a tiny bucket still leaves one token for normal requests."""
    limiter = SimpleRateLimiter(max_requests=2, window_size=60, priority_reserve_ratio=0.9)
    assert limiter._reserve == 1
    assert limiter.is_allowed({})[0]
    assert not limiter.is_allowed({})[0]
    assert limiter.is_allowed({"priority": 5})[0]


def test_update_config_recomputes_reserve():
    limiter = SimpleRateLimiter(max_requests=10, window_size=60, priority_reserve_ratio=0.2)
    assert limiter._reserve == 2

    limiter.update_config(max_requests=5)

    assert limiter._reserve == 1


def test_update_config_applies_priority_reserve_ratio():
    """Review (PR #1196): a hot-reloaded reserve ratio must actually take effect."""
    limiter = SimpleRateLimiter(max_requests=10, window_size=60, priority_reserve_ratio=0.0)
    assert limiter._reserve == 0

    limiter.update_config(priority_reserve_ratio=0.5)

    assert limiter.priority_reserve_ratio == 0.5
    assert limiter._reserve == 5


def test_update_config_rejects_invalid_priority_reserve_ratio():
    limiter = SimpleRateLimiter(max_requests=10, window_size=60, priority_reserve_ratio=0.2)

    limiter.update_config(priority_reserve_ratio=1.5)
    assert limiter.priority_reserve_ratio == 0.2
    assert limiter._reserve == 2

    limiter.update_config(priority_reserve_ratio=-0.1)
    assert limiter.priority_reserve_ratio == 0.2
    assert limiter._reserve == 2


def test_inference_server_hot_reload_forwards_priority_reserve_ratio():
    """Review (PR #1196): _apply_config_changes must forward priority_reserve_ratio
    to the limiter, otherwise the reserve fraction never hot-reloads.
    """
    from motor.config.coordinator import CoordinatorConfig
    from motor.coordinator.api_server.inference_server import InferenceServer

    server = InferenceServer.__new__(InferenceServer)
    server._api_key_config = None
    server._agent_status_machine = None
    server._infer_ssl_config = None
    limiter = SimpleRateLimiter(max_requests=10, window_size=60, priority_reserve_ratio=0.0)
    holder = RateLimitConfigHolder(rate_limiter=limiter)
    server._rate_limit_config = holder

    new_config = CoordinatorConfig()
    new_config.rate_limit_config.max_requests = 10
    new_config.rate_limit_config.window_size = 60
    new_config.rate_limit_config.priority_reserve_ratio = 0.5

    server._apply_config_changes(new_config)

    assert limiter.priority_reserve_ratio == 0.5
    assert limiter._reserve == 5


def test_priority_none_and_malformed_request_data_are_normal_requests():
    limiter = SimpleRateLimiter(max_requests=4, window_size=60, priority_reserve_ratio=0.5)
    assert limiter._extract_priority(None) is None
    assert limiter._extract_priority({}) is None
    assert limiter._extract_priority({"priority": "3"}) is None
    assert limiter.is_allowed(None)[0]


def test_strict_priority_bypasses_rate_limit():
    """WP-E2: a trusted strict request is allowed even with an exhausted bucket."""
    limiter = SimpleRateLimiter(max_requests=2, window_size=60)
    limiter._bucket.tokens = 0.0
    allowed, info = limiter.is_allowed({"priority": 1, "strict_priority": 3})
    assert allowed is True
    assert info.get("strict") is True
    # bucket untouched: strict traffic does not deplete normal capacity
    assert limiter._bucket.tokens == 0.0


def test_strict_priority_absent_keeps_baseline():
    """WP-E2: without strict_priority the limiter behaves exactly as before."""
    limiter = SimpleRateLimiter(max_requests=2, window_size=60, priority_reserve_ratio=0.5)
    limiter._bucket.tokens = 0.0
    allowed, info = limiter.is_allowed({"priority": 1})
    assert allowed is False
    assert "strict" not in info


def _scope_with_strict_token(token: str) -> dict:
    import base64
    import json

    hint = base64.urlsafe_b64encode(json.dumps({"strict_priority": {"level": 3}}).encode()).decode().rstrip("=")
    return {
        "type": "http",
        "path": "/v1/completions",
        "method": "POST",
        "headers": [
            (b"x-agent-hint", hint.encode()),
            (b"x-strict-priority-token", token.encode()),
        ],
    }


def test_strict_trust_config_hot_reload_takes_effect_immediately():
    """Review 194528148: the middleware must live-read the holder — a disabled
    gate or a rotated token list has to invalidate the old snapshot at once
    (an old token bypassing the limiter is a security regression).
    """
    from motor.coordinator.middleware.fastapi_middleware import SimpleRateLimitMiddleware as FastapiMiddleware

    holder = RateLimitConfigHolder(
        strict_priority_enabled=True,
        strict_priority_tokens=["old-token"],
    )
    middleware = FastapiMiddleware(app=None, config_holder=holder)

    info = middleware._extract_request_data(_scope_with_strict_token("old-token"))
    assert info["strict_priority"] is not None, "precondition: old token trusted while enabled"

    # Hot-reload: gate disabled — the old snapshot must stop trusting.
    holder.strict_priority_enabled = False
    info = middleware._extract_request_data(_scope_with_strict_token("old-token"))
    assert info["strict_priority"] is None

    # Hot-reload: tokens rotated — the old token must no longer pass.
    holder.strict_priority_enabled = True
    holder.strict_priority_tokens = ["new-token"]
    info = middleware._extract_request_data(_scope_with_strict_token("old-token"))
    assert info["strict_priority"] is None
    info = middleware._extract_request_data(_scope_with_strict_token("new-token"))
    assert info["strict_priority"] is not None


def test_filter_sensitive_headers_covers_strict_token():
    """Review 194528212: the strict-priority bearer token is a credential —
    DEBUG header dumps must not leak its value.
    """
    from motor.common.http.security_utils import filter_sensitive_headers

    headers = {
        "X-Strict-Priority-Token": "secret-token-value",
        "x-strict-priority-token": "secret-token-value",
        "Authorization": "Bearer x",
        "X-Agent-Hint": "aGVsbG8",
    }
    filtered = filter_sensitive_headers(headers)
    values = [str(v) for v in filtered.values()]
    assert "secret-token-value" not in " ".join(values)
    assert "Bearer x" not in " ".join(values)
    assert filtered.get("X-Agent-Hint") == "aGVsbG8"
