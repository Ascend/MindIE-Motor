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
带 --start 时按容器内部署打印：每台服务器要创建哪些容器，以及进入容器后执行的命令。
"""

from __future__ import annotations

import json
import os
import shlex
import sys

from lib import constant as C
from lib.docker_utils import (
    DOCKER_KV_CONDUCTOR_NAME,
    kv_conductor_enabled,
    kv_store_enabled,
    npu_docker_card_count,
    validate_docker_kv_conductor,
)

INSTANCE_NUM_MAX = 16
DEPLOY_MODE_SINGLE_CONTAINER = "single_container"


class ConfigError(ValueError):
    """user_config.json 缺少生成命令所需的字段，或字段不合法。"""


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
_KV_CONDUCTOR_IP = '"<KV Conductor所在服务器IP>"'
_RENDER_IP = '"<Render所在服务器IP>"'
_MANAGEMENT_NAME = "motor-ctrl"


def _management_title(*, render_enabled: bool, kv_conductor: bool, in_container: bool = False) -> str:
    extra: list[str] = []
    if render_enabled:
        from lib.generator.render import docker_render_container_name

        extra.append(f"Render容器【{docker_render_container_name(_MANAGEMENT_NAME)}】")
    if kv_conductor:
        extra.append(f"KV Conductor容器【{DOCKER_KV_CONDUCTOR_NAME}】")
    if not extra or in_container:
        return "管理面"
    launched = " ".join([f"管理面容器【{_MANAGEMENT_NAME}】", *extra])
    return f"管理面（以下命令会同时拉起 {launched}）"


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


def _argv_value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def _engine_template_name(hardware_type: object) -> str:
    """容器内创建清单里，推理容器对应的 docker run 模板名。"""
    if not isinstance(hardware_type, str) or not hardware_type:
        return "推理容器模板"
    try:
        if hardware_type in C.HARDWARE_TYPE_A5_POD16:
            return "ENTER_DOCKER_RUN_A5_POD16"
        if npu_docker_card_count(hardware_type) == 16:
            return "ENTER_DOCKER_RUN_A3"
        if hardware_type in C.HARDWARE_TYPE_A5:
            return "ENTER_DOCKER_RUN_A5"
    except ValueError:
        return "推理容器模板"
    return "ENTER_DOCKER_RUN_A2"


def _shared_template(title: str) -> str:
    if title == "kv_store":
        return "ENTER_DOCKER_RUN_KVS"
    return "ENTER_DOCKER_RUN_CTRL"


def _optional_container_plans(
    user_config: dict, main_name: str, *, kv_conductor: bool
) -> list[tuple[int, str, str, str, list[str]]]:
    """Render 与 KV Conductor 跟管理面同机，只出现在容器内方案里。"""
    from lib.generator.render import docker_render_container_name, is_render_enabled

    plans: list[tuple[int, str, str, str, list[str]]] = []
    if is_render_enabled(user_config):
        name = docker_render_container_name(main_name)
        plans.append(
            (
                1,
                "Render",
                name,
                "ENTER_DOCKER_RUN_CTRL",
                ["--start", "--container-name", name, "--role", "render"],
            )
        )
    if kv_conductor:
        plans.append(
            (
                1,
                "KV Conductor",
                DOCKER_KV_CONDUCTOR_NAME,
                "ENTER_DOCKER_RUN_CTRL",
                [
                    "--start",
                    "--container-name",
                    DOCKER_KV_CONDUCTOR_NAME,
                    "--role",
                    "kv_conductor",
                    "--pod-ip",
                    _HOST_IP,
                    "--nic-name",
                    _NIC,
                    "--coordinator-ip",
                    _SERVER1_IP,
                    "--controller-ip",
                    _SERVER1_IP,
                ],
            )
        )
    return plans


def _container_hint(title: str) -> str:
    """容器规划里，容器名后面的一句用途说明。"""
    if title == "管理面":
        return "管理面容器。"
    if title == "kv_store":
        return "池化后端。"
    if title == "Render":
        return "Render分词器。"
    if title == "KV Conductor":
        return "KV Cache索引。"
    if title == "单容器":
        return "单容器。"
    if title.startswith("Prefill"):
        return "Prefill推理容器。"
    if title.startswith("Decode"):
        return "Decode推理容器。"
    if title.startswith("混部"):
        return "混部推理容器。"
    return f"{title}。"


def _format_in_container(
    plans: list[tuple[int, str, str, str, list[str]]],
    deployer_py: str,
    config_dir: str,
    server_count: int,
) -> str:
    """plans 每项是 (服务器序号, 标题, 容器名, 模板名, 启动参数)。"""
    lines = [
        "容器内部署。先在对应服务器创建容器，再进入该容器执行启动命令。",
        "本命令只打印计划，不创建容器，也不启动服务。",
        "",
        f"预计使用 {server_count} 台服务器。",
        "",
        "1、创建容器：",
        "",
    ]
    grouped: dict[int, list[tuple[str, str]]] = {}
    for server, title, container, _template, _argv in plans:
        grouped.setdefault(server, []).append((title, container))
    for server in range(1, server_count + 1):
        items = grouped.get(server)
        if not items:
            continue
        lines.append(f"服务器 {server}：")
        for title, container in items:
            lines.append(f"- {container}：{_container_hint(title)}")
        lines.append("")
    lines.extend(["在各个容器中执行以下启动命令。", ""])
    for server, title, container, _template, argv in plans:
        label = title if title.endswith("容器") else f"{title}容器"
        lines.append(f"服务器 {server}：")
        lines.append(f"  创建{label} {container}")
        lines.append("  在容器内执行以下启动命令")
        command = _shell_command(deployer_py, config_dir, argv)
        lines.extend(f"  {line}" for line in command.splitlines())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _in_container_plans(
    servers: list[tuple[str, list[str]]],
    shared: list[tuple[str, list[str]]],
    user_config: dict,
    *,
    main_name: str,
    engine_template: str,
    kv_conductor: bool,
) -> list[tuple[int, str, str, str, list[str]]]:
    plans: list[tuple[int, str, str, str, list[str]]] = []
    for title, argv in shared:
        plans.append((1, title, _argv_value(argv, "--container-name"), _shared_template(title), argv))
    plans.extend(_optional_container_plans(user_config, main_name, kv_conductor=kv_conductor))
    for index, (title, argv) in enumerate(servers, start=1):
        plans.append((index, title, _argv_value(argv, "--container-name"), engine_template, argv))
    return plans


def _engine_entries(
    *,
    role: str,
    name_prefix: str,
    instance_num: int,
    pod_num: int,
    in_container: bool,
    kv_enabled: bool,
    kv_conductor: bool = False,
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
            if in_container and kv_conductor:
                argv.extend(["--kv-conductor-ip", _KV_CONDUCTOR_IP])
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

    try:
        validate_docker_kv_conductor(user_config)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    kv_enabled = kv_store_enabled(user_config)
    kv_conductor = kv_conductor_enabled(user_config)
    engine_template = _engine_template_name(deploy.get("hardware_type"))
    if deploy.get("deploy_mode") == DEPLOY_MODE_SINGLE_CONTAINER:
        argv = ["--start"] if in_container else []
        argv.extend(["--container-name", "motor-single", "--pod-ip", _HOST_IP, "--nic-name", _NIC])
        if kv_enabled:
            argv.extend(["--kv-store-ip", _HOST_IP])
        if in_container:
            from lib.generator.render import is_render_enabled

            if is_render_enabled(user_config):
                argv.extend(["--render-ip", _RENDER_IP])
        if not in_container:
            return _format([("单容器", argv)], deployer_py, config_dir)
        plans = _in_container_plans(
            [("单容器", argv)],
            [],
            user_config,
            main_name="motor-single",
            engine_template=engine_template,
            kv_conductor=False,
        )
        return _format_in_container(plans, deployer_py, config_dir, 1)

    hybrid = "hybrid_instances_num" in deploy
    separation = "p_instances_num" in deploy or "d_instances_num" in deploy
    if hybrid and separation:
        raise ConfigError("motor_deploy_config 同时包含混部字段和 PD 分离字段")
    if not hybrid and not separation:
        raise ConfigError("motor_deploy_config 缺少 p_instances_num/d_instances_num 或 hybrid_instances_num")

    shared = [_management_entry(user_config, in_container, kv_enabled, kv_conductor)]
    if kv_enabled:
        shared.append(_kv_store_entry(in_container, kv_conductor=kv_conductor))
    if hybrid:
        servers = _engine_entries(
            role="union",
            name_prefix="u",
            instance_num=_positive_int(deploy, "hybrid_instances_num"),
            pod_num=_positive_int(deploy, "single_hybrid_instance_pod_num"),
            in_container=in_container,
            kv_enabled=kv_enabled,
            kv_conductor=kv_conductor,
        )
        if in_container:
            plans = _in_container_plans(
                servers,
                shared,
                user_config,
                main_name=_MANAGEMENT_NAME,
                engine_template=engine_template,
                kv_conductor=kv_conductor,
            )
            return _format_in_container(plans, deployer_py, config_dir, len(servers))
        return _format(servers, deployer_py, config_dir, shared)

    servers = _engine_entries(
        role="prefill",
        name_prefix="p",
        instance_num=_positive_int(deploy, "p_instances_num"),
        pod_num=_positive_int(deploy, "single_p_instance_pod_num"),
        in_container=in_container,
        kv_enabled=kv_enabled,
        kv_conductor=kv_conductor,
    )
    servers.extend(
        _engine_entries(
            role="decode",
            name_prefix="d",
            instance_num=_positive_int(deploy, "d_instances_num"),
            pod_num=_positive_int(deploy, "single_d_instance_pod_num"),
            in_container=in_container,
            kv_enabled=kv_enabled,
            kv_conductor=kv_conductor,
        )
    )
    if in_container:
        plans = _in_container_plans(
            servers,
            shared,
            user_config,
            main_name=_MANAGEMENT_NAME,
            engine_template=engine_template,
            kv_conductor=kv_conductor,
        )
        return _format_in_container(plans, deployer_py, config_dir, len(servers))
    return _format(servers, deployer_py, config_dir, shared)


def _management_entry(
    user_config: dict, in_container: bool, kv_enabled: bool, kv_conductor: bool
) -> tuple[str, list[str]]:
    from lib.generator.render import is_render_enabled

    argv = ["--start"] if in_container else []
    argv.extend(
        [
            "--container-name",
            _MANAGEMENT_NAME,
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
    if in_container and kv_conductor:
        argv.extend(["--kv-conductor-ip", _KV_CONDUCTOR_IP])
    if in_container and is_render_enabled(user_config):
        argv.extend(["--render-ip", _RENDER_IP])
    title = _management_title(
        render_enabled=is_render_enabled(user_config),
        kv_conductor=kv_conductor,
        in_container=in_container,
    )
    return title, argv


def _kv_store_entry(in_container: bool, *, kv_conductor: bool = False) -> tuple[str, list[str]]:
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
    if in_container and kv_conductor:
        argv.extend(["--kv-conductor-ip", _KV_CONDUCTOR_IP])
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
