# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""IPv6-literal handling for SafeHTTPSClient / AsyncSafeHTTPSClient / HTTPClientPool."""

import asyncio

import pytest

from motor.common.http.http_client import (
    AsyncSafeHTTPSClient,
    HTTPClientPool,
    SafeHTTPSClient,
)


class TestSafeHTTPSClientIPv6:
    @pytest.mark.parametrize(
        "address,expected_base_url",
        [
            ("127.0.0.1:1025", "http://127.0.0.1:1025"),
            ("localhost:1025", "http://localhost:1025"),
            ("::1:1025", "http://[::1]:1025"),
            ("[::1]:1025", "http://[::1]:1025"),
            ("2001:db8::1:1025", "http://[2001:db8::1]:1025"),
            ("[2001:db8::1]:1025", "http://[2001:db8::1]:1025"),
        ],
    )
    def test_base_url_normalized(self, address, expected_base_url):
        client = SafeHTTPSClient(address=address)
        try:
            assert client.base_url == expected_base_url
        finally:
            client.close()


class TestAsyncSafeHTTPSClientIPv6:
    @pytest.mark.parametrize(
        "address,expected_base_url",
        [
            ("127.0.0.1:1025", "http://127.0.0.1:1025"),
            ("[::1]:1025", "http://[::1]:1025"),
            ("::1:1025", "http://[::1]:1025"),
            ("2001:db8::1:1025", "http://[2001:db8::1]:1025"),
        ],
    )
    def test_base_url_normalized(self, address, expected_base_url):
        client = AsyncSafeHTTPSClient.create_client(address=address)
        try:
            assert str(client.base_url).rstrip('/') == expected_base_url
        finally:
            asyncio.run(client.aclose())


class TestHTTPClientPoolKeyNormalization:
    def test_pool_key_unified_for_ipv6_literal(self):
        """('::1', 1025) and ('[::1]', 1025) must produce the same pool_key
        so that bracketed and bare IPv6 inputs share the same cached client.
        """
        pool = HTTPClientPool()
        key_bare = pool._get_pool_key("::1", "1025")
        key_bracketed = pool._get_pool_key("[::1]", "1025")
        assert key_bare == key_bracketed
        assert key_bare.startswith("[::1]:1025")

    def test_pool_key_distinguishes_v4_from_v6(self):
        pool = HTTPClientPool()
        assert pool._get_pool_key("127.0.0.1", "1025") != pool._get_pool_key("::1", "1025")

    def test_pool_key_format_ipv4(self):
        pool = HTTPClientPool()
        assert pool._get_pool_key("127.0.0.1", "1025") == "127.0.0.1:1025:"

    def test_pool_key_format_ipv6(self):
        pool = HTTPClientPool()
        assert pool._get_pool_key("2001:db8::1", "2379") == "[2001:db8::1]:2379:"
