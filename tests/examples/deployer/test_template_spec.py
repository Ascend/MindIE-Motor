# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You can obtain a copy of this software according to the terms and conditions of the Mulan PSL v2:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from pathlib import Path

import lib.constant as C
import pytest
from lib.config_validator import validate_template_spec
from lib.generator.controller import generate_yaml_controller
from lib.generator.coordinator import generate_yaml_coordinator
from lib.generator.engine import generate_yaml_engine
from lib.generator.infer_service import (
    _find_infer_service_set_doc,
    generate_yaml_infer_service_set,
    get_infer_role,
)
from lib.generator.k8s_utils import extract_resources
from lib.generator.kv_conductor import generate_yaml_kv_conductor
from lib.generator.mf_store import generate_yaml_mf_store
from lib.utils import apply_template_spec, deep_merge_template_spec, load_yaml

DEPLOYER_ROOT = Path(__file__).resolve().parents[3] / "examples" / "deployer"
YAML_TEMPLATE_ROOT = DEPLOYER_ROOT / "yaml_template"


def _pd_user_config(*, extra_prefill=None, extra_decode=None, extra_controller=None, extra_coordinator=None):
    deploy = {
        C.CONFIG_JOB_ID: "pd-ns",
        C.IMAGE_NAME: "mindie:default",
        C.HARDWARE_TYPE: C.HARDWARE_TYPE_800I_A3,
        C.P_INSTANCES_NUM: 1,
        C.D_INSTANCES_NUM: 1,
        C.SINGER_P_INSTANCES_NUM: 1,
        C.SINGER_D_INSTANCES_NUM: 1,
        C.P_POD_NPU_NUM: 4,
        C.D_POD_NPU_NUM: 4,
    }
    return {
        C.MOTOR_DEPLOY_CONFIG: deploy,
        C.MOTOR_CONTROLLER_CONFIG: extra_controller or {},
        C.MOTOR_COORDINATOR_CONFIG: extra_coordinator or {},
        C.MOTOR_ENGINE_PREFILL_CONFIG: extra_prefill or {},
        C.MOTOR_ENGINE_DECODE_CONFIG: extra_decode or {},
    }


def _deployment_doc(output_file):
    data = load_yaml(str(output_file), False)
    deployment_data, _, _ = extract_resources(data)
    return deployment_data


def test_deep_merge_template_spec_merges_dicts_and_replaces_scalar_lists():
    target = {
        "metadata": {"labels": {"a": "1", "b": "2"}, "name": "keep"},
        "spec": {"replicas": 1, "command": ["/bin/bash", "-c", "old.sh"]},
    }
    overrides = {
        "metadata": {"labels": {"b": "overridden", "c": "3"}},
        "spec": {"command": ["/bin/bash", "-c", "new.sh"]},
    }

    deep_merge_template_spec(target, overrides)

    assert target == {
        "metadata": {"labels": {"a": "1", "b": "overridden", "c": "3"}, "name": "keep"},
        "spec": {"replicas": 1, "command": ["/bin/bash", "-c", "new.sh"]},
    }


def test_deep_merge_template_spec_merges_dict_lists_by_index():
    """对象列表（如 containers）按索引递归合并：字段级覆盖，未提到的字段保留。"""
    target = {
        "spec": {
            "containers": [
                {"name": "c0", "resources": {"requests": {"memory": "64Gi", "cpu": "16"}}},
                {"name": "c1", "image": "img:1"},
            ]
        }
    }
    overrides = {
        "spec": {
            "containers": [
                {"resources": {"requests": {"memory": "1234Gi"}}},
                {"image": "img:patched"},
                {"name": "c2", "image": "img:2"},
            ]
        }
    }

    deep_merge_template_spec(target, overrides)

    assert target["spec"]["containers"] == [
        # 字段级覆盖：memory 被覆盖，cpu 保留
        {"name": "c0", "resources": {"requests": {"memory": "1234Gi", "cpu": "16"}}},
        {"name": "c1", "image": "img:patched"},
        # 覆盖列表更长时，多余元素追加
        {"name": "c2", "image": "img:2"},
    ]


def test_deep_merge_template_spec_replaces_mixed_or_empty_lists():
    target = {
        "spec": {"ports": [{"port": 80}], "args": ["a", "b"], "empty": [], "mixed": [{"x": 1}, "s"]},
        "metadata": {"finalizers": ["f1"]},
    }
    overrides = {
        "spec": {"ports": [{"port": 443}], "args": ["c"], "empty": [{"y": 2}], "mixed": [{"x": 2}, "t"]},
        "metadata": {"finalizers": []},
    }

    deep_merge_template_spec(target, overrides)

    assert target == {
        # 目标是对象列表 → 索引合并
        "spec": {"ports": [{"port": 443}], "args": ["c"], "empty": [{"y": 2}], "mixed": [{"x": 2}, "t"]},
        # 空列表整体替换
        "metadata": {"finalizers": []},
    }


def test_apply_template_spec_ignores_missing_or_invalid_role_config():
    workload = {"metadata": {"labels": {"app": "x"}}, "spec": {}}

    apply_template_spec(workload, {})
    apply_template_spec(workload, None)
    apply_template_spec(None, {C.TEMPLATE_SPEC: {}})

    assert workload == {"metadata": {"labels": {"app": "x"}}, "spec": {}}


def test_apply_template_spec_rejects_non_dict_spec():
    with pytest.raises(ValueError, match="templateSpec must be a JSON object"):
        apply_template_spec({"spec": {}}, {C.TEMPLATE_SPEC: ["not", "a", "dict"]})


def test_validate_template_spec_accepts_dict_and_rejects_other_types():
    validate_template_spec({C.MOTOR_CONTROLLER_CONFIG: {C.TEMPLATE_SPEC: {"spec": {}}}})
    validate_template_spec({C.MOTOR_CONTROLLER_CONFIG: {}})

    with pytest.raises(ValueError, match="templateSpec must be a JSON object"):
        validate_template_spec({C.MOTOR_CONTROLLER_CONFIG: {C.TEMPLATE_SPEC: "deployment"}})


def test_controller_template_spec_overrides_template_but_not_scattered_fields(tmp_path):
    template_spec = {
        C.METADATA: {C.LABELS: {"controller-custom": "yes"}},
        C.SPEC: {
            C.TEMPLATE: {
                C.SPEC: {
                    "terminationGracePeriodSeconds": 99,
                    C.CONTAINERS: [{C.IMAGE: "template-spec-image"}],
                }
            }
        },
    }
    user_config = _pd_user_config(extra_controller={C.TEMPLATE_SPEC: template_spec, C.IMAGE_NAME: "mindie:controller"})

    generate_yaml_controller(
        str(YAML_TEMPLATE_ROOT / "controller_template.yaml"),
        str(tmp_path / "controller.yaml"),
        user_config,
    )

    deployment = _deployment_doc(tmp_path / "controller.yaml")
    assert deployment[C.METADATA][C.LABELS]["controller-custom"] == "yes"
    pod_spec = deployment[C.SPEC][C.TEMPLATE][C.SPEC]
    assert pod_spec["terminationGracePeriodSeconds"] == 99
    # 散字段优先级高于 templateSpec：image_name 覆盖 templateSpec 中的 image
    assert pod_spec[C.CONTAINERS][0][C.IMAGE] == "mindie:controller"
    # 未覆盖的模板字段保留
    assert deployment[C.METADATA][C.LABELS]["app"] == "mindie-motor-controller"


def test_coordinator_template_spec_applied(tmp_path):
    template_spec = {C.SPEC: {C.TEMPLATE: {C.SPEC: {"terminationGracePeriodSeconds": 42}}}}
    user_config = _pd_user_config(extra_coordinator={C.TEMPLATE_SPEC: template_spec})

    generate_yaml_coordinator(
        str(YAML_TEMPLATE_ROOT / "coordinator_template.yaml"),
        str(tmp_path / "coordinator.yaml"),
        user_config,
    )

    deployment = _deployment_doc(tmp_path / "coordinator.yaml")
    pod_spec = deployment[C.SPEC][C.TEMPLATE][C.SPEC]
    assert pod_spec["terminationGracePeriodSeconds"] == 42
    assert pod_spec[C.CONTAINERS][0][C.IMAGE] == "mindie:default"


def test_engine_template_spec_applied_per_role(tmp_path):
    prefill_spec = {C.SPEC: {C.TEMPLATE: {C.SPEC: {"terminationGracePeriodSeconds": 7}}}}
    decode_spec = {C.SPEC: {C.TEMPLATE: {C.SPEC: {"terminationGracePeriodSeconds": 8}}}}
    user_config = _pd_user_config(
        extra_prefill={C.TEMPLATE_SPEC: prefill_spec},
        extra_decode={C.TEMPLATE_SPEC: decode_spec},
    )

    generate_yaml_engine(
        str(YAML_TEMPLATE_ROOT / "engine_template.yaml"),
        str(tmp_path / "mindie_server"),
        user_config,
    )

    prefill = load_yaml(str(tmp_path / "mindie_server_p0.yaml"), True)
    decode = load_yaml(str(tmp_path / "mindie_server_d0.yaml"), True)
    assert prefill[C.SPEC][C.TEMPLATE][C.SPEC]["terminationGracePeriodSeconds"] == 7
    assert decode[C.SPEC][C.TEMPLATE][C.SPEC]["terminationGracePeriodSeconds"] == 8


def test_template_spec_snake_case_key_accepted(tmp_path):
    """user_config 命名约定为 snake_case：template_spec 与 templateSpec 均可，snake 优先。"""
    snake_spec = {C.SPEC: {C.TEMPLATE: {C.SPEC: {"terminationGracePeriodSeconds": 11}}}}
    camel_spec = {C.SPEC: {C.TEMPLATE: {C.SPEC: {"terminationGracePeriodSeconds": 22}}}}
    user_config = _pd_user_config(extra_controller={C.TEMPLATE_SPEC_SNAKE: snake_spec})
    user_config[C.MOTOR_COORDINATOR_CONFIG] = {C.TEMPLATE_SPEC: camel_spec}

    generate_yaml_controller(
        str(YAML_TEMPLATE_ROOT / "controller_template.yaml"),
        str(tmp_path / "controller.yaml"),
        user_config,
    )
    generate_yaml_coordinator(
        str(YAML_TEMPLATE_ROOT / "coordinator_template.yaml"),
        str(tmp_path / "coordinator.yaml"),
        user_config,
    )

    controller = _deployment_doc(tmp_path / "controller.yaml")
    coordinator = _deployment_doc(tmp_path / "coordinator.yaml")
    assert controller[C.SPEC][C.TEMPLATE][C.SPEC]["terminationGracePeriodSeconds"] == 11
    assert coordinator[C.SPEC][C.TEMPLATE][C.SPEC]["terminationGracePeriodSeconds"] == 22


def test_template_spec_snake_case_wins_when_both_present(tmp_path):
    snake_spec = {C.METADATA: {C.LABELS: {"which-key": "snake"}}}
    camel_spec = {C.METADATA: {C.LABELS: {"which-key": "camel"}}}
    user_config = _pd_user_config(extra_controller={C.TEMPLATE_SPEC_SNAKE: snake_spec, C.TEMPLATE_SPEC: camel_spec})

    generate_yaml_controller(
        str(YAML_TEMPLATE_ROOT / "controller_template.yaml"),
        str(tmp_path / "controller.yaml"),
        user_config,
    )

    controller = _deployment_doc(tmp_path / "controller.yaml")
    assert controller[C.METADATA][C.LABELS]["which-key"] == "snake"


def test_validate_template_spec_rejects_non_dict_snake_key():
    with pytest.raises(ValueError, match="template_spec"):
        validate_template_spec({C.MOTOR_CONTROLLER_CONFIG: {C.TEMPLATE_SPEC_SNAKE: "deployment"}})


def test_kv_conductor_template_spec_applied(tmp_path):
    template_spec = {C.SPEC: {C.TEMPLATE: {C.SPEC: {"terminationGracePeriodSeconds": 33}}}}
    user_config = _pd_user_config()
    user_config[C.KV_CONDUCTOR_CONFIG] = {C.TEMPLATE_SPEC_SNAKE: template_spec}

    generate_yaml_kv_conductor(
        str(YAML_TEMPLATE_ROOT / "kv_conductor_template.yaml"),
        str(tmp_path / "kv_conductor.yaml"),
        user_config,
        user_config[C.KV_CONDUCTOR_CONFIG],
    )

    deployment = _deployment_doc(tmp_path / "kv_conductor.yaml")
    assert deployment[C.SPEC][C.TEMPLATE][C.SPEC]["terminationGracePeriodSeconds"] == 33


def test_mf_store_template_spec_applied(tmp_path):
    template_spec = {C.SPEC: {C.TEMPLATE: {C.SPEC: {"terminationGracePeriodSeconds": 44}}}}
    user_config = _pd_user_config()
    user_config[C.MF_STORE_CONFIG] = {C.TEMPLATE_SPEC_SNAKE: template_spec}

    generate_yaml_mf_store(
        str(YAML_TEMPLATE_ROOT / "mf_store_template.yaml"),
        str(tmp_path / "mf_store.yaml"),
        user_config,
    )

    deployment = _deployment_doc(tmp_path / "mf_store.yaml")
    assert deployment[C.SPEC][C.TEMPLATE][C.SPEC]["terminationGracePeriodSeconds"] == 44


def test_infer_service_set_role_template_spec_applied(tmp_path, monkeypatch):
    """CRD 模式：template_spec 深度合并到 InferServiceSet 对应角色对象。"""
    from lib.generator import k8s_utils

    # 全量并行执行时其他用例可能已置位全局 kv 开关，显式重置保证测试隔离
    monkeypatch.setattr(k8s_utils, "g_kv_conductor_enabled", False)
    monkeypatch.setattr(k8s_utils, "g_kv_store_enabled", False)
    monkeypatch.setattr(k8s_utils, "g_controller_service", "ctrl.pd-ns.svc.cluster.local")
    monkeypatch.setattr(k8s_utils, "g_coordinator_service", "coord.pd-ns.svc.cluster.local")
    coordinator_spec = {C.SPEC: {C.TEMPLATE: {C.SPEC: {"terminationGracePeriodSeconds": 55}}}}
    prefill_spec = {C.SPEC: {C.TEMPLATE: {C.SPEC: {"terminationGracePeriodSeconds": 66}}}}
    user_config = _pd_user_config(
        extra_coordinator={C.TEMPLATE_SPEC_SNAKE: coordinator_spec},
        extra_prefill={C.TEMPLATE_SPEC_SNAKE: prefill_spec},
    )
    # InferServiceSet 模式要求 engine_config / model_config
    for key in (C.MOTOR_ENGINE_PREFILL_CONFIG, C.MOTOR_ENGINE_DECODE_CONFIG):
        user_config[key][C.ENGINE_TYPE] = C.ENGINE_TYPE_VLLM
        user_config[key]["model_config"] = {"model_name": "m", "model_path": "/mnt/weight/m"}

    generate_yaml_infer_service_set(
        str(YAML_TEMPLATE_ROOT / "infer_service_template.yaml"),
        str(tmp_path / "infer_service.yaml"),
        user_config,
    )

    infer_doc = _find_infer_service_set_doc(load_yaml(str(tmp_path / "infer_service.yaml"), False))
    coordinator_role = get_infer_role(infer_doc, C.COORDINATOR)
    prefill_role = get_infer_role(infer_doc, C.ROLE_PREFILL)
    assert coordinator_role[C.SPEC][C.TEMPLATE][C.SPEC]["terminationGracePeriodSeconds"] == 55
    assert prefill_role[C.SPEC][C.TEMPLATE][C.SPEC]["terminationGracePeriodSeconds"] == 66
