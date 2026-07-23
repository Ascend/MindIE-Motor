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
from lib.generator.infer_service import (
    _configure_controller_role,
    _configure_coordinator_role,
    _configure_kv_conductor_role,
    _configure_kv_store_role,
    _find_infer_service_set_doc,
    get_infer_role,
)
from lib.utils import (
    apply_coordinator_infer_node_port,
    apply_node_selector_override,
    get_coordinator_infer_node_port,
    load_yaml,
)


DEPLOYER_ROOT = Path(__file__).resolve().parents[3] / "examples" / "deployer"


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (None, 31015),
        ("", 31015),
        ("-", None),
        (" 32015 ", 32015),
        (32016, 32016),
    ],
)
def test_get_coordinator_infer_node_port(configured, expected):
    deploy_config = {}
    if configured is not None:
        deploy_config[C.COORDINATOR_INFER_NODE_PORT] = configured

    assert get_coordinator_infer_node_port(deploy_config) == expected


def test_apply_coordinator_infer_node_port_removes_fixed_port():
    service = {C.SPEC: {C.PORTS: [{C.PORT: 1025, C.NODE_PORT: 31015}]}}

    apply_coordinator_infer_node_port(service, {C.COORDINATOR_INFER_NODE_PORT: "-"})

    assert C.NODE_PORT not in service[C.SPEC][C.PORTS][0]


def test_get_coordinator_infer_node_port_rejects_invalid_value():
    with pytest.raises(ValueError, match="coordinator_infer_node_port"):
        get_coordinator_infer_node_port({C.COORDINATOR_INFER_NODE_PORT: "invalid"})


def test_apply_node_selector_override_merges_and_validates():
    pod_spec = {C.NODE_SELECTOR: {C.ACCELERATOR_TYPE: C.ACCELERATOR_TYPE_A3}}
    apply_node_selector_override(pod_spec, {C.PREFILL_NODE_SELECTOR: {"label1": "value1"}}, C.PREFILL_NODE_SELECTOR)

    assert pod_spec[C.NODE_SELECTOR] == {
        C.ACCELERATOR_TYPE: C.ACCELERATOR_TYPE_A3,
        "label1": "value1",
    }

    with pytest.raises(ValueError, match="prefill_node_selector"):
        apply_node_selector_override({}, {C.PREFILL_NODE_SELECTOR: "label1=value1"}, C.PREFILL_NODE_SELECTOR)


def test_infer_service_roles_apply_component_selectors_and_node_port(monkeypatch):
    template_path = DEPLOYER_ROOT / "yaml_template" / "infer_service_template.yaml"
    infer_doc = _find_infer_service_set_doc(load_yaml(str(template_path), False))
    selectors = {
        C.CONTROLLER_NODE_SELECTOR: {"label1": "controller"},
        C.COORDINATOR_NODE_SELECTOR: {"label1": "coordinator"},
        C.KV_POOL_NODE_SELECTOR: {"label1": "kv-pool"},
        C.KV_CONDUCTOR_NODE_SELECTOR: {"label1": "kv-conductor"},
    }
    user_config = {
        C.MOTOR_DEPLOY_CONFIG: {
            C.CONFIG_JOB_ID: "selector-test",
            C.IMAGE_NAME: "mindie:latest",
            C.COORDINATOR_INFER_NODE_PORT: "-",
            **selectors,
        },
        C.MOTOR_CONTROLLER_CONFIG: {},
        C.MOTOR_COORDINATOR_CONFIG: {},
    }
    monkeypatch.setattr(k8s_utils, "g_kv_store_enabled", False)
    monkeypatch.setattr(k8s_utils, "g_kv_conductor_enabled", False)

    _configure_controller_role(infer_doc, user_config)
    _configure_coordinator_role(infer_doc, user_config)
    _configure_kv_store_role(infer_doc, user_config)
    _configure_kv_conductor_role(infer_doc, user_config)

    for role_name, selector_key in [
        (C.CONTROLLER, C.CONTROLLER_NODE_SELECTOR),
        (C.COORDINATOR, C.COORDINATOR_NODE_SELECTOR),
        (C.ROLE_KV_STORE, C.KV_POOL_NODE_SELECTOR),
        (C.ROLE_KV_CONDUCTOR, C.KV_CONDUCTOR_NODE_SELECTOR),
    ]:
        role = get_infer_role(infer_doc, role_name)
        pod_spec = role[C.SPEC][C.TEMPLATE][C.SPEC]
        assert pod_spec[C.NODE_SELECTOR]["label1"] == selectors[selector_key]["label1"]

    coordinator = get_infer_role(infer_doc, C.COORDINATOR)
    assert C.NODE_PORT not in coordinator[C.SERVICES][0][C.SPEC][C.PORTS][0]
