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

import yaml

import lib.constant as C
import deploy as deploy_module
from lib.generator import k8s_utils
from lib.generator.engine import build_engine_env_items


DEPLOYER_ROOT = Path(__file__).resolve().parents[3] / "examples" / "deployer"


def _sglang_pd_user_config(*, p_instances_num=1):
    return {
        C.MOTOR_DEPLOY_CONFIG: {
            C.CONFIG_JOB_ID: "sglang-pd",
            C.IMAGE_NAME: "mindie:sglang",
            C.HARDWARE_TYPE: C.HARDWARE_TYPE_800I_A3,
            C.DEPLOY_MODE_CONFIG_KEY: C.DEPLOY_MODE_MULTI_DEPLOYMENT_YAML,
            C.P_INSTANCES_NUM: p_instances_num,
            C.D_INSTANCES_NUM: 1,
            C.SINGER_P_INSTANCES_NUM: 1,
            C.SINGER_D_INSTANCES_NUM: 1,
            C.P_POD_NPU_NUM: 4,
            C.D_POD_NPU_NUM: 4,
        },
        C.MOTOR_ENGINE_PREFILL_CONFIG: {C.ENGINE_TYPE: C.ENGINE_TYPE_SGLANG},
        C.MOTOR_ENGINE_DECODE_CONFIG: {C.ENGINE_TYPE: C.ENGINE_TYPE_SGLANG},
    }


def _deploy_paths(tmp_path):
    return {
        "controller_input_yaml": str(DEPLOYER_ROOT / "yaml_template" / "controller_template.yaml"),
        "controller_output_yaml": str(tmp_path / "mindie_motor_controller.yaml"),
        "coordinator_input_yaml": str(DEPLOYER_ROOT / "yaml_template" / "coordinator_template.yaml"),
        "coordinator_output_yaml": str(tmp_path / "mindie_motor_coordinator.yaml"),
        "engine_input_yaml": str(DEPLOYER_ROOT / "yaml_template" / "engine_template.yaml"),
        "engine_output_yaml": str(tmp_path / k8s_utils.g_engine_base_name),
        "kv_store_input_yaml": str(DEPLOYER_ROOT / "yaml_template" / "kv_cache_store_template.yaml"),
        "kv_store_output_yaml": str(tmp_path / "mindie_motor_kv_store.yaml"),
        "kv_conductor_input_yaml": str(DEPLOYER_ROOT / "yaml_template" / "kv_conductor_template.yaml"),
        "kv_conductor_output_yaml": str(tmp_path / "mindie_motor_kv_conductor.yaml"),
        "infer_service_input_yaml": str(DEPLOYER_ROOT / "yaml_template" / "infer_service_template.yaml"),
        "infer_service_output_yaml": str(tmp_path / "infer_service.yaml"),
        "single_container_input_yaml": str(DEPLOYER_ROOT / "yaml_template" / "single_container_template.yaml"),
        "single_container_output_yaml": str(tmp_path / "mindie_motor_single_container.yaml"),
        "mf_store_input_yaml": str(DEPLOYER_ROOT / "yaml_template" / "mf_store_template.yaml"),
        "mf_store_output_yaml": str(tmp_path / "mindie_motor_mf_store.yaml"),
    }


def _env_item(container, name):
    for item in container.get(C.ENV, []):
        if item.get(C.NAME) == name:
            return item
    return None


def test_build_engine_env_items_adds_sglang_host_ip_only_for_sglang(monkeypatch):
    deploy_config = {C.HARDWARE_TYPE: C.HARDWARE_TYPE_800I_A3}
    monkeypatch.setattr(k8s_utils, "g_mf_store_enabled", False)

    monkeypatch.setattr(k8s_utils, "g_engine_type", "vllm")
    vllm_names = {item[C.NAME] for item in build_engine_env_items(C.ROLE_PREFILL, deploy_config, "job-p0")}
    assert C.ENV_SGLANG_HOST_IP not in vllm_names

    monkeypatch.setattr(k8s_utils, "g_engine_type", C.ENGINE_TYPE_SGLANG)
    sglang_items = build_engine_env_items(C.ROLE_PREFILL, deploy_config, "job-p0")
    host_ip = next(item for item in sglang_items if item[C.NAME] == C.ENV_SGLANG_HOST_IP)
    assert host_ip["valueFrom"]["fieldRef"]["fieldPath"] == "status.podIP"


def test_handle_update_instance_num_injects_sglang_host_ip(tmp_path, monkeypatch):
    baseline_config = _sglang_pd_user_config(p_instances_num=1)
    current_config = _sglang_pd_user_config(p_instances_num=2)
    monkeypatch.setattr(k8s_utils, "g_engine_type", "vllm")
    monkeypatch.setattr(k8s_utils, "g_mf_store_enabled", False)
    monkeypatch.setattr(k8s_utils, "g_engine_base_name", "mindie-server")
    monkeypatch.setattr(deploy_module, "get_baseline_config_from_configmap", lambda _: baseline_config)
    monkeypatch.setattr(deploy_module, "get_deploy_paths", lambda: _deploy_paths(tmp_path))
    monkeypatch.setattr(C, "OUTPUT_ROOT_PATH", str(tmp_path))
    monkeypatch.setattr(k8s_utils, "create_motor_config_configmap", lambda *_a, **_k: None)
    monkeypatch.setattr(k8s_utils, "resolve_nodeports_for_yaml_files", lambda *_a, **_k: None)
    monkeypatch.setattr(k8s_utils, "safe_exec_cmd", lambda *_a, **_k: None)
    monkeypatch.setattr(k8s_utils, "get_existing_engine_instance_indices", lambda *_a, **_k: {0})

    deploy_module.handle_update_instance_num(current_config)

    assert k8s_utils.g_engine_type == C.ENGINE_TYPE_SGLANG
    scaled_yaml = tmp_path / "sglang_p1.yaml"
    assert scaled_yaml.is_file()
    with open(scaled_yaml, encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    container = data[C.SPEC][C.TEMPLATE][C.SPEC][C.CONTAINERS][0]
    host_ip = _env_item(container, C.ENV_SGLANG_HOST_IP)
    assert host_ip is not None
    assert host_ip["valueFrom"]["fieldRef"]["fieldPath"] == "status.podIP"
