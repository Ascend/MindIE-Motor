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
from pathlib import Path


def _load_mooncake_config_module():
    repo_root = Path(__file__).resolve().parents[3]
    module_path = repo_root / "examples" / "deployer" / "startup" / "mooncake_config.py"
    spec = importlib.util.spec_from_file_location("mooncake_config", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generate_kv_cache_pool_config_brackets_ipv6_master_service(tmp_path, monkeypatch):
    module = _load_mooncake_config_module()
    user_config = tmp_path / "user_config.json"
    output = tmp_path / "kv_pool.json"
    user_config.write_text(json.dumps({"kv_cache_pool_config": {"port": "50088"}}), encoding="utf-8")
    monkeypatch.setenv("KVP_MASTER_SERVICE", "2001:db8::1")

    assert module.generate_kv_cache_pool_config(str(output), str(user_config)) is True

    generated = json.loads(output.read_text(encoding="utf-8"))
    assert generated["master_server_address"] == "[2001:db8::1]:50088"


def test_generate_kv_conductor_config_brackets_ipv6_mooncake_endpoint(tmp_path, monkeypatch):
    module = _load_mooncake_config_module()
    user_config = tmp_path / "user_config.json"
    output = tmp_path / "kv_conductor.json"
    user_config.write_text(
        json.dumps(
            {
                "kv_cache_pool_config": {"port": "50088"},
                "kv_conductor_config": {"kvevent_instance": {"mooncake_master": {}}},
                "motor_engine_prefill_config": {
                    "engine_type": "vllm",
                    "engine_config": {"served_model_name": "qwen"},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KVP_MASTER_SERVICE", "2001:db8::1")

    assert module.generate_kv_conductor_config(str(output), str(user_config)) is True

    generated = json.loads(output.read_text(encoding="utf-8"))
    assert generated["kvevent_instance"]["mooncake_master"]["endpoint"] == "tcp://[2001:db8::1]:50088"
