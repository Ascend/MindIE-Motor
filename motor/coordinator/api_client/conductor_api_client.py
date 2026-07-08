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
from motor.common.utils.net import format_address, format_host
from motor.config.coordinator import CoordinatorConfig


TENANT_ID = "default"
logger = get_logger(__name__)
# 需要向 Conductor 注册 kv-events 的角色集合
_KVA_ROLES = frozenset({PDRole.ROLE_P, PDRole.ROLE_U})


def conductor_instance_id(instance: Instance) -> str:
    """Return the Conductor tenant key for a KVA-eligible instance."""
    if instance.role == PDRole.ROLE_U:
        return f"vllm-union-{instance.id}"
    return f"vllm-prefill-{instance.id}"


class ConductorApiClient:
    coordinator_config = CoordinatorConfig.from_json()

    @staticmethod
    def register_kv_instance(instances: list[Instance]) -> None:
        """
        register_kv_instance.

        :returns:
        """
        logger.info("register_kv_instance started.")

        for instance in instances:
            if instance.role not in _KVA_ROLES:
                continue
            for ep in instance.get_all_endpoints():
                ConductorApiClient().register_post(instance, ep)

    @staticmethod
    def unregister_kv_instance(instances: list[Instance]) -> None:
        """
        unregister_kv_instance.

        :returns:
        """
        logger.info("unregister_kv_instance started.")

        for instance in instances:
            if instance.role not in _KVA_ROLES:
                continue
            for ep in instance.get_all_endpoints():
                ConductorApiClient().unregister_post(instance, ep)

    @classmethod
    def register_post(cls, instance: Instance, endpoint: Endpoint) -> None:
        """
        unregister_kv_instance.

        :returns:
        """
        prefill_kv_event_config = cls.coordinator_config.prefill_kv_event_config
        kv_endpoints = prefill_kv_event_config.endpoint.split("*:")
        if len(kv_endpoints) != 2:
            logger.debug(f"kv_endpoints size not 2  :  {prefill_kv_event_config.endpoint}")
            return

        instance_id = conductor_instance_id(instance)
        register_data: dict = {
            "endpoint": f"{kv_endpoints[0]}{format_host(endpoint.ip)}:{str(int(kv_endpoints[1]) + endpoint.id)}",
            "type": prefill_kv_event_config.engine_type,
            "modelname": instance.model_name,
            "block_size": prefill_kv_event_config.block_size,
            "instance_id": instance_id,
            "dp_rank": endpoint.id,
        }
        if TENANT_ID != "default":
            register_data["tenant_id"] = TENANT_ID

        if prefill_kv_event_config.replay_endpoint != "":
            replay_endpoints = prefill_kv_event_config.replay_endpoint.split("*:")
            if len(replay_endpoints) == 2:
                replay_endpoint = (
                    f"{replay_endpoints[0]}{format_host(endpoint.ip)}:{str(int(replay_endpoints[1]) + endpoint.id)}"
                )
                register_data["replay_endpoint"] = replay_endpoint

        client_args = {
            "address": format_address(
                prefill_kv_event_config.conductor_service, prefill_kv_event_config.http_server_port
            )
        }
        try:
            with SafeHTTPSClient(timeout=2, **client_args) as client:
                client.post("/register", register_data)
                logger.info(
                    "Register success! role=%s conductor_id=%s",
                    instance.role,
                    instance_id,
                )

        except Exception as e:
            logger.error(
                "Exception occurred while register to controller at %s: %s", client_args.get('address', 'unknown'), e
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
            "address": format_address(
                prefill_kv_event_config.conductor_service, prefill_kv_event_config.http_server_port
            )
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
                "Exception occurred while register to controller at %s: %s", client_args.get('address', 'unknown'), e
            )
        logger.info(f"unregister_data : {register_data}")

    @classmethod
    def query_conductor(cls, instances: list[Instance], encoded_ids: list[int]) -> dict[str, Any]:
        """
        unregister_kv_instance.

        :returns:
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
            "address": format_address(
                prefill_kv_event_config.conductor_service, prefill_kv_event_config.http_server_port
            )
        }
        try:
            with SafeHTTPSClient(timeout=0.2, **client_args) as client:
                response = client.post("/query", query_data)
                logger.info(f"query success! {response}")
                return response
        except Exception as e:
            logger.error(
                "Exception occurred while register to controller at %s: %s", client_args.get('address', 'unknown'), e
            )
        return {}

    @classmethod
    def _build_register_payload(cls, instance: Instance, endpoint: Endpoint) -> dict[str, Any]:
        prefill_kv_event_config = cls.coordinator_config.prefill_kv_event_config
        kv_endpoints = prefill_kv_event_config.endpoint.split("*:")
        if len(kv_endpoints) != 2:
            return {}

        instance_id = conductor_instance_id(instance)
        payload: dict[str, Any] = {
            "endpoint": f"{kv_endpoints[0]}{endpoint.ip}:{str(int(kv_endpoints[1]) + endpoint.id)}",
            "type": prefill_kv_event_config.engine_type,
            "modelname": instance.model_name,
            "block_size": prefill_kv_event_config.block_size,
            "instance_id": instance_id,
            "dp_rank": endpoint.id,
        }

        if TENANT_ID != "default":
            payload["tenant_id"] = TENANT_ID

        if prefill_kv_event_config.replay_endpoint != "":
            replay_endpoints = prefill_kv_event_config.replay_endpoint.split("*:")
            if len(replay_endpoints) == 2:
                payload["replay_endpoint"] = (
                    f"{replay_endpoints[0]}{endpoint.ip}:{str(int(replay_endpoints[1]) + endpoint.id)}"
                )

        return payload

    @classmethod
    def get_registered_services(cls) -> list[dict[str, Any]]:
        prefill_kv_event_config = cls.coordinator_config.prefill_kv_event_config
        client_args = {
            "address": f"{prefill_kv_event_config.conductor_service}:{prefill_kv_event_config.http_server_port}"
        }

        with SafeHTTPSClient(timeout=2, **client_args) as client:
            response = client.get("/services")
            if not isinstance(response, dict):
                return []
            services = response.get("services", [])
            return services if isinstance(services, list) else []

    @staticmethod
    def _normalize_service_key(service: dict[str, Any]) -> tuple[str, int, str, str]:
        instance_id = service.get("InstanceID", "")
        dp_raw = service.get("DPRank", -1)
        if isinstance(dp_raw, int):
            dp_rank = dp_raw
        else:
            dp_rank = -1
        endpoint = service.get("Endpoint", "")
        replay_endpoint = service.get("ReplayEndpoint", "")
        return instance_id, dp_rank, endpoint, replay_endpoint

    @classmethod
    def re_register_kv_instances(cls, instances: list[Instance]) -> None:
        logger.info("re_register_kv_instances started.")
        try:
            registered_services = cls.get_registered_services()
        except Exception:
            logger.info("no registered services found in conductor, skipping re-register.")
            return
        registered_keys = {cls._normalize_service_key(service) for service in registered_services}

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

                expected_key = (
                    payload.get("instance_id", ""),
                    int(payload.get("dp_rank", -1)),
                    payload.get("endpoint", ""),
                    payload.get("replay_endpoint", ""),
                )

                if expected_key not in registered_keys:
                    logger.info(
                        "service missing in conductor, re-registering instance=%s dp_rank=%s",
                        payload.get("instance_id"),
                        payload.get("dp_rank"),
                    )
                    cls.register_post(instance, ep)
