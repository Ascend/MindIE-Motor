# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from __future__ import annotations

import argparse
import copy
import os
import re
import shlex
import stat
import sys
from datetime import datetime

import lib.constant as C
import lib.docker_utils as D
from lib.docker_utils import logger, read_json, resolve_config_paths, validate_instance_nums, validate_pd_hybrid_config
from lib.in_place_run import run_in_place


def _write_file(path: str, content: str, executable: bool = False) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    if executable:
        mode = os.stat(path).st_mode
        os.chmod(path, mode | stat.S_IXUSR)


def _examples_root(deployer_dir: str) -> str:
    return os.path.dirname(os.path.abspath(deployer_dir))


def _in_place_workspace_slot(role: str | None, job_name: str | None) -> str:
    if not role:
        return "single"
    if D.engine_role_of(role) or role == "kv_store":
        return (job_name or "").strip() or ("kvs" if role == "kv_store" else D.engine_role_of(role))
    return "ctrl"


def _workspace_namespace(container_name: str | None) -> str | None:
    raw = (container_name or os.environ.get("NAME") or "").strip()
    if not raw or raw in {".", ".."} or "/" in raw or "\\" in raw:
        return None
    return raw


def default_in_place_workspace(
    deployer_dir: str,
    role: str | None,
    job_name: str | None,
    container_name: str | None = None,
) -> str:
    parts = [_examples_root(deployer_dir), "motor_workspace"]
    namespace = _workspace_namespace(container_name)
    if namespace:
        parts.append(namespace)
    parts.append(_in_place_workspace_slot(role, job_name))
    return os.path.join(*parts)


def _ensure_workspace(workspace: str) -> bool:
    if os.path.exists(workspace) and not os.path.isdir(workspace):
        logger.error("workspace exists but is not a directory: %s", workspace)
        return False
    try:
        os.makedirs(workspace, exist_ok=True)
    except OSError as exc:
        logger.error("Failed to create workspace %s: %s", workspace, exc)
        return False
    if not os.path.isdir(workspace):
        logger.error("workspace is not a directory: %s", workspace)
        return False
    return True


def _validate_topology(user_config: dict) -> bool:
    deploy_config = user_config.get(C.MOTOR_DEPLOY_CONFIG, {})
    if not isinstance(deploy_config, dict):
        logger.error("motor_deploy_config is required.")
        return False
    if C.HYBRID_INSTANCES_NUM in deploy_config:
        try:
            validate_pd_hybrid_config(user_config)
        except ValueError as exc:
            logger.error("%s", exc)
            return False
    try:
        validate_instance_nums(user_config)
    except (KeyError, ValueError) as exc:
        logger.error("%s", exc)
        return False
    return True


def _controller_ports(user_config: dict):
    api = (user_config.get(C.MOTOR_CONTROLLER_CONFIG) or {}).get("api_config") or {}
    return [
        ("controller", int(api.get("controller_api_port", 2026))),
        ("controller-obs", int(api.get("observability_api_port", 2027))),
    ]


def _ports_to_check(
    user_config: dict,
    params: D.DockerDeployParams,
    identity: D.DockerRuntimeIdentity | None,
    in_place: bool = False,
):
    if identity is None:
        if in_place:
            return [("infer", params.infer_container_port), ("obs", params.obs_container_port)]
        return [("infer", params.infer_host_port), ("obs", params.obs_host_port)]
    control = D.control_role_of(identity.role)
    ports = []
    if control in ("coordinator", D.ROLE_COORDINATOR_CONTROLLER):
        ports.extend([("infer", params.infer_container_port), ("obs", params.obs_container_port)])
    if control in ("controller", D.ROLE_COORDINATOR_CONTROLLER):
        ports.extend(_controller_ports(user_config))
    if D.engine_role_of(identity.role):
        ports.extend(D.collect_engine_listen_ports(user_config, identity.role))
    seen: set[int] = set()
    unique = []
    for label, port in ports:
        if port in seen:
            continue
        seen.add(port)
        unique.append((label, port))
    return unique


def preflight(
    user_config: dict,
    params: D.DockerDeployParams,
    identity: D.DockerRuntimeIdentity | None,
    *,
    in_place: bool = True,
    engine_ports: D.EnginePortOverrides | None = None,
    devices_arg: str | None = None,
    check_devices: bool = True,
    template_fallback: bool = False,
) -> bool:
    ok = True

    if identity is None:
        if params.deploy_mode != C.DEPLOY_MODE_SINGLE_CONTAINER:
            logger.error(
                "motor_deploy_config.deploy_mode is '%s', expected '%s'. "
                "For multi-container PD pass --role, or use deploy.py for Kubernetes.",
                params.deploy_mode,
                C.DEPLOY_MODE_SINGLE_CONTAINER,
            )
            ok = False
    elif params.deploy_mode == C.DEPLOY_MODE_SINGLE_CONTAINER:
        logger.error(
            "motor_deploy_config.deploy_mode is '%s'; omit --role for single-container, "
            "or set deploy_mode to a multi-container value (for example infer_service_set).",
            params.deploy_mode,
        )
        ok = False

    config_for_ports = user_config
    if identity is not None and engine_ports and engine_ports.specified():
        config_for_ports = copy.deepcopy(user_config)
        D.apply_engine_port_overrides(config_for_ports, identity.role, engine_ports)

    if check_devices and (identity is None or D.engine_role_of(identity.role)):
        try:
            D.validate_devices_vs_world_size(
                config_for_ports,
                identity.role if identity else None,
                devices_arg=devices_arg,
                hardware_type=params.hardware_type,
                template_fallback=template_fallback,
            )
        except ValueError as exc:
            logger.error("%s", exc)
            ok = False

    for label, port in _ports_to_check(config_for_ports, params, identity, in_place=in_place):
        if D.port_in_use(port):
            holder = D.describe_port_holder(port)
            logger.error(
                "Host %s port %s is already in use%s. "
                "Another process (not this workspace's Motor container) is holding it.",
                label,
                port,
                f" by: {holder}" if holder else "",
            )
            ok = False

    return ok


def _prepare_and_render(args, deployer_dir: str):
    user_config_path, env_config_path = resolve_config_paths(
        args.config_dir, args.user_config_path, args.env_config_path
    )
    user_config = read_json(user_config_path)
    if not _validate_topology(user_config):
        logger.error("Preflight checks failed. Aborting.")
        sys.exit(1)

    try:
        params = D.derive_params(user_config)
    except (KeyError, TypeError, ValueError) as exc:
        logger.error("Failed to derive deploy params: %s", exc)
        sys.exit(1)

    workspace = default_in_place_workspace(
        deployer_dir,
        args.role,
        args.job_name,
        container_name=_container_name_from_args_or_env(args),
    )
    if not _ensure_workspace(workspace):
        logger.error("Preflight checks failed. Aborting.")
        sys.exit(1)
    configmap_path = os.path.join(workspace, "configmap")

    try:
        identity = D.resolve_runtime_identity(
            user_config,
            role=args.role,
            job_name=args.job_name,
            pod_ip=args.pod_ip,
            coordinator_ip=args.coordinator_ip,
            controller_ip=args.controller_ip,
            kv_store_ip=args.kv_store_ip,
            kv_store_enabled=params.kv_store_enabled,
            nic_name=getattr(args, "nic_name", None),
        )
    except ValueError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    engine_ports = D.engine_port_overrides_from_args(args)
    try:
        D.validate_engine_port_overrides(identity, engine_ports)
    except ValueError as exc:
        logger.error("%s", exc)
        sys.exit(1)
    if not preflight(
        user_config,
        params,
        identity,
        in_place=True,
        engine_ports=engine_ports,
        devices_arg=getattr(args, "devices", None),
    ):
        logger.error("Preflight checks failed. Aborting.")
        sys.exit(1)

    try:
        D.prepare_configmap(
            deployer_dir,
            configmap_path,
            user_config_path,
            env_config_path,
            role=identity.role if identity else None,
            engine_ports=engine_ports,
        )
    except ValueError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    start_motor_path = os.path.join(workspace, "start_motor.sh")
    _write_file(
        start_motor_path,
        D.render_start_motor_sh(
            params,
            configmap_path,
            identity=identity,
            nic_name=getattr(args, "nic_name", None),
            pod_ip=getattr(args, "pod_ip", None),
        ),
        executable=True,
    )
    logger.info("Artifacts written to %s (start_motor.sh).", workspace)
    return params, workspace, identity


def format_restart_command(argv: list[str] | None = None) -> str:
    raw = list(argv if argv is not None else sys.argv)
    if not raw:
        return "python3 docker_deploy.py --start"
    script = os.path.abspath(raw[0])
    return " ".join(shlex.quote(part) for part in ["python3", script, *raw[1:]])


def _log_role_slug(identity: D.DockerRuntimeIdentity | None) -> str:
    if identity is None:
        return "single"
    role = (identity.role or "unknown").replace("+", "-")
    role = re.sub(r"[^A-Za-z0-9._-]+", "-", role).strip("-._") or "unknown"
    job = (identity.job_name or "").strip()
    if job and re.fullmatch(r"[A-Za-z0-9._-]+", job):
        return f"{role}-{job}"
    return role


def _new_run_log_path(workspace: str, identity: D.DockerRuntimeIdentity | None = None) -> str | None:
    log_dir = os.path.join(workspace, "log")
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError as exc:
        logger.error("Failed to create log directory %s: %s", log_dir, exc)
        return None
    slug = _log_role_slug(identity)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    n = 0
    while True:
        name = f"docker-{slug}-{stamp}.log" if n == 0 else f"docker-{slug}-{stamp}-{n}.log"
        path = os.path.join(log_dir, name)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            n += 1
            continue
        except OSError as exc:
            logger.error("Failed to create log file %s: %s", path, exc)
            return None
        os.close(fd)
        return path


def _start_in_place(
    workspace: str,
    identity: D.DockerRuntimeIdentity | None = None,
) -> int:
    start_motor_path = os.path.join(workspace, "start_motor.sh")
    if not os.path.isfile(start_motor_path):
        logger.error("start_motor.sh not found: %s", start_motor_path)
        return 1
    log_path = _new_run_log_path(workspace, identity)
    if log_path is None:
        return 1
    logger.info("starting Motor in this environment via %s", start_motor_path)
    return run_in_place(start_motor_path, log_path, format_restart_command())


def _print_in_place_banner(
    workspace: str, identity: D.DockerRuntimeIdentity | None, params: D.DockerDeployParams
) -> None:
    print(f"工作目录：{workspace}", flush=True)
    if identity is None:
        print("正在本环境拉起服务", flush=True)
        return
    print(f"角色：{identity.role}", flush=True)
    print(f"本机 IP：{identity.pod_ip}", flush=True)
    print(f"Coordinator：{identity.coordinator_ip}", flush=True)
    print(f"Controller：{identity.controller_ip}", flush=True)
    print(f"推理地址：{D.infer_endpoint(params, identity)}", flush=True)
    print("正在本环境拉起服务", flush=True)


def run(args, deployer_dir: str) -> int:
    if not getattr(args, "start", False):
        logger.error(
            "Host path is one-click (omit --create / --start) or --create. Pass --start only inside a container."
        )
        return 1
    params, workspace, identity = _prepare_and_render(args, deployer_dir)
    _print_in_place_banner(workspace, identity, params)
    return _start_in_place(workspace, identity=identity)


def _parse_role(value: str) -> str:
    try:
        role = D.normalize_docker_role(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if not D.is_supported_docker_role(role):
        raise argparse.ArgumentTypeError(
            f"unknown role {value!r}. Valid: {', '.join(D.DOCKER_MULTI_ROLES)}. "
            "coordinator,controller is an alias for coordinator_controller."
        )
    return role


def _config_dir_from_args(args) -> str:
    return (getattr(args, "config_dir", None) or "").strip()


def _container_name_from_args_or_env(args) -> str:
    return (getattr(args, "container_name", None) or os.environ.get("NAME") or "").strip()


def resolve_enter_env(args, deployer_dir: str) -> dict[str, str]:
    name = (getattr(args, "container_name", None) or "").strip()
    if not name:
        raise ValueError("--container-name is required when creating a container.")
    user_config_path, _env_config_path = resolve_config_paths(
        args.config_dir, args.user_config_path, args.env_config_path
    )
    env = D.create_env_from_config(user_config_path, deployer_dir)
    env["NAME"] = name
    return env


def _host_bind_source(stripped: str) -> str | None:
    if not stripped.startswith("-v "):
        return None
    spec = stripped[3:].strip().strip('"').strip("'")
    if not spec:
        return None
    if spec.startswith("$"):
        return spec.split(":", 1)[0].strip() or None
    if len(spec) >= 2 and spec[0].isalpha() and spec[1] == ":":
        rest = spec[2:]
        sep = rest.find(":")
        if sep < 0:
            return spec
        return spec[: 2 + sep]
    return spec.split(":", 1)[0].strip() or None


def _resolve_bind_host_path(raw: str, env: dict[str, str]) -> str:
    if not raw.startswith("$"):
        return raw
    name = raw[1:].strip("{}")
    return (env.get(name) or os.environ.get(name) or "").strip()


def _require_host_binds(template: str, env: dict[str, str] | None = None) -> None:
    missing: list[str] = []
    seen: set[str] = set()
    for line in template.splitlines():
        raw = _host_bind_source(line.strip().rstrip("\\").strip())
        if not raw:
            continue
        host_path = _resolve_bind_host_path(raw, env or {})
        label = host_path or raw
        if label in seen:
            continue
        seen.add(label)
        if not host_path or not os.path.exists(host_path):
            missing.append(label)
    if missing:
        raise ValueError(
            "Host bind path(s) do not exist: %s. "
            "Create them, or remove the corresponding -v lines from "
            "ENTER_DOCKER_RUN_A2 / ENTER_DOCKER_RUN_A3 / ENTER_DOCKER_RUN_A5 / "
            "ENTER_DOCKER_RUN_CTRL / ENTER_DOCKER_RUN_KVS." % ", ".join(missing)
        )


def _enter_line_is_weight(line: str) -> bool:
    stripped = line.strip().rstrip("\\").strip()
    return stripped.startswith("-e WEIGHT=") or (stripped.startswith("-v ") and "$WEIGHT" in stripped)


def apply_enter_weight(template: str, weight: str | None) -> str:
    if (weight or "").strip():
        return template
    return "".join(line for line in template.splitlines(keepends=True) if not _enter_line_is_weight(line))


def apply_enter_shm(template: str, dshm_size: str | None) -> str:
    if not (dshm_size or "").strip():
        return template
    docker_shm = D.k8s_quantity_to_docker_shm(dshm_size)
    kept: list[str] = []
    replaced = False
    for line in template.splitlines(keepends=True):
        stripped = line.strip().rstrip("\\").strip()
        if stripped.startswith("--shm-size="):
            indent = line[: len(line) - len(line.lstrip())]
            cont = " \\\n" if line.rstrip().endswith("\\") else "\n"
            kept.append(f"{indent}--shm-size={docker_shm}{cont}")
            replaced = True
            continue
        kept.append(line)
    if not replaced:
        return template
    return "".join(kept)


def _enter_template_attaches_npu(template: str) -> bool:
    return template is not C.ENTER_DOCKER_RUN_CTRL and template is not C.ENTER_DOCKER_RUN_KVS


def enter_docker_run_template(role: str | None, hardware_type: str | None = None) -> str:
    control, engine = D.split_docker_role(role)
    if not hardware_type:
        raise ValueError("motor_deploy_config.hardware_type is required to select A2/A5 vs A3 docker-run template.")
    if control and not engine:
        D.npu_docker_card_count(hardware_type)
        return C.ENTER_DOCKER_RUN_CTRL
    if engine == "kv_store":
        D.npu_docker_card_count(hardware_type)
        return C.ENTER_DOCKER_RUN_KVS
    if D.npu_docker_card_count(hardware_type) == 16:
        return C.ENTER_DOCKER_RUN_A3
    if hardware_type in C.HARDWARE_TYPE_950I_A5:
        return C.ENTER_DOCKER_RUN_A5
    return C.ENTER_DOCKER_RUN_A2


def _enter_davinci_id(stripped: str) -> int | None:
    for prefix in ("--device /dev/davinci", "--device=/dev/davinci"):
        if stripped.startswith(prefix):
            rest = stripped[len(prefix) :]
            if rest.isdigit():
                return int(rest)
    return None


def apply_enter_devices(template: str, devices_arg: str | None, *, attach_npu: bool) -> str:
    raw = (devices_arg or "").strip()
    if not attach_npu:
        if raw:
            logger.info("Ignoring --devices; this docker-run template does not attach NPU.")
        return template
    if not raw:
        return template
    wanted = D.parse_visible_device_ids(raw)
    wanted_set = set(wanted)
    present: set[int] = set()
    for line in template.splitlines():
        device_id = _enter_davinci_id(line.strip().rstrip("\\").strip())
        if device_id is not None:
            present.add(device_id)
    missing = [device_id for device_id in wanted if device_id not in present]
    if missing:
        raise ValueError(
            f"--devices {raw} includes card(s) {missing} not in the NPU docker-run template. "
            "A2/A5 templates list davinci0-7; A3 lists davinci0-15. Pick listed cards."
        )
    visible = ",".join(str(device_id) for device_id in wanted)
    kept: list[str] = []
    inserted_env = False
    for line in template.splitlines(keepends=True):
        stripped = line.strip().rstrip("\\").strip()
        device_id = _enter_davinci_id(stripped)
        if device_id is not None and device_id not in wanted_set:
            continue
        if not inserted_env and stripped.startswith("--device"):
            indent = line[: len(line) - len(line.lstrip())]
            kept.append(f"{indent}-e ASCEND_VISIBLE_DEVICES={visible} \\\n")
            inserted_env = True
        kept.append(line)
    if not inserted_env:
        raise ValueError("NPU docker-run template has no --device /dev/davinciN matching --devices.")
    return "".join(kept)


def _path_under(path: str, prefix: str) -> bool:
    path_abs = os.path.abspath(path)
    prefix_abs = os.path.abspath(prefix)
    return path_abs == prefix_abs or path_abs.startswith(prefix_abs + os.sep)


def _deploy_config_from_args(args) -> dict:
    user_config_path = (getattr(args, "user_config_path", None) or "").strip()
    if not user_config_path:
        config_dir = _config_dir_from_args(args)
        user_config_path = os.path.join(config_dir, "user_config.json")
    user_config = read_json(user_config_path)
    deploy = user_config.get(C.MOTOR_DEPLOY_CONFIG)
    if not isinstance(deploy, dict):
        raise ValueError("motor_deploy_config is required.")
    return deploy


def _hardware_type_from_args(args) -> str:
    deploy = _deploy_config_from_args(args)
    hardware_type = deploy.get(C.HARDWARE_TYPE)
    if not hardware_type:
        raise ValueError("motor_deploy_config.hardware_type is required.")
    D.npu_docker_card_count(hardware_type)
    return hardware_type


def _dshm_size_from_args(args) -> str | None:
    return D._derive_dshm_size(_deploy_config_from_args(args))


def extra_create_bind_paths(args, examples_dir: str) -> list[str]:
    paths: list[str] = []
    config_dir = _config_dir_from_args(args)
    if config_dir:
        paths.append(os.path.abspath(config_dir))
    for attr in ("user_config_path", "env_config_path"):
        value = (getattr(args, attr, None) or "").strip()
        if value:
            paths.append(os.path.dirname(os.path.abspath(value)))
    extra: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if _path_under(path, examples_dir) or path in seen:
            continue
        seen.add(path)
        extra.append(path)
    return extra


def _insert_before_image(template: str, lines: list[str]) -> str:
    if not lines:
        return template
    kept: list[str] = []
    inserted = False
    for line in template.splitlines(keepends=True):
        if not inserted and '"$IMAGE"' in line:
            kept.extend(lines)
            inserted = True
        kept.append(line)
    return "".join(kept)


def insert_create_binds(template: str, host_paths: list[str]) -> str:
    return _insert_before_image(template, [f"  -v {shlex.quote(path)}:{shlex.quote(path)} \\\n" for path in host_paths])


def build_in_place_inner_command(args, deployer_dir: str) -> str:
    script = os.path.join(os.path.abspath(deployer_dir), "docker_deploy.py")
    argv = ["python3", script, "--start"]
    config_dir = _config_dir_from_args(args)
    if config_dir:
        argv += ["--config_dir", os.path.abspath(config_dir)]
    else:
        argv += [
            "--user_config_path",
            os.path.abspath((getattr(args, "user_config_path", None) or "").strip()),
            "--env_config_path",
            os.path.abspath((getattr(args, "env_config_path", None) or "").strip()),
        ]
    name = _container_name_from_args_or_env(args)
    if name:
        argv += ["--container-name", name]
    role = getattr(args, "role", None)
    if role:
        argv += ["--role", role]
    job_name = getattr(args, "job_name", None)
    if job_name:
        argv += ["--instance-name", job_name]
    for flag, attr in (
        ("--pod-ip", "pod_ip"),
        ("--nic-name", "nic_name"),
        ("--coordinator-ip", "coordinator_ip"),
        ("--controller-ip", "controller_ip"),
        ("--kv-store-ip", "kv_store_ip"),
    ):
        value = getattr(args, attr, None)
        if value:
            argv += [flag, str(value)]
    for flag, attr in (
        ("--node-manager-port", "node_manager_port"),
        ("--data-parallel-rpc-port", "data_parallel_rpc_port"),
        ("--kv-port", "kv_port"),
        ("--base-port", "base_port"),
    ):
        value = getattr(args, attr, None)
        if value is not None:
            argv += [flag, str(value)]
    return " ".join(shlex.quote(part) for part in argv)


_IMAGE_BASH = C.ENTER_DOCKER_RUN_IMAGE_BASH


def attach_one_click_command(template: str, inner: str) -> str:
    body = template.rstrip()
    if not body.endswith(_IMAGE_BASH):
        raise ValueError('docker run template must end with "$IMAGE" bash')
    script = inner + "; exec bash"
    return body[: -len(_IMAGE_BASH)] + f'"$IMAGE" bash -c {shlex.quote(script)}\n'


def _validate_one_click_identity(args, deployer_dir: str) -> None:
    user_config_path, _env_config_path = resolve_config_paths(
        args.config_dir, args.user_config_path, args.env_config_path
    )
    user_config = read_json(user_config_path)
    if not _validate_topology(user_config):
        raise ValueError("Preflight checks failed.")
    params = D.derive_params(user_config)
    identity = D.resolve_runtime_identity(
        user_config,
        role=args.role,
        job_name=args.job_name,
        pod_ip=args.pod_ip,
        coordinator_ip=args.coordinator_ip,
        controller_ip=args.controller_ip,
        kv_store_ip=args.kv_store_ip,
        kv_store_enabled=params.kv_store_enabled,
        nic_name=getattr(args, "nic_name", None),
    )
    engine_ports = D.engine_port_overrides_from_args(args)
    D.validate_engine_port_overrides(identity, engine_ports)
    D.validate_devices_vs_world_size(
        user_config,
        identity.role if identity else None,
        devices_arg=getattr(args, "devices", None),
        hardware_type=params.hardware_type,
        template_fallback=True,
    )
    if not preflight(
        user_config,
        params,
        identity,
        in_place=True,
        engine_ports=engine_ports,
        check_devices=False,
    ):
        raise ValueError("Preflight checks failed.")


def _validate_host_create(name: str, image: str) -> None:
    if not D.docker_available():
        raise ValueError("docker is not available on this host.")
    if D.container_exists(name):
        raise ValueError(
            f"Container '{name}' already exists. "
            f"This script will not replace it. docker exec -it {name} bash "
            f"and pass --start, or docker rm -f {name} and create again."
        )
    if not D.image_exists(image):
        raise ValueError(f"Image not found locally: {image} (docker image inspect failed).")


def _run_enter(args, deployer_dir: str, *, start_service: bool = False) -> int:
    try:
        env = resolve_enter_env(args, deployer_dir)
    except ValueError as exc:
        logger.error("%s", exc)
        return 1
    if start_service:
        try:
            D.require_pod_ip_and_nic(getattr(args, "pod_ip", None), getattr(args, "nic_name", None))
            _validate_one_click_identity(args, deployer_dir)
        except (KeyError, TypeError, ValueError) as exc:
            logger.error("%s", exc)
            return 1
    try:
        _validate_host_create(env["NAME"], env["IMAGE"])
        hardware_type = _hardware_type_from_args(args)
        template = enter_docker_run_template(getattr(args, "role", None), hardware_type)
        dshm_size = _dshm_size_from_args(args)
    except ValueError as exc:
        logger.error("%s", exc)
        return 1
    attach_npu = _enter_template_attaches_npu(template)
    devices_arg = getattr(args, "devices", None)
    try:
        command = apply_enter_devices(template, devices_arg, attach_npu=attach_npu)
        if attach_npu:
            user_config_path, _env_config_path = resolve_config_paths(
                args.config_dir, args.user_config_path, args.env_config_path
            )
            D.validate_devices_vs_world_size(
                read_json(user_config_path),
                getattr(args, "role", None),
                devices_arg=devices_arg,
                hardware_type=hardware_type,
                template_fallback=True,
            )
    except ValueError as exc:
        logger.error("%s", exc)
        return 1
    command = insert_create_binds(command, extra_create_bind_paths(args, env["EXAMPLES"]))
    command = apply_enter_weight(command, env.get("WEIGHT"))
    try:
        command = apply_enter_shm(command, dshm_size)
    except ValueError as exc:
        logger.error("%s", exc)
        return 1
    try:
        _require_host_binds(command, env)
    except ValueError as exc:
        logger.error("%s", exc)
        return 1
    if start_service:
        try:
            command = attach_one_click_command(command, build_in_place_inner_command(args, deployer_dir))
        except ValueError as exc:
            logger.error("%s", exc)
            return 1
    if not attach_npu and "--device" in command:
        logger.error(
            "This docker-run template must not contain --device. Edit ENTER_DOCKER_RUN_CTRL or ENTER_DOCKER_RUN_KVS."
        )
        return 1
    print(command, end="", flush=True)
    merged = os.environ.copy()
    merged.update(env)
    os.execvpe("/bin/bash", ["bash", "-c", command], merged)
    return 1


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Automated Docker deployment for MindIE Motor. "
        "Omit --create/--start to create a container and start Motor on this TTY. "
        "Pass --create to only create the container (bash, no Motor). "
        "Pass --start inside a container to start Motor. "
        "Omit --role for single-container; pass --role for one host-network role."
    )
    # 必传参数
    parser.add_argument(
        "--config_dir",
        "--dir",
        type=str,
        default=None,
        required=False,
        help="Directory containing user_config.json and env.json. "
        "Mutually exclusive with --config/--env. "
        "Must be passed on the command line, not taken from the environment.",
    )
    parser.add_argument(
        "--container-name",
        type=str,
        default=None,
        help="Container name. Required when creating a container. "
        "If this name already exists, the script errors and does not replace it.",
    )
    parser.add_argument(
        "--role",
        type=_parse_role,
        default=None,
        metavar="ROLE",
        help="Start one role. Omit for single-container. "
        "Dedicated management container: --role coordinator,controller "
        "(no NPU devices). Engine create attaches NPUs. "
        "Control-plane and engine cannot share a container. "
        "One-click (no --create) needs the same identity flags as --start. "
        "Valid: " + ", ".join(D.DOCKER_MULTI_ROLES) + ".",
    )
    parser.add_argument(
        "--instance-name",
        dest="job_name",
        type=str,
        default=None,
        help="This engine instance, e.g. p0 / d0 / u0. One container is one instance. "
        "Required for --role prefill/decode/union. Forbidden for other roles.",
    )
    parser.add_argument(
        "--coordinator-ip",
        type=str,
        default=None,
        help="Coordinator IPv4. User-supplied. Required for prefill/decode/union/kv_store "
        "and for --role controller. --role coordinator / coordinator_controller default "
        "to this container's --pod-ip.",
    )
    parser.add_argument(
        "--controller-ip",
        type=str,
        default=None,
        help="Controller IPv4. User-supplied. Required for prefill/decode/union/kv_store "
        "and for --role coordinator. --role controller / coordinator_controller default "
        "to this container's --pod-ip.",
    )
    parser.add_argument(
        "--pod-ip",
        type=str,
        default=None,
        help="This host's HCCL IPv4 (same as vllm-ascend local_ip). "
        "Required for one-click and --start. Not required for --create. "
        "Loopback is rejected.",
    )
    parser.add_argument(
        "--nic-name",
        type=str,
        default=None,
        help="Host NIC for HCCL/Gloo/TP (same as vllm-ascend nic_name). "
        "Required for one-click and --start. Not required for --create. "
        "Exports HCCL_IF_IP and GLOO/TP/HCCL_SOCKET_IFNAME. "
        "--pod-ip must be this NIC's IPv4.",
    )
    # 可选、常用参数
    parser.add_argument(
        "--start",
        action="store_true",
        dest="start",
        help="Prepare the workspace and start Motor in this environment. "
        "Does not create a container. Pass this after you are inside a container. "
        "Requires --pod-ip and --nic-name.",
    )
    parser.add_argument(
        "--create",
        action="store_true",
        dest="create",
        help="Create the container only (bash, Motor not started). "
        "Uses the docker run template in this file "
        "(image/weight from user_config.json). "
        "Pass --config_dir and --container-name. "
        "Does not require --pod-ip or --nic-name. "
        "Dedicated management --create must pass --role coordinator,controller "
        "(or coordinator / controller) and uses ENTER_DOCKER_RUN_CTRL "
        "with no /dev/davinci* nodes. --role kv_store uses ENTER_DOCKER_RUN_KVS "
        "(no NPU devices; binds driver, /driver, /var/log). Otherwise uses "
        "ENTER_DOCKER_RUN_A2 (davinci0-7), ENTER_DOCKER_RUN_A3 (davinci0-15), "
        "or ENTER_DOCKER_RUN_A5 (davinci0-7 plus A5 UB paths) from hardware_type "
        "(every card in that template, or only --devices if passed). "
        "Does not write EXAMPLES or CONFIG_DIR into the container; "
        "--start must pass --config_dir again. "
        "Passes NAME into the container so --start workspaces split by container name.",
    )
    parser.add_argument(
        "--devices",
        type=str,
        help="Host NPU ids, e.g. '0' or '0,1'. Engine/single create keeps only those "
        "--device /dev/davinciN lines already in the A2/A5 or A3 template and sets "
        "-e ASCEND_VISIBLE_DEVICES; omit to attach every card in that template. "
        "Ignored for dedicated control-plane and kv_store create. Not used with "
        "--start (the container already has whatever create attached).",
    )
    # 不常用，兜底
    parser.add_argument(
        "--user_config_path",
        "--config",
        type=str,
        help="Path to user_config.json. Requires --env. Cannot be used with --config_dir.",
    )
    parser.add_argument(
        "--env_config_path",
        "--env",
        type=str,
        help="Path to env.json. Requires --config. Cannot be used with --config_dir.",
    )
    parser.add_argument(
        "--kv-store-ip",
        type=str,
        default=None,
        help="KV Cache Store host IP. Required when KV is enabled except --role kv_store "
        "(defaults to --pod-ip). Error if KV is disabled.",
    )
    parser.add_argument(
        "--node-manager-port",
        type=int,
        default=None,
        dest="node_manager_port",
        help="Override this engine container's node_manager_port in the workspace configmap. "
        "Only with --role prefill/decode/union. Omit to keep user_config.json.",
    )
    parser.add_argument(
        "--data-parallel-rpc-port",
        type=int,
        default=None,
        dest="data_parallel_rpc_port",
        help="Override this engine container's data_parallel_rpc_port. Only with --role prefill/decode/union.",
    )
    parser.add_argument(
        "--kv-port",
        type=int,
        default=None,
        dest="kv_port",
        help="Override this engine container's kv_port (occupies kv_port .. kv_port+NPU cards). "
        "Only with --role prefill/decode/union.",
    )
    parser.add_argument(
        "--base-port",
        type=int,
        default=None,
        dest="base_port",
        help="Override this engine container's endpoint base_port (service/mgmt). "
        "Only with --role prefill/decode/union.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    deployer_dir = os.path.dirname(os.path.abspath(__file__))
    if getattr(args, "create", False) and getattr(args, "start", False):
        logger.error("Pass only one of --create and --start.")
        sys.exit(1)
    if getattr(args, "start", False):
        sys.exit(run(args, deployer_dir))
    sys.exit(_run_enter(args, deployer_dir, start_service=not getattr(args, "create", False)))


if __name__ == "__main__":
    main()
