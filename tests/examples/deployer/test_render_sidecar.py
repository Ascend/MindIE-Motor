# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from pathlib import Path

import pytest

import lib.constant as C
from lib.generator import k8s_utils
from lib.generator.coordinator import generate_yaml_coordinator
from lib.generator.infer_service import (
    _find_infer_service_set_doc,
    generate_yaml_infer_service_set,
    get_infer_role,
)
from lib.generator.render import (
    ASCEND_DRIVER_VOLUME_NAME,
    RENDER_CONTAINER_NAME,
    build_docker_render_run_argv,
    build_render_exec_argv,
    configure_render_sidecar,
    role_needs_docker_render,
)
from lib.generator.single_container import generate_yaml_single_container
from lib.utils import load_yaml

DEPLOYER_ROOT = Path(__file__).resolve().parents[3] / "examples" / "deployer"
DEPLOYMENT_MODES = [
    ("multi", "coordinator_template.yaml", generate_yaml_coordinator),
    ("infer-service", "infer_service_template.yaml", generate_yaml_infer_service_set),
    (
        "single-container",
        "single_container_template.yaml",
        generate_yaml_single_container,
    ),
]


def _user_config(use_cpu_image=True):
    engine = {
        C.ENGINE_TYPE: C.ENGINE_TYPE_VLLM,
        C.ENGINE_CONFIG: {
            "model": "/mnt/weight/qwen",
            "served_model_name": "qwen",
            "max_model_len": 2048,
        },
    }
    return {
        C.MOTOR_DEPLOY_CONFIG: {
            C.CONFIG_JOB_ID: "render-test",
            C.IMAGE_NAME: "mindie-npu:test",
            C.HARDWARE_TYPE: C.HARDWARE_TYPE_800I_A3,
            C.WEIGHT_MOUNT_PATH: "/mnt/weight",
            C.P_INSTANCES_NUM: 1,
            C.D_INSTANCES_NUM: 1,
            C.SINGER_P_INSTANCES_NUM: 1,
            C.SINGER_D_INSTANCES_NUM: 1,
            C.P_POD_NPU_NUM: 1,
            C.D_POD_NPU_NUM: 1,
        },
        C.MOTOR_COORDINATOR_CONFIG: {
            C.RENDER_CONFIG: {
                "enable": True,
                "endpoint": {"host": "127.0.0.1", "port": 8110},
                **({C.IMAGE_NAME: "vllm-render-cpu:test"} if use_cpu_image else {}),
            }
        },
        C.MOTOR_ENGINE_PREFILL_CONFIG: engine,
        C.MOTOR_ENGINE_DECODE_CONFIG: {
            C.ENGINE_TYPE: engine[C.ENGINE_TYPE],
            C.ENGINE_CONFIG: dict(engine[C.ENGINE_CONFIG]),
        },
    }


def _pod_spec(stale_sidecar=False):
    containers = [{C.NAME: "coordinator"}]
    volumes = []
    if stale_sidecar:
        containers.append({C.NAME: RENDER_CONTAINER_NAME, C.IMAGE: "stale"})
        volumes.append({C.NAME: ASCEND_DRIVER_VOLUME_NAME, C.HOST_PATH: {C.PATH: "/stale"}})
    return {C.CONTAINERS: containers, C.VOLUMES: volumes}


def _render_container(pod_spec):
    matches = [item for item in pod_spec[C.CONTAINERS] if item.get(C.NAME) == RENDER_CONTAINER_NAME]
    assert len(matches) == 1
    return matches[0]


def _render_arg(container, name):
    index = container["args"].index(name)
    return container["args"][index + 1]


def test_cpu_render_uses_dedicated_image_without_ascend_runtime():
    pod_spec = _pod_spec(stale_sidecar=True)
    configure_render_sidecar(pod_spec, _user_config())

    container = _render_container(pod_spec)
    assert container[C.IMAGE] == "vllm-render-cpu:test"
    assert container[C.PORTS][0]["containerPort"] == 8110
    assert _render_arg(container, "--renderer-num-workers") == "4"
    assert C.ASCEND_910_NPU_NUM not in container[C.RESOURCES][C.REQUESTS]
    assert container["command"] == ["vllm"]
    assert "VLLM_PLUGINS" not in {item[C.NAME] for item in container[C.ENV]}
    assert ASCEND_DRIVER_VOLUME_NAME not in {item[C.NAME] for item in pod_spec[C.VOLUMES]}


def test_render_launch_args_are_serialized():
    config = _user_config()
    config[C.MOTOR_COORDINATOR_CONFIG][C.RENDER_CONFIG]["launch_args"] = {
        "renderer_num_workers": 7,
        "enable_auto_tool_choice": True,
        "tool_call_parser": "hermes",
        "default_chat_template_kwargs": {"enable_thinking": False},
        "allowed_media_domains": ["images.example.com", "media.example.com"],
        "reasoning_parser": None,
        "unused_flag": False,
    }
    pod_spec = _pod_spec()

    configure_render_sidecar(pod_spec, config)

    args = _render_container(pod_spec)["args"]
    assert _render_arg(_render_container(pod_spec), "--renderer-num-workers") == "7"
    assert "--enable-auto-tool-choice" in args
    assert _render_arg(_render_container(pod_spec), "--tool-call-parser") == "hermes"
    assert _render_arg(_render_container(pod_spec), "--default-chat-template-kwargs") == ('{"enable_thinking":false}')
    domains_index = args.index("--allowed-media-domains")
    assert args[domains_index + 1 : domains_index + 3] == ["images.example.com", "media.example.com"]
    assert "--reasoning-parser" not in args
    assert "--unused-flag" not in args


@pytest.mark.parametrize("num_workers", [0, True, "4"])
def test_render_rejects_invalid_renderer_worker_count(num_workers):
    config = _user_config()
    config[C.MOTOR_COORDINATOR_CONFIG][C.RENDER_CONFIG]["launch_args"] = {"renderer_num_workers": num_workers}

    with pytest.raises(ValueError, match="renderer_num_workers"):
        configure_render_sidecar(_pod_spec(), config)


@pytest.mark.parametrize(
    ("launch_args", "error"),
    [
        ([], "must be an object"),
        ({"port": 8200}, "managed by Motor"),
        ({"Port": 8200}, "managed by Motor"),
        ({"served-model-name": "other"}, "managed by Motor"),
        ({"custom": object()}, "must be null, bool, string, number, object, or array"),
        ({"custom": [object()]}, "contains an unsupported value"),
    ],
)
def test_render_rejects_invalid_launch_args(launch_args, error):
    config = _user_config()
    config[C.MOTOR_COORDINATOR_CONFIG][C.RENDER_CONFIG]["launch_args"] = launch_args

    with pytest.raises(ValueError, match=error):
        configure_render_sidecar(_pod_spec(), config)


def test_docker_render_run_argv_uses_launch_args():
    config = _user_config()
    config[C.MOTOR_COORDINATOR_CONFIG][C.RENDER_CONFIG]["launch_args"] = {
        "renderer_num_workers": 3,
        "enable_auto_tool_choice": True,
        "allowed_media_domains": ["images.example.com", "media.example.com"],
    }

    argv = build_docker_render_run_argv("motor-cc-vllm-render", config)

    assert argv is not None
    assert argv[:3] == ["docker", "run", "-d"]
    assert "--renderer-num-workers" in argv
    assert argv[argv.index("--renderer-num-workers") + 1] == "3"
    assert "--enable-auto-tool-choice" in argv
    domains_index = argv.index("--allowed-media-domains")
    assert argv[domains_index + 1 : domains_index + 3] == ["images.example.com", "media.example.com"]
    assert "--host" in argv
    assert argv[argv.index("--port") + 1] == "8110"


def test_docker_render_exec_argv_matches_run_command():
    config = _user_config()
    argv = build_docker_render_run_argv("motor-cc-vllm-render", config)
    exec_argv = build_render_exec_argv(config)
    assert argv is not None
    assert argv[-len(exec_argv) :] == exec_argv
    assert role_needs_docker_render("render") is False
    assert role_needs_docker_render("prefill") is False
    assert role_needs_docker_render("coordinator") is True
    disabled = _user_config()
    disabled[C.MOTOR_COORDINATOR_CONFIG][C.RENDER_CONFIG]["enable"] = False
    with pytest.raises(ValueError, match="enable is false"):
        build_render_exec_argv(disabled)


def test_disabled_render_removes_stale_sidecar_and_driver_mount():
    config = _user_config()
    config[C.MOTOR_COORDINATOR_CONFIG][C.RENDER_CONFIG]["enable"] = False
    pod_spec = _pod_spec(stale_sidecar=True)

    configure_render_sidecar(pod_spec, config)

    assert [item[C.NAME] for item in pod_spec[C.CONTAINERS]] == ["coordinator"]
    assert pod_spec[C.VOLUMES] == []


def test_ascend_render_inherits_service_image_and_mounts_driver_without_requesting_npu():
    pod_spec = _pod_spec()
    configure_render_sidecar(pod_spec, _user_config(use_cpu_image=False))

    container = _render_container(pod_spec)
    assert container[C.IMAGE] == "mindie-npu:test"
    assert C.ASCEND_910_NPU_NUM not in container[C.RESOURCES][C.REQUESTS]
    assert container["command"] == ["/bin/bash", "-c"]
    assert "sitecustomize.py" in container["args"][0]
    assert "PYTHONPATH" in container["args"][0]
    assert "CpuPlatform" in container["args"][3]
    assert "vllm.triton_utils" in container["args"][3]
    assert container["args"][4:6] == ["launch", "render"]
    env = {item[C.NAME]: item[C.VALUE] for item in container[C.ENV]}
    assert env["VLLM_PLUGINS"] == ""
    assert env["TORCH_DEVICE_BACKEND_AUTOLOAD"] == "0"
    assert env["VLLM_CACHE_ROOT"] == "/tmp/vllm-render-cache"
    assert env["TRITON_CACHE_DIR"] == "/tmp/vllm-render-cache/triton"
    assert ASCEND_DRIVER_VOLUME_NAME in {item[C.NAME] for item in pod_spec[C.VOLUMES]}
    assert ASCEND_DRIVER_VOLUME_NAME in {item[C.NAME] for item in container[C.VOLUME_MOUNTS]}


@pytest.mark.parametrize("image_name", [123, "   "])
def test_render_rejects_invalid_image_name(image_name):
    config = _user_config()
    config[C.MOTOR_COORDINATOR_CONFIG][C.RENDER_CONFIG][C.IMAGE_NAME] = image_name
    with pytest.raises(ValueError, match="image_name"):
        configure_render_sidecar(_pod_spec(), config)


def test_render_rejects_mismatched_prefill_and_decode_models():
    config = _user_config()
    config[C.MOTOR_ENGINE_DECODE_CONFIG][C.ENGINE_CONFIG]["model"] = "/mnt/weight/other"
    with pytest.raises(ValueError, match="must match"):
        configure_render_sidecar(_pod_spec(), config)


@pytest.mark.parametrize(("mode", "template", "generator"), DEPLOYMENT_MODES)
def test_deployment_mode_adds_render_to_coordinator_pod(tmp_path, monkeypatch, mode, template, generator):
    for setting in (
        "g_kv_store_enabled",
        "g_kv_conductor_enabled",
        "g_mf_store_enabled",
    ):
        monkeypatch.setattr(k8s_utils, setting, False)
    output = tmp_path / f"{mode}.yaml"
    generator(str(DEPLOYER_ROOT / "yaml_template" / template), str(output), _user_config())

    documents = load_yaml(str(output), False)
    if mode == "infer-service":
        document = _find_infer_service_set_doc(documents)
        pod_spec = get_infer_role(document, C.COORDINATOR)[C.SPEC][C.TEMPLATE][C.SPEC]
    else:
        pod_spec = documents[0][C.SPEC][C.TEMPLATE][C.SPEC]
    assert _render_container(pod_spec)[C.IMAGE] == "vllm-render-cpu:test"
