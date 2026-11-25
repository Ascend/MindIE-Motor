# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""RBG cleanup is opt-in; the legacy shell path retains its original commands."""

import json
import os
import subprocess
from pathlib import Path

import pytest
from lib.rbg import lifecycle

SCRIPT = Path(__file__).resolve().parents[3] / "examples/cloud_native_deploy/cleanup.sh"
LEGACY_COMMANDS = [
    "delete configmap motor-config -n demo --ignore-not-found=true",
    "delete inferservicesets.mindcluster.huawei.com --all -n demo --ignore-not-found=true",
    "delete deployment --all -n demo --ignore-not-found=true",
    "delete service --all -n demo --ignore-not-found=true",
    "delete serviceaccount mindie-motor-controller -n demo --ignore-not-found=true",
    "delete clusterrolebinding mindie-controller-binding-demo --ignore-not-found=true",
    "delete clusterrole mindie-controller-role-demo --ignore-not-found=true",
]


@pytest.mark.parametrize("mode", ["", "infer_service_set", "multi_deployment", "single_container", "rbg"])
@pytest.mark.parametrize("python_exit", [0, 17])
def test_cleanup_shell_preserves_legacy_commands_and_isolates_rbg(tmp_path, mode, python_exit):
    commands = tmp_path / "commands"
    for name, body in {
        "kubectl": 'echo "$*" >> "$COMMAND_LOG"',
        "python3": 'echo "python3 $*" >> "$COMMAND_LOG"; cat >/dev/null; exit "$PYTHON_EXIT"',
    }.items():
        executable = tmp_path / name
        executable.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        executable.chmod(0o755)
    env = dict(
        os.environ,
        PATH=str(tmp_path) + os.pathsep + os.environ["PATH"],
        MOTOR_DEPLOY_MODE=mode,
        WORKDIR=str(tmp_path),
        COMMAND_LOG=str(commands),
        PYTHON_EXIT=str(python_exit),
    )
    result = subprocess.run(["bash", str(SCRIPT), "demo"], env=env, capture_output=True, text=True, check=False)
    actual = commands.read_text(encoding="utf-8").splitlines() if commands.exists() else []
    if mode == "rbg":
        assert result.returncode == python_exit
        assert actual == ["python3 - demo"]
    else:
        assert result.returncode == 0, result.stderr
        assert actual == LEGACY_COMMANDS


@pytest.mark.parametrize("mode,namespace", [("rbg", "demo"), ("infer_service_set", "demo"), ("rbg", "other")])
def test_explicit_rbg_cleanup_requires_matching_config(monkeypatch, mode, namespace):
    config = {"motor_deploy_config": {"deploy_mode": mode, "job_id": namespace}}
    monkeypatch.setattr(
        lifecycle, "_deletion_query", lambda *a, **kw: [{"data": {"user_config.json": json.dumps(config)}}]
    )
    deleted = []
    monkeypatch.setattr(lifecycle, "delete_rbg", deleted.append)
    if mode == "rbg" and namespace == "demo":
        lifecycle.cleanup_rbg("demo")
        assert deleted == [config]
    else:
        with pytest.raises(ValueError):
            lifecycle.cleanup_rbg("demo")
        assert not deleted


def test_rbg_cleanup_missing_config_is_idempotent_but_api_errors_propagate(monkeypatch):
    monkeypatch.setattr(lifecycle, "_deletion_query", lambda *a, **kw: [])
    monkeypatch.setattr(lifecycle, "delete_rbg", lambda *a: pytest.fail("No RBG deletion without config"))
    lifecycle.cleanup_rbg("demo")

    def failed(*args, **kwargs):
        raise RuntimeError("Forbidden")

    monkeypatch.setattr(lifecycle, "_deletion_query", failed)
    with pytest.raises(RuntimeError, match="Forbidden"):
        lifecycle.cleanup_rbg("demo")
