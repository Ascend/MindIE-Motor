import json

import pytest

import lib.constant as C
from lib.generator import k8s_utils


@pytest.fixture(autouse=True)
def mock_kubectl_path(monkeypatch):
    monkeypatch.setattr(k8s_utils, "_get_kubectl_path", lambda: "kubectl")


def test_get_accelerator_type_from_cluster_returns_first_node_label(monkeypatch):
    k8s_utils._g_accelerator_type_cache.clear()
    nodes_json = {
        "items": [
            {"metadata": {"name": "node-0", "labels": {"host-arch": "huawei-arm"}}},
            {
                "metadata": {
                    "name": "node-1",
                    "labels": {C.ACCELERATOR_TYPE: C.ACCELERATOR_TYPE_A3},
                }
            },
            {
                "metadata": {
                    "name": "node-2",
                    "labels": {C.ACCELERATOR_TYPE: C.ACCELERATOR_TYPE_910B},
                }
            },
        ]
    }
    monkeypatch.setattr(k8s_utils, "run_cmd_get_output", lambda _args: json.dumps(nodes_json))

    assert k8s_utils.get_accelerator_type_from_cluster() == C.ACCELERATOR_TYPE_A3


def test_get_accelerator_type_from_cluster_uses_accelerator_type_cache_key(monkeypatch):
    k8s_utils._g_accelerator_type_cache.clear()
    call_count = {"n": 0}

    def fake_run(_args):
        call_count["n"] += 1
        return json.dumps(
            {
                "items": [
                    {
                        "metadata": {
                            "name": "node-0",
                            "labels": {C.ACCELERATOR_TYPE: C.ACCELERATOR_TYPE_910B},
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr(k8s_utils, "run_cmd_get_output", fake_run)

    assert k8s_utils.get_accelerator_type_from_cluster() == C.ACCELERATOR_TYPE_910B
    assert k8s_utils.get_accelerator_type_from_cluster() == C.ACCELERATOR_TYPE_910B
    assert call_count["n"] == 1
    assert k8s_utils._g_accelerator_type_cache[C.ACCELERATOR_TYPE] == C.ACCELERATOR_TYPE_910B


def test_get_accelerator_type_from_cluster_raises_when_label_missing(monkeypatch):
    k8s_utils._g_accelerator_type_cache.clear()
    monkeypatch.setattr(
        k8s_utils,
        "run_cmd_get_output",
        lambda _args: json.dumps({"items": [{"metadata": {"name": "node-0", "labels": {}}}]}),
    )

    with pytest.raises(RuntimeError, match=C.ACCELERATOR_TYPE):
        k8s_utils.get_accelerator_type_from_cluster()
