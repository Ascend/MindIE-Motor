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

import lib.constant as C
from lib.generator.engine import is_hybrid_deploy, set_weight_mount


RENDER_CONTAINER_NAME = "vllm-render"
ASCEND_DRIVER_VOLUME_NAME = "ascend-driver-lib64"
ASCEND_DRIVER_PATH = "/usr/local/Ascend/driver/lib64"
LOCAL_RENDER_HOSTS = {"127.0.0.1", "localhost", "::1"}
DEFAULT_RENDERER_NUM_WORKERS = 4


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
    served_model_name = engine_config.get("served_model_name") or legacy_model_config.get("model_name")
    if not isinstance(model, str) or not model:
        raise ValueError(f"{section_name}.engine_config.model is required when Render is enabled")
    return model, _normalize_served_model_names(served_model_name)


def resolve_render_model(user_config: dict[str, Any]) -> tuple[str, list[str]]:
    """Resolve and validate the model consumed by the Render sidecar."""
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    if is_hybrid_deploy(deploy_config):
        section_name = C.MOTOR_ENGINE_UNION_CONFIG
        section = user_config.get(section_name, {})
        return _resolve_model_config(section, section_name)

    prefill = user_config.get(C.MOTOR_ENGINE_PREFILL_CONFIG, {})
    prefill_model = _resolve_model_config(prefill, C.MOTOR_ENGINE_PREFILL_CONFIG)
    decode = user_config.get(C.MOTOR_ENGINE_DECODE_CONFIG)
    if decode is not None:
        decode_model = _resolve_model_config(decode, C.MOTOR_ENGINE_DECODE_CONFIG)
        if decode_model != prefill_model:
            raise ValueError("Prefill and decode model/served_model_name must match when Render is enabled")
    return prefill_model


def _ensure_ascend_driver_mount(pod_spec: dict[str, Any], container: dict[str, Any]) -> None:
    volumes = pod_spec.setdefault(C.VOLUMES, [])
    if not any(volume.get(C.NAME) == ASCEND_DRIVER_VOLUME_NAME for volume in volumes):
        volumes.append(
            {
                C.NAME: ASCEND_DRIVER_VOLUME_NAME,
                C.HOST_PATH: {C.PATH: ASCEND_DRIVER_PATH, "type": "Directory"},
            }
        )

    mounts = container.setdefault(C.VOLUME_MOUNTS, [])
    if not any(mount.get(C.NAME) == ASCEND_DRIVER_VOLUME_NAME for mount in mounts):
        mounts.append(
            {
                C.NAME: ASCEND_DRIVER_VOLUME_NAME,
                C.MOUNT_PATH: ASCEND_DRIVER_PATH,
                "readOnly": True,
            }
        )


def _build_render_command(
    use_cpu_image: bool,
    model: str,
    served_model_names: list[str],
    port: int,
    renderer_num_workers: int,
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
        "--renderer-num-workers",
        str(renderer_num_workers),
        "--disable-access-log-for-endpoints",
        "/health",
    ]
    if use_cpu_image:
        return ["vllm"], render_args[1:]

    startup_script = """\
source /usr/local/Ascend/ascend-toolkit/set_env.sh 2>/dev/null || true
source /usr/local/Ascend/nnal/atb/set_env.sh 2>/dev/null || true
export LD_LIBRARY_PATH="/usr/local/Ascend/driver/lib64/driver:/usr/local/Ascend/driver/lib64/common:${LD_LIBRARY_PATH:-}"
exec "$@"
"""
    return ["/bin/bash", "-c"], [startup_script, "render-entrypoint", *render_args]


def _build_render_container(
    image: str,
    use_cpu_image: bool,
    model: str,
    served_model_names: list[str],
    port: int,
    renderer_num_workers: int,
) -> dict[str, Any]:
    command, args = _build_render_command(use_cpu_image, model, served_model_names, port, renderer_num_workers)
    return {
        C.NAME: RENDER_CONTAINER_NAME,
        C.IMAGE: image,
        "imagePullPolicy": "IfNotPresent",
        "command": command,
        "args": args,
        C.ENV: [
            {C.NAME: "HF_HUB_OFFLINE", C.VALUE: "1"},
            {C.NAME: "TRANSFORMERS_OFFLINE", C.VALUE: "1"},
            {C.NAME: "VLLM_LOGGING_LEVEL", C.VALUE: "INFO"},
            {C.NAME: "OMP_NUM_THREADS", C.VALUE: "4"},
        ],
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


def configure_render_sidecar(pod_spec: dict[str, Any], user_config: dict[str, Any]) -> None:
    """Reconcile the Render sidecar in a Coordinator-bearing PodSpec."""
    containers = pod_spec.setdefault(C.CONTAINERS, [])
    containers[:] = [container for container in containers if container.get(C.NAME) != RENDER_CONTAINER_NAME]
    pod_spec[C.VOLUMES] = [
        volume for volume in pod_spec.get(C.VOLUMES, []) if volume.get(C.NAME) != ASCEND_DRIVER_VOLUME_NAME
    ]

    render_config = user_config.get(C.MOTOR_COORDINATOR_CONFIG, {}).get(C.RENDER_CONFIG, {})
    if not render_config.get("enabled", False):
        return

    endpoint = render_config.get("endpoint", {})
    host = endpoint.get("host", "127.0.0.1")
    if host not in LOCAL_RENDER_HOSTS:
        raise ValueError("Render sidecar endpoint.host must be localhost, 127.0.0.1, or ::1")
    port = endpoint.get("port", 8100)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("Render sidecar endpoint.port must be an integer between 1 and 65535")

    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    image_name = render_config.get(C.IMAGE_NAME, "")
    if not isinstance(image_name, str):
        raise ValueError("Render image_name must be a string")
    if image_name and not image_name.strip():
        raise ValueError("Render image_name cannot contain only whitespace")
    use_cpu_image = bool(image_name)
    image = image_name if use_cpu_image else deploy_config[C.IMAGE_NAME]

    coordinator_config = user_config.get(C.MOTOR_COORDINATOR_CONFIG, {})
    inference_workers_config = coordinator_config.get("inference_workers_config", {})
    renderer_num_workers = inference_workers_config.get("num_workers", DEFAULT_RENDERER_NUM_WORKERS)
    if not isinstance(renderer_num_workers, int) or isinstance(renderer_num_workers, bool) or renderer_num_workers <= 0:
        raise ValueError("inference_workers_config.num_workers must be a positive integer")

    model, served_model_names = resolve_render_model(user_config)
    container = _build_render_container(
        image,
        use_cpu_image,
        model,
        served_model_names,
        port,
        renderer_num_workers,
    )
    weight_mount_path = deploy_config.get(C.WEIGHT_MOUNT_PATH, C.DEFAULT_WEIGHT_MOUNT_PATH)
    set_weight_mount(pod_spec, container, weight_mount_path)
    if not use_cpu_image:
        _ensure_ascend_driver_mount(pod_spec, container)
    containers.append(container)
