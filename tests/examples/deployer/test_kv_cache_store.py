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
from lib.generator import k8s_utils  # noqa: E402
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


@pytest.mark.parametrize(
    "master, scale_in, effective",
    [
        ({}, True, False),
        ({"enable_kv_migration": True}, True, True),
        ({"enable_kv_migration": True}, False, False),
        ({"enable_kv_migration": False}, True, False),
    ],
)
def test_scale_in_migration_switch_in_pod_environment(master, scale_in, effective, monkeypatch):
    monkeypatch.setattr(k8s_utils, "g_mmc_enable_scale_in_drain", False)
    config = {
        C.MOTOR_CONTROLLER_CONFIG: {C.FAULT_TOLERANCE_CONFIG: master},
        C.KV_CACHE_STORE_CONFIG: {
            C.MMC_LOCAL_SERVICE_CONFIG_KEY: "standalone",
            C.MMC_ENABLE_KV_DRAIN_ON_SCALE_IN: scale_in,
        },
    }
    normalize_kv_cache_store_config(config)
    env = {item[C.NAME]: item.get(C.VALUE) for item in k8s_utils.build_kv_store_env_items()}
    if master.get("enable_kv_migration"):
        assert env[C.ENV_MMC_ENABLE_KV_DRAIN_ON_SCALE_IN] == str(effective).lower()
    else:
        assert {C.ENV_MMC_ENABLE_KV_DRAIN_ON_SCALE_IN, C.ENV_MMC_DRAIN_API_URL, "POD_UID"}.isdisjoint(env)
    assert env[C.ENV_KV_STORE_BACKEND] == C.MMC_STORE_BACKEND


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


@pytest.mark.parametrize("configured_url", [None, "http://external-meta:8080"])
def test_memcache_drain_inventory_has_service_address_and_pod_uid(monkeypatch, configured_url):
    monkeypatch.setattr(k8s_utils, "g_kv_store_service", "memcache")
    monkeypatch.setattr(k8s_utils, "g_mmc_drain_api_url", "")
    monkeypatch.setattr(k8s_utils, "g_kv_store_backend", "")
    monkeypatch.setattr(k8s_utils, "g_mmc_local_service_mode", "")
    config = {
        C.MOTOR_CONTROLLER_CONFIG: {C.FAULT_TOLERANCE_CONFIG: {"enable_kv_migration": True}},
        C.KV_CACHE_STORE_CONFIG: {
            C.KV_STORE_BACKEND: C.MMC_STORE_BACKEND,
            C.MMC_LOCAL_SERVICE_CONFIG_KEY: "standalone",
            C.MMC_DRAIN_API_URL_KEY: configured_url,
        },
    }
    normalize_kv_cache_store_config(config)

    env = {item[C.NAME]: item for item in k8s_utils.build_kv_store_env_items()}
    assert env[C.ENV_MMC_DRAIN_API_URL][C.VALUE] == (configured_url or "http://memcache:50090")
    assert env["POD_UID"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.uid"
