# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "examples/deployer/patch/patch_apply_container_snapshot.py"
COMMON_SCRIPT = Path(__file__).resolve().parents[3] / "examples/deployer/startup/common.sh"
ENGINE_SCRIPT = Path(__file__).resolve().parents[3] / "examples/deployer/startup/roles/engine.sh"
SPEC = importlib.util.spec_from_file_location("patch_apply_container_snapshot", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
PATCH_APPLIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCH_APPLIER)


def _write_config(tmp_path, snapshot_config=None):
    config = {}
    if snapshot_config is not None:
        config[PATCH_APPLIER.SNAPSHOT_CONFIG_KEY] = snapshot_config
    config_path = tmp_path / "user_config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path


def _function_text(name):
    lines = COMMON_SCRIPT.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}()"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "}")
    return "".join(lines[start : end + 1])


@pytest.mark.parametrize("snapshot_config", [None, {}, {"enable_snapshot": False}])
def test_disabled_snapshot_does_not_apply_patches(tmp_path, monkeypatch, snapshot_config):
    monkeypatch.setenv("USER_CONFIG_PATH", str(_write_config(tmp_path, snapshot_config)))

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("patch command must not run when container snapshot is disabled")

    monkeypatch.setattr(PATCH_APPLIER.subprocess, "run", unexpected_run)

    assert PATCH_APPLIER.main() == 0


def test_enabled_snapshot_applies_both_patches(tmp_path, monkeypatch):
    monkeypatch.setenv("USER_CONFIG_PATH", str(_write_config(tmp_path, {"enable_snapshot": True})))
    calls = []

    def record_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(PATCH_APPLIER.subprocess, "run", record_run)

    assert PATCH_APPLIER.main() == 0
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[0] == "/bin/bash"
    assert command[1].endswith("/0.30.0/apply_patches.sh")
    assert command[2].endswith("/container_snapshot/vllm_v0.30.0_container_snapshot.patch")
    assert command[3].endswith("/container_snapshot/vllm_ascend_v0.30.0_container_snapshot.patch")
    assert kwargs == {"check": False}


@pytest.mark.parametrize("snapshot_config", [True, {"enable_snapshot": "true"}])
def test_invalid_snapshot_config_fails_before_applying_patches(tmp_path, monkeypatch, snapshot_config):
    monkeypatch.setenv("USER_CONFIG_PATH", str(_write_config(tmp_path, snapshot_config)))

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("patch command must not run for invalid configuration")

    monkeypatch.setattr(PATCH_APPLIER.subprocess, "run", unexpected_run)

    assert PATCH_APPLIER.main() == 1


def test_patch_failure_is_returned(tmp_path, monkeypatch):
    monkeypatch.setenv("USER_CONFIG_PATH", str(_write_config(tmp_path, {"enable_snapshot": True})))
    monkeypatch.setattr(
        PATCH_APPLIER.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=7),
    )

    assert PATCH_APPLIER.main() == 7


@pytest.mark.parametrize(
    ("snapshot_config", "expected_status"),
    [
        (None, 1),
        ({}, 1),
        ({"enable_snapshot": False}, 1),
        ({"enable_snapshot": True}, 0),
        ({"enable_snapshot": "true"}, 2),
    ],
)
def test_snapshot_enabled_check_status(tmp_path, monkeypatch, snapshot_config, expected_status):
    monkeypatch.setenv("USER_CONFIG_PATH", str(_write_config(tmp_path, snapshot_config)))

    assert PATCH_APPLIER.check_enabled() == expected_status


def test_snapshot_engine_env_requires_pod_ip(tmp_path):
    config_path = _write_config(tmp_path, {"enable_snapshot": True})
    script = tmp_path / "run_snapshot_env_without_pod_ip.sh"
    script.write_text(
        f"{_function_text('set_container_snapshot_engine_env')}\n"
        f'export MOTOR_PATCH_ROOT="{SCRIPT.parent}" USER_CONFIG_PATH="{config_path}"\n'
        "unset POD_IP\n"
        "set_container_snapshot_engine_env\n",
        encoding="utf-8",
    )

    result = subprocess.run(["bash", str(script)], check=False, capture_output=True, text=True)

    assert result.returncode != 0
    assert "POD_IP is required when container snapshot is enabled" in result.stderr


def test_snapshot_engine_env_skips_when_checker_is_missing(tmp_path):
    script = tmp_path / "run_snapshot_env_without_checker.sh"
    script.write_text(
        f"{_function_text('set_container_snapshot_engine_env')}\n"
        f'export MOTOR_PATCH_ROOT="{tmp_path / "missing-patch-root"}"\n'
        "set_container_snapshot_engine_env\n",
        encoding="utf-8",
    )

    result = subprocess.run(["bash", str(script)], check=False, capture_output=True, text=True)

    assert result.returncode == 0
    assert "container snapshot patch checker not found" in result.stdout


def test_engine_configures_snapshot_network_environment_conditionally():
    common_script = COMMON_SCRIPT.read_text(encoding="utf-8")
    engine_script = ENGINE_SCRIPT.read_text(encoding="utf-8")

    check = 'python3 "$snapshot_check_script" --check-enabled'
    assert check in common_script
    assert 'if [ ! -f "$snapshot_check_script" ]; then' in common_script
    assert 'export HCCL_IF_IP="$POD_IP"' in common_script
    assert 'export GLOO_SOCKET_IFNAME="${GLOO_SOCKET_IFNAME:-eth0}"' in common_script
    assert 'export TP_SOCKET_IFNAME="${TP_SOCKET_IFNAME:-eth0}"' in common_script
    assert 'export HCCL_SOCKET_IFNAME="${HCCL_SOCKET_IFNAME:-eth0}"' in common_script
    assert "set_container_snapshot_engine_env || exit 1" in engine_script
    assert engine_script.index("apply_instance_port_env") < engine_script.index("set_container_snapshot_engine_env")
    assert engine_script.index("set_container_snapshot_engine_env") < engine_script.index(
        "python3 -m motor.node_manager.main"
    )
