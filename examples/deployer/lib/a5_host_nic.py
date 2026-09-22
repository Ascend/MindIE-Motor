# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Shared A5 host-nic overlay detection (K8s + Docker).

Gated by ASCEND_GLOBAL_RESOURCE_CONFIG.comm_resource_config.protocol_desc:
  uboe:device  → UBOE
  roce:device  → 1825 / RoCE
  ub_rtp:device → UBG

Docker: Ascend950 for UBOE/1825 (8 cards); Ascend950-Pod16 only for same-host UBG (16 cards).
"""

from __future__ import annotations

import json
import os
import re

import lib.constant as C


def agrc_requests_host_nic_overlay(value) -> bool:
    """True if ASCEND_GLOBAL_RESOURCE_CONFIG selects uboe/roce/ub_rtp device protocol."""
    if value is None:
        return False
    if isinstance(value, dict):
        payload = value
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value).strip()
        if not text:
            return False
        compact = text.replace(" ", "")
        if any(token in compact for token in C.A5_HOST_NIC_PROTOCOL_TOKENS):
            return True
        try:
            payload = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            return False
    if not isinstance(payload, dict):
        return False
    desc = payload.get("comm_resource_config.protocol_desc")
    if desc is None:
        return False
    items = [desc] if isinstance(desc, str) else list(desc) if isinstance(desc, (list, tuple)) else [str(desc)]
    return any(any(token in str(item) for token in C.A5_HOST_NIC_PROTOCOL_TOKENS) for item in items)


def env_requests_a5_host_nic_overlay(env_config) -> bool:
    """True when env.json selects ScaleOut protocol via AGRC protocol_desc."""
    if not isinstance(env_config, dict):
        return False
    for section in C.A5_HOST_NIC_ENV_SECTIONS:
        role_env = env_config.get(section) or {}
        if not isinstance(role_env, dict):
            continue
        if agrc_requests_host_nic_overlay(role_env.get(C.A5_UBOE_RESOURCE_CONFIG_KEY)):
            return True
    return False


def env_file_requests_a5_host_nic_overlay(env_config_path: str | None) -> bool:
    """Read env.json from path; False if missing or unreadable."""
    if not env_config_path or not os.path.exists(env_config_path):
        return False
    try:
        with open(env_config_path, encoding="utf-8") as handle:
            return env_requests_a5_host_nic_overlay(json.load(handle))
    except (OSError, json.JSONDecodeError):
        return False


def apply_a5_host_nic_privileged(
    command: str,
    hardware_type: str,
    env_config_path: str | None,
    *,
    attach_npu: bool,
) -> str:
    """Inject --privileged for A5 engine docker create when AGRC requests overlay.

    Aligns with K8s apply_a5_engine_pod_config (uboe/roce/ub_rtp:device).
    CTRL/KVS (attach_npu=False) unchanged. Idempotent if already present.
    """
    if not attach_npu or hardware_type not in C.HARDWARE_TYPE_A5:
        return command
    if not env_file_requests_a5_host_nic_overlay(env_config_path):
        return command
    if re.search(r"(^|\s)--privileged(\s|\\|$)", command):
        return command
    if not command.startswith("docker run "):
        return command
    return "docker run --privileged " + command[len("docker run ") :]
