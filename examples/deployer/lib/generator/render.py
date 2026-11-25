# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import lib.constant as C
from lib.bind_mount import bind_mount_is_new

logger = logging.getLogger(__name__)


RENDER_CONTAINER_NAME = "vllm-render"
ASCEND_DRIVER_VOLUME_NAME = "ascend-driver-lib64"
ASCEND_DRIVER_PATH = "/usr/local/Ascend/driver/lib64"
LOCAL_RENDER_HOSTS = {"127.0.0.1", "localhost", "::1"}
DEFAULT_RENDERER_NUM_WORKERS = 4
SHARED_RENDER_ENGINE_ARGS = {
    "chat_template",
    "chat_template_content_format",
    "default_chat_template_kwargs",
    "tokenizer",
    "tokenizer_mode",
    "tokenizer_revision",
    "trust_remote_code",
}
PREFILL_RENDER_ENGINE_ARGS = {"max_model_len"}
DECODE_RENDER_ENGINE_ARGS = {
    "enable_auto_tool_choice",
    "reasoning_parser",
    "tool_call_parser",
}
INHERITED_RENDER_ENGINE_ARGS = SHARED_RENDER_ENGINE_ARGS | PREFILL_RENDER_ENGINE_ARGS | DECODE_RENDER_ENGINE_ARGS
MANAGED_RENDER_LAUNCH_ARGS = {
    "disable_access_log_for_endpoints",
    "host",
    "model",
    "model_tag",
    "port",
    "served_model_name",
}
RENDER_CACHE_ROOT = "/tmp/vllm-render-cache"  # nosec B108 - container-local cache
CPU_RENDER_LAUNCHER = "from vllm.entrypoints.cli.main import main; main()"
CPU_RENDER_SITECUSTOMIZE = """\
import importlib.util

_find_spec = importlib.util.find_spec
importlib.util.find_spec = lambda name, *args, **kwargs: (
    None if name in {"triton", "pytorch-triton-xpu"} else _find_spec(name, *args, **kwargs)
)
import vllm.triton_utils
importlib.util.find_spec = _find_spec

from vllm import platforms
from vllm.platforms.cpu import CpuPlatform

platforms.current_platform = CpuPlatform()
"""


@dataclass(frozen=True)
class RenderLaunchSpec:
    """Fully resolved Render process configuration shared by every deployment path."""

    image: str
    use_cpu_image: bool
    model: str
    served_model_names: tuple[str, ...]
    port: int
    launch_args: dict[str, Any]


def _normalize_served_model_names(value: Any) -> list[str]:
    if isinstance(value, str) and value:
        return [value]
    if isinstance(value, list) and value and all(isinstance(item, str) and item for item in value):
        return value
    raise ValueError("vLLM engine_config.served_model_name must be a non-empty string or list of strings")


def _resolve_model_config(engine_section: dict[str, Any], section_name: str) -> tuple[str, list[str]]:
    if engine_section.get(C.ENGINE_TYPE, C.ENGINE_TYPE_VLLM) != C.ENGINE_TYPE_VLLM:
        raise ValueError(f"Render sidecar requires engine_type='vllm' in {section_name}")

    engine_config = engine_section.get(C.ENGINE_CONFIG, {})
    legacy_model_config = engine_section.get("model_config", {})
    model = engine_config.get("model") or legacy_model_config.get("model_path")
    if "served_model_name" in engine_config:
        served_model_name = engine_config.get("served_model_name") or model
    else:
        served_model_name = legacy_model_config.get("model_name") or model
    if not isinstance(model, str) or not model:
        raise ValueError(f"{section_name}.engine_config.model is required when Render is enabled")
    return model, _normalize_served_model_names(served_model_name)


def _resolve_render_engine_sections(user_config: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    from lib.generator.engine import is_hybrid_deploy

    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    if is_hybrid_deploy(deploy_config):
        section_name = C.MOTOR_ENGINE_UNION_CONFIG
        return [(section_name, user_config.get(section_name, {}))]

    sections = [
        (C.MOTOR_ENGINE_PREFILL_CONFIG, user_config.get(C.MOTOR_ENGINE_PREFILL_CONFIG, {})),
    ]
    decode = user_config.get(C.MOTOR_ENGINE_DECODE_CONFIG)
    if decode is not None:
        sections.append((C.MOTOR_ENGINE_DECODE_CONFIG, decode))
    return sections


def _resolve_render_model(engine_sections: list[tuple[str, dict[str, Any]]]) -> tuple[str, list[str]]:
    first_section_name, first_section = engine_sections[0]
    model = _resolve_model_config(first_section, first_section_name)
    for section_name, section in engine_sections[1:]:
        if _resolve_model_config(section, section_name) != model:
            raise ValueError("Prefill and decode model/served_model_name must match when Render is enabled")
    return model


def resolve_render_model(user_config: dict[str, Any]) -> tuple[str, list[str]]:
    """Resolve and validate the model consumed by the Render sidecar."""
    return _resolve_render_model(_resolve_render_engine_sections(user_config))


def _ensure_readonly_host_mount(
    pod_spec: dict[str, Any], container: dict[str, Any], volume_name: str, path: str
) -> None:
    volumes = pod_spec.setdefault(C.VOLUMES, [])
    if not any(volume.get(C.NAME) == volume_name for volume in volumes):
        volumes.append(
            {
                C.NAME: volume_name,
                C.HOST_PATH: {C.PATH: path, "type": "Directory"},
            }
        )

    mounts = container.setdefault(C.VOLUME_MOUNTS, [])
    if not any(mount.get(C.NAME) == volume_name for mount in mounts):
        mounts.append(
            {
                C.NAME: volume_name,
                C.MOUNT_PATH: path,
                "readOnly": True,
            }
        )


def _serialize_launch_args(launch_args: dict[str, Any]) -> list[str]:
    if not isinstance(launch_args, dict):
        raise ValueError("render_config.launch_args must be an object")

    args: list[str] = []
    normalized_names: set[str] = set()
    for name, value in launch_args.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("render_config.launch_args keys must be non-empty strings")
        normalized_name = name.strip().lstrip("-").replace("-", "_").lower()
        if not normalized_name or normalized_name in MANAGED_RENDER_LAUNCH_ARGS:
            raise ValueError(f"render_config.launch_args.{name} is managed by Motor")
        if normalized_name in normalized_names:
            raise ValueError(f"render_config.launch_args contains duplicate argument: {name}")
        normalized_names.add(normalized_name)

        if value is None or value is False:
            continue
        args.append(f"--{normalized_name.replace('_', '-')}")
        if value is True:
            continue
        if isinstance(value, dict):
            args.append(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, (dict, list)):
                    args.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
                elif isinstance(item, (str, int, float)) and not isinstance(item, bool):
                    args.append(str(item))
                else:
                    raise ValueError(f"render_config.launch_args.{name} contains an unsupported value")
        elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
            args.append(str(value))
        else:
            raise ValueError(f"render_config.launch_args.{name} must be null, bool, string, number, object, or array")
    return args


def _normalize_launch_arg_name(name: object, source: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{source} keys must be non-empty strings")
    return name.strip().lstrip("-").replace("-", "_").lower()


def _derive_render_launch_args(
    engine_sections: list[tuple[str, dict[str, Any]]],
    overridden_names: set[str],
) -> dict[str, Any]:
    derived: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for section_index, (section_name, section) in enumerate(engine_sections):
        engine_config = section.get(C.ENGINE_CONFIG, {})
        if not isinstance(engine_config, dict):
            raise ValueError(f"{section_name}.engine_config must be an object")
        section_args: dict[str, Any] = {}
        for raw_name, value in engine_config.items():
            normalized_name = _normalize_launch_arg_name(raw_name, f"{section_name}.engine_config")
            if normalized_name not in INHERITED_RENDER_ENGINE_ARGS:
                continue
            if normalized_name in PREFILL_RENDER_ENGINE_ARGS and section_index > 0:
                continue
            if (
                normalized_name in DECODE_RENDER_ENGINE_ARGS
                and len(engine_sections) > 1
                and section_index < len(engine_sections) - 1
            ):
                continue
            if normalized_name in section_args and section_args[normalized_name] != value:
                raise ValueError(f"{section_name}.engine_config contains conflicting values for {normalized_name}")
            section_args[normalized_name] = value

        for name, value in section_args.items():
            if name in derived and derived[name] != value and name not in overridden_names:
                raise ValueError(f"Render launch argument {name} conflicts between {sources[name]} and {section_name}")
            derived[name] = value
            sources[name] = section_name
    return derived


def _normalize_user_launch_args(render_config: dict[str, Any]) -> dict[str, Any]:
    configured_launch_args = render_config.get("launch_args", {})
    if not isinstance(configured_launch_args, dict):
        raise ValueError("render_config.launch_args must be an object")
    normalized: dict[str, Any] = {}
    for raw_name, value in configured_launch_args.items():
        normalized_name = _normalize_launch_arg_name(raw_name, "render_config.launch_args")
        if normalized_name in MANAGED_RENDER_LAUNCH_ARGS:
            raise ValueError(f"render_config.launch_args.{raw_name} is managed by Motor")
        if normalized_name in normalized:
            raise ValueError(f"render_config.launch_args contains duplicate argument: {raw_name}")
        normalized[normalized_name] = value
    return normalized


def _resolve_launch_args(
    render_config: dict[str, Any],
    engine_sections: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    """Merge Motor defaults, Engine compatibility args, then explicit Render overrides."""
    user_launch_args = _normalize_user_launch_args(render_config)
    launch_args = {
        "renderer_num_workers": DEFAULT_RENDERER_NUM_WORKERS,
        **_derive_render_launch_args(engine_sections, set(user_launch_args)),
        **user_launch_args,
    }
    hf_overrides = launch_args.get("hf_overrides", {})
    if isinstance(hf_overrides, dict):
        # Render needs no quantized kernels, but ModelConfig validates quantization before Render clears it.
        launch_args["hf_overrides"] = {"quantization_config": None, **hf_overrides}
    renderer_num_workers = launch_args.get("renderer_num_workers")
    if not isinstance(renderer_num_workers, int) or isinstance(renderer_num_workers, bool) or renderer_num_workers <= 0:
        raise ValueError("render_config.launch_args.renderer_num_workers must be a positive integer")
    return launch_args


def _build_render_command(
    use_cpu_image: bool,
    model: str,
    served_model_names: list[str] | tuple[str, ...],
    port: int,
    launch_args: dict[str, Any],
) -> tuple[list[str], list[str]]:
    render_args = [
        "vllm",
        "launch",
        "render",
        model,
        "--served-model-name",
        *served_model_names,
        "--host",
        "0.0.0.0",
        "--port",
        str(port),
        "--disable-access-log-for-endpoints",
        "/health,/v1/chat/completions/derender,/v1/completions/derender",
    ]
    render_args.extend(_serialize_launch_args(launch_args))
    if use_cpu_image:
        return ["vllm"], render_args[1:]

    startup_script = """\
source /usr/local/Ascend/ascend-toolkit/set_env.sh 2>/dev/null || true
source /usr/local/Ascend/nnal/atb/set_env.sh 2>/dev/null || true
export LD_LIBRARY_PATH="/usr/local/Ascend/driver/lib64/driver:/usr/local/Ascend/driver/lib64/common:${LD_LIBRARY_PATH:-}"
bootstrap_root="${VLLM_CACHE_ROOT}/bootstrap"
mkdir -p "${bootstrap_root}"
printf '%s' "$2" > "${bootstrap_root}/sitecustomize.py"
export PYTHONPATH="${bootstrap_root}:${PYTHONPATH:-}"
exec python3 -c "$1" "${@:3}"
"""
    return ["/bin/bash", "-c"], [
        startup_script,
        "render-entrypoint",
        CPU_RENDER_LAUNCHER,
        CPU_RENDER_SITECUSTOMIZE,
        *render_args[1:],
    ]


def _build_render_container(
    image: str,
    use_cpu_image: bool,
    model: str,
    served_model_names: list[str] | tuple[str, ...],
    port: int,
    launch_args: dict[str, Any],
) -> dict[str, Any]:
    command, args = _build_render_command(use_cpu_image, model, served_model_names, port, launch_args)
    env = [
        {C.NAME: "HF_HUB_OFFLINE", C.VALUE: "1"},
        {C.NAME: "TRANSFORMERS_OFFLINE", C.VALUE: "1"},
        {C.NAME: "VLLM_LOGGING_LEVEL", C.VALUE: "INFO"},
        # Render performs preprocessing only; disabling MLA avoids vLLM CPU MLA+Mamba cache validation conflicts.
        {C.NAME: "VLLM_MLA_DISABLE", C.VALUE: "1"},
        {C.NAME: "OMP_NUM_THREADS", C.VALUE: "4"},
    ]
    if not use_cpu_image:
        env.extend(
            [
                {C.NAME: "VLLM_PLUGINS", C.VALUE: ""},
                {C.NAME: "TORCH_DEVICE_BACKEND_AUTOLOAD", C.VALUE: "0"},
                {C.NAME: "VLLM_CACHE_ROOT", C.VALUE: RENDER_CACHE_ROOT},
                {C.NAME: "TRITON_CACHE_DIR", C.VALUE: f"{RENDER_CACHE_ROOT}/triton"},
            ]
        )

    return {
        C.NAME: RENDER_CONTAINER_NAME,
        C.IMAGE: image,
        "imagePullPolicy": "IfNotPresent",
        "command": command,
        "args": args,
        C.ENV: env,
        C.PORTS: [{C.NAME: "render", "containerPort": port, "protocol": "TCP"}],
        "startupProbe": {
            "httpGet": {C.PATH: "/health", C.PORT: "render"},
            "periodSeconds": 5,
            "timeoutSeconds": 3,
            "failureThreshold": 120,
        },
        "readinessProbe": {
            "httpGet": {C.PATH: "/health", C.PORT: "render"},
            "periodSeconds": 5,
            "timeoutSeconds": 3,
            "failureThreshold": 3,
        },
        "livenessProbe": {
            "httpGet": {C.PATH: "/health", C.PORT: "render"},
            "periodSeconds": 10,
            "timeoutSeconds": 3,
            "failureThreshold": 5,
        },
        C.RESOURCES: {
            C.REQUESTS: {"cpu": "4", "memory": "8Gi"},
            C.LIMITS: {"cpu": "16", "memory": "32Gi"},
        },
        C.SECURITY_CONTEXT: {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "seccompProfile": {"type": "Unconfined"},
        },
    }


def _render_config_section(user_config: dict[str, Any]) -> dict[str, Any]:
    section = user_config.get(C.MOTOR_COORDINATOR_CONFIG, {}).get(C.RENDER_CONFIG, {})
    return section if isinstance(section, dict) else {}


def is_render_enabled(user_config: dict[str, Any]) -> bool:
    """Resolve Render intent, with an explicit switch overriding the KV-affinity default."""
    render_config = _render_config_section(user_config)
    if "enable" in render_config:
        return render_config.get("enable") is True

    coordinator_config = user_config.get(C.MOTOR_COORDINATOR_CONFIG, {})
    scheduler_config = coordinator_config.get("scheduler_config", {}) if isinstance(coordinator_config, dict) else {}
    if not isinstance(scheduler_config, dict):
        return False
    prefill_scheduler_type = scheduler_config.get(
        "prefill_scheduler_type",
        scheduler_config.get("scheduler_type"),
    )
    if not isinstance(prefill_scheduler_type, str) or prefill_scheduler_type.lower() != "kv_cache_affinity":
        return False
    engine_sections = _resolve_render_engine_sections(user_config)
    for section_name, section in engine_sections:
        if (
            section_name not in user_config
            or not isinstance(section, dict)
            or section.get(C.ENGINE_TYPE, C.ENGINE_TYPE_VLLM) != C.ENGINE_TYPE_VLLM
        ):
            logger.warning("Render auto-enable skipped: %s must describe a vllm engine", section_name)
            return False
    return True


def _resolve_render_endpoint(render_config: dict[str, Any]) -> tuple[str, int]:
    endpoint = render_config.get("endpoint", {})
    if not isinstance(endpoint, dict):
        endpoint = {}
    host = endpoint.get("host", "127.0.0.1")
    if host not in LOCAL_RENDER_HOSTS:
        raise ValueError("Render sidecar endpoint.host must be localhost, 127.0.0.1, or ::1")
    port = endpoint.get("port", 8100)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("Render sidecar endpoint.port must be an integer between 1 and 65535")
    return host, port


def render_listen_port(user_config: dict[str, Any]) -> int:
    """Port Render binds. Defaults to 8100 when the endpoint is omitted."""
    _host, port = _resolve_render_endpoint(_render_config_section(user_config))
    return port


def role_needs_docker_render(role: str | None) -> bool:
    """True when this Docker role co-locates Coordinator and needs a Render sibling.

    Omitting ``--role`` is the single-container deploy. That container starts
    Render itself, so a sibling would bind the same port.
    """
    if not role:
        return False
    if role in ("coordinator", "coordinator_controller"):
        return True
    return False


def docker_render_container_name(main_container_name: str) -> str:
    """Sibling container name for Docker-only Render (mirrors K8s sidecar)."""
    return f"{main_container_name}-{RENDER_CONTAINER_NAME}"


def resolve_render_image(user_config: dict[str, Any]) -> tuple[str, bool]:
    """Return ``(image, use_cpu_image)`` using the same rules as K8s sidecar."""
    render_config = _render_config_section(user_config)
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    image_name = render_config.get(C.IMAGE_NAME, "")
    if not isinstance(image_name, str):
        raise ValueError("Render image_name must be a string")
    if image_name and not image_name.strip():
        raise ValueError("Render image_name cannot contain only whitespace")
    use_cpu_image = bool(image_name)
    image = image_name if use_cpu_image else deploy_config[C.IMAGE_NAME]
    if not isinstance(image, str) or not image.strip():
        raise ValueError("Render image is empty; set render_config.image_name or motor_deploy_config.image_name")
    return image, use_cpu_image


def resolve_render_launch_spec(user_config: dict[str, Any]) -> RenderLaunchSpec:
    """Resolve the one Render launch contract consumed by K8s, Docker, and in-place execution."""
    if not is_render_enabled(user_config):
        raise ValueError("render_config.enable is false; cannot start Render")

    render_config = _render_config_section(user_config)
    _host, port = _resolve_render_endpoint(render_config)
    engine_sections = _resolve_render_engine_sections(user_config)
    model, served_model_names = _resolve_render_model(engine_sections)
    image, use_cpu_image = resolve_render_image(user_config)
    launch_args = _resolve_launch_args(render_config, engine_sections)
    return RenderLaunchSpec(
        image=image,
        use_cpu_image=use_cpu_image,
        model=model,
        served_model_names=tuple(served_model_names),
        port=port,
        launch_args=launch_args,
    )


def render_runtime_env(use_cpu_image: bool) -> dict[str, str]:
    env = {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "VLLM_LOGGING_LEVEL": "INFO",
        "VLLM_MLA_DISABLE": "1",
        "OMP_NUM_THREADS": "4",
    }
    if not use_cpu_image:
        env.update(
            {
                "VLLM_PLUGINS": "",
                "TORCH_DEVICE_BACKEND_AUTOLOAD": "0",
                "VLLM_CACHE_ROOT": RENDER_CACHE_ROOT,
                "TRITON_CACHE_DIR": f"{RENDER_CACHE_ROOT}/triton",
                "LD_LIBRARY_PATH": ("/usr/local/Ascend/driver/lib64/common:/usr/local/Ascend/driver/lib64/driver"),
            }
        )
    return env


def build_render_exec_argv(user_config: dict[str, Any]) -> list[str]:
    """Argv to exec ``vllm launch render`` inside an existing container."""
    spec = resolve_render_launch_spec(user_config)
    command, args = _build_render_command(
        spec.use_cpu_image,
        spec.model,
        spec.served_model_names,
        spec.port,
        spec.launch_args,
    )
    return [*command, *args]


def exec_render_in_place(user_config: dict[str, Any]) -> None:
    """Replace this process with vLLM Render (Docker ``--start --role render``)."""
    spec = resolve_render_launch_spec(user_config)
    os.environ.update(render_runtime_env(spec.use_cpu_image))
    command, args = _build_render_command(
        spec.use_cpu_image,
        spec.model,
        spec.served_model_names,
        spec.port,
        spec.launch_args,
    )
    argv = [*command, *args]
    os.execvp(argv[0], argv)


def build_docker_render_run_argv(container_name: str, user_config: dict[str, Any]) -> list[str] | None:
    """Build ``docker run ...`` argv for a host-network Render sibling container.

    Returns ``None`` when Render is disabled. Aligns command/env/image with
    ``configure_render_sidecar`` so Docker-only matches the K8s sidecar path,
    including ``render_config.launch_args`` serialization.
    """
    if not is_render_enabled(user_config):
        return None

    spec = resolve_render_launch_spec(user_config)
    command, args = _build_render_command(
        spec.use_cpu_image,
        spec.model,
        spec.served_model_names,
        spec.port,
        spec.launch_args,
    )

    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    weight_mount_path = str(deploy_config.get(C.WEIGHT_MOUNT_PATH, C.DEFAULT_WEIGHT_MOUNT_PATH) or "").strip()

    argv: list[str] = [
        "docker",
        "run",
        "-d",
        "--name",
        container_name,
        "--net=host",
        "-u",
        "root",
        "-e",
        "HF_HUB_OFFLINE=1",
        "-e",
        "TRANSFORMERS_OFFLINE=1",
        "-e",
        "VLLM_LOGGING_LEVEL=INFO",
        "-e",
        "VLLM_MLA_DISABLE=1",
        "-e",
        "OMP_NUM_THREADS=4",
    ]
    seen_binds: set[str] = set()

    def add_bind(spec: str) -> None:
        if bind_mount_is_new(spec, seen_binds):
            argv.extend(["-v", spec])
            return
        logger.info("Skipped duplicate Render bind mount %s", spec)

    add_bind("/root/ascend/log:/root/ascend/log")
    add_bind("/root/.cache:/root/.cache")
    add_bind("/mnt:/mnt:ro")
    if weight_mount_path:
        add_bind(f"{weight_mount_path}:{weight_mount_path}:ro")
    if not spec.use_cpu_image:
        argv.extend(
            [
                "-e",
                "VLLM_PLUGINS=",
                "-e",
                "TORCH_DEVICE_BACKEND_AUTOLOAD=0",
                "-e",
                f"VLLM_CACHE_ROOT={RENDER_CACHE_ROOT}",
                "-e",
                f"TRITON_CACHE_DIR={RENDER_CACHE_ROOT}/triton",
                "-e",
                "LD_LIBRARY_PATH=/usr/local/Ascend/driver/lib64/common:/usr/local/Ascend/driver/lib64/driver",
            ]
        )
        add_bind(f"{ASCEND_DRIVER_PATH}:{ASCEND_DRIVER_PATH}:ro")
    argv.append(spec.image)
    argv.extend(command)
    argv.extend(args)
    return argv


def configure_render_sidecar(pod_spec: dict[str, Any], user_config: dict[str, Any]) -> None:
    """Reconcile the Render sidecar in a Coordinator-bearing PodSpec."""
    containers = pod_spec.setdefault(C.CONTAINERS, [])
    containers[:] = [container for container in containers if container.get(C.NAME) != RENDER_CONTAINER_NAME]
    pod_spec[C.VOLUMES] = [
        volume for volume in pod_spec.get(C.VOLUMES, []) if volume.get(C.NAME) != ASCEND_DRIVER_VOLUME_NAME
    ]

    if not is_render_enabled(user_config):
        return

    spec = resolve_render_launch_spec(user_config)
    container = _build_render_container(
        spec.image,
        spec.use_cpu_image,
        spec.model,
        spec.served_model_names,
        spec.port,
        spec.launch_args,
    )
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    weight_mount_path = deploy_config.get(C.WEIGHT_MOUNT_PATH, C.DEFAULT_WEIGHT_MOUNT_PATH)
    _ensure_readonly_host_mount(pod_spec, container, "mnt", "/mnt")
    if weight_mount_path.rstrip("/") != "/mnt":
        from lib.generator.engine import set_weight_mount

        set_weight_mount(pod_spec, container, weight_mount_path)
    if not spec.use_cpu_image:
        _ensure_readonly_host_mount(pod_spec, container, ASCEND_DRIVER_VOLUME_NAME, ASCEND_DRIVER_PATH)
    containers.append(container)
