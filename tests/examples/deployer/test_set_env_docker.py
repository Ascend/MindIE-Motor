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


DEPLOYER_ROOT = Path(__file__).resolve().parents[3] / "examples" / "deployer"
STARTUP_ROOT = DEPLOYER_ROOT / "startup"
sys.path.insert(0, str(STARTUP_ROOT))

from set_env_docker import set_env_docker  # noqa: E402


def _make_hybrid_single_container_user_config():
    return {
        "version": "v2.0",
        "motor_deploy_config": {
            "deploy_mode": "single_container",
            "job_id": "pd-hybrid-single",
            "hybrid_instances_num": 1,
            "single_hybrid_instance_pod_num": 1,
            "hybrid_pod_npu_num": 2,
        },
        "motor_controller_config": {},
        "motor_coordinator_config": {},
        "motor_engine_union_config": {
            "engine_type": "vllm",
            "engine_config": {
                "served_model_name": "qwen3-8B",
                "model": "/mnt/weight/qwen3_8B",
            },
        },
    }


def _prepare_configmap(tmp_path):
    shell_names = [
        "common.sh",
        "all_combine_in_single_container.sh",
        "controller.sh",
        "coordinator.sh",
        "engine.sh",
        "kv_pool.sh",
        "kv_conductor.sh",
    ]
    for name in shell_names:
        (tmp_path / name).write_text("", encoding="utf-8")

    (tmp_path / "user_config.json").write_text(
        json.dumps(_make_hybrid_single_container_user_config()),
        encoding="utf-8",
    )
    (tmp_path / "env.json").write_text(
        json.dumps(
            {
                "motor_common_env": {},
                "motor_controller_env": {},
                "motor_coordinator_env": {},
                "motor_engine_union_env": {"UNION_ONLY_KEY": "union"},
            }
        ),
        encoding="utf-8",
    )


def test_set_env_docker_injects_union_env_for_single_container_hybrid(tmp_path):
    _prepare_configmap(tmp_path)

    set_env_docker(str(tmp_path))

    single_container_shell = (tmp_path / "all_combine_in_single_container.sh").read_text(encoding="utf-8")
    common_shell = (tmp_path / "common.sh").read_text(encoding="utf-8")

    assert "function set_union_env()" in single_container_shell
    assert 'export UNION_ONLY_KEY="union"' in single_container_shell
    assert 'export engine_type="vllm"' in common_shell


def test_set_env_docker_uses_union_engine_type_when_prefill_absent(tmp_path):
    _prepare_configmap(tmp_path)

    set_env_docker(str(tmp_path))

    common_shell = (tmp_path / "common.sh").read_text(encoding="utf-8")
    assert 'export engine_type="vllm"' in common_shell
