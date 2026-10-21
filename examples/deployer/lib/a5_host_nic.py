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
  uboe:device / ub_ctp:device → UBOE (K8s stays on CNI, aligned with !1050)
  ub_rtp:device → UBG (K8s hostNetwork, aligned with Docker --net=host)

roce:device does not select this overlay. Host RoCE is configured in the
deployment copy, not by this switch.

Docker: Ascend950 for UBOE (8 cards); Ascend950-Pod16 only for same-host UBG (16 cards).
"""

from __future__ import annotations

import json
import os
import re

import lib.constant as C


def agrc_requests_host_nic_overlay(value) -> bool:
    """True if ASCEND_GLOBAL_RESOURCE_CONFIG selects uboe/ub_ctp/ub_rtp. Not RoCE."""
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


def agrc_requests_ubg_host_network(value) -> bool:
    """True only when protocol_desc contains ub_rtp:device. Other fields do not count."""
    if value is None:
        return False
    if isinstance(value, dict):
        payload = value
    else:
        text = str(value).strip()
        if not text:
            return False
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
    return any(C.A5_UBG_PROTOCOL_TOKEN in str(item).replace(" ", "") for item in items)


def _env_requests_agrc(env_config, predicate) -> bool:
    if not isinstance(env_config, dict):
        return False
    for section in C.A5_HOST_NIC_ENV_SECTIONS:
        role_env = env_config.get(section) or {}
        if not isinstance(role_env, dict):
            continue
        if predicate(role_env.get(C.A5_UBOE_RESOURCE_CONFIG_KEY)):
            return True
    return False


def env_requests_a5_host_nic_overlay(env_config) -> bool:
    """True when env.json selects ScaleOut protocol via AGRC protocol_desc."""
    return _env_requests_agrc(env_config, agrc_requests_host_nic_overlay)


def env_requests_a5_ubg_host_network(env_config) -> bool:
    """True when env.json selects same-host UBG (ub_rtp:device)."""
    return _env_requests_agrc(env_config, agrc_requests_ubg_host_network)


def _load_env_config(env_config_path: str | None):
    if not env_config_path or not os.path.exists(env_config_path):
        return None
    try:
        with open(env_config_path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def env_file_requests_a5_host_nic_overlay(env_config_path: str | None) -> bool:
    """Read env.json from path; False if missing or unreadable."""
    env_config = _load_env_config(env_config_path)
    return False if env_config is None else env_requests_a5_host_nic_overlay(env_config)


def env_file_requests_a5_ubg_host_network(env_config_path: str | None) -> bool:
    """Read env.json from path; True only for ub_rtp:device."""
    env_config = _load_env_config(env_config_path)
    return False if env_config is None else env_requests_a5_ubg_host_network(env_config)


def apply_a5_host_nic_privileged(
    command: str,
    hardware_type: str,
    env_config_path: str | None,
    *,
    attach_npu: bool,
) -> str:
    """Inject --privileged for A5 engine docker create when AGRC requests overlay.

    Aligns with K8s apply_a5_engine_pod_config (uboe/ub_ctp/ub_rtp:device).
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
