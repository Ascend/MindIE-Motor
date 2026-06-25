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

"""Tests for config_utils — re_register_interval_sec resolution."""

from motor.config.config_utils import (
    _resolve_re_register_interval_sec,
    DEFAULT_RE_REGISTER_INTERVAL_SEC,
    RE_REGISTER_INTERVAL_SEC,
    PREFILL_KV_EVENT_CONFIG,
)

# The top-level key for motor_coordinator config used by the resolver.
_MOTOR_COORDINATOR_KEY = "motor_coordinator_config"

# ------------------------------------------------------------------
# _resolve_re_register_interval_sec
# ------------------------------------------------------------------


class TestResolveReRegisterIntervalSec:
    """Cover all branches of _resolve_re_register_interval_sec."""

    def test_returns_default_when_motor_coordinator_missing(self):
        """Top-level motor_coordinator key absent → default."""
        result = _resolve_re_register_interval_sec({})
        assert result == DEFAULT_RE_REGISTER_INTERVAL_SEC

    def test_returns_default_when_motor_coordinator_not_dict(self):
        """motor_coordinator value is not a dict → default."""
        result = _resolve_re_register_interval_sec({_MOTOR_COORDINATOR_KEY: "string-value"})
        assert result == DEFAULT_RE_REGISTER_INTERVAL_SEC

    def test_returns_default_when_prefill_kv_event_config_missing(self):
        """motor_coordinator exists but lacks prefill_kv_event_config → default."""
        result = _resolve_re_register_interval_sec({_MOTOR_COORDINATOR_KEY: {}})
        assert result == DEFAULT_RE_REGISTER_INTERVAL_SEC

    def test_returns_default_when_prefill_kv_event_config_not_dict(self):
        """prefill_kv_event_config is present but not a dict → default."""
        result = _resolve_re_register_interval_sec({_MOTOR_COORDINATOR_KEY: {PREFILL_KV_EVENT_CONFIG: "not-a-dict"}})
        assert result == DEFAULT_RE_REGISTER_INTERVAL_SEC

    def test_returns_default_when_re_register_interval_missing(self):
        """prefill_kv_event_config exists but has no re_register_interval_sec → default."""
        result = _resolve_re_register_interval_sec(
            {_MOTOR_COORDINATOR_KEY: {PREFILL_KV_EVENT_CONFIG: {"other_key": 123}}}
        )
        assert result == DEFAULT_RE_REGISTER_INTERVAL_SEC

    def test_returns_default_when_re_register_interval_is_none(self):
        """re_register_interval_sec explicitly set to None → default."""
        result = _resolve_re_register_interval_sec(
            {_MOTOR_COORDINATOR_KEY: {PREFILL_KV_EVENT_CONFIG: {RE_REGISTER_INTERVAL_SEC: None}}}
        )
        assert result == DEFAULT_RE_REGISTER_INTERVAL_SEC

    def test_returns_configured_value(self):
        """Custom interval is returned as int."""
        result = _resolve_re_register_interval_sec(
            {_MOTOR_COORDINATOR_KEY: {PREFILL_KV_EVENT_CONFIG: {RE_REGISTER_INTERVAL_SEC: 120}}}
        )
        assert result == 120

    def test_accepts_string_value(self):
        """String numeric value is cast to int."""
        result = _resolve_re_register_interval_sec(
            {_MOTOR_COORDINATOR_KEY: {PREFILL_KV_EVENT_CONFIG: {RE_REGISTER_INTERVAL_SEC: "45"}}}
        )
        assert result == 45
        assert isinstance(result, int)

    def test_accepts_zero(self):
        """Zero is a valid explicit value (disables timer)."""
        result = _resolve_re_register_interval_sec(
            {_MOTOR_COORDINATOR_KEY: {PREFILL_KV_EVENT_CONFIG: {RE_REGISTER_INTERVAL_SEC: 0}}}
        )
        assert result == 0
