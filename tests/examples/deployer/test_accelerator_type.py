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

import pytest

import lib.constant as C
from lib.generator import k8s_utils


@pytest.fixture(autouse=True)
def mock_kubectl_path(monkeypatch):
    monkeypatch.setattr(k8s_utils, "_get_kubectl_path", lambda: "kubectl")


def _nodes_json(*label_maps):
    return {
        "items": [
            {
                "metadata": {
                    "name": f"node-{index}",
                    "labels": labels,
                }
            }
            for index, labels in enumerate(label_maps)
        ]
    }


def test_get_accelerator_type_from_cluster_returns_matching_a3_label(monkeypatch):
    k8s_utils._g_accelerator_type_cache.clear()
    monkeypatch.setattr(
        k8s_utils,
        "run_cmd_get_output",
        lambda _args: json.dumps(
            _nodes_json(
                {"host-arch": "huawei-arm"},
                {C.ACCELERATOR_TYPE: C.ACCELERATOR_TYPE_A3},
                {C.ACCELERATOR_TYPE: C.ACCELERATOR_TYPE_910B},
            )
        ),
    )

    assert k8s_utils.get_accelerator_type_from_cluster(C.HARDWARE_TYPE_800I_A3) == C.ACCELERATOR_TYPE_A3


def test_get_accelerator_type_from_cluster_returns_matching_a2_label(monkeypatch):
    k8s_utils._g_accelerator_type_cache.clear()
    monkeypatch.setattr(
        k8s_utils,
        "run_cmd_get_output",
        lambda _args: json.dumps(
            _nodes_json(
                {C.ACCELERATOR_TYPE: C.ACCELERATOR_TYPE_A3},
                {C.ACCELERATOR_TYPE: C.ACCELERATOR_TYPE_910B},
            )
        ),
    )

    assert k8s_utils.get_accelerator_type_from_cluster(C.HARDWARE_TYPE_800I_A2) == C.ACCELERATOR_TYPE_910B


def test_get_accelerator_type_from_cluster_uses_hardware_type_cache_key(monkeypatch):
    k8s_utils._g_accelerator_type_cache.clear()
    call_count = {"n": 0}

    def fake_run(_args):
        call_count["n"] += 1
        return json.dumps(_nodes_json({C.ACCELERATOR_TYPE: C.ACCELERATOR_TYPE_910B}))

    monkeypatch.setattr(k8s_utils, "run_cmd_get_output", fake_run)

    assert k8s_utils.get_accelerator_type_from_cluster(C.HARDWARE_TYPE_800I_A2) == C.ACCELERATOR_TYPE_910B
    assert k8s_utils.get_accelerator_type_from_cluster(C.HARDWARE_TYPE_800I_A2) == C.ACCELERATOR_TYPE_910B
    assert call_count["n"] == 1
    assert k8s_utils._g_accelerator_type_cache[C.HARDWARE_TYPE_800I_A2] == C.ACCELERATOR_TYPE_910B


def test_get_accelerator_type_from_cluster_raises_when_label_missing(monkeypatch):
    k8s_utils._g_accelerator_type_cache.clear()
    monkeypatch.setattr(
        k8s_utils,
        "run_cmd_get_output",
        lambda _args: json.dumps(_nodes_json({})),
    )

    with pytest.raises(RuntimeError, match=C.ACCELERATOR_TYPE):
        k8s_utils.get_accelerator_type_from_cluster(C.HARDWARE_TYPE_800I_A3)


def test_get_accelerator_type_from_cluster_raises_when_no_matching_generation(monkeypatch):
    k8s_utils._g_accelerator_type_cache.clear()
    monkeypatch.setattr(
        k8s_utils,
        "run_cmd_get_output",
        lambda _args: json.dumps(_nodes_json({C.ACCELERATOR_TYPE: C.ACCELERATOR_TYPE_910B})),
    )

    with pytest.raises(RuntimeError, match=C.HARDWARE_TYPE_800I_A3):
        k8s_utils.get_accelerator_type_from_cluster(C.HARDWARE_TYPE_800I_A3)


def test_get_accelerator_type_from_cluster_a5_uses_hardware_type(monkeypatch):
    k8s_utils._g_accelerator_type_cache.clear()
    a5_type = C.HARDWARE_TYPE_950I_A5[0]
    monkeypatch.setattr(
        k8s_utils,
        "run_cmd_get_output",
        lambda _args: json.dumps(_nodes_json({C.ACCELERATOR_TYPE: a5_type})),
    )

    assert k8s_utils.get_accelerator_type_from_cluster(a5_type) == a5_type
