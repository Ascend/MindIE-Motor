# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import json
import sys
from pathlib import Path

import pytest

DEPLOYER_ROOT = Path(__file__).resolve().parents[3] / "examples" / "deployer"
sys.path.insert(0, str(DEPLOYER_ROOT))

import lib.constant as C  # noqa: E402
from lib.generator import k8s_utils  # noqa: E402


@pytest.fixture(autouse=True)
def reset_engine_base_name(monkeypatch):
    monkeypatch.setattr(k8s_utils, "g_engine_base_name", "mindie-server")


def _resource_list_json(*items):
    return json.dumps({"items": items})


def _pod(name, phase):
    return {"metadata": {"name": name}, "status": {"phase": phase}}


def _deployment(name):
    return {"metadata": {"name": name}}


def _mock_kubectl_command(monkeypatch, pods_payload="", deployments_payload="", returncode=0, stderr=""):
    def fake_run_cmd_get_output(args, timeout=60):
        if returncode != 0:
            raise RuntimeError(f"Command failed (exit {returncode}): {stderr or pods_payload or deployments_payload}")
        resource = next((arg for arg in args if arg in ("pods", "deployments")), None)
        if resource == "pods":
            return pods_payload.strip()
        if resource == "deployments":
            return deployments_payload.strip()
        raise AssertionError(f"Unexpected kubectl resource in args: {args}")

    monkeypatch.setattr(k8s_utils, "_get_kubectl_path", lambda: "/usr/bin/kubectl")
    monkeypatch.setattr(k8s_utils, "run_cmd_get_output", fake_run_cmd_get_output)


def _deployments_for_indices(node_type, indices):
    return _resource_list_json(*(_deployment(f"mindie-server-{node_type}{idx}") for idx in indices))


def test_compute_scale_in_delete_indices_without_pending_uses_high_index():
    assert k8s_utils.compute_scale_in_delete_indices({0, 1, 4}, 2, set(), 1) == [4]
    assert k8s_utils.compute_scale_in_delete_indices({0, 1}, 1, set(), 1) == [1]


def test_compute_scale_in_delete_indices_sparse_scale_in_chain():
    pending = {1, 2, 3}
    assert k8s_utils.compute_scale_in_delete_indices({0, 1, 2, 3, 4}, 2, pending, 3) == [3, 2, 1]


def test_compute_scale_in_delete_indices_prioritizes_pending_over_in_range():
    pending = {0}
    assert k8s_utils.compute_scale_in_delete_indices({0, 1}, 1, pending, 1) == [0]


def test_compute_scale_out_add_indices_fills_lowest_missing():
    assert k8s_utils.compute_scale_out_add_indices({0, 4}, 3, 1) == [1]
    assert k8s_utils.compute_scale_out_add_indices({1}, 2, 1) == [0]


def test_get_instance_scale_in_delete_order_prioritizes_pending_p(monkeypatch):
    pods_payload = _resource_list_json(
        _pod("mindie-server-p0-abc123", "Pending"),
        _pod("mindie-server-p1-def456", "Running"),
    )
    deployments_payload = _deployments_for_indices(C.NODE_TYPE_P, [0, 1])

    _mock_kubectl_command(monkeypatch, pods_payload=pods_payload, deployments_payload=deployments_payload)

    assert k8s_utils.get_instance_scale_in_delete_order("pd-separate", C.NODE_TYPE_P, 2, 1) == [0]


def test_get_instance_scale_in_delete_order_without_pending_uses_high_index(monkeypatch):
    pods_payload = _resource_list_json(
        _pod("mindie-server-p0-abc123", "Running"),
        _pod("mindie-server-p1-def456", "Running"),
    )
    deployments_payload = _deployments_for_indices(C.NODE_TYPE_P, [0, 1])

    _mock_kubectl_command(monkeypatch, pods_payload=pods_payload, deployments_payload=deployments_payload)

    assert k8s_utils.get_instance_scale_in_delete_order("pd-separate", C.NODE_TYPE_P, 2, 1) == [1]


def test_get_instance_scale_in_delete_order_fills_from_high_index(monkeypatch):
    pods_payload = _resource_list_json(
        _pod("mindie-server-p0-abc123", "Pending"),
        _pod("mindie-server-p1-def456", "Running"),
        _pod("mindie-server-p2-ghi789", "Running"),
    )
    deployments_payload = _deployments_for_indices(C.NODE_TYPE_P, [0, 1, 2])

    _mock_kubectl_command(monkeypatch, pods_payload=pods_payload, deployments_payload=deployments_payload)

    assert k8s_utils.get_instance_scale_in_delete_order("pd-separate", C.NODE_TYPE_P, 3, 1) == [0, 2]


def test_get_instance_scale_in_delete_order_deletes_orphan_on_sparse_scale_in(monkeypatch):
    pods_payload = _resource_list_json(
        _pod("mindie-server-p0-abc123", "Running"),
        _pod("mindie-server-p1-def456", "Running"),
        _pod("mindie-server-p4-ghi789", "Running"),
    )
    deployments_payload = _deployments_for_indices(C.NODE_TYPE_P, [0, 1, 4])

    _mock_kubectl_command(monkeypatch, pods_payload=pods_payload, deployments_payload=deployments_payload)

    assert k8s_utils.get_instance_scale_in_delete_order("pd-separate", C.NODE_TYPE_P, 3, 2) == [4]


def test_get_instance_scale_in_delete_order_prioritizes_pending_d(monkeypatch):
    pods_payload = _resource_list_json(
        _pod("mindie-server-d0-abc123", "Pending"),
        _pod("mindie-server-d1-def456", "Running"),
    )
    deployments_payload = _deployments_for_indices(C.NODE_TYPE_D, [0, 1])

    _mock_kubectl_command(monkeypatch, pods_payload=pods_payload, deployments_payload=deployments_payload)

    assert k8s_utils.get_instance_scale_in_delete_order("pd-separate", C.NODE_TYPE_D, 2, 1) == [0]


def test_get_instance_scale_in_delete_order_prioritizes_pending_u(monkeypatch):
    pods_payload = _resource_list_json(
        _pod("mindie-server-u0-abc123", "Pending"),
        _pod("mindie-server-u1-def456", "Running"),
    )
    deployments_payload = _deployments_for_indices(C.NODE_TYPE_U, [0, 1])

    _mock_kubectl_command(monkeypatch, pods_payload=pods_payload, deployments_payload=deployments_payload)

    assert k8s_utils.get_instance_scale_in_delete_order("pd-hybrid", C.NODE_TYPE_U, 2, 1) == [0]


def test_get_instance_scale_in_delete_order_kubectl_failure_uses_high_index(monkeypatch):
    _mock_kubectl_command(monkeypatch, returncode=1, stderr="boom")

    assert k8s_utils.get_instance_scale_in_delete_order("pd-separate", C.NODE_TYPE_P, 2, 1) == [1]


@pytest.mark.parametrize("node_type", [C.NODE_TYPE_P, C.NODE_TYPE_D, C.NODE_TYPE_U])
def test_get_existing_engine_instance_indices(monkeypatch, node_type):
    deployments_payload = _resource_list_json(
        _deployment(f"mindie-server-{node_type}1"),
        _deployment("mindie-server-controller"),
    )

    _mock_kubectl_command(monkeypatch, deployments_payload=deployments_payload)

    assert k8s_utils.get_existing_engine_instance_indices("pd-separate", node_type) == {1}


@pytest.mark.parametrize(
    ("existing_indices", "base", "total", "expected"),
    [
        ({1}, 1, 2, [0]),
        ({0}, 1, 2, [1]),
        ({0, 4}, 2, 3, [1]),
    ],
)
def test_get_instance_scale_out_indices_fills_missing_index(monkeypatch, existing_indices, base, total, expected):
    monkeypatch.setattr(
        k8s_utils,
        "get_existing_engine_instance_indices",
        lambda job_id, node_type: existing_indices,
    )

    assert k8s_utils.get_instance_scale_out_indices("pd-separate", C.NODE_TYPE_P, base, total) == expected


def test_get_instance_scale_out_indices_query_failure_uses_count_based_order(monkeypatch):
    monkeypatch.setattr(
        k8s_utils,
        "get_existing_engine_instance_indices",
        lambda job_id, node_type: None,
    )

    assert k8s_utils.get_instance_scale_out_indices("pd-separate", C.NODE_TYPE_P, 1, 2) == [1]


@pytest.mark.parametrize(
    ("node_type", "deploy_config", "baseline_deploy_config"),
    [
        (
            C.NODE_TYPE_P,
            {
                C.CONFIG_JOB_ID: "pd-separate",
                C.P_INSTANCES_NUM: 1,
                C.D_INSTANCES_NUM: 1,
            },
            {
                C.CONFIG_JOB_ID: "pd-separate",
                C.P_INSTANCES_NUM: 2,
                C.D_INSTANCES_NUM: 1,
            },
        ),
        (
            C.NODE_TYPE_D,
            {
                C.CONFIG_JOB_ID: "pd-separate",
                C.P_INSTANCES_NUM: 1,
                C.D_INSTANCES_NUM: 1,
            },
            {
                C.CONFIG_JOB_ID: "pd-separate",
                C.P_INSTANCES_NUM: 1,
                C.D_INSTANCES_NUM: 2,
            },
        ),
        (
            C.NODE_TYPE_U,
            {
                C.CONFIG_JOB_ID: "pd-hybrid",
                C.HYBRID_INSTANCES_NUM: 1,
            },
            {
                C.CONFIG_JOB_ID: "pd-hybrid",
                C.HYBRID_INSTANCES_NUM: 2,
            },
        ),
    ],
)
def test_scale_engine_by_type_deletes_pending_instance_first(
    tmp_path, monkeypatch, node_type, deploy_config, baseline_deploy_config
):
    yaml_to_remove = tmp_path / f"mindie-server_{node_type}0.yaml"
    yaml_to_remove.write_text("kind: Deployment\n", encoding="utf-8")
    commands = []

    monkeypatch.setattr(
        k8s_utils,
        "get_instance_scale_in_delete_order",
        lambda job_id, node_type, base, total: [0],
    )
    monkeypatch.setattr(k8s_utils, "safe_exec_cmd", commands.append)
    monkeypatch.setattr(k8s_utils, "g_engine_base_name", "mindie-server")

    k8s_utils.scale_engine_by_type(deploy_config, baseline_deploy_config, str(tmp_path), node_type)

    assert commands == [
        [
            "kubectl",
            "delete",
            "deployment",
            f"mindie-server-{node_type}0",
            "-n",
            deploy_config[C.CONFIG_JOB_ID],
            "--ignore-not-found=true",
        ],
    ]
    assert not yaml_to_remove.exists()
