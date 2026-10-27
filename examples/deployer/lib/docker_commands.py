# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""从 user_config.json 生成 docker_deploy.py 的启动命令。

由 docker_deploy.py --show-commands 调用。只打印，不创建容器，也不启动服务。
"""

from __future__ import annotations

import json
import os
import shlex
import sys

INSTANCE_NUM_MAX = 16
DEPLOY_MODE_SINGLE_CONTAINER = "single_container"


class ConfigError(ValueError):
    """user_config.json 缺少生成命令所需的字段，或字段不合法。"""


def kv_store_enabled(user_config: dict) -> bool:
    """与 docker_deploy 相同：MultiConnector，或写了非空的 kv_cache_store_config。"""
    engine = user_config.get("motor_engine_prefill_config") or user_config.get("motor_engine_union_config") or {}
    engine_config = engine.get("engine_config") if isinstance(engine, dict) else None
    if not isinstance(engine_config, dict):
        engine_config = {}
    kv_transfer = engine_config.get("kv_transfer_config")
    if not isinstance(kv_transfer, dict):
        kv_transfer = {}
    kv_connector = kv_transfer.get("kv_connector", "")
    kv_store_cfg = user_config.get("kv_cache_store_config")
    return kv_connector == "MultiConnector" or (isinstance(kv_store_cfg, dict) and bool(kv_store_cfg))


def _positive_int(deploy: dict, key: str) -> int:
    if key not in deploy:
        raise ConfigError(f"motor_deploy_config 缺少 {key}")
    value = deploy[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} 必须是正整数，当前是 {value!r}")
    if value < 1 or value > INSTANCE_NUM_MAX:
        raise ConfigError(f"{key} 必须在 1 到 {INSTANCE_NUM_MAX} 之间，当前是 {value}")
    return value


def _container_name(prefix: str, index: int, pod_index: int, pod_num: int) -> str:
    if pod_num == 1:
        return f"motor-{prefix}{index}"
    return f"motor-{prefix}{index}-{pod_index}"


def _shell_command(deployer_py: str, config_dir: str, argv: list[str]) -> str:
    """把参数排成可复制的多行命令。无值的开关（如 --start）单独占一行。"""
    lines = [
        f"python3 {shlex.quote(deployer_py)} \\",
        f"  --config_dir {shlex.quote(config_dir)} \\",
    ]
    rendered: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        bare = index + 1 >= len(argv) or str(argv[index + 1]).startswith("--")
        if token.startswith("--") and bare:
            rendered.append(token)
            index += 1
            continue
        rendered.append(f"{token} {argv[index + 1]}")
        index += 2
    for part_index, part in enumerate(rendered):
        suffix = " \\" if part_index < len(rendered) - 1 else ""
        lines.append(f"  {part}{suffix}")
    return "\n".join(lines)


_HOST_IP = '"<本机IP>"'
_NIC = '"<本机网卡>"'
_SERVER1_IP = '"<服务器1 IP>"'


def _format(
    servers: list[tuple[str, list[str]]],
    deployer_py: str,
    config_dir: str,
    shared: list[tuple[str, list[str]]] | None = None,
) -> str:
    """servers 每项独占一台。shared 是管理面、kv_store，固定放在服务器 1，不另计。"""
    shared = list(shared or [])
    lines = [f"预计使用 {len(servers)} 台服务器。", ""]
    if shared and servers:
        for title, argv in shared:
            lines.append(f"服务器 1：{title}")
            lines.append(_shell_command(deployer_py, config_dir, argv))
            lines.append("")
    for index, (title, argv) in enumerate(servers, start=1):
        lines.append(f"服务器 {index}：{title}")
        lines.append(_shell_command(deployer_py, config_dir, argv))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _engine_entries(
    *,
    role: str,
    name_prefix: str,
    instance_num: int,
    pod_num: int,
    in_container: bool,
    kv_enabled: bool,
) -> list[tuple[str, list[str]]]:
    role_label = {"prefill": "Prefill", "decode": "Decode", "union": "混部"}[role]
    entries: list[tuple[str, list[str]]] = []
    for index in range(instance_num):
        instance_name = f"{name_prefix}{index}"
        for pod_index in range(pod_num):
            container = _container_name(name_prefix, index, pod_index, pod_num)
            argv = ["--start"] if in_container else []
            argv.extend(
                [
                    "--container-name",
                    container,
                    "--role",
                    role,
                    "--instance-name",
                    instance_name,
                    "--pod-ip",
                    _HOST_IP,
                    "--nic-name",
                    _NIC,
                    "--coordinator-ip",
                    _SERVER1_IP,
                    "--controller-ip",
                    _SERVER1_IP,
                ]
            )
            if kv_enabled:
                argv.extend(["--kv-store-ip", _SERVER1_IP])
            title = role_label + " " + instance_name
            if pod_num > 1:
                title += f"（{pod_index + 1}/{pod_num}）"
            entries.append((title, argv))
    return entries


def render(user_config: dict, config_dir: str, deployer_py: str, *, in_container: bool = False) -> str:
    """返回可复制的 shell 文本。config_dir 与 deployer_py 按原样写入命令。"""
    if not isinstance(user_config, dict):
        raise ConfigError("user_config.json 必须是 JSON 对象")
    deploy = user_config.get("motor_deploy_config")
    if not isinstance(deploy, dict):
        raise ConfigError("user_config.json 缺少 motor_deploy_config")

    encode_num = deploy.get("e_instances_num", 0)
    if isinstance(encode_num, int) and not isinstance(encode_num, bool) and encode_num > 0:
        raise ConfigError("当前脚本不生成 encode 实例的 Docker 命令")

    kv_enabled = kv_store_enabled(user_config)
    if deploy.get("deploy_mode") == DEPLOY_MODE_SINGLE_CONTAINER:
        argv = ["--start"] if in_container else []
        argv.extend(["--container-name", "motor-single", "--pod-ip", _HOST_IP, "--nic-name", _NIC])
        if kv_enabled:
            argv.extend(["--kv-store-ip", _HOST_IP])
        return _format([("单容器", argv)], deployer_py, config_dir)

    hybrid = "hybrid_instances_num" in deploy
    separation = "p_instances_num" in deploy or "d_instances_num" in deploy
    if hybrid and separation:
        raise ConfigError("motor_deploy_config 同时包含混部字段和 PD 分离字段")
    if not hybrid and not separation:
        raise ConfigError("motor_deploy_config 缺少 p_instances_num/d_instances_num 或 hybrid_instances_num")

    shared = [_management_entry(in_container, kv_enabled)]
    if kv_enabled:
        shared.append(_kv_store_entry(in_container))
    if hybrid:
        servers = _engine_entries(
            role="union",
            name_prefix="u",
            instance_num=_positive_int(deploy, "hybrid_instances_num"),
            pod_num=_positive_int(deploy, "single_hybrid_instance_pod_num"),
            in_container=in_container,
            kv_enabled=kv_enabled,
        )
        return _format(servers, deployer_py, config_dir, shared)

    servers = _engine_entries(
        role="prefill",
        name_prefix="p",
        instance_num=_positive_int(deploy, "p_instances_num"),
        pod_num=_positive_int(deploy, "single_p_instance_pod_num"),
        in_container=in_container,
        kv_enabled=kv_enabled,
    )
    servers.extend(
        _engine_entries(
            role="decode",
            name_prefix="d",
            instance_num=_positive_int(deploy, "d_instances_num"),
            pod_num=_positive_int(deploy, "single_d_instance_pod_num"),
            in_container=in_container,
            kv_enabled=kv_enabled,
        )
    )
    return _format(servers, deployer_py, config_dir, shared)


def _management_entry(in_container: bool, kv_enabled: bool) -> tuple[str, list[str]]:
    argv = ["--start"] if in_container else []
    argv.extend(
        [
            "--container-name",
            "motor-ctrl",
            "--role",
            "coordinator,controller",
            "--pod-ip",
            _HOST_IP,
            "--nic-name",
            _NIC,
        ]
    )
    if kv_enabled:
        argv.extend(["--kv-store-ip", _HOST_IP])
    return "管理面", argv


def _kv_store_entry(in_container: bool) -> tuple[str, list[str]]:
    argv = ["--start"] if in_container else []
    argv.extend(
        [
            "--container-name",
            "motor-kvs",
            "--role",
            "kv_store",
            "--pod-ip",
            _HOST_IP,
            "--nic-name",
            _NIC,
            "--coordinator-ip",
            _HOST_IP,
            "--controller-ip",
            _HOST_IP,
        ]
    )
    return "kv_store", argv


def _load_user_config(config_dir: str) -> dict:
    path = os.path.join(config_dir, "user_config.json")
    if not os.path.isfile(path):
        raise ConfigError(f"找不到 {path}")
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ConfigError(f"{path} 必须是 JSON 对象")
    return data


def emit(config_dir: str | None, deployer_py: str, *, in_container: bool, create: bool) -> int:
    """打印启动命令。in_container 为真时每条命令带 --start。不启动服务。"""
    if create:
        print("--show-commands 不能和 --create 一起用。", file=sys.stderr)
        return 1
    if not config_dir:
        print("--show-commands 需要 --config_dir。", file=sys.stderr)
        return 1
    config_dir = os.path.abspath(config_dir)
    try:
        user_config = _load_user_config(config_dir)
        text = render(user_config, config_dir, deployer_py, in_container=in_container)
    except (ConfigError, json.JSONDecodeError, OSError) as exc:
        print(f"无法生成启动命令：{exc}", file=sys.stderr)
        return 1
    env_path = os.path.join(config_dir, "env.json")
    if not os.path.isfile(env_path):
        print(f"同目录下还没有 env.json，启动前需要备好：{env_path}", file=sys.stderr)
    sys.stdout.write(text)
    return 0
