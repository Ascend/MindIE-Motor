# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

# tests/examples/deployer/test_kv_cache_store.py
import sys
from pathlib import Path
import pytest

DEPLOYER_ROOT = Path(__file__).resolve().parents[3] / "examples" / "deployer"
sys.path.insert(0, str(DEPLOYER_ROOT))

import lib.constant as C  # noqa: E402
from lib.generator.kv_cache_store import (  # noqa: E402
    normalize_kv_cache_store_config,
    gen_kv_store_env,
)


def test_normalize_kv_cache_store_config():
    # 正常情况（空字典，补全默认值）
    user_config = {C.KV_CACHE_STORE_CONFIG: {}}
    res = normalize_kv_cache_store_config(user_config)
    assert res[C.KV_CACHE_STORE_PORT] == C.DEFAULT_KV_CACHE_STORE_PORT
    assert res[C.KV_STORE_BACKEND] == C.DEFAULT_KV_STORE_BACKEND

    # 异常情况（缺少配置）
    with pytest.raises(ValueError, match="Missing or invalid"):
        normalize_kv_cache_store_config({})


def test_gen_kv_store_env():
    # mooncake 后端测试
    kv_store_config = {
        C.KV_STORE_BACKEND: "mooncake",
        C.KV_CACHE_STORE_PORT: 1234,
        C.KV_STORE_EVICTION_HIGH_WATERMARK_RATIO: 0.9,
        C.KV_STORE_EVICTION_RATIO: 0.1,
    }
    env = gen_kv_store_env(kv_store_config)
    env_dict = {item[C.NAME]: item.get(C.VALUE) for item in env}

    assert env_dict[C.ENV_KV_STORE_BACKEND] == "mooncake"
    assert env_dict[C.ENV_KV_CACHE_STORE_PORT] == "1234"
    assert env_dict[C.ENV_KV_STORE_EVICTION_HIGH_WATERMARK_RATIO] == "0.9"
    assert env_dict[C.ENV_KV_STORE_EVICTION_RATIO] == "0.1"

    # mmc 后端测试
    kv_store_config = {
        C.KV_STORE_BACKEND: C.MMC_STORE_BACKEND,
        C.KV_CACHE_STORE_PORT: 1234,
    }
    env = gen_kv_store_env(kv_store_config)
    env_dict = {item[C.NAME]: item.get(C.VALUE) for item in env}

    assert env_dict[C.ENV_KV_STORE_BACKEND] == C.MMC_STORE_BACKEND
    assert C.ENV_MMC_CONFIG_STORE_URL in env_dict
