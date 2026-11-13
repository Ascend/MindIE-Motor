# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Tests for the explicit mooncake ``master_server_address`` (external / HA master).

One parametrised case per behaviour: deployer skip/env, validation, engine and conductor
rendering. Plain and etcd/HA addressing are the two representative forms; the remaining
variants share the same code paths.
"""

import importlib.util
import json
from pathlib import Path

import pytest

import lib.constant as C
from lib.config_validator import validate_kv_cache_store_config
from lib.generator import k8s_utils
from lib.generator.kv_cache_store import (
    apply_kv_store_service_domain,
    gen_kv_store_env,
    normalize_kv_cache_store_config,
)

_DEPLOYER_ROOT = Path(__file__).resolve().parents[3] / "examples" / "deployer"
_ETCD = "etcd://10.0.0.1:2379;10.0.0.2:2379"


@pytest.fixture(autouse=True)
def _restore_kv_globals():
    names = (
        "g_kv_store_deploy_pod",
        "g_kv_store_external",
        "g_kv_store_service",
        "g_kv_cache_store_port",
        "g_kv_store_backend",
    )
    saved = {name: getattr(k8s_utils, name) for name in names}
    yield
    for name, value in saved.items():
        setattr(k8s_utils, name, value)


def _load_mooncake_config_module():
    path = _DEPLOYER_ROOT / "startup/roles/kv_store_backends/mooncake/mooncake_config.py"
    spec = importlib.util.spec_from_file_location("mooncake_config_external_master", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_user_config(tmp_path, kv_config, extra=None):
    payload = {"kv_cache_store_config": kv_config}
    payload.update(extra or {})
    path = tmp_path / "user_config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("address", "expected_host", "expected_port"),
    [("10.1.1.1:50100", "10.1.1.1", 50100), (_ETCD, "", C.DEFAULT_KV_CACHE_STORE_PORT)],
)
def test_external_master_wiring(address, expected_host, expected_port):
    """Skip the local pod, sync the legacy globals and keep the env contract for both paths."""
    k8s_utils.set_kv_store_service("mindie-motor-kvs-master")
    k8s_utils.g_kv_cache_store_port = C.DEFAULT_KV_CACHE_STORE_PORT
    # ``port`` in the config must not override the port carried by the explicit address.
    user_config = {C.KV_CACHE_STORE_CONFIG: {"backend": "mooncake", "port": 50088, "master_server_address": address}}

    apply_kv_store_service_domain({C.CONFIG_JOB_ID: "ns-external"}, user_config)

    assert k8s_utils.g_kv_store_deploy_pod is False
    assert k8s_utils.g_kv_store_external is True
    assert k8s_utils.g_kv_store_service == expected_host
    assert k8s_utils.g_kv_cache_store_port == expected_port

    env = {item[C.NAME]: item[C.VALUE] for item in gen_kv_store_env(normalize_kv_cache_store_config(user_config))}
    multi = {item[C.NAME]: item[C.VALUE] for item in k8s_utils.build_kv_store_env_items()}
    if expected_host:
        assert env[C.ENV_KVS_MASTER_SERVICE] == multi[C.ENV_KVS_MASTER_SERVICE] == expected_host
        assert env[C.ENV_KV_CACHE_STORE_PORT] == "50100"
    else:
        assert C.ENV_KVS_MASTER_SERVICE not in env
        assert C.ENV_KVS_MASTER_SERVICE not in multi
        # No single host exists, so no port is exposed either.
        assert C.ENV_KV_CACHE_STORE_PORT not in env
    assert env[C.ENV_KV_STORE_EXTERNAL] == multi[C.ENV_KV_STORE_EXTERNAL] == "1"
    assert env[C.ENV_KV_STORE_BACKEND] == "mooncake"
    assert C.ENV_KV_STORE_EVICTION_RATIO not in env


@pytest.mark.parametrize("address", ["10.0.0.5:50088", "[2001:db8::1]:50088", _ETCD])
def test_validate_accepts_supported_addresses(address):
    validate_kv_cache_store_config({C.KV_CACHE_STORE_CONFIG: {"backend": "mooncake", "master_server_address": address}})


@pytest.mark.parametrize("address", ["2001:db8::1", "10.0.0.5:70000", "http://10.0.0.5:8080/metadata"])
def test_validate_rejects_invalid_addresses(address):
    with pytest.raises(ValueError):
        validate_kv_cache_store_config(
            {C.KV_CACHE_STORE_CONFIG: {"backend": "mooncake", "master_server_address": address}}
        )


def test_validate_rejects_non_mooncake_backend():
    with pytest.raises(ValueError):
        validate_kv_cache_store_config(
            {C.KV_CACHE_STORE_CONFIG: {"backend": "memcache", "master_server_address": "10.0.0.5:50088"}}
        )


def test_validate_noop_without_field():
    validate_kv_cache_store_config({C.KV_CACHE_STORE_CONFIG: {"backend": "mooncake"}})
    validate_kv_cache_store_config({})
    # An empty/None value means "not configured", matching the runtime fallback.
    for empty in ("", "   ", None):
        validate_kv_cache_store_config(
            {C.KV_CACHE_STORE_CONFIG: {"backend": "mooncake", "master_server_address": empty}}
        )


@pytest.mark.parametrize(
    ("address", "extra_config", "expected"),
    [
        (
            _ETCD,
            {"store_mode": "standalone", "global_segment_size": "200GB"},
            {"global_segment_size": 0, "local_buffer_size": "1GB"},
        ),
        (
            "10.0.0.5:50100",
            {"global_segment_size": "2GB", "metadata_server": "P2PHANDSHAKE"},
            {"global_segment_size": "2GB", "metadata_server": "P2PHANDSHAKE", "local_buffer_size": None},
        ),
    ],
)
def test_engine_config_uses_explicit_master_verbatim(tmp_path, monkeypatch, address, extra_config, expected):
    module = _load_mooncake_config_module()
    user_config = _write_user_config(tmp_path, {"master_server_address": address, **extra_config})
    output = tmp_path / "kv_cache_store_config.json"
    monkeypatch.setenv("KVS_MASTER_SERVICE", "should-be-ignored")

    assert module.generate_kv_cache_store_config(str(output), str(user_config)) is True

    generated = json.loads(output.read_text(encoding="utf-8"))
    assert generated["master_server_address"] == address
    for key, value in expected.items():
        if value is None:
            assert key not in generated
        else:
            assert generated[key] == value


def test_engine_config_falls_back_to_service_and_port(tmp_path, monkeypatch):
    module = _load_mooncake_config_module()
    user_config = _write_user_config(tmp_path, {"port": "50088"})
    output = tmp_path / "kv_cache_store_config.json"
    monkeypatch.setenv("KVS_MASTER_SERVICE", "10.0.0.9")

    assert module.generate_kv_cache_store_config(str(output), str(user_config)) is True

    generated = json.loads(output.read_text(encoding="utf-8"))
    assert generated["master_server_address"] == "10.0.0.9:50088"


@pytest.mark.parametrize(
    ("address", "expected_endpoint"),
    # The second form is scheme-less but still self-describing: no tcp:// endpoint may be derived.
    [("10.0.0.5:50088", "tcp://10.0.0.5:50088"), ("h1:2379;h2:2379", None)],
)
def test_conductor_endpoint_from_master(tmp_path, address, expected_endpoint):
    module = _load_mooncake_config_module()
    user_config = _write_user_config(
        tmp_path,
        {"master_server_address": address},
        extra={
            "kv_conductor_config": {"kvevent_instance": {"mooncake_master": {}}},
            "motor_engine_prefill_config": {"engine_config": {"served_model_name": "model-a"}},
        },
    )
    output = tmp_path / "kv_conductor_config.json"

    assert module.generate_kv_conductor_config(str(output), str(user_config)) is True

    generated = json.loads(output.read_text(encoding="utf-8"))
    assert generated["kvevent_instance"]["mooncake_master"].get("endpoint") == expected_endpoint
