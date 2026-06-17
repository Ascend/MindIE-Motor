# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from typing import Any

from motor.common.logger import get_logger
from motor.common.resources.instance import Instance, Endpoint, PDRole
from motor.common.http.http_client import SafeHTTPSClient
from motor.config.coordinator import CoordinatorConfig


TENANT_ID = "default"
logger = get_logger(__name__)
# Roles whose KV events should be registered with the conductor.
_KVA_ROLES = frozenset({PDRole.ROLE_P, PDRole.ROLE_U})

# When consecutive query failures reach this threshold, the conductor is
# presumed to have restarted and all instance endpoints must be re-registered.
_QUERY_FAILURE_THRESHOLD = 10
_query_failure_count = 0
_needs_reregister = False


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
        """Endpoint patterns (kv_event_registration)."""
        return cls.coordinator_config.scheduler_config.kv_event_registration

    @classmethod
    def _kv_base(cls):
        """Base config: conductor addr, engine type, block size."""
        return cls.coordinator_config.prefill_kv_event_config

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
        base = cls._kv_base()
        mode = cls._resolve_backend_mode()
        sb = cls._resolve_store_backend()

        if mode == "pool":
            cls._register_pool(reg, base, sb)
            for instance in instances:
                if instance.role not in _KVA_ROLES:
                    continue
                for ep in instance.get_all_endpoints():
                    cls._register_hbm_dp(reg, base, sb, instance, ep)
        else:
            for instance in instances:
                if instance.role not in _KVA_ROLES:
                    continue
                for ep in instance.get_all_endpoints():
                    cls._register_yuanrong_dp(reg, base, sb, instance, ep)

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
    def _register_pool(cls, reg, base, store_backend: str) -> None:
        """Register the centralized pool once per cluster (domain name)."""
        if cls._pool_registered:
            return
        if not reg.pool_endpoint:
            logger.warning("No pool_endpoint for %s, skipping pool registration", store_backend)
            return

        register_data: dict = {
            "instance_id": f"{store_backend.lower()}-pool",
            "endpoint": reg.pool_endpoint,
            "type": base.engine_type,
            "store_backend": store_backend,
            "modelname": base.model_path or "default",
            "block_size": base.block_size,
            "dp_rank": 0,
        }
        if TENANT_ID != "default":
            register_data["tenant_id"] = TENANT_ID

        client_args = {"address": f"{base.conductor_service}:{base.http_server_port}"}
        try:
            with SafeHTTPSClient(timeout=2, **client_args) as client:
                client.post("/register", register_data)
                cls._pool_registered = True
                logger.info("Pool registered: backend=%s endpoint=%s", store_backend, reg.pool_endpoint)
        except Exception as e:
            logger.error("Pool registration failed for %s: %s", store_backend, e)

    # ── HBM per-DP (Mooncake / Memcache) ─────────────────────────────

    @classmethod
    def _register_hbm_dp(cls, reg, base, store_backend: str, instance: "Instance", endpoint: "Endpoint") -> None:
        """Register a single DP's HBM endpoint for pool-backend auto-attach."""
        instance_id = conductor_instance_id(instance)
        xpu_url = cls._resolve_endpoint_url(reg.xpu_endpoint or reg.endpoint, endpoint.ip, endpoint.id)

        replay_url = cls._resolve_endpoint_url(reg.replay_endpoint, endpoint.ip, endpoint.id)
        register_data: dict = {
            "instance_id": instance_id,
            "type": base.engine_type,
            "store_backend": store_backend,
            "modelname": instance.model_name,
            "block_size": base.block_size,
            "dp_rank": endpoint.id,
        }
        if xpu_url:
            register_data["medium_endpoints"] = {"xpu": xpu_url}
        if TENANT_ID != "default":
            register_data["tenant_id"] = TENANT_ID
        if replay_url:
            register_data["replay_endpoint"] = replay_url

        client_args = {"address": f"{base.conductor_service}:{base.http_server_port}"}
        try:
            with SafeHTTPSClient(timeout=2, **client_args) as client:
                client.post("/register", register_data)
                mode = "ZMQ+HTTP" if xpu_url else "HTTP-only"
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
    def _register_yuanrong_dp(cls, reg, base, store_backend: str, instance: "Instance", endpoint: "Endpoint") -> None:
        """Register a single DP with multi-port endpoints for YuanRong."""
        instance_id = conductor_instance_id(instance)
        medium_endpoints = cls._build_medium_endpoints(reg, endpoint.ip, endpoint.id)
        has_endpoints = any(v != "" for v in medium_endpoints.values())

        replay_url = cls._resolve_endpoint_url(reg.replay_endpoint, endpoint.ip, endpoint.id)
        register_data: dict = {
            "instance_id": instance_id,
            "type": base.engine_type,
            "store_backend": store_backend,
            "modelname": instance.model_name,
            "block_size": base.block_size,
            "dp_rank": endpoint.id,
        }
        if has_endpoints:
            register_data["medium_endpoints"] = {k: v for k, v in medium_endpoints.items() if v}
        if TENANT_ID != "default":
            register_data["tenant_id"] = TENANT_ID
        if replay_url:
            register_data["replay_endpoint"] = replay_url

        client_args = {"address": f"{base.conductor_service}:{base.http_server_port}"}
        try:
            with SafeHTTPSClient(timeout=2, **client_args) as client:
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
            logger.debug(f"endpoint pattern malformed: {pattern}")
            return None
        return f"{parts[0]}{ip}:{int(parts[1]) + dp_rank}"

    @classmethod
    def _build_medium_endpoints(cls, config, ip: str, dp_rank: int) -> dict[str, str]:
        """Build the medium_endpoints map from per-medium endpoint patterns."""
        xpu_url = cls._resolve_endpoint_url(config.xpu_endpoint, ip, dp_rank)
        cpu_url = cls._resolve_endpoint_url(config.cpu_endpoint, ip, dp_rank)
        disk_url = cls._resolve_endpoint_url(config.disk_endpoint, ip, dp_rank)
        fallback = cls._resolve_endpoint_url(config.endpoint, ip, dp_rank)
        return {
            "xpu": xpu_url or fallback or "",
            "cpu": cpu_url or fallback or "",
            "disk": disk_url or fallback or "",
        }

    @classmethod
    def register_post(cls, instance: "Instance", endpoint: "Endpoint") -> None:
        """Legacy single-DP registration (used by re-registration path)."""
        reg = cls._kv_reg()
        base = cls._kv_base()
        instance_id = conductor_instance_id(instance)
        sb = cls._resolve_store_backend()

        medium_endpoints = cls._build_medium_endpoints(reg, endpoint.ip, endpoint.id)
        if all(v == "" for v in medium_endpoints.values()):
            logger.debug("no endpoint configured for kv events, skipping registration")
            return

        replay_url = cls._resolve_endpoint_url(reg.replay_endpoint, endpoint.ip, endpoint.id)
        register_data: dict = {
            "medium_endpoints": medium_endpoints,
            "type": base.engine_type,
            "store_backend": sb,
            "modelname": instance.model_name,
            "block_size": base.block_size,
            "instance_id": instance_id,
            "dp_rank": endpoint.id,
        }
        if TENANT_ID != "default":
            register_data["tenant_id"] = TENANT_ID
        if replay_url:
            register_data["replay_endpoint"] = replay_url

        client_args = {"address": f"{base.conductor_service}:{base.http_server_port}"}
        try:
            with SafeHTTPSClient(timeout=2, **client_args) as client:
                client.post("/register", register_data)
                logger.info("Register success! role=%s conductor_id=%s", instance.role, instance_id)
        except Exception as e:
            logger.error(
                "Exception occurred while register to controller at %s: %s", client_args.get("address", "unknown"), e
            )
        logger.info(f"register_data : {register_data}")

    @classmethod
    def unregister_post(cls, instance: Instance, endpoint: Endpoint) -> None:
        """
        unregister_kv_instance.

        :returns:
        """
        prefill_kv_event_config = cls.coordinator_config.prefill_kv_event_config
        instance_id = conductor_instance_id(instance)
        register_data: dict = {
            "type": prefill_kv_event_config.engine_type,
            "modelname": instance.model_name,
            "block_size": prefill_kv_event_config.block_size,
            "instance_id": instance_id,
            "dp_rank": endpoint.id,
        }
        if TENANT_ID != "default":
            register_data["tenant_id"] = TENANT_ID

        client_args = {
            "address": f"{prefill_kv_event_config.conductor_service}:{prefill_kv_event_config.http_server_port}"
        }
        try:
            with SafeHTTPSClient(timeout=2, **client_args) as client:
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
        logger.info(f"unregister_data : {register_data}")

    @classmethod
    def query_conductor(cls, instances: list[Instance], encoded_ids: list[int]) -> dict[str, Any]:
        """Query KV conductor for prefix cache overlap scores.

        On consecutive query failures (conductor restart / network partition),
        triggers automatic re-registration of all prefill instance endpoints
        once the conductor becomes reachable again.
        """
        prefill_kv_event_config = cls.coordinator_config.prefill_kv_event_config
        query_data: dict = {
            "model": instances[0].model_name,
            "block_size": prefill_kv_event_config.block_size,
            "token_ids": encoded_ids,
        }
        if TENANT_ID != "default":
            query_data["tenant_id"] = TENANT_ID

        logger.debug(f"query_data : {query_data}")

        client_args = {
            "address": f"{prefill_kv_event_config.conductor_service}:{prefill_kv_event_config.http_server_port}"
        }
        global _query_failure_count, _needs_reregister

        try:
            with SafeHTTPSClient(timeout=0.5, **client_args) as client:
                response = client.post("/query", query_data)
                logger.info(f"query success! {response}")

                if _needs_reregister:
                    try:
                        # Verify the conductor is truly alive before re-registering.
                        # A /health check confirms the conductor process is up (not
                        # just a transient network recovery) and avoids unnecessary
                        # DuplicateRegistration errors when the original registrations
                        # are still valid.
                        client.get("/health")
                    except Exception as e:
                        logger.warning(
                            "Re-registration skipped: conductor health check failed (%s). "
                            "Will retry on next successful query.",
                            e,
                        )
                        _query_failure_count = 0
                        return response

                    # Health check passed — conductor is up: re-register.
                    _needs_reregister = False
                    try:
                        logger.warning(
                            "Conductor recovered after %d consecutive failures, "
                            "re-registering all prefill instance endpoints",
                            _query_failure_count,
                        )
                        cls.register_kv_instance(instances)
                    except Exception as e:
                        logger.error("Re-registration during recovery failed: %s", e)
                _query_failure_count = 0

                return response
        except Exception as e:
            _query_failure_count += 1
            logger.error(
                "Exception occurred while register to conductor at %s: %s",
                client_args.get('address', 'unknown'),
                e,
            )
            if _query_failure_count >= _QUERY_FAILURE_THRESHOLD and not _needs_reregister:
                _needs_reregister = True
                logger.warning(
                    "Query failure count reached %d (threshold=%d), "
                    "conductor may have restarted; will re-register on next successful query",
                    _query_failure_count,
                    _QUERY_FAILURE_THRESHOLD,
                )
        return {}
