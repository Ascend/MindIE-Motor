# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from lib.nodeport_allocator import (
    ClusterNodePort,
    PlannedNodePort,
    _purpose_for,
    allocate_free_ports,
    collect_cluster_nodeports,
    collect_planned_nodeports,
    find_conflicts,
    rewrite_yaml_nodeports,
    resolve_and_rewrite_nodeports,
    write_nodeport_conflict_files,
)
from lib.generator import k8s_utils
from lib.utils import is_observability_service_name
import lib.constant as C


def _write_svc_yaml(path: Path, name: str, namespace: str, node_port: int, port: int = 1025) -> None:
    path.write_text(
        f"""apiVersion: v1
kind: Service
metadata:
  name: {name}
  namespace: {namespace}
spec:
  type: NodePort
  ports:
  - nodePort: {node_port}
    port: {port}
    targetPort: {port}
""",
        encoding="utf-8",
    )


def _write_infer_service_set_yaml(path: Path, namespace: str = "mindie") -> None:
    path.write_text(
        f"""apiVersion: mindcluster.huawei.com/v1
kind: InferServiceSet
metadata:
  name: vllm
  namespace: {namespace}
spec:
  replicas: 1
  template:
    roles:
    - name: coordinator
      services:
      - name: mindie-motor-coordinator-infer
        spec:
          type: NodePort
          ports:
          - nodePort: 31015
            port: 1025
            targetPort: 1025
      - name: mindie-motor-coordinator-obs
        spec:
          type: NodePort
          ports:
          - nodePort: 31017
            port: 1027
            targetPort: 1027
    - name: controller
      services:
      - name: mindie-motor-observability
        spec:
          type: NodePort
          ports:
          - nodePort: 31027
            port: 1027
            targetPort: 1027
""",
        encoding="utf-8",
    )


def test_collect_planned_nodeports(tmp_path: Path):
    yaml_path = tmp_path / "coord.yaml"
    _write_svc_yaml(yaml_path, "mindie-motor-coordinator-infer", "mindie-a", 31015, 1025)
    planned = collect_planned_nodeports([str(yaml_path)])
    assert len(planned) == 1
    assert planned[0].port == 31015
    assert planned[0].purpose == "Coordinator infer"


def test_collect_planned_infer_service_set_nested(tmp_path: Path):
    yaml_path = tmp_path / "infer.yaml"
    _write_infer_service_set_yaml(yaml_path, "mindie-b")
    planned = collect_planned_nodeports([str(yaml_path)])
    ports = sorted(p.port for p in planned)
    assert ports == [31015, 31017, 31027]
    assert all(p.service_namespace == "mindie-b" for p in planned)
    purposes = {p.port: p.purpose for p in planned}
    assert purposes[31015] == "Coordinator infer"
    assert purposes[31017] == "Coordinator metrics"
    assert purposes[31027] == "Controller observability"


def test_collect_infer_service_set_null_roles_safe(tmp_path: Path):
    yaml_path = tmp_path / "infer_null.yaml"
    yaml_path.write_text(
        """apiVersion: mindcluster.huawei.com/v1
kind: InferServiceSet
metadata:
  name: vllm
  namespace: mindie
spec:
  template:
    roles: null
""",
        encoding="utf-8",
    )
    assert collect_planned_nodeports([str(yaml_path)]) == []


def test_find_conflicts_ignores_self_owned():
    planned = [
        PlannedNodePort(31015, "a.yaml", "mindie-motor-coordinator-infer", "ns1", "Coordinator infer", 1025),
    ]
    cluster = {
        31015: ClusterNodePort(31015, "ns1", "mindie-motor-coordinator-infer"),
    }
    assert find_conflicts(planned, cluster, "ns1") == []


def test_find_conflicts_detects_other_namespace():
    planned = [
        PlannedNodePort(31015, "a.yaml", "mindie-motor-coordinator-infer", "ns2", "Coordinator infer", 1025),
    ]
    cluster = {
        31015: ClusterNodePort(31015, "ns1", "mindie-motor-coordinator-infer"),
    }
    conflicts = find_conflicts(planned, cluster, "ns2")
    assert len(conflicts) == 1
    assert conflicts[0][0].port == 31015


def test_find_conflicts_self_owned_first_does_not_hide_real_conflict():
    planned = [
        PlannedNodePort(31015, "a.yaml", "other", "ns1", "Coordinator infer", 1025),
        PlannedNodePort(31015, "b.yaml", "other-svc", "ns2", "Coordinator infer", 1025),
    ]
    cluster = {
        31015: ClusterNodePort(31015, "ns1", "other"),
    }
    conflicts = find_conflicts(planned, cluster, "ns1")
    assert len(conflicts) == 1
    assert conflicts[0][0].service_name == "other-svc"


def test_allocate_free_ports_skips_used():
    planned = [
        PlannedNodePort(31015, "a.yaml", "infer", "ns2", "Coordinator infer", 1025),
        PlannedNodePort(31017, "a.yaml", "obs", "ns2", "Coordinator metrics", 1027),
    ]
    cluster = {
        31015: ClusterNodePort(31015, "ns1", "other"),
        31016: ClusterNodePort(31016, "ns1", "other2"),
    }
    conflicts = find_conflicts(planned, cluster, "ns2")
    remapping = allocate_free_ports(conflicts, cluster, planned)
    assert remapping[31015] == 31018


def test_rewrite_yaml_nodeports(tmp_path: Path):
    yaml_path = tmp_path / "svc.yaml"
    _write_svc_yaml(yaml_path, "mindie-motor-coordinator-infer", "ns2", 31015, 1025)
    rewrite_yaml_nodeports([str(yaml_path)], {31015: 31016})
    text = yaml_path.read_text(encoding="utf-8")
    assert "nodePort: 31016" in text
    assert "nodePort: 31015" not in text


def test_rewrite_infer_service_set_nested(tmp_path: Path):
    yaml_path = tmp_path / "infer.yaml"
    _write_infer_service_set_yaml(yaml_path)
    rewrite_yaml_nodeports([str(yaml_path)], {31015: 31115, 31017: 31117, 31027: 31127})
    text = yaml_path.read_text(encoding="utf-8")
    assert "nodePort: 31115" in text
    assert "nodePort: 31117" in text
    assert "nodePort: 31127" in text
    assert "nodePort: 31015" not in text


def test_collect_planned_skips_malformed_yaml(tmp_path: Path):
    yaml_path = tmp_path / "bad.yaml"
    yaml_path.write_text(":\n  - broken: [", encoding="utf-8")
    assert collect_planned_nodeports([str(yaml_path)]) == []


def test_collect_planned_skips_non_integer_nodeport(tmp_path: Path):
    yaml_path = tmp_path / "bad_port.yaml"
    yaml_path.write_text(
        """apiVersion: v1
kind: Service
metadata:
  name: mindie-motor-coordinator-infer
  namespace: mindie
spec:
  type: NodePort
  ports:
  - nodePort: not-a-port
    port: 1025
""",
        encoding="utf-8",
    )
    assert collect_planned_nodeports([str(yaml_path)]) == []


def test_rewrite_skips_malformed_yaml(tmp_path: Path):
    yaml_path = tmp_path / "bad.yaml"
    yaml_path.write_text(":\n  - broken: [", encoding="utf-8")
    rewrite_yaml_nodeports([str(yaml_path)], {31015: 31016})
    assert yaml_path.read_text(encoding="utf-8") == ":\n  - broken: ["


def test_collect_cluster_invalid_json_raises():
    class _Result:
        returncode = 0
        stdout = "{not-json"
        stderr = ""

    with (
        patch("lib.nodeport_allocator.subprocess.run", return_value=_Result()),
        pytest.raises(RuntimeError, match="invalid kubectl json"),
    ):
        collect_cluster_nodeports()


def test_resolve_skips_when_kubectl_missing(tmp_path: Path):
    yaml_path = tmp_path / "svc.yaml"
    _write_svc_yaml(yaml_path, "mindie-motor-coordinator-infer", "ns2", 31015, 1025)

    with patch(
        "lib.nodeport_allocator.collect_cluster_nodeports",
        side_effect=RuntimeError("[NodePort] failed to query cluster services: no kubectl"),
    ):
        remapping = resolve_and_rewrite_nodeports([str(yaml_path)], "ns2")
    assert remapping == {}
    assert "nodePort: 31015" in yaml_path.read_text(encoding="utf-8")


def test_resolve_and_rewrite_with_y(tmp_path: Path):
    yaml_path = tmp_path / "svc.yaml"
    _write_svc_yaml(yaml_path, "mindie-motor-coordinator-infer", "ns2", 31015, 1025)
    cluster_json = {
        "items": [
            {
                "metadata": {"namespace": "ns1", "name": "other"},
                "spec": {"ports": [{"nodePort": 31015, "port": 1025}]},
            }
        ]
    }

    class _Result:
        returncode = 0
        stdout = json.dumps(cluster_json)
        stderr = ""

    with (
        patch("lib.nodeport_allocator.subprocess.run", return_value=_Result()),
        patch("lib.nodeport_allocator.sys.stdin") as stdin,
        patch("builtins.input", return_value="y"),
    ):
        stdin.isatty.return_value = True
        remapping = resolve_and_rewrite_nodeports([str(yaml_path)], "ns2")
    assert remapping[31015] == 31016
    assert "nodePort: 31016" in yaml_path.read_text(encoding="utf-8")


def test_resolve_with_user_port(tmp_path: Path):
    yaml_path = tmp_path / "svc.yaml"
    _write_svc_yaml(yaml_path, "mindie-motor-coordinator-infer", "ns2", 31015, 1025)
    cluster = {31015: ClusterNodePort(31015, "ns1", "other")}

    with (
        patch("lib.nodeport_allocator.collect_cluster_nodeports", return_value=cluster),
        patch("lib.nodeport_allocator.sys.stdin") as stdin,
        patch("builtins.input", return_value="31520"),
    ):
        stdin.isatty.return_value = True
        remapping = resolve_and_rewrite_nodeports([str(yaml_path)], "ns2")
    assert remapping[31015] == 31520
    assert "nodePort: 31520" in yaml_path.read_text(encoding="utf-8")


def test_resolve_n_keeps_port_and_writes_showlog_file(tmp_path: Path, monkeypatch):
    yaml_path = tmp_path / "svc.yaml"
    _write_svc_yaml(yaml_path, "mindie-motor-coordinator-infer", "ns2", 31015, 1025)
    out_dir = tmp_path / "output_yamls"
    out_dir.mkdir()
    monkeypatch.setattr(C, "OUTPUT_ROOT_PATH", str(out_dir))
    cluster = {31015: ClusterNodePort(31015, "ns1", "other")}

    with (
        patch("lib.nodeport_allocator.collect_cluster_nodeports", return_value=cluster),
        patch("lib.nodeport_allocator.sys.stdin") as stdin,
        patch("builtins.input", return_value="N"),
    ):
        stdin.isatty.return_value = True
        remapping = resolve_and_rewrite_nodeports([str(yaml_path)], "ns2")

    assert remapping == {}
    assert "nodePort: 31015" in yaml_path.read_text(encoding="utf-8")
    warn = (out_dir / C.NODEPORT_CONFLICT_COORDINATOR_FILE).read_text(encoding="utf-8")
    assert "already allocated" in warn
    assert "coordinator_infer_node_port" in warn
    assert "Suggested NodePort range: 30000-32767" in warn


def test_resolve_dry_run_no_rewrite_no_prompt(tmp_path: Path):
    yaml_path = tmp_path / "svc.yaml"
    _write_svc_yaml(yaml_path, "mindie-motor-coordinator-infer", "ns2", 31015, 1025)
    cluster = {31015: ClusterNodePort(31015, "ns1", "other")}

    with (
        patch("lib.nodeport_allocator.collect_cluster_nodeports", return_value=cluster),
        patch("builtins.input") as prompt,
    ):
        remapping = resolve_and_rewrite_nodeports([str(yaml_path)], "ns2", dry_run=True)
    assert remapping == {}
    assert "nodePort: 31015" in yaml_path.read_text(encoding="utf-8")
    prompt.assert_not_called()


def test_resolve_dry_run_does_not_clear_existing_warning_files(tmp_path: Path, monkeypatch):
    yaml_path = tmp_path / "svc.yaml"
    _write_svc_yaml(yaml_path, "mindie-motor-coordinator-infer", "ns2", 31015, 1025)
    out_dir = tmp_path / "output_yamls"
    out_dir.mkdir()
    monkeypatch.setattr(C, "OUTPUT_ROOT_PATH", str(out_dir))
    warn_path = out_dir / C.NODEPORT_CONFLICT_COORDINATOR_FILE
    warn_path.write_text("keep-me\n", encoding="utf-8")
    cluster = {31015: ClusterNodePort(31015, "ns1", "other")}

    with patch("lib.nodeport_allocator.collect_cluster_nodeports", return_value=cluster):
        resolve_and_rewrite_nodeports([str(yaml_path)], "ns2", dry_run=True)
    assert warn_path.read_text(encoding="utf-8") == "keep-me\n"


def test_is_observability_service_name_avoids_robust_false_positive():
    assert is_observability_service_name("mindie-motor-coordinator-obs")
    assert is_observability_service_name("mindie-motor-observability")
    assert is_observability_service_name("mindie-motor-coordinator-obs-service")
    assert not is_observability_service_name("robust-service")
    assert not is_observability_service_name("mindie-motor-coordinator-infer")


def test_purpose_for_accepts_non_string_service_name():
    assert _purpose_for(None, 1025) == "Coordinator infer"
    assert "Service" in _purpose_for(12345, None)


def test_resolve_non_tty_treats_as_n(tmp_path: Path, monkeypatch):
    yaml_path = tmp_path / "svc.yaml"
    _write_svc_yaml(yaml_path, "mindie-motor-coordinator-infer", "ns2", 31015, 1025)
    out_dir = tmp_path / "output_yamls"
    out_dir.mkdir()
    monkeypatch.setattr(C, "OUTPUT_ROOT_PATH", str(out_dir))
    cluster = {31015: ClusterNodePort(31015, "ns1", "other")}

    with (
        patch("lib.nodeport_allocator.collect_cluster_nodeports", return_value=cluster),
        patch("lib.nodeport_allocator.sys.stdin") as stdin,
    ):
        stdin.isatty.return_value = False
        remapping = resolve_and_rewrite_nodeports([str(yaml_path)], "ns2")
    assert remapping == {}
    assert "nodePort: 31015" in yaml_path.read_text(encoding="utf-8")
    warn = (out_dir / C.NODEPORT_CONFLICT_COORDINATOR_FILE).read_text(encoding="utf-8")
    assert "Suggested NodePort range" in warn


def test_collect_cluster_skips_non_integer_nodeport():
    cluster_json = {
        "items": [
            {
                "metadata": {"namespace": "ns1", "name": "bad"},
                "spec": {"ports": [{"nodePort": "x", "port": 1025}]},
            },
            {
                "metadata": {"namespace": "ns1", "name": "ok"},
                "spec": {"ports": [{"nodePort": 31015, "port": 1025}]},
            },
        ]
    }

    class _Result:
        returncode = 0
        stdout = json.dumps(cluster_json)
        stderr = ""

    with patch("lib.nodeport_allocator.subprocess.run", return_value=_Result()):
        used = collect_cluster_nodeports()
    assert list(used) == [31015]


def test_resolve_nodeports_for_yaml_files_y(tmp_path: Path):
    yaml_path = tmp_path / "svc.yaml"
    _write_svc_yaml(yaml_path, "mindie-motor-coordinator-infer", "ns2", 31015, 1025)
    cluster = {31015: ClusterNodePort(31015, "ns1", "other")}

    with (
        patch("lib.nodeport_allocator.collect_cluster_nodeports", return_value=cluster),
        patch("lib.nodeport_allocator.sys.stdin") as stdin,
        patch("builtins.input", return_value="y"),
    ):
        stdin.isatty.return_value = True
        k8s_utils.resolve_nodeports_for_yaml_files(
            {C.CONFIG_JOB_ID: "ns2"},
            [str(yaml_path)],
        )
    assert "nodePort: 31016" in yaml_path.read_text(encoding="utf-8")


def test_resolve_nodeports_for_yaml_files_dry_run_no_rewrite(tmp_path: Path):
    yaml_path = tmp_path / "svc.yaml"
    _write_svc_yaml(yaml_path, "mindie-motor-coordinator-infer", "ns2", 31015, 1025)
    cluster = {31015: ClusterNodePort(31015, "ns1", "other")}

    with (
        patch("lib.nodeport_allocator.collect_cluster_nodeports", return_value=cluster),
        patch("builtins.input") as prompt,
    ):
        k8s_utils.resolve_nodeports_for_yaml_files(
            {C.CONFIG_JOB_ID: "ns2"},
            [str(yaml_path)],
            dry_run=True,
        )
    assert "nodePort: 31015" in yaml_path.read_text(encoding="utf-8")
    prompt.assert_not_called()


def test_write_controller_conflict_file(tmp_path: Path, monkeypatch):
    out_dir = tmp_path / "output_yamls"
    out_dir.mkdir()
    monkeypatch.setattr(C, "OUTPUT_ROOT_PATH", str(out_dir))
    item = PlannedNodePort(31027, "a.yaml", "mindie-motor-observability", "ns2", "Controller observability", 1027)
    owner = ClusterNodePort(31027, "ns1", "other")
    write_nodeport_conflict_files([(item, owner)])
    text = (out_dir / C.NODEPORT_CONFLICT_CONTROLLER_FILE).read_text(encoding="utf-8")
    assert "controller_observability_node_port" in text
    assert (out_dir / C.NODEPORT_CONFLICT_COORDINATOR_FILE).read_text(encoding="utf-8") == ""


def test_invalid_user_port_reprompts_then_y(tmp_path: Path):
    yaml_path = tmp_path / "svc.yaml"
    _write_svc_yaml(yaml_path, "mindie-motor-coordinator-infer", "ns2", 31015, 1025)
    cluster = {31015: ClusterNodePort(31015, "ns1", "other")}

    with (
        patch("lib.nodeport_allocator.collect_cluster_nodeports", return_value=cluster),
        patch("lib.nodeport_allocator.sys.stdin") as stdin,
        patch("builtins.input", side_effect=["29999", "y"]),
    ):
        stdin.isatty.return_value = True
        remapping = resolve_and_rewrite_nodeports([str(yaml_path)], "ns2")
    assert remapping[31015] == 31016


def test_user_port_rejects_other_planned_service_port(tmp_path: Path):
    yaml_a = tmp_path / "infer.yaml"
    yaml_b = tmp_path / "obs.yaml"
    _write_svc_yaml(yaml_a, "mindie-motor-coordinator-infer", "ns2", 31015, 1025)
    _write_svc_yaml(yaml_b, "mindie-motor-coordinator-obs", "ns2", 31017, 1027)
    cluster = {31015: ClusterNodePort(31015, "ns1", "other")}

    with (
        patch("lib.nodeport_allocator.collect_cluster_nodeports", return_value=cluster),
        patch("lib.nodeport_allocator.sys.stdin") as stdin,
        patch("builtins.input", side_effect=["31017", "32000"]),
    ):
        stdin.isatty.return_value = True
        remapping = resolve_and_rewrite_nodeports([str(yaml_a), str(yaml_b)], "ns2")
    assert remapping[31015] == 32000


def test_same_port_multiple_services_prompts_once(tmp_path: Path):
    yaml_a = tmp_path / "a.yaml"
    yaml_b = tmp_path / "b.yaml"
    _write_svc_yaml(yaml_a, "svc-a", "ns2", 31015, 1025)
    _write_svc_yaml(yaml_b, "svc-b", "ns2", 31015, 1025)
    cluster = {31015: ClusterNodePort(31015, "ns1", "other")}
    answers = ["N"]

    with (
        patch("lib.nodeport_allocator.collect_cluster_nodeports", return_value=cluster),
        patch("lib.nodeport_allocator.sys.stdin") as stdin,
        patch("builtins.input", side_effect=answers) as prompt,
    ):
        stdin.isatty.return_value = True
        remapping = resolve_and_rewrite_nodeports([str(yaml_a), str(yaml_b)], "ns2")
    assert remapping == {}
    assert prompt.call_count == 1
