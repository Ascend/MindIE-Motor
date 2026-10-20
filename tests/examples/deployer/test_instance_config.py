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

import pytest

ROOT = Path(__file__).resolve().parents[3] / "examples/deployer"


def load(relative):
    spec = importlib.util.spec_from_file_location(Path(relative).stem, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("role,index,base", [("prefill", 0, 11000), ("prefill", 1, 11000), ("decode", 0, 12000)])
def test_instance_config_refresh_and_shutdown(tmp_path, monkeypatch, role, index, base):
    adapter = load("startup/instance_config.py")
    nm = {"api_config": {"node_manager_port": base + 26}, "endpoint_config": {"base_port": base}}
    nm["port_allocator_config"] = {"enable": False}
    engine = {"data_parallel_rpc_port": base + 8000, "kv_transfer_config": {"kv_port": str(base + 19001)}}
    key = f"motor_engine_{role}_config"
    original = json.dumps({key: {"motor_nodemanger_config": nm, "engine_config": engine}})
    source, dest = tmp_path / "source.json", tmp_path / "runtime/user_config.json"
    source.write_text(original, encoding="utf-8")
    adapter.adapt_config(source, dest, role, index, {})
    before = dest.read_bytes()
    adapter.adapt_config(source, dest, role, index, {})
    assert dest.read_bytes() == before and source.read_text(encoding="utf-8") == original
    actual = json.loads(before)[key]
    assert actual["motor_nodemanger_config"]["api_config"]["node_manager_port"] == base + 26 + index * 100
    assert actual["engine_config"]["data_parallel_rpc_port"] == base + 8000 + index * 100
    assert actual["engine_config"]["kv_transfer_config"]["kv_port"] == str(base + 19001 + index * 100)
    env = {"ASCEND_BASE_PORT": "22000", "HIXL_LISTEN_PORT": "26666"}
    assert adapter.adapt_env(role, index, env) == {
        "ASCEND_BASE_PORT": str(22000 + index * 4000),
        "HIXL_LISTEN_PORT": str(26666 + index * 200),
    }
    for name, value in {"CONFIG_PATH": str(dest.parent), "ROLE": role, "INSTANCE_INDEX": str(index)}.items():
        monkeypatch.setenv(name, value)
    prestop = load("prestop/prestop.py")
    assert prestop.get_nm_port(prestop.load_config()) == base + 26 + index * 100
    engine["data_parallel_rpc_port"] = 65536
    source.write_text(json.dumps({key: {"motor_nodemanger_config": nm, "engine_config": engine}}), encoding="utf-8")
    with pytest.raises(ValueError):
        adapter.adapt_config(source, dest, role, index, {})
    assert dest.read_bytes() == before
