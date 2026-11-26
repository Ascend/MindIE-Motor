# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import lib.constant as C
from lib.generator import k8s_utils
from lib.generator.infer_service import (
    _find_infer_service_set_doc,
    generate_yaml_infer_service_set,
    get_infer_role,
)
from lib.generator.kv_conductor import generate_yaml_kv_conductor
from lib.utils import load_yaml


DEPLOYER_ROOT = Path(__file__).resolve().parents[3] / "examples" / "deployer"


@pytest.mark.parametrize("infer_service", [False, True])
@pytest.mark.parametrize(
    "identity_config",
    [
        {},
        {"identity_mode": "legacy"},
        {"identity_mode": "v1"},
    ],
)
def test_generated_workload_preserves_identity_mode(tmp_path, monkeypatch, infer_service, identity_config):
    """Both deployment formats must start the mode configured for Coordinator."""
    monkeypatch.setattr(k8s_utils, "g_kv_conductor_enabled", True)
    monkeypatch.setattr(k8s_utils, "g_kv_store_enabled", False)
    monkeypatch.setattr(k8s_utils, "g_generate_yaml_list", [])
    user_config = {
        C.MOTOR_DEPLOY_CONFIG: {
            C.CONFIG_JOB_ID: "identity-test",
            C.IMAGE_NAME: "mindie:test",
            C.HARDWARE_TYPE: C.HARDWARE_TYPE_800I_A3,
            C.P_INSTANCES_NUM: 1,
            C.D_INSTANCES_NUM: 1,
            C.SINGER_P_INSTANCES_NUM: 1,
            C.SINGER_D_INSTANCES_NUM: 1,
            C.P_POD_NPU_NUM: 4,
            C.D_POD_NPU_NUM: 4,
        },
        C.MOTOR_CONTROLLER_CONFIG: {},
        C.MOTOR_COORDINATOR_CONFIG: {},
        C.MOTOR_ENGINE_PREFILL_CONFIG: {},
        C.MOTOR_ENGINE_DECODE_CONFIG: {},
        C.KV_CONDUCTOR_CONFIG: {C.KV_CONDUCTOR_PORT: 13333, **identity_config},
    }
    output = tmp_path / "workload.yaml"
    if infer_service:
        generate_yaml_infer_service_set(
            str(DEPLOYER_ROOT / "yaml_template" / "infer_service_template.yaml"),
            str(output),
            user_config,
        )
        workload = get_infer_role(
            _find_infer_service_set_doc(load_yaml(str(output), False)),
            C.ROLE_KV_CONDUCTOR,
        )
    else:
        conductor_config = user_config[C.KV_CONDUCTOR_CONFIG]
        user_config[C.KV_CONDUCTOR_CONFIG] = {
            C.IMAGE_NAME: "mindie:conductor",
            C.ADDITIONAL_LABELS: {"component": "conductor"},
            C.ADDITIONAL_ANNOTATIONS: {"owner": "routing"},
        }
        generate_yaml_kv_conductor(
            str(DEPLOYER_ROOT / "yaml_template" / "kv_conductor_template.yaml"),
            str(output),
            user_config,
            conductor_config,
        )
        workload = next(doc for doc in load_yaml(str(output), False) if doc[C.KIND] == C.DEPLOYMENT_KIND)
        assert workload[C.SPEC][C.TEMPLATE][C.SPEC][C.CONTAINERS][0][C.IMAGE] == "mindie:conductor"
        for metadata in (workload[C.METADATA], workload[C.SPEC][C.TEMPLATE][C.METADATA]):
            assert metadata[C.LABELS]["component"] == "conductor"
            assert metadata[C.ANNOTATIONS]["owner"] == "routing"
    env = {item[C.NAME]: item.get(C.VALUE) for item in workload[C.SPEC][C.TEMPLATE][C.SPEC][C.CONTAINERS][0][C.ENV]}
    assert env["KV_CONDUCTOR_IDENTITY_MODE"] == identity_config.get("identity_mode", "legacy")
    assert "KV_CONDUCTOR_IDENTITY_MAIN_GROUP" not in env


def test_invalid_identity_mode_fails_before_workload_generation(tmp_path):
    output = tmp_path / "workload.yaml"
    with pytest.raises(ValueError, match="identity_mode must be"):
        generate_yaml_kv_conductor(
            str(DEPLOYER_ROOT / "yaml_template" / "kv_conductor_template.yaml"),
            str(output),
            {},
            {"identity_mode": "v2"},
        )
    assert not output.exists()


@pytest.mark.parametrize("mode", [None, "", "legacy", "v1", "v2"])
def test_startup_passes_identity_mode_only_for_v1(tmp_path, mode):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available")
    python_stub = tmp_path / "python"
    python_stub.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$CAPTURE_ARGS"\n', encoding="utf-8")
    python_stub.chmod(0o755)
    startup = tmp_path / "kv_conductor.sh"
    startup.write_text(
        "set_kv_conductor_env() { :; }\n"
        + (DEPLOYER_ROOT / "startup" / "roles" / "kv_conductor.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    output = tmp_path / "args.txt"
    env = {key: value for key, value in os.environ.items() if not key.startswith("KV_CONDUCTOR_")}
    env.update(
        PATH=f"{tmp_path}{os.pathsep}{env['PATH']}",
        CAPTURE_ARGS=str(output),
        KV_CONDUCTOR_PORT="tcp://host:14444",
    )
    if mode is not None:
        env["KV_CONDUCTOR_IDENTITY_MODE"] = mode
    result = subprocess.run([bash, str(startup)], env=env, capture_output=True, text=True, check=False)
    if mode == "v2":
        assert result.returncode == 2
        assert "KV_CONDUCTOR_IDENTITY_MODE" in result.stderr
        assert not output.exists()
        return
    assert result.returncode == 0, result.stderr
    expected = ["-m", "motor.kv_conductor", "--host", "0.0.0.0", "--port", "14444"]
    if mode == "v1":
        expected.extend(["--identity-mode", "v1"])
    assert output.read_text(encoding="utf-8").splitlines() == expected
