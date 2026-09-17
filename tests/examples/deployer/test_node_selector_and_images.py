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
from lib.generator import k8s_utils
from lib.generator.controller import generate_yaml_controller
from lib.generator.coordinator import generate_yaml_coordinator
from lib.generator.engine import generate_yaml_engine
from lib.generator.infer_service import (
    _find_infer_service_set_doc,
    generate_yaml_infer_service_set,
    get_infer_role,
)
from lib.generator.kv_conductor import generate_yaml_kv_conductor
from lib.utils import load_yaml


DEPLOYER_ROOT = Path(__file__).resolve().parents[3] / "examples" / "deployer"


def _pd_user_config(hardware_type, *, extra_deploy=None, extra_prefill=None, extra_decode=None):
    deploy = {
        C.CONFIG_JOB_ID: "pd-ns",
        C.IMAGE_NAME: "mindie:default",
        C.HARDWARE_TYPE: hardware_type,
        C.P_INSTANCES_NUM: 1,
        C.D_INSTANCES_NUM: 1,
        C.SINGER_P_INSTANCES_NUM: 1,
        C.SINGER_D_INSTANCES_NUM: 1,
        C.P_POD_NPU_NUM: 4,
        C.D_POD_NPU_NUM: 4,
    }
    if extra_deploy:
        deploy.update(extra_deploy)
    return {
        C.MOTOR_DEPLOY_CONFIG: deploy,
        C.MOTOR_CONTROLLER_CONFIG: {},
        C.MOTOR_COORDINATOR_CONFIG: {},
        C.MOTOR_ENGINE_PREFILL_CONFIG: extra_prefill or {},
        C.MOTOR_ENGINE_DECODE_CONFIG: extra_decode or {},
    }


def _engine_node_selector(tmp_path, user_config, node_type):
    k8s_utils.g_generate_yaml_list = []
    generate_yaml_engine(
        str(DEPLOYER_ROOT / "yaml_template" / "engine_template.yaml"),
        str(tmp_path / "mindie_server"),
        user_config,
    )
    suffix = f"_{node_type}0.yaml"
    output_file = next(path for path in k8s_utils.g_generate_yaml_list if path.endswith(suffix))
    with open(output_file, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return data[C.SPEC][C.TEMPLATE][C.SPEC][C.NODE_SELECTOR], data


def test_a2_engine_node_selector_keeps_accelerator_type(tmp_path):
    """A2/A3 share "accelerator", so accelerator-type must stay to tell them apart."""
    user_config = _pd_user_config(C.HARDWARE_TYPE_800I_A2)
    node_selector, _ = _engine_node_selector(tmp_path, user_config, C.NODE_TYPE_P)
    assert node_selector == {
        C.ACCELERATOR: C.ACCELERATOR_910,
        C.ACCELERATOR_TYPE: C.ACCELERATOR_TYPE_A3,  # stub value from conftest
    }


def test_a5_engine_node_selector_without_chip_name(tmp_path):
    user_config = _pd_user_config(C.HARDWARE_TYPE_ASCEND950)
    node_selector, data = _engine_node_selector(tmp_path, user_config, C.NODE_TYPE_P)
    assert node_selector == {C.ACCELERATOR: C.ACCELERATOR_A5}
    assert C.NPU_CHIP_NAME_LABEL not in node_selector
    container = data[C.SPEC][C.TEMPLATE][C.SPEC][C.CONTAINERS][0]
    assert C.ASCEND_950_NPU_NUM in container[C.RESOURCES][C.REQUESTS]


def test_pd_chip_name_selector_applied_per_role(tmp_path):
    user_config = _pd_user_config(
        C.HARDWARE_TYPE_ASCEND950,
        extra_prefill={C.NPU_CHIP_NAME_KEY: "Ascend950PR"},
        extra_decode={C.NPU_CHIP_NAME_KEY: "Ascend950DT"},
    )
    prefill_selector, _ = _engine_node_selector(tmp_path, user_config, C.NODE_TYPE_P)
    decode_selector, _ = _engine_node_selector(tmp_path, user_config, C.NODE_TYPE_D)
    assert prefill_selector == {
        C.ACCELERATOR: C.ACCELERATOR_A5,
        C.NPU_CHIP_NAME_LABEL: "Ascend950PR",
    }
    assert decode_selector == {
        C.ACCELERATOR: C.ACCELERATOR_A5,
        C.NPU_CHIP_NAME_LABEL: "Ascend950DT",
    }


def test_component_image_overrides_deploy_image(tmp_path):
    user_config = _pd_user_config(
        C.HARDWARE_TYPE_800I_A3,
        extra_prefill={C.IMAGE_NAME: "mindie:prefill"},
        extra_decode={C.IMAGE_NAME: "mindie:decode"},
    )
    user_config[C.MOTOR_CONTROLLER_CONFIG][C.IMAGE_NAME] = "mindie:controller"
    user_config[C.MOTOR_COORDINATOR_CONFIG][C.IMAGE_NAME] = "mindie:coordinator"

    generate_yaml_controller(
        str(DEPLOYER_ROOT / "yaml_template" / "controller_template.yaml"),
        str(tmp_path / "controller.yaml"),
        user_config,
    )
    generate_yaml_coordinator(
        str(DEPLOYER_ROOT / "yaml_template" / "coordinator_template.yaml"),
        str(tmp_path / "coordinator.yaml"),
        user_config,
    )
    _, prefill_data = _engine_node_selector(tmp_path, user_config, C.NODE_TYPE_P)
    _, decode_data = _engine_node_selector(tmp_path, user_config, C.NODE_TYPE_D)

    def _deployment_image(path):
        docs = load_yaml(str(path), False)
        if isinstance(docs, list):
            deployment = next(doc for doc in docs if doc.get(C.KIND) == C.DEPLOYMENT_KIND)
        else:
            deployment = docs
        return deployment[C.SPEC][C.TEMPLATE][C.SPEC][C.CONTAINERS][0][C.IMAGE]

    assert _deployment_image(tmp_path / "controller.yaml") == "mindie:controller"
    assert _deployment_image(tmp_path / "coordinator.yaml") == "mindie:coordinator"
    assert prefill_data[C.SPEC][C.TEMPLATE][C.SPEC][C.CONTAINERS][0][C.IMAGE] == "mindie:prefill"
    assert decode_data[C.SPEC][C.TEMPLATE][C.SPEC][C.CONTAINERS][0][C.IMAGE] == "mindie:decode"


def test_infer_service_set_uses_chip_name_and_images(tmp_path):
    user_config = _pd_user_config(
        C.HARDWARE_TYPE_ASCEND950,
        extra_prefill={
            C.IMAGE_NAME: "mindie:prefill",
            C.NPU_CHIP_NAME_KEY: "Ascend950PR",
        },
        extra_decode={
            C.IMAGE_NAME: "mindie:decode",
            C.NPU_CHIP_NAME_KEY: "Ascend950DT",
        },
    )
    user_config[C.MOTOR_CONTROLLER_CONFIG][C.IMAGE_NAME] = "mindie:controller"
    output = tmp_path / "infer_service.yaml"
    k8s_utils.g_generate_yaml_list = []
    generate_yaml_infer_service_set(
        str(DEPLOYER_ROOT / "yaml_template" / "infer_service_template.yaml"),
        str(output),
        user_config,
    )
    infer_doc = _find_infer_service_set_doc(load_yaml(str(output), False))
    controller = get_infer_role(infer_doc, C.CONTROLLER)
    prefill = get_infer_role(infer_doc, C.ROLE_PREFILL)
    decode = get_infer_role(infer_doc, C.ROLE_DECODE)
    assert controller[C.SPEC][C.TEMPLATE][C.SPEC][C.CONTAINERS][0][C.IMAGE] == "mindie:controller"
    assert prefill[C.SPEC][C.TEMPLATE][C.SPEC][C.CONTAINERS][0][C.IMAGE] == "mindie:prefill"
    assert decode[C.SPEC][C.TEMPLATE][C.SPEC][C.CONTAINERS][0][C.IMAGE] == "mindie:decode"
    assert prefill[C.SPEC][C.TEMPLATE][C.SPEC][C.NODE_SELECTOR] == {
        C.ACCELERATOR: C.ACCELERATOR_A5,
        C.NPU_CHIP_NAME_LABEL: "Ascend950PR",
    }
    assert decode[C.SPEC][C.TEMPLATE][C.SPEC][C.NODE_SELECTOR] == {
        C.ACCELERATOR: C.ACCELERATOR_A5,
        C.NPU_CHIP_NAME_LABEL: "Ascend950DT",
    }


def _kv_conductor_image(tmp_path, output_name, user_config):
    output = tmp_path / output_name
    k8s_utils.g_generate_yaml_list = []
    generate_yaml_kv_conductor(
        str(DEPLOYER_ROOT / "yaml_template" / "kv_conductor_template.yaml"),
        str(output),
        user_config,
        user_config[C.KV_CONDUCTOR_CONFIG],
    )
    docs = load_yaml(str(output), False)
    docs = docs if isinstance(docs, list) else [docs]
    deployment = next(doc for doc in docs if doc.get(C.KIND) == C.DEPLOYMENT_KIND)
    return deployment[C.SPEC][C.TEMPLATE][C.SPEC][C.CONTAINERS][0][C.IMAGE]


def test_kv_conductor_image_override_and_fallback(tmp_path):
    user_config = _pd_user_config(C.HARDWARE_TYPE_800I_A3)
    user_config[C.KV_CONDUCTOR_CONFIG] = {C.KV_CONDUCTOR_PORT: 13333}

    assert _kv_conductor_image(tmp_path, "kv_conductor_default.yaml", user_config) == "mindie:default"

    user_config[C.KV_CONDUCTOR_CONFIG][C.IMAGE_NAME] = "mindie:kv-conductor"
    assert _kv_conductor_image(tmp_path, "kv_conductor_override.yaml", user_config) == "mindie:kv-conductor"


def test_infer_service_set_kv_conductor_image_override(tmp_path, monkeypatch):
    monkeypatch.setattr(k8s_utils, "g_kv_conductor_enabled", True)
    user_config = _pd_user_config(C.HARDWARE_TYPE_800I_A3)
    user_config[C.KV_CONDUCTOR_CONFIG] = {
        C.KV_CONDUCTOR_PORT: 13333,
        C.IMAGE_NAME: "mindie:kv-conductor",
    }
    output = tmp_path / "infer_service.yaml"
    k8s_utils.g_generate_yaml_list = []
    generate_yaml_infer_service_set(
        str(DEPLOYER_ROOT / "yaml_template" / "infer_service_template.yaml"),
        str(output),
        user_config,
    )
    infer_doc = _find_infer_service_set_doc(load_yaml(str(output), False))
    role = get_infer_role(infer_doc, C.ROLE_KV_CONDUCTOR)
    assert role is not None
    assert role[C.SPEC][C.TEMPLATE][C.SPEC][C.CONTAINERS][0][C.IMAGE] == "mindie:kv-conductor"
