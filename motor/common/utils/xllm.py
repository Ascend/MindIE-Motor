# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from typing import Any

from motor.common.utils.net import format_host


def xllm_kv_endpoints(ip: str, business_port: int, dp_rank: int, config: Any) -> dict[str, str]:
    """Bind local Host confirmation to the same instance and DP as engine content."""
    offsets = {
        "endpoint": config.xllm_event_port_offset,
        "motor_pool_endpoint": config.xllm_pool_port_offset,
        "replay_endpoint": config.xllm_replay_port_offset,
    }
    if dp_rank != 0:
        raise ValueError("xLLM union integration currently requires one DP endpoint per instance")
    ports = {}
    for name, offset in offsets.items():
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise TypeError(f"xLLM {name} port offset must be an integer")
        port = business_port + offset
        if not 1 <= port <= 65535:
            raise ValueError(f"xLLM {name} port is outside 1-65535")
        ports[name] = port
    if len(set(ports.values()) | {business_port}) != 4:
        raise ValueError("xLLM engine, event, pool and replay ports must be distinct")
    return {name: f"tcp://{format_host(ip)}:{port}" for name, port in ports.items()}
