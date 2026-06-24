# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""IPv6 single-stack: create_shared_socket must pick the right address family."""

import socket

import pytest

from motor.coordinator.process.inference_manager import create_shared_socket


@pytest.mark.skipif(not hasattr(socket, "SO_REUSEPORT"), reason="SO_REUSEPORT not available")
class TestCreateSharedSocket:
    def test_ipv4_host_uses_af_inet(self):
        sock = create_shared_socket("127.0.0.1", 0)
        assert sock is not None
        try:
            assert sock.family == socket.AF_INET
        finally:
            sock.close()

    def test_ipv6_host_uses_af_inet6(self):
        sock = create_shared_socket("::1", 0)
        assert sock is not None
        try:
            assert sock.family == socket.AF_INET6
        finally:
            sock.close()

    def test_ipv6_unspecified_uses_af_inet6(self):
        sock = create_shared_socket("::", 0)
        assert sock is not None
        try:
            assert sock.family == socket.AF_INET6
        finally:
            sock.close()

    def test_bracketed_ipv6_host_binds_after_stripping_brackets(self):
        sock = create_shared_socket("[::1]", 0)
        assert sock is not None
        try:
            assert sock.family == socket.AF_INET6
        finally:
            sock.close()
