# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import sys
from pathlib import Path

import pytest

DEPLOYER_ROOT = Path(__file__).resolve().parents[3] / "examples" / "deployer"
sys.path.insert(0, str(DEPLOYER_ROOT))

import lib.constant as C  # noqa: E402
import lib.generator.infer_service as infer_service_module  # noqa: E402
from lib.generator import k8s_utils  # noqa: E402
from lib.generator.infer_service import (  # noqa: E402
    generate_yaml_infer_service_set,
    get_infer_role,
    _find_infer_service_set_doc,
)
from lib.utils import load_yaml  # noqa: E402

INFER_SERVICE_TEMPLATE = str(DEPLOYER_ROOT / "yaml_template" / "infer_service_template.yaml")


def make_pd_separation_user_config():
    return {
        C.MOTOR_DEPLOY_CONFIG: {
            C.CONFIG_JOB_ID: "pd-separate",
            C.IMAGE_NAME: "mindie:latest",
            C.HARDWARE_TYPE: C.HARDWARE_TYPE_800I_A3,
            C.P_INSTANCES_NUM: 2,
            C.D_INSTANCES_NUM: 3,
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
            C.HYBRID_INSTANCES_NUM: 2,
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


def generate_infer_service(tmp_path, monkeypatch, user_config):
    output_yaml = str(tmp_path / "infer_service.yaml")
    k8s_utils.g_generate_yaml_list = []
    monkeypatch.setattr(k8s_utils, "g_controller_service", "ctrl.test.svc.cluster.local")
    monkeypatch.setattr(k8s_utils, "g_coordinator_service", "coord.test.svc.cluster.local")
    generate_yaml_infer_service_set(INFER_SERVICE_TEMPLATE, output_yaml, user_config)
    return output_yaml


def load_infer_doc(output_yaml):
    all_docs = load_yaml(output_yaml, False)
    return _find_infer_service_set_doc(all_docs)


def test_scaling_policy_rendered_for_pd_separation_roles(tmp_path, monkeypatch):
    user_config = make_pd_separation_user_config()
    user_config[C.SCALING_POLICY] = {
        C.ROLE_PREFILL: {
            C.SCALING_MIN_REPLICAS: 2,
            C.SCALING_MAX_REPLICAS: 6,
            C.SCALING_METRIC: "motor_prefill_utilization",
            C.SCALING_TARGET: 0.5,
        },
        # metric / target / max_replicas fall back to defaults
        C.ROLE_DECODE: {C.SCALING_MIN_REPLICAS: 1},
    }

    output_yaml = generate_infer_service(tmp_path, monkeypatch, user_config)
    infer_doc = load_infer_doc(output_yaml)

    prefill_policy = get_infer_role(infer_doc, C.ROLE_PREFILL)[C.SCALING_POLICY_FIELD]
    assert prefill_policy == {
        "type": "HPA",
        C.SPEC: {
            "minReplicas": 2,
            "maxReplicas": 6,
            "metrics": [
                {
                    "type": "External",
                    "external": {
                        "metric": {
                            C.NAME: "motor_prefill_utilization",
                            C.SELECTOR: {
                                C.MATCHLABELS: {
                                    "kubernetes_namespace": "pd-separate",
                                    "infer_huawei_com_inferservice_name": "vllm-0",
                                }
                            },
                        },
                        "target": {"type": "Value", "value": "0.5"},
                    },
                }
            ],
        },
    }

    decode_policy = get_infer_role(infer_doc, C.ROLE_DECODE)[C.SCALING_POLICY_FIELD]
    decode_spec = decode_policy[C.SPEC]
    assert decode_policy["type"] == "HPA"
    assert decode_spec["minReplicas"] == 1
    # max_replicas defaults to the role's configured instance count
    assert decode_spec["maxReplicas"] == 3
    decode_metric = decode_spec["metrics"][0]["external"]
    assert decode_metric["metric"][C.NAME] == C.DEFAULT_DECODE_SCALING_METRIC
    assert decode_metric["target"] == {"type": "Value", "value": str(C.DEFAULT_SCALING_TARGET)}
    assert decode_metric["metric"][C.SELECTOR][C.MATCHLABELS]["kubernetes_namespace"] == "pd-separate"

    # roles without a scaling_policy entry stay untouched
    assert C.SCALING_POLICY_FIELD not in get_infer_role(infer_doc, C.ROLE_UNION)


def test_scaling_policy_absent_keeps_output_byte_identical(tmp_path, monkeypatch):
    # generate_unique_id embeds a random suffix in job names; pin it so two
    # generation runs are comparable byte-for-byte.
    monkeypatch.setattr(infer_service_module, "generate_unique_id", lambda: "fixeduuid")
    without_policy = generate_infer_service(tmp_path / "without", monkeypatch, make_pd_separation_user_config())
    empty_policy_config = make_pd_separation_user_config()
    empty_policy_config[C.SCALING_POLICY] = {}
    with_empty_policy = generate_infer_service(tmp_path / "empty", monkeypatch, empty_policy_config)

    baseline = Path(without_policy).read_bytes()
    assert Path(with_empty_policy).read_bytes() == baseline
    assert b"scalingPolicy" not in baseline


def test_scaling_policy_hybrid_union(tmp_path, monkeypatch):
    user_config = make_pd_hybrid_user_config()
    user_config[C.SCALING_POLICY] = {
        C.ROLE_UNION: {
            C.SCALING_MIN_REPLICAS: 1,
            C.SCALING_MAX_REPLICAS: 4,
            C.SCALING_METRIC: "motor_decode_utilization",
            C.SCALING_TARGET: 0.8,
        },
        # zeroed prefill/decode roles are not configured by _configure_engine_role
        C.ROLE_PREFILL: {C.SCALING_MIN_REPLICAS: 1, C.SCALING_MAX_REPLICAS: 4},
    }

    output_yaml = generate_infer_service(tmp_path, monkeypatch, user_config)
    infer_doc = load_infer_doc(output_yaml)

    union_policy = get_infer_role(infer_doc, C.ROLE_UNION)[C.SCALING_POLICY_FIELD]
    assert union_policy[C.SPEC]["minReplicas"] == 1
    assert union_policy[C.SPEC]["maxReplicas"] == 4
    assert union_policy[C.SPEC]["metrics"][0]["external"]["metric"][C.NAME] == "motor_decode_utilization"
    assert union_policy[C.SPEC]["metrics"][0]["external"]["target"] == {"type": "Value", "value": "0.8"}
    assert C.SCALING_POLICY_FIELD not in get_infer_role(infer_doc, C.ROLE_PREFILL)
    assert C.SCALING_POLICY_FIELD not in get_infer_role(infer_doc, C.ROLE_DECODE)


def test_scaling_policy_union_requires_explicit_metric(tmp_path, monkeypatch):
    user_config = make_pd_hybrid_user_config()
    user_config[C.SCALING_POLICY] = {C.ROLE_UNION: {C.SCALING_MIN_REPLICAS: 1, C.SCALING_MAX_REPLICAS: 4}}

    with pytest.raises(ValueError, match=C.SCALING_METRIC):
        generate_infer_service(tmp_path, monkeypatch, user_config)


def test_scaling_policy_rejects_invalid_replicas(tmp_path, monkeypatch):
    user_config = make_pd_separation_user_config()
    user_config[C.SCALING_POLICY] = {
        C.ROLE_PREFILL: {C.SCALING_MIN_REPLICAS: 4, C.SCALING_MAX_REPLICAS: 2},
    }

    with pytest.raises(ValueError, match="min_replicas"):
        generate_infer_service(tmp_path, monkeypatch, user_config)

    user_config[C.SCALING_POLICY] = {C.ROLE_PREFILL: {C.SCALING_MIN_REPLICAS: 0}}
    with pytest.raises(ValueError, match="min_replicas"):
        generate_infer_service(tmp_path, monkeypatch, user_config)


def test_scaling_policy_rejects_non_dict_section(tmp_path, monkeypatch):
    user_config = make_pd_separation_user_config()
    user_config[C.SCALING_POLICY] = {C.ROLE_PREFILL: "motor_prefill_utilization"}

    with pytest.raises(ValueError, match="must be a dict"):
        generate_infer_service(tmp_path, monkeypatch, user_config)


def test_scaling_policy_target_type_average_value_override(tmp_path, monkeypatch):
    """Raw (non-normalized) metrics can opt back into AverageValue explicitly."""
    user_config = make_pd_separation_user_config()
    user_config[C.SCALING_POLICY] = {
        C.ROLE_PREFILL: {
            C.SCALING_METRIC: "vllm:num_requests_waiting",
            C.SCALING_TARGET: 4,
            C.SCALING_TARGET_TYPE: "AverageValue",
        },
    }

    output_yaml = generate_infer_service(tmp_path, monkeypatch, user_config)
    policy = get_infer_role(load_infer_doc(output_yaml), C.ROLE_PREFILL)[C.SCALING_POLICY_FIELD]
    target = policy[C.SPEC]["metrics"][0]["external"]["target"]
    assert target == {"type": "AverageValue", "averageValue": "4.0"}


def test_scaling_policy_rejects_unknown_target_type(tmp_path, monkeypatch):
    user_config = make_pd_separation_user_config()
    user_config[C.SCALING_POLICY] = {C.ROLE_PREFILL: {C.SCALING_TARGET_TYPE: "bogus"}}

    with pytest.raises(ValueError, match=C.SCALING_TARGET_TYPE):
        generate_infer_service(tmp_path, monkeypatch, user_config)
