# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Regression tests ensuring observability stack defines no privileged containers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
STACK_DIR = REPO_ROOT / "examples" / "features" / "observability" / "stack"


def test_docker_compose_removes_privileged_cadvisor_and_npu_exporter() -> None:
    compose = yaml.safe_load((STACK_DIR / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert "cadvisor" not in services
    assert "ascend-npu-exporter" not in services
    assert "node-exporter" in services
    for svc_name, svc_cfg in services.items():
        assert not svc_cfg.get("privileged"), f"service {svc_name} must not set privileged: true"
        assert "npu" not in (svc_cfg.get("profiles") or []), f"service {svc_name} must not use npu profile"


def test_stack_scripts_and_configs_have_no_privileged_exporter_references() -> None:
    for script_name in ("launch.sh", "start.sh", "stop.sh"):
        script = STACK_DIR / script_name
        res = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)
        assert res.returncode == 0, f"bash -n failed for {script}: {res.stderr}"
        text = script.read_text(encoding="utf-8")
        assert "--profile npu" not in text
        assert "cAdvisor" not in text

    for rel in (
        ".env.example",
        "prometheus/prometheus.yml",
        "prometheus/prometheus-real.yml",
        "scripts/discover-targets.py",
        "scripts/start-native.sh",
    ):
        content = (STACK_DIR / rel).read_text(encoding="utf-8").lower()
        assert "cadvisor" not in content, f"unexpected cadvisor reference in {rel}"
        assert "npu-exporter" not in content, f"unexpected npu-exporter reference in {rel}"
        assert "npu_exporter" not in content, f"unexpected npu_exporter reference in {rel}"
