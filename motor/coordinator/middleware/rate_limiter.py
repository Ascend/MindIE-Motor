#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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
from typing import Dict, Any, Optional
from motor.common.logger import get_logger

logger = get_logger(__name__)

DEFAULT_REQ_CONGESTION_TRIGGER_RATIO = 0.85
DEFAULT_REQ_CONGESTION_CLEAR_RATIO = 0.75


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
    
    def try_consume(self, tokens: int = 1) -> bool:
        """
        
        Args:
            tokens: Number of tokens to consume
            
        Returns:
            bool: Whether successfully consumed tokens
        """
        with self._lock:
            now = time.time()
            elapsed = now - self.last_refill
            tokens_to_add = elapsed * self.refill_rate
            self.tokens = min(self.capacity, self.tokens + tokens_to_add)
            self.last_refill = now
            
            # Check if there are enough tokens
            if self.tokens >= tokens:
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

        Args:
            capacity: New bucket capacity.
            refill_rate: New token refill rate (tokens per second).
        """
        with self._lock:
            old_capacity = self.capacity
            self.capacity = capacity
            self.refill_rate = refill_rate

            if self.tokens > capacity:
                self.tokens = capacity
            elif capacity > old_capacity:
                added_capacity = capacity - old_capacity
                self.tokens = min(capacity, self.tokens + added_capacity)


class SimpleRateLimiter:
    
    def __init__(self, 
                 max_requests: int = 100,
                 window_size: int = 60):
        """
        
        Args:
            max_requests: Maximum number of requests in time window
            window_size: Time window size (seconds)
        """
        self.max_requests = max_requests
        self.window_size = window_size
        
        # Calculate token bucket parameters
        self.capacity = max_requests  # Bucket capacity equals maximum requests
        self.refill_rate = max_requests / window_size  # Tokens added per second
        self._congestion_alarm_sent = False
        
        # Use single global token bucket
        self._bucket = TokenBucket(
            capacity=self.capacity,
            refill_rate=self.refill_rate
        )
        
        logger.info(f"Initialized global rate limiter: max_requests={max_requests}, window_size={window_size}s")


    def is_allowed(self, request_data: Optional[Dict[str, Any]] = None) -> tuple[bool, Dict[str, Any]]:
        """
        
        Args:
            request_data: Request data (optional)
            
        Returns:
            tuple: (whether allowed, rate limiting info)
        """
        try:
            # Try to consume one token
            allowed = self._bucket.try_consume()
            available = self._bucket.get_available_tokens()

            req_congestion_trigger_threshold = int(self.max_requests * DEFAULT_REQ_CONGESTION_TRIGGER_RATIO)
            req_congestion_clear_threshold = int(self.max_requests * DEFAULT_REQ_CONGESTION_CLEAR_RATIO)

            from motor.common.alarm.req_congestion_event import ReqCongestionEvent, RequestCongestionReason
            from motor.coordinator.api_client.controller_api_client import ControllerApiClient

            additional_information = ""
            if not self._congestion_alarm_sent and available >= req_congestion_trigger_threshold:
                self._congestion_alarm_sent = True
                additional_information = (
                    f"The current number of inference requests in the system is{available},"
                    f"which is greater than the configured maximum number of requests {self.max_requests}*85%."
                )
                event = ReqCongestionEvent(
                    reason_id=RequestCongestionReason.DEALING_WITH_CONGESTION,
                    additional_information=additional_information
                )
                ControllerApiClient.report_alarms(event.model_dump())
            elif self._congestion_alarm_sent and available < req_congestion_clear_threshold:
                self._congestion_alarm_sent = False
                additional_information = (
                    f"The current number of inference requests in the system is {available},"
                    f"which is less than the configured maximum number of requests {self.max_requests}*75%."
                )           
                event = ReqCongestionEvent(
                    reason_id=RequestCongestionReason.DEALING_WITH_CONGESTION,
                    additional_information=additional_information
                )
                ControllerApiClient.report_alarms(event.model_dump())

            # Build rate limiting info
            limit_info = {
                "allowed": allowed,
                "available": available,
                "limit": self.max_requests,
                "window_size": self.window_size,
                "scope": "global",
                "timestamp": time.time()
            }
            
            if not allowed:
                logger.warning(f"Request globally rate limited: available: {available}/{self.max_requests}")
            
            return allowed, limit_info
            
        except Exception as e:
            logger.error(f"Rate limiting check failed: {e}")
            # Allow request by default when error occurs
            return True, {
                "error": str(e),
                "allowed": True,
                "timestamp": time.time()
            }

    def update_config(self, max_requests: int | None = None, window_size: int | None = None) -> None:
        """Update rate limiting parameters at runtime (for hot reload).

        Update max_requests and/or window_size, and sync the TokenBucket's
        capacity and refill_rate accordingly.

        Args:
            max_requests: New maximum number of requests; None means no change.
            window_size: New time window in seconds; None means no change.
        """
        if max_requests is not None:
            self.max_requests = max_requests
        if window_size is not None:
            self.window_size = window_size

        # Recalculate and update TokenBucket parameters
        new_capacity = self.max_requests
        new_refill_rate = self.max_requests / self.window_size
        self._bucket.update_params(capacity=new_capacity, refill_rate=new_refill_rate)

        logger.info(
            "Updated rate limiter: max_requests=%s, window_size=%ss",
            self.max_requests,
            self.window_size,
        )
    