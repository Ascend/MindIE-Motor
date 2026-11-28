# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Unit tests for the fail-closed bind checks."""

import pytest

from motor.common.security_posture import ensure_secure_bind, is_loopback_bind


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "[::1]", "localhost", "LOCALHOST"])
def test_loopback_hosts_are_detected(host):
    assert is_loopback_bind(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "[::]", "10.0.0.12", "coordinator.default.svc", "", "   "])
def test_non_loopback_hosts_are_not_detected(host):
    assert is_loopback_bind(host) is False


def test_loopback_without_auth_is_allowed():
    ensure_secure_bind("Coordinator inference API", "127.0.0.1", auth_enabled=False, allow_insecure_bind=False)


def test_auth_enabled_allows_any_bind():
    ensure_secure_bind("Coordinator inference API", "0.0.0.0", auth_enabled=True, allow_insecure_bind=False)


def test_explicit_opt_in_allows_any_bind():
    ensure_secure_bind("Coordinator management API", "::", auth_enabled=False, allow_insecure_bind=True)


def test_non_loopback_without_auth_raises():
    with pytest.raises(RuntimeError) as excinfo:
        ensure_secure_bind("Coordinator management API", "10.0.0.12", auth_enabled=False, allow_insecure_bind=False)
    message = str(excinfo.value)
    assert "Coordinator management API" in message
    assert "allow_insecure_bind" in message
