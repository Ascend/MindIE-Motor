# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import asyncio
import hashlib
import threading
from collections.abc import Callable
from enum import Enum
from ssl import Purpose
from typing import Any

import httpx
import requests
from requests import Response
from requests.adapters import HTTPAdapter

from motor.common.http.cert_util import CertUtil
from motor.common.logger import get_logger
from motor.common.utils.net import format_address, split_address
from motor.common.utils.singleton import ThreadSafeSingleton
from motor.config.tls_config import TLSConfig
import motor.common.utils.error as cancel_error


def _normalize_address(address: str) -> str:
    """Make sure IPv6 literals inside an ``host:port`` string are bracketed."""
    host, port = split_address(address)
    return format_address(host, port) if port else address


logger = get_logger(__name__)

Canceller = Callable[[str], None]


class ConnectionMode(Enum):
    SHORT = "short"
    LONG = "long"


class SafeHTTPSClient:
    def __init__(
        self,
        address: str,
        protocol: str = 'http://',
        tls_config: TLSConfig | None = None,
        mode: ConnectionMode = ConnectionMode.SHORT,
        timeout: float = 5,
    ):
        self.protocol = protocol
        self.timeout = timeout
        self.session = requests.Session()
        self.verify = tls_config.enable_tls if tls_config else False

        if tls_config and tls_config.enable_tls:
            self.protocol = 'https://'
            ssl_context = CertUtil.create_ssl_context(tls_config=tls_config, purpose=Purpose.CLIENT_AUTH)

            adapter = HTTPAdapter()
            adapter.init_poolmanager(
                connections=10,
                ssl_context=ssl_context,
                maxsize=10,
            )
            self.session.mount(self.protocol, adapter)
        self.base_url = self.protocol + _normalize_address(address).rstrip('/')

        # set https headers
        self.session.headers.update(
            {
                'User-Agent': 'Secure-HTTPS-Client/1.0',
                'Accept': 'application/json',
                # Default: close = short connection; else keep alive = long connection
                'Connection': 'close' if mode == ConnectionMode.SHORT else 'Keep-Alive',
                'Content-Type': 'application/json',
            }
        )
        logger.debug(
            "SafeHTTPSClient initialized. address=%s, tls=%s, mode=%s, timeout=%s",
            address,
            bool(tls_config and tls_config.enable_tls),
            mode.value,
            timeout,
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def request(
        self, method: str, endpoint: str, data: dict | None = None, params: dict | None = None
    ) -> dict[str, Any]:
        resp = self._request(method, endpoint, data, params)
        return resp.json() if resp else None

    def get(self, endpoint: str, params: dict | None = None) -> dict[str, Any]:
        return self.request('GET', endpoint, params=params)

    def do_get(self, endpoint: str, params: dict | None = None) -> Response:
        return self._request('GET', endpoint, params=params)

    def post(self, endpoint: str, data: dict | None = None) -> dict[str, Any]:
        return self.request('POST', endpoint, data=data)

    def do_post(
        self,
        endpoint: str,
        data: dict | None = None,
        query_params: dict | None = None,
    ) -> Response:
        return self._request('POST', endpoint, data=data, params=query_params)

    def close(self) -> None:
        logger.debug("SafeHTTPSClient closing. address=%s", self.base_url)
        self.session.close()

    def _request(self, method: str, endpoint: str, data: dict | None = None, params: dict | None = None) -> Response:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        logger.debug(
            "HTTP request start. method=%s, url=%s, timeout=%s",
            method.upper(),
            url,
            self.timeout,
        )
        try:
            response = self.session.request(
                method=method.upper(), url=url, json=data, params=params, timeout=self.timeout, verify=self.verify
            )

            response.raise_for_status()
            logger.debug(
                "HTTP request success. method=%s, url=%s, status_code=%s",
                method.upper(),
                url,
                response.status_code,
            )
            return response
        except requests.exceptions.SSLError as e:
            logger.debug(
                "SSL verify failed. url=%s, error=%s. "
                "Possible causes: 1) CA/cert mismatch 2) expired cert "
                "3) hostname mismatch. "
                "Check: cert path in tls_config, cert expiry date.",
                url,
                e,
            )
            raise RuntimeError(f"SSL verify failed: {e}") from e
        except requests.exceptions.HTTPError as e:
            status = getattr(e.response, "status_code", "unknown")
            logger.debug(
                "HTTP error response. url=%s, status_code=%s, body=%s. "
                "Possible causes: 1) peer rejected request "
                "2) peer service down 3) auth failure.",
                url,
                status,
                getattr(e.response, "text", ""),
            )
            raise RuntimeError(f"http response error {e.response.status_code}, {e.response.text}") from e
        except Exception as e:
            logger.debug(
                "HTTP request send failed. url=%s, error=%s. "
                "Possible causes: 1) connection refused (peer down) "
                "2) network unreachable 3) DNS failure. "
                "Check: ping/telnet peer, ss -tlnp | grep port.",
                url,
                e,
            )
            raise RuntimeError(f"send request {url} error: {e}") from e


class HttpClientContext(httpx.AsyncClient):
    def __init__(self, base_url: str, verify: bool, **client_kwargs):
        super().__init__(base_url=base_url, verify=verify, **client_kwargs)
        self._cancellers: dict[str, Canceller] = {}

    def register_canceller(self, canceller_id: str, canceller: Canceller):
        self._cancellers[canceller_id] = canceller

    def unregister_canceller(self, canceller_id: str):
        if canceller_id in self._cancellers:
            del self._cancellers[canceller_id]

    async def cancel_all(self):
        reason = f"{cancel_error.NODE_FAULT}: {super().base_url}"
        for canceller in list(self._cancellers.values()):
            if canceller:
                await canceller(reason)


class AsyncSafeHTTPSClient:
    """Async HTTP client factory for HTTPClientPool to create httpx.AsyncClient."""

    @staticmethod
    def create_client(address: str, tls_config: TLSConfig | None = None, **client_kwargs):
        verify = True

        normalized = _normalize_address(address)
        if tls_config and tls_config.enable_tls:
            verify = CertUtil.create_ssl_context(tls_config=tls_config, purpose=Purpose.CLIENT_AUTH)
            base_url = f"https://{normalized}"
        else:
            base_url = f"http://{normalized}"

        if 'limits' not in client_kwargs:
            client_kwargs['limits'] = httpx.Limits(
                max_connections=None,
                max_keepalive_connections=None,
            )

        logger.debug(
            "AsyncSafeHTTPSClient created. base_url=%s, verify=%s, limits=%s",
            base_url,
            bool(verify),
            client_kwargs.get("limits"),
        )
        return HttpClientContext(base_url=base_url, verify=verify, **client_kwargs)


class HTTPClientPool(ThreadSafeSingleton):
    """
    HTTP client pool (singleton). Caches httpx.AsyncClient by endpoint and TLS
    config to avoid creating a new client per request.
    """

    def __init__(self):
        if hasattr(self, '_initialized'):
            return

        self._lock = threading.Lock()
        self._client_pool: dict[str, HttpClientContext] = {}
        self._tls_hash_cache: dict[int, str] = {}
        self._initialized = True

    async def get_client(
        self, ip: str, port: str, tls_config: TLSConfig | None = None, **client_kwargs
    ) -> HttpClientContext:
        """Get or create HTTP client (thread-safe, double-checked locking)."""
        pool_key = self._get_pool_key(ip, port, tls_config)

        client = self._client_pool.get(pool_key)
        if client and not client.is_closed:
            logger.debug("HTTPClientPool cache hit. pool_key=%s", pool_key)
            return client

        old_client_to_close: HttpClientContext | None = None
        with self._lock:
            client = self._client_pool.get(pool_key)
            if client and not client.is_closed:
                logger.debug("HTTPClientPool cache hit (post-lock). pool_key=%s", pool_key)
                return client

            address = format_address(ip, port)
            client = AsyncSafeHTTPSClient.create_client(address=address, tls_config=tls_config, **client_kwargs)

            if pool_key in self._client_pool:
                old_client_to_close = self._client_pool[pool_key]
                if old_client_to_close and old_client_to_close.is_closed:
                    old_client_to_close = None

            self._client_pool[pool_key] = client
            logger.info(
                "HTTPClientPool new client created. pool_key=%s, address=%s, tls=%s",
                pool_key,
                address,
                bool(tls_config and tls_config.enable_tls),
            )

        await self._safe_aclose(old_client_to_close)
        return client

    async def close_client(self, ip: str, port: str, tls_config: TLSConfig | None = None) -> None:
        """Close and remove the client for the given endpoint (thread-safe)."""
        pool_key = self._get_pool_key(ip, port, tls_config)
        with self._lock:
            client = self._client_pool.pop(pool_key, None)
        await self._safe_aclose(client)

    async def close_all(self) -> None:
        """Close all cached clients (thread-safe). Typically called on process shutdown."""
        with self._lock:
            to_close = list(self._client_pool.values())
            self._client_pool.clear()
        for client in to_close:
            await self._safe_aclose(client)

    async def warmup_clients(
        self,
        endpoints: list[tuple[str, str]],  # [(ip, port), ...]
        tls_config: TLSConfig | None = None,
        **client_kwargs,
    ) -> dict[str, bool]:
        """Warm up clients for the given endpoints (async batch create)."""
        results = {}
        tasks = []

        for ip, port in endpoints:
            pool_key = self._get_pool_key(ip, port, tls_config)
            existing_client = self._client_pool.get(pool_key)
            if existing_client and not existing_client.is_closed:
                results[pool_key] = True
                continue

            task = self._warmup_single_client(ip, port, tls_config, pool_key, **client_kwargs)
            tasks.append((pool_key, task))

        if tasks:
            warmup_results = await asyncio.gather(*[task for _, task in tasks], return_exceptions=True)
            for (pool_key, _), result in zip(tasks, warmup_results):
                results[pool_key] = not isinstance(result, Exception)

        return results

    def get_pool_keys_for_endpoints(
        self,
        endpoints: list[tuple[str, str]],
        tls_config: TLSConfig | None = None,
    ) -> set[str]:
        """Return pool_key set for given endpoints and TLS config (for cleanup)."""
        return {self._get_pool_key(ip, str(port), tls_config) for ip, port in endpoints}

    async def cleanup_unused_clients(
        self,
        active_endpoints: set[str],  # set of pool_key
    ) -> int:
        """Close clients not in active_endpoints (pool_key set); returns count closed."""
        to_remove: list[tuple[str, HttpClientContext]] = []
        with self._lock:
            for pool_key, client in list(self._client_pool.items()):
                if pool_key not in active_endpoints:
                    to_remove.append((pool_key, client))
            for pool_key, _ in to_remove:
                del self._client_pool[pool_key]

        for _, client in to_remove:
            await client.cancel_all()
        for _, client in to_remove:
            await self._safe_aclose(client)
        return len(to_remove)

    def _get_pool_key(self, ip: str, port: str, tls_config: TLSConfig | None = None) -> str:
        """Build pool key from ip, port and TLS config (with hash cache)."""
        tls_hash = ""
        if tls_config:
            tls_id = id(tls_config)
            if tls_id in self._tls_hash_cache:
                tls_hash = self._tls_hash_cache[tls_id]
            else:
                tls_str = f"{tls_config.enable_tls}_{tls_config.ca_file}_{tls_config.cert_file}_{tls_config.key_file}"
                tls_hash = hashlib.md5(tls_str.encode(), usedforsecurity=False).hexdigest()[:8]
                self._tls_hash_cache[tls_id] = tls_hash

        return f"{format_address(ip, port)}:{tls_hash}"

    async def _safe_aclose(self, client: httpx.AsyncClient | None) -> None:
        """Close client outside lock; ignore errors."""
        if not client or client.is_closed:
            return
        try:
            await client.aclose()
        except Exception as e:
            logger.warning("Ignored error closing HTTP client: %s", e)

    async def _warmup_single_client(
        self, ip: str, port: str, tls_config: TLSConfig | None, pool_key: str, **client_kwargs
    ) -> None:
        """Warm up a single endpoint client (thread-safe)."""
        with self._lock:
            client = self._client_pool.get(pool_key)
            if client and not client.is_closed:
                logger.debug("HTTPClientPool warmup skipped (already cached). pool_key=%s", pool_key)
                return

            address = format_address(ip, port)
            client = AsyncSafeHTTPSClient.create_client(address=address, tls_config=tls_config, **client_kwargs)
            self._client_pool[pool_key] = client
            logger.debug(
                "HTTPClientPool warmup created new client. pool_key=%s, address=%s",
                pool_key,
                address,
            )

    def register_canceller(self, key: str, canceller_id: str, callback: Canceller):
        client = self._client_pool.get(key)
        if client:
            client.register_canceller(canceller_id, callback)

    def unregister_canceller(self, key: str, canceller_id: str):
        client = self._client_pool.get(key)
        if client:
            client.unregister_canceller(canceller_id)
