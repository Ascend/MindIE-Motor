# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of the License at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import json
import sys
from pathlib import Path


DEPLOYER_ROOT = Path(__file__).resolve().parents[3] / "examples" / "deployer"
sys.path.insert(0, str(DEPLOYER_ROOT))

from lib.prepare_utils import (  # noqa: E402
    configmap_assets,
    kubectl_from_file_args,
    prepare_local_configmap,
    prepare_rendered_local_configmap,
)


def test_shared_configmap_assets_have_unique_runtime_names():
    assets = configmap_assets(DEPLOYER_ROOT)
    target_names = [target for _source, target in assets]

    assert len(target_names) == len(set(target_names))
    assert {
        "boot.sh",
        "common.sh",
        "engine.sh",
        "kv_conductor.sh",
        "mf_store.sh",
        "mooncake_config.py",
        "kv_store_backends.memcache.mmc-local-inprocess.conf",
        "kv_store_backends.memcache.mmc-local-standalone.conf",
    }.issubset(target_names)


def test_local_copy_and_kubectl_args_use_same_asset_manifest(tmp_path):
    user_config = tmp_path / "selected-user.json"
    env_config = tmp_path / "selected-env.json"
    destination = tmp_path / "configmap"
    user_config.write_text(json.dumps({"source": "user"}), encoding="utf-8")
    env_config.write_text(json.dumps({"source": "env"}), encoding="utf-8")

    prepare_local_configmap(DEPLOYER_ROOT, destination, user_config, env_config)

    asset_names = {target for _source, target in configmap_assets(DEPLOYER_ROOT)}
    copied_names = {path.name for path in destination.iterdir()} - {"user_config.json", "env.json"}
    kubectl_names = {arg.split("=", 2)[1] for arg in kubectl_from_file_args(DEPLOYER_ROOT)}
    assert copied_names == asset_names
    assert kubectl_names == asset_names
    assert json.loads((destination / "user_config.json").read_text(encoding="utf-8")) == {"source": "user"}
    assert json.loads((destination / "env.json").read_text(encoding="utf-8")) == {"source": "env"}


def test_rendered_local_configmap_applies_hook_to_copy_before_rendering(tmp_path):
    user_config = tmp_path / "selected-user.json"
    env_config = tmp_path / "selected-env.json"
    destination = tmp_path / "configmap"
    original = {
        "motor_deploy_config": {"deploy_mode": "multi_deployment", "job_id": "prepare-test"},
        "motor_engine_union_config": {
            "engine_type": "original",
            "engine_config": {"served_model_name": "test-model"},
        },
        "north_config": {"name": "test"},
    }
    user_config.write_text(json.dumps(original), encoding="utf-8")
    env_config.write_text(
        json.dumps({"motor_common_env": {}, "motor_mf_store_env": {"MF_STORE_MARKER": "rendered"}}),
        encoding="utf-8",
    )

    def override_copied_config(copied_path):
        copied = json.loads(copied_path.read_text(encoding="utf-8"))
        copied["motor_engine_union_config"]["engine_type"] = "overridden"
        copied_path.write_text(json.dumps(copied), encoding="utf-8")

    prepare_rendered_local_configmap(
        DEPLOYER_ROOT,
        destination,
        user_config,
        env_config,
        before_render=override_copied_config,
    )

    assert json.loads(user_config.read_text(encoding="utf-8")) == original
    assert 'export engine_type="overridden"' in (destination / "common.sh").read_text(encoding="utf-8")
    assert 'export MF_STORE_MARKER="rendered"' in (destination / "mf_store.sh").read_text(encoding="utf-8")
