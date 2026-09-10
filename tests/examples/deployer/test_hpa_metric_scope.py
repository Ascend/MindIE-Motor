# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import copy
import sys
from pathlib import Path

import pytest
import yaml

DEPLOYER_ROOT = Path(__file__).resolve().parents[3] / "examples" / "deployer"
sys.path.insert(0, str(DEPLOYER_ROOT))

import lib.constant as C  # noqa: E402
from lib.generator import k8s_utils  # noqa: E402
from lib.generator.infer_service import (  # noqa: E402
    generate_yaml_infer_service_set,
    update_infer_service_replicas_only,
    get_infer_role,
    _find_infer_service_set_doc,
)
from lib.utils import load_yaml  # noqa: E402


def make_pd_separation_user_config():
    return {
        C.MOTOR_DEPLOY_CONFIG: {
            C.CONFIG_JOB_ID: "pd-separate",
            C.IMAGE_NAME: "mindie:latest",
            C.HARDWARE_TYPE: C.HARDWARE_TYPE_800I_A3,
            C.P_INSTANCES_NUM: 1,
            C.D_INSTANCES_NUM: 1,
            C.SINGER_P_INSTANCES_NUM: 1,
            C.SINGER_D_INSTANCES_NUM: 1,
            C.P_POD_NPU_NUM: 4,
            C.D_POD_NPU_NUM: 4,
        },
        C.MOTOR_ENGINE_PREFILL_CONFIG: {},
        "motor_engine_decode_config": {},
    }


def make_pd_hybrid_user_config():
    return {
        C.MOTOR_DEPLOY_CONFIG: {
            C.CONFIG_JOB_ID: "pd-hybrid",
            C.IMAGE_NAME: "mindie:latest",
            C.HARDWARE_TYPE: C.HARDWARE_TYPE_800I_A3,
            C.HYBRID_INSTANCES_NUM: 1,
            C.SINGLE_HYBRID_INSTANCE_POD_NUM: 1,
            C.HYBRID_POD_NPU_NUM: 4,
        },
        "motor_coordinator_config": {},
        C.MOTOR_ENGINE_UNION_CONFIG: {
            C.ENGINE_TYPE: C.ENGINE_TYPE_VLLM,
            "model_config": {
                "model_name": "qwen3-8B",
                "model_path": "/mnt/weight/qwen3_8B",
                "npu_mem_utils": 0.9,
                "parallel_config": {"dp_size": 2, "tp_size": 2, "pp_size": 1},
            },
            C.ENGINE_CONFIG: {"max_model_len": 2048},
        },
    }


def make_deploy_paths(tmp_path):
    return {
        "infer_service_input_yaml": str(DEPLOYER_ROOT / "yaml_template" / "infer_service_template.yaml"),
        "infer_service_output_yaml": str(tmp_path / "infer_service.yaml"),
    }


def _make_external_metric(with_selector=True, labels=None):
    metric = {
        "type": "External",
        "external": {
            "metric": {"name": "vllm_total_requests"},
            "target": {"type": "AverageValue", "averageValue": "5"},
        },
    }
    if with_selector:
        metric["external"]["metric"]["selector"] = {"matchLabels": labels or {}}
    return metric


def _make_policy(policy_type="HPA", metrics=None):
    return {
        "type": policy_type,
        "spec": {
            "minReplicas": 1,
            "maxReplicas": 4,
            "metrics": metrics if metrics is not None else [],
        },
    }


@pytest.mark.parametrize("policy_type", ["HPA", "Custom"])
def test_generate_yaml_infer_service_set_scopes_external_metrics_to_job(tmp_path, monkeypatch, policy_type):
    """Namespace changes must reach every opted-in metric without changing other selectors or policies."""
    user_config = make_pd_separation_user_config()
    paths = make_deploy_paths(tmp_path)
    docs = load_yaml(paths["infer_service_input_yaml"], False)
    infer_doc = _find_infer_service_set_doc(docs)
    labels = {"kubernetes_namespace": "old-job", "infer_huawei_com_inferservice_name": "stale-0"}
    external = _make_external_metric(with_selector=True, labels=labels)
    unscoped = copy.deepcopy(external)
    del unscoped["external"]["metric"]["selector"]
    other_labels = copy.deepcopy(external)
    del other_labels["external"]["metric"]["selector"]["matchLabels"]["kubernetes_namespace"]
    policy = _make_policy(
        policy_type=policy_type,
        metrics=[
            external,
            copy.deepcopy(external),
            unscoped,
            other_labels,
            {"type": "Resource", "resource": {"name": "cpu"}},
        ],
    )
    for role_name in (C.ROLE_PREFILL, C.ROLE_DECODE):
        get_infer_role(infer_doc, role_name)["scalingPolicy"] = copy.deepcopy(policy)
    input_yaml = tmp_path / "scaling_template.yaml"
    input_yaml.write_text(yaml.safe_dump_all(docs), encoding="utf-8")
    monkeypatch.setattr(k8s_utils, "g_generate_yaml_list", [])

    generate_yaml_infer_service_set(str(input_yaml), paths["infer_service_output_yaml"], user_config)

    output = _find_infer_service_set_doc(load_yaml(paths["infer_service_output_yaml"], False))
    expected = copy.deepcopy(policy)
    namespace = user_config[C.MOTOR_DEPLOY_CONFIG][C.CONFIG_JOB_ID]
    if policy_type == "HPA":
        for metric in expected["spec"]["metrics"][:2]:
            labels = metric["external"]["metric"]["selector"]["matchLabels"]
            labels["kubernetes_namespace"] = namespace
            labels["infer_huawei_com_inferservice_name"] = "vllm-0"
        expected["spec"]["metrics"][3]["external"]["metric"]["selector"]["matchLabels"] = {
            "infer_huawei_com_inferservice_name": "vllm-0"
        }
    assert output[C.METADATA][C.NAMESPACE] == namespace
    for role_name in (C.ROLE_PREFILL, C.ROLE_DECODE):
        assert get_infer_role(output, role_name)["scalingPolicy"] == expected
    assert "scalingPolicy" not in get_infer_role(output, C.ROLE_UNION)


def test_generate_yaml_infer_service_set_scopes_union_role_for_hybrid(tmp_path, monkeypatch):
    """Hybrid deploy: union role scalingPolicy gets scoped, and spec.metrics missing must not break."""
    user_config = make_pd_hybrid_user_config()
    paths = make_deploy_paths(tmp_path)
    docs = load_yaml(paths["infer_service_input_yaml"], False)
    infer_doc = _find_infer_service_set_doc(docs)
    labels = {"kubernetes_namespace": "old-job", "infer_huawei_com_inferservice_name": "stale-0"}
    external = _make_external_metric(with_selector=True, labels=labels)
    policy = _make_policy(policy_type="HPA", metrics=[external])
    # union role gets the policy; prefill/decode get a policy without spec.metrics
    get_infer_role(infer_doc, C.ROLE_UNION)["scalingPolicy"] = copy.deepcopy(policy)
    no_metrics_policy = {"type": "HPA", "spec": {"minReplicas": 1, "maxReplicas": 4}}
    get_infer_role(infer_doc, C.ROLE_PREFILL)["scalingPolicy"] = copy.deepcopy(no_metrics_policy)
    get_infer_role(infer_doc, C.ROLE_DECODE)["scalingPolicy"] = copy.deepcopy(no_metrics_policy)
    input_yaml = tmp_path / "scaling_template.yaml"
    input_yaml.write_text(yaml.safe_dump_all(docs), encoding="utf-8")
    monkeypatch.setattr(k8s_utils, "g_generate_yaml_list", [])

    generate_yaml_infer_service_set(str(input_yaml), paths["infer_service_output_yaml"], user_config)

    output = _find_infer_service_set_doc(load_yaml(paths["infer_service_output_yaml"], False))
    namespace = user_config[C.MOTOR_DEPLOY_CONFIG][C.CONFIG_JOB_ID]
    union_policy = get_infer_role(output, C.ROLE_UNION)["scalingPolicy"]
    union_labels = union_policy["spec"]["metrics"][0]["external"]["metric"]["selector"]["matchLabels"]
    assert union_labels["kubernetes_namespace"] == namespace
    assert union_labels["infer_huawei_com_inferservice_name"] == "vllm-0"
    # spec.metrics missing must be safely skipped
    assert get_infer_role(output, C.ROLE_PREFILL)["scalingPolicy"] == no_metrics_policy
    assert get_infer_role(output, C.ROLE_DECODE)["scalingPolicy"] == no_metrics_policy


def test_set_scaling_policy_scope_tolerates_null_external_and_null_metric(tmp_path, monkeypatch):
    """external: null / metric: null in template must not raise AttributeError."""
    user_config = make_pd_separation_user_config()
    paths = make_deploy_paths(tmp_path)
    docs = load_yaml(paths["infer_service_input_yaml"], False)
    infer_doc = _find_infer_service_set_doc(docs)
    policy = _make_policy(
        policy_type="HPA",
        metrics=[
            {"type": "External", "external": None},
            {"type": "External", "external": {"metric": None, "target": {"type": "AverageValue", "averageValue": "5"}}},
        ],
    )
    for role_name in (C.ROLE_PREFILL, C.ROLE_DECODE):
        get_infer_role(infer_doc, role_name)["scalingPolicy"] = copy.deepcopy(policy)
    input_yaml = tmp_path / "scaling_template.yaml"
    input_yaml.write_text(yaml.safe_dump_all(docs), encoding="utf-8")
    monkeypatch.setattr(k8s_utils, "g_generate_yaml_list", [])

    generate_yaml_infer_service_set(str(input_yaml), paths["infer_service_output_yaml"], user_config)

    output = _find_infer_service_set_doc(load_yaml(paths["infer_service_output_yaml"], False))
    for role_name in (C.ROLE_PREFILL, C.ROLE_DECODE):
        assert get_infer_role(output, role_name)["scalingPolicy"] == policy


def test_update_infer_service_replicas_only_backfills_scaling_scope(tmp_path, monkeypatch):
    """Manual scale path rewrites selector to the current job_id even if yaml was generated for an old one."""
    user_config = make_pd_hybrid_user_config()
    paths = make_deploy_paths(tmp_path)
    monkeypatch.setattr(k8s_utils, "g_generate_yaml_list", [])
    generate_yaml_infer_service_set(
        paths["infer_service_input_yaml"],
        paths["infer_service_output_yaml"],
        user_config,
    )

    # Simulate a stale selector left by an old job_id, then run manual scale with a new job_id.
    docs = load_yaml(paths["infer_service_output_yaml"], False)
    infer_doc = _find_infer_service_set_doc(docs)
    labels = {"kubernetes_namespace": "old-job", "infer_huawei_com_inferservice_name": "stale-0"}
    get_infer_role(infer_doc, C.ROLE_UNION)["scalingPolicy"] = _make_policy(
        policy_type="HPA", metrics=[_make_external_metric(with_selector=True, labels=labels)]
    )
    Path(paths["infer_service_output_yaml"]).write_text(yaml.safe_dump_all(docs), encoding="utf-8")

    deploy_config = copy.deepcopy(user_config[C.MOTOR_DEPLOY_CONFIG])
    deploy_config[C.CONFIG_JOB_ID] = "new-job"
    deploy_config[C.HYBRID_INSTANCES_NUM] = 2
    update_infer_service_replicas_only(paths["infer_service_output_yaml"], deploy_config, user_config)

    output = _find_infer_service_set_doc(load_yaml(paths["infer_service_output_yaml"], False))
    union_policy = get_infer_role(output, C.ROLE_UNION)["scalingPolicy"]
    union_labels = union_policy["spec"]["metrics"][0]["external"]["metric"]["selector"]["matchLabels"]
    assert union_labels["kubernetes_namespace"] == "new-job"
    assert union_labels["infer_huawei_com_inferservice_name"] == "vllm-0"
    assert get_infer_role(output, C.ROLE_UNION)[C.REPLICAS] == 2
