# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import time
from typing import Any

import msgspec

from motor.common.logger import get_logger
from motor.common.resources.instance import Instance, Endpoint, PDRole
from motor.common.http.http_client import SafeHTTPSClient
from motor.common.utils.net import format_address, format_host
from motor.config.coordinator import CoordinatorConfig


TENANT_ID = "default"
logger = get_logger(__name__)
# Roles whose KV events should be registered with the conductor.
_KVA_ROLES = frozenset({PDRole.ROLE_P, PDRole.ROLE_U})

# Content-Type for MessagePack query bodies / responses.
MSGPACK_CONTENT_TYPE = "application/msgpack"


def encode_query_msgpack(query_data: dict[str, Any]) -> bytes:
    """Encode a /query request dict into MessagePack (msgspec).

    Mirrors the kv-conductor's `QueryRequest` serde fields
    (model / block_size / token_ids / tenant_id). Shared with the
    end-to-end benchmark and tests so the wire codec has one implementation.
    """
    return msgspec.msgpack.encode(query_data)


def decode_query_response_msgpack(payload: bytes) -> dict[str, Any]:
    """Decode a /query MessagePack response into the same dict shape the
    JSON path produces (tenant → instance → {longest_matched, DP, ...}).
    """
    return msgspec.msgpack.decode(payload)


def conductor_instance_id(instance: Instance) -> str:
    """Return the Conductor tenant key for a KVA-eligible instance."""
    if instance.role == PDRole.ROLE_U:
        return f"vllm-union-{instance.id}"
    return f"vllm-prefill-{instance.id}"


class ConductorApiClient:
    coordinator_config = CoordinatorConfig.from_json()

    # Pool registration is once-per-cluster; HBM DP registrations are per-instance.
    _pool_registered: bool = False

    # ── Config ────────────────────────────────────────────────────────

    @classmethod
    def _kv_reg(cls):
        """Unified KV event config (conductor addr + registration patterns)."""
        return cls.coordinator_config.scheduler_config.kv_conductor_config

    @classmethod
    def _resolve_store_backend(cls) -> str:
        return cls._kv_reg().store_backend or "Mooncake"

    @classmethod
    def _resolve_backend_mode(cls) -> str:
        sb = cls._resolve_store_backend()
        if sb in ("Mooncake", "Memcache"):
            return "pool"
        if sb in ("YuanRong", ""):
            return "per_dp"
        logger.warning("Unknown store_backend=%s, falling back to per_dp", sb)
        return "per_dp"

    @classmethod
    def register_kv_instance(cls, instances: list[Instance]) -> None:
        """Register all KVA-eligible instance endpoints with the KV conductor."""
        logger.info("register_kv_instance started.")
        reg = cls._kv_reg()
        mode = cls._resolve_backend_mode()
        sb = cls._resolve_store_backend()

        if mode == "pool":
            cls._register_pool(reg, sb)
            for instance in instances:
                if instance.role not in _KVA_ROLES:
                    continue
                for ep in instance.get_all_endpoints():
                    cls._register_hbm_dp(reg, sb, instance, ep)
        else:
            for instance in instances:
                if instance.role not in _KVA_ROLES:
                    continue
                for ep in instance.get_all_endpoints():
                    cls._register_yuanrong_dp(reg, sb, instance, ep)

    @classmethod
    def unregister_kv_instance(cls, instances: list[Instance]) -> None:
        """Unregister all KVA-eligible instance endpoints from the KV conductor."""
        logger.info("unregister_kv_instance started.")

        for instance in instances:
            if instance.role not in _KVA_ROLES:
                continue
            for ep in instance.get_all_endpoints():
                cls.unregister_post(instance, ep)

    # ── Pool registration (Mooncake / Memcache) ──────────────────────

    @classmethod
    def _register_pool(cls, reg, store_backend: str) -> None:
        """Register the centralized pool once per cluster (domain name)."""
        if cls._pool_registered:
            return
        if not reg.pool_endpoint:
            logger.warning("No pool_endpoint for %s, skipping pool registration", store_backend)
            return

        register_data: dict = {
            "instance_id": f"{store_backend.lower()}-pool",
            "endpoint": reg.pool_endpoint,
            "type": reg.engine_type,
            "store_backend": store_backend,
            "modelname": reg.model_path or "default",
            "block_size": reg.block_size,
            "dp_rank": 0,
        }
        if TENANT_ID != "default":
            register_data["tenant_id"] = TENANT_ID

        client_args = {"address": format_address(reg.conductor_service, reg.http_server_port)}
        try:
            with SafeHTTPSClient(timeout=15, **client_args) as client:
                client.post("/register", register_data)
                cls._pool_registered = True
                logger.info("Pool registered: backend=%s endpoint=%s", store_backend, reg.pool_endpoint)
        except Exception as e:
            logger.error("Pool registration failed for %s: %s", store_backend, e)

    # ── HBM per-DP (Mooncake / Memcache) ─────────────────────────────

    @classmethod
    def _register_hbm_dp(cls, reg, store_backend: str, instance: "Instance", endpoint: "Endpoint") -> None:
        """Register a single DP's HBM endpoint for pool-backend auto-attach."""
        instance_id = conductor_instance_id(instance)
        npu_url = cls._resolve_endpoint_url(
            reg.npu_endpoint or reg.xpu_endpoint or reg.endpoint,
            endpoint.ip,
            endpoint.id,
        )

        replay_url = cls._resolve_endpoint_url(reg.replay_endpoint, endpoint.ip, endpoint.id)
        register_data: dict = {
            "instance_id": instance_id,
            "type": reg.engine_type,
            "store_backend": store_backend,
            "modelname": instance.model_name,
            "block_size": reg.block_size,
            "dp_rank": endpoint.id,
        }
        if npu_url:
            register_data["medium_endpoints"] = {"npu": npu_url}
        if TENANT_ID != "default":
            register_data["tenant_id"] = TENANT_ID
        if replay_url:
            register_data["replay_endpoint"] = replay_url

        client_args = {"address": format_address(reg.conductor_service, reg.http_server_port)}
        try:
            with SafeHTTPSClient(timeout=15, **client_args) as client:
                client.post("/register", register_data)
                mode = "ZMQ+HTTP" if npu_url else "HTTP-only"
                logger.info(
                    "HBM DP registered (%s): instance=%s dp=%d replay=%s",
                    mode,
                    instance_id,
                    endpoint.id,
                    replay_url or "none",
                )
        except Exception as e:
            logger.error("HBM DP registration failed for %s dp=%d: %s", instance_id, endpoint.id, e)

    # ── YuanRong per-DP multi-port ────────────────────────────────────

    @classmethod
    def _register_yuanrong_dp(cls, reg, store_backend: str, instance: "Instance", endpoint: "Endpoint") -> None:
        """Register a single DP with multi-port endpoints for YuanRong."""
        instance_id = conductor_instance_id(instance)
        medium_endpoints = cls._build_medium_endpoints(reg, endpoint.ip, endpoint.id)
        has_endpoints = any(v != "" for v in medium_endpoints.values())

        replay_url = cls._resolve_endpoint_url(reg.replay_endpoint, endpoint.ip, endpoint.id)
        register_data: dict = {
            "instance_id": instance_id,
            "type": reg.engine_type,
            "store_backend": store_backend,
            "modelname": instance.model_name,
            "block_size": reg.block_size,
            "dp_rank": endpoint.id,
        }
        if has_endpoints:
            register_data["medium_endpoints"] = {k: v for k, v in medium_endpoints.items() if v}
        if TENANT_ID != "default":
            register_data["tenant_id"] = TENANT_ID
        if replay_url:
            register_data["replay_endpoint"] = replay_url

        client_args = {"address": format_address(reg.conductor_service, reg.http_server_port)}
        try:
            with SafeHTTPSClient(timeout=15, **client_args) as client:
                client.post("/register", register_data)
                mode = "ZMQ+HTTP" if has_endpoints else "HTTP-only"
                logger.info(
                    "YuanRong DP registered (%s): instance=%s dp=%d replay=%s",
                    mode,
                    instance_id,
                    endpoint.id,
                    replay_url or "none",
                )
        except Exception as e:
            logger.error("YuanRong DP registration failed for %s dp=%d: %s", instance_id, endpoint.id, e)

    # ── Shared helpers ────────────────────────────────────────────────

    @staticmethod
    def _resolve_endpoint_url(pattern: str, ip: str, dp_rank: int) -> str | None:
        """Resolve an endpoint pattern like 'tcp://*:5557' with the given IP and dp_rank offset."""
        if not pattern:
            return None
        parts = pattern.split("*:")
        if len(parts) != 2:
            logger.debug("endpoint pattern malformed: %s", pattern)
            return None
        return f"{parts[0]}{format_host(ip)}:{int(parts[1]) + dp_rank}"

    @classmethod
    def _build_medium_endpoints(cls, config, ip: str, dp_rank: int) -> dict[str, str]:
        """Build the medium_endpoints map from per-medium endpoint patterns."""
        npu_url = cls._resolve_endpoint_url(config.npu_endpoint or config.xpu_endpoint, ip, dp_rank)
        cpu_url = cls._resolve_endpoint_url(config.cpu_endpoint, ip, dp_rank)
        disk_url = cls._resolve_endpoint_url(config.disk_endpoint, ip, dp_rank)
        fallback = cls._resolve_endpoint_url(config.endpoint, ip, dp_rank)
        return {
            "npu": npu_url or fallback or "",
            "cpu": cpu_url or fallback or "",
            "disk": disk_url or fallback or "",
        }

    @classmethod
    def register_post(cls, instance: "Instance", endpoint: "Endpoint") -> None:
        """Legacy single-DP registration (used by re-registration path)."""
        reg = cls._kv_reg()
        instance_id = conductor_instance_id(instance)
        sb = cls._resolve_store_backend()

        medium_endpoints = cls._build_medium_endpoints(reg, endpoint.ip, endpoint.id)
        if all(v == "" for v in medium_endpoints.values()):
            logger.debug("no endpoint configured for kv events, skipping registration")
            return

        replay_url = cls._resolve_endpoint_url(reg.replay_endpoint, endpoint.ip, endpoint.id)
        register_data: dict = {
            "medium_endpoints": medium_endpoints,
            "type": reg.engine_type,
            "store_backend": sb,
            "modelname": instance.model_name,
            "block_size": reg.block_size,
            "instance_id": instance_id,
            "dp_rank": endpoint.id,
        }
        if TENANT_ID != "default":
            register_data["tenant_id"] = TENANT_ID
        if replay_url:
            register_data["replay_endpoint"] = replay_url

        client_args = {"address": format_address(reg.conductor_service, reg.http_server_port)}
        try:
            with SafeHTTPSClient(timeout=15, **client_args) as client:
                client.post("/register", register_data)
                logger.info("Register success! role=%s conductor_id=%s", instance.role, instance_id)
        except Exception as e:
            logger.error(
                "Exception occurred while register to controller at %s: %s", client_args.get("address", "unknown"), e
            )
        logger.info("register_data : %s", register_data)

    @classmethod
    def unregister_post(cls, instance: Instance, endpoint: Endpoint) -> None:
        """
        unregister_kv_instance.

        :returns:
        """
        reg = cls._kv_reg()
        instance_id = conductor_instance_id(instance)
        register_data: dict = {
            "type": reg.engine_type,
            "modelname": instance.model_name,
            "block_size": reg.block_size,
            "instance_id": instance_id,
            "dp_rank": endpoint.id,
        }
        if TENANT_ID != "default":
            register_data["tenant_id"] = TENANT_ID

        client_args = {"address": format_address(reg.conductor_service, reg.http_server_port)}
        try:
            with SafeHTTPSClient(timeout=15, **client_args) as client:
                client.post("/unregister", register_data)
                logger.info(
                    "UnRegister success! role=%s conductor_id=%s",
                    instance.role,
                    instance_id,
                )

        except Exception as e:
            logger.error(
                "Exception occurred while register to conductor at %s: %s", client_args.get('address', 'unknown'), e
            )
        logger.info("unregister_data : %s", register_data)

    # ── Circuit breaker for /query ──────────────────────────────────
    _query_failures: int = 0
    _query_cool_until: float = 0.0
    _QUERY_CB_THRESHOLD: int = 3  # consecutive failures to trip
    _QUERY_CB_COOLDOWN: float = 30.0  # seconds to stay open

    @classmethod
    def _encode_query(cls, query_data: dict[str, Any], encoding: str) -> tuple[bytes | None, str]:
        """Encode the query body for the requested wire encoding.

        Returns ``(body, content_type)``; JSON bodies are encoded lazily by
        SafeHTTPSClient (``body=None``).
        """
        if encoding == "msgpack":
            return encode_query_msgpack(query_data), MSGPACK_CONTENT_TYPE
        if encoding == "json":
            return None, "application/json"
        logger.warning("Unknown query_encoding=%s, falling back to msgpack", encoding)
        return encode_query_msgpack(query_data), MSGPACK_CONTENT_TYPE

    @classmethod
    def _decode_query_response(cls, response: Any, encoding: str) -> dict[str, Any]:
        """Decode a /query response honoring the server's Content-Type.

        MessagePack responses are decoded with msgspec; everything else
        (including legacy JSON servers that ignore the Accept header) is
        parsed as JSON — so an upgraded client keeps working against an
        older kv-conductor binary.
        """
        content_type = response.headers.get("Content-Type", "").lower()
        if content_type.startswith(MSGPACK_CONTENT_TYPE):
            return decode_query_response_msgpack(response.content)
        if encoding == "msgpack":
            logger.debug(
                "conductor replied with Content-Type=%s (expected msgpack); parsing as JSON",
                content_type or "none",
            )
        return response.json()

    @classmethod
    def query_conductor(cls, instances: list[Instance], encoded_ids: list[int]) -> dict[str, Any]:
        """Query KV conductor for prefix cache matched blocks.

        Wire encoding is selected by ``kv_conductor_config.query_encoding``
        (default ``"msgpack"``): MessagePack bodies are faster to serialize
        and smaller on long-context queries. Response parsing follows the
        server's Content-Type, so legacy JSON conductors still work.

        Circuit breaker: after ``_QUERY_CB_THRESHOLD`` consecutive failures,
        skip queries for ``_QUERY_CB_COOLDOWN`` seconds.
        """
        # ── Circuit open? ──────────────────────────────────────────
        if cls._query_failures >= cls._QUERY_CB_THRESHOLD:
            if time.time() < cls._query_cool_until:
                logger.debug(
                    "query conductor circuit open (failures=%d, cool until=%.0f)",
                    cls._query_failures,
                    cls._query_cool_until,
                )
                return {}
            # Cooldown expired — half-open, try one request
            logger.info(
                "query conductor circuit half-open, retrying (failures=%d)",
                cls._query_failures,
            )

        reg = cls._kv_reg()
        query_data: dict = {
            "model": instances[0].model_name,
            "block_size": reg.block_size,
            "token_ids": encoded_ids,
        }
        if TENANT_ID != "default":
            query_data["tenant_id"] = TENANT_ID

        logger.debug(
            "query_data : model=%s block_size=%s tokens=%d",
            query_data["model"],
            query_data["block_size"],
            len(encoded_ids),
        )

        encoding = getattr(reg, "query_encoding", "msgpack")
        body, content_type = cls._encode_query(query_data, encoding)

        client_args = {"address": format_address(reg.conductor_service, reg.http_server_port)}

        try:
            with SafeHTTPSClient(timeout=3, **client_args) as client:
                if body is not None:
                    response = client.post_bytes("/query", body, content_type=content_type)
                else:
                    response = client.do_post("/query", data=query_data)
                parsed = cls._decode_query_response(response, encoding)
                # INFO: full match result (per-instance longest_matched / per-DP
                # matched blocks) is key observability data for KV-affinity tuning.
                logger.info("conductor query response: %s", parsed)
                cls._query_failures = 0  # reset on success
                return parsed
        except Exception as e:
            cls._query_failures += 1
            if cls._query_failures >= cls._QUERY_CB_THRESHOLD:
                cls._query_cool_until = time.time() + cls._QUERY_CB_COOLDOWN
                logger.warning(
                    "query conductor circuit OPEN (failures=%d, cool for %.0fs): %s",
                    cls._query_failures,
                    cls._QUERY_CB_COOLDOWN,
                    e,
                )
            else:
                logger.error(
                    "Exception occurred while querying conductor at %s: %s",
                    client_args.get('address', 'unknown'),
                    e,
                )
        return {}

    @classmethod
    def _build_register_payload(cls, instance: Instance, endpoint: Endpoint) -> dict[str, Any]:
        """Build registration payload using the unified kv_conductor_config config.

        Produces the same payload format as :meth:`register_post` so the
        re-registration comparison is consistent.
        """
        reg = cls._kv_reg()
        instance_id = conductor_instance_id(instance)
        sb = cls._resolve_store_backend()

        medium_endpoints = cls._build_medium_endpoints(reg, endpoint.ip, endpoint.id)
        # Keep only non-empty endpoints
        filtered = {k: v for k, v in medium_endpoints.items() if v}
        if not filtered:
            return {}

        replay_url = cls._resolve_endpoint_url(reg.replay_endpoint, endpoint.ip, endpoint.id)
        payload: dict[str, Any] = {
            "medium_endpoints": filtered,
            "type": reg.engine_type,
            "store_backend": sb,
            "modelname": instance.model_name,
            "block_size": reg.block_size,
            "instance_id": instance_id,
            "dp_rank": endpoint.id,
        }
        if TENANT_ID != "default":
            payload["tenant_id"] = TENANT_ID
        if replay_url:
            payload["replay_endpoint"] = replay_url

        return payload

    @classmethod
    def get_registered_services(cls) -> list[dict[str, Any]]:
        """Get registered services from the conductor.

        Tries both API flavours so the same code works with:
        - kv-conductor: ``GET /workers`` → ``{"workers": [...]}``
        - Mooncake Master: ``GET /services`` → ``{"services": [...]}``
        """
        reg = cls._kv_reg()
        client_args = {"address": format_address(reg.conductor_service, reg.http_server_port)}

        with SafeHTTPSClient(timeout=15, **client_args) as client:
            # ── kv-conductor flavour (preferred) ─────────────────────
            try:
                response = client.get("/workers")
            except Exception:
                response = None
            if isinstance(response, dict):
                workers = response.get("workers")
                if isinstance(workers, list) and workers:
                    return workers

            # ── Mooncake Master flavour (fallback) ───────────────────
            try:
                response = client.get("/services")
            except Exception:
                response = None
            if isinstance(response, dict):
                services = response.get("services", [])
                if isinstance(services, list):
                    return services

        return []

    @staticmethod
    def _normalize_service_key(service: dict[str, Any]) -> set[tuple[str, int]]:
        """Extract (instance_id, dp_rank) pairs from a service entry.

        Handles both response formats:

        - kv-conductor ``WorkerSummary``:
          ``{"instance_id": "...", "endpoints": {"0": {...}, "1": {...}}}``
        - Mooncake Master service entry:
          ``{"InstanceID": "...", "DPRank": 0, ...}``
        """
        keys: set[tuple[str, int]] = set()

        # ── kv-conductor format: nested endpoints HashMap ────────────
        instance_id = service.get("instance_id", "")
        endpoints = service.get("endpoints")
        if instance_id and isinstance(endpoints, dict):
            for dp_rank_str in endpoints:
                try:
                    dp_rank = int(dp_rank_str)
                except (ValueError, TypeError):
                    continue
                keys.add((instance_id, dp_rank))
            if keys:
                return keys

        # ── Mooncake Master format: flat fields ──────────────────────
        instance_id = service.get("InstanceID", "")
        if instance_id:
            dp_raw = service.get("DPRank", -1)
            if isinstance(dp_raw, int):
                dp_rank = dp_raw
            else:
                try:
                    dp_rank = int(dp_raw)
                except (ValueError, TypeError):
                    dp_rank = -1
            keys.add((instance_id, dp_rank))

        return keys

    @classmethod
    def re_register_kv_instances(cls, instances: list[Instance]) -> None:
        """Re-register any KVA-eligible instances that are missing from the conductor.

        Compares the set of locally known (instance_id, dp_rank) pairs against
        those already registered on the conductor (via GET /workers).  Missing
        entries are re-registered with :meth:`register_post`.
        """
        logger.info("re_register_kv_instances started.")
        try:
            registered_services = cls.get_registered_services()
        except Exception:
            logger.info("no registered services found in conductor, skipping re-register.")
            return

        # Collect all (instance_id, dp_rank) already registered on the conductor.
        registered_dps: set[tuple[str, int]] = set()
        for worker in registered_services:
            if isinstance(worker, dict):
                registered_dps |= cls._normalize_service_key(worker)

        for instance in instances:
            if instance.role not in _KVA_ROLES:
                continue
            for ep in instance.get_all_endpoints():
                payload = cls._build_register_payload(instance, ep)
                if not payload:
                    logger.debug(
                        "skip re-register because payload build failed for instance=%s endpoint=%s",
                        instance.id,
                        ep.id,
                    )
                    continue

                instance_id = conductor_instance_id(instance)
                if (instance_id, ep.id) in registered_dps:
                    continue  # already registered

                logger.info(
                    "service missing in conductor, re-registering instance=%s dp_rank=%s",
                    instance_id,
                    ep.id,
                )
                cls.register_post(instance, ep)
