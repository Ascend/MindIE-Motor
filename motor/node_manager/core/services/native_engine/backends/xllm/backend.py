# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import json

from motor.common.resources.instance import PDRole
from motor.common.utils.net import format_address
from motor.common.utils.xllm import xllm_kv_endpoints
from motor.config.coordinator import CoordinatorConfig
from motor.node_manager.core.services.native_engine.backends.base import build_endpoint_config
from motor.node_manager.core.services.native_engine.models import CommandSpec, LaunchContext, LaunchSpec, ProbeSpec


class XllmBackend:
    """Launch one native single-device xLLM engine under Motor lifecycle ownership."""

    engine_type = "xllm"

    @staticmethod
    def _flag_value(value: object) -> str:
        if isinstance(value, (bool, dict, list)):
            return json.dumps(value, separators=(",", ":"))
        if value is None:
            raise ValueError("xLLM flags cannot be null")
        return str(value)

    def prepare(self, context: LaunchContext) -> LaunchSpec:
        if context.role != PDRole.ROLE_U or context.dp_rank != 0 or context.node_rank != 0:
            raise ValueError("xLLM currently supports single-node union instances with DP=1 only")
        if context.headless or context.snapshot_metadata or context.d2d_peer_ips:
            raise ValueError("xLLM headless, snapshots and cross-instance weight transfer are not supported")
        endpoint = build_endpoint_config(context, self.engine_type)
        deploy = endpoint.deploy_config
        if endpoint.engine_type != self.engine_type:
            raise ValueError("xLLM backend requires engine_type=xllm")
        parallel = deploy.get_parallel_config(context.role.value)
        if any(getattr(parallel, key) != 1 for key in ("dp_size", "tp_size", "pp_size", "pcp_size")):
            raise ValueError("xLLM union integration currently requires DP=TP=PP=CP=1")
        if deploy.infer_tls_config and deploy.infer_tls_config.enable_tls:
            raise ValueError("xLLM native inference TLS is not supported by this backend")
        options = dict(deploy.engine_config.configs)
        for key in ("tp_size", "pp_size", "max_model_len"):
            options.pop(key, None)
        for key in ("enable_disagg_pd", "enable_service_routing", "enable_kvcache_store", "enable_xtensor"):
            if options.get(key):
                raise ValueError(f"xLLM union backend does not support {key}")
        if int(options.get("nnodes", 1)) != 1:
            raise ValueError("xLLM union backend does not support multiple worker processes")
        options.update(
            {
                "model": deploy.model_config.model_path,
                "model_id": deploy.model_config.model_name,
                "port": context.business_port,
                "node_rank": 0,
                "nnodes": 1,
                "dp_size": 1,
            }
        )
        if not options["model"] or not options["model_id"]:
            raise ValueError("xLLM requires model and model_id")
        options.setdefault("backend", "llm")
        if options["backend"] != "llm":
            raise ValueError("xLLM union backend currently supports text LLMs only")
        rpc_port = context.business_port + 5000
        if rpc_port > 65535:
            raise ValueError("xLLM business port + 5000 must be within 1-65535")
        options.setdefault("master_node_addr", format_address(context.host, rpc_port))
        config = CoordinatorConfig.from_json(context.config_path)
        reg = config.scheduler_config.kv_conductor_config
        if reg.conductor_service:
            if options.get("enable_prefix_cache") is False or int(options.get("num_speculative_tokens", 0)):
                raise ValueError("xLLM KV events require prefix cache and no speculative draft cache pool")
            if int(options.get("block_size", 128)) != reg.block_size:
                raise ValueError("Motor KV block_size must match xLLM block_size")
            events = options.get("kv_events_config", {})
            if isinstance(events, str):
                events = json.loads(events)
            events = dict(events)
            urls = xllm_kv_endpoints(context.host, context.business_port, 0, reg)
            for key, value in urls.items():
                if key in events and events[key] != value:
                    raise ValueError(f"xLLM {key} conflicts with Motor-managed KV endpoint")
                events[key] = value
            events["publisher"] = "zmq"
            options["kv_events_config"] = events
        argv = tuple(f"--{key}={self._flag_value(value)}" for key, value in options.items())
        environment = dict(context.environment)
        environment.setdefault("HCCL_IF_BASE_PORT", str(context.business_port + 100))
        health = deploy.health_check_config
        return LaunchSpec(
            command=CommandSpec(argv=("xllm",) + argv, env=environment),
            probe=ProbeSpec(
                path="/health",
                timeout_seconds=float(health.health_collector_timeout),
                startup_timeout_seconds=float(health.startup_timeout),
                max_attempts=health.health_collector_timeout_retry_attempts,
            ),
            deploy_config=deploy,
        )
