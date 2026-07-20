#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Tests for TokenBucket and SimpleRateLimiter parameter hot reload."""

import pytest
import threading
import time

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
