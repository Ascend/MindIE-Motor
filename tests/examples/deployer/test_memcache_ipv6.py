# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest


DEPLOYER_ROOT = Path(__file__).resolve().parents[3] / "examples" / "deployer"
META_SERVICE_PATH = DEPLOYER_ROOT / "startup" / "roles" / "kv_store_backends" / "memcache" / "memcache_meta_service.py"


def _load_meta_service(monkeypatch):
    captured = SimpleNamespace(config=None)

    class FakeMetaConfig:
        pass

    class FakeMetaService:
        @staticmethod
        def setup(config):
            captured.config = config

        @staticmethod
        def main():
            pass

    monkeypatch.setitem(
        sys.modules,
        "memcache_hybrid",
        SimpleNamespace(MetaService=FakeMetaService, MetaConfig=FakeMetaConfig),
    )
    spec = importlib.util.spec_from_file_location("memcache_meta_service", META_SERVICE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, captured


def test_meta_service_brackets_ipv6_urls(monkeypatch):
    module, captured = _load_meta_service(monkeypatch)
    monkeypatch.setenv("POD_IP", "2001:db8::8")
    monkeypatch.setenv("KV_CACHE_STORE_PORT", "50088")
    monkeypatch.setenv("MMC_CONFIG_STORE_URL", "tcp://0.0.0.0:50089")
    monkeypatch.setenv("MMC_METRICS_URL", "http://0.0.0.0:50090")

    module.main()

    assert captured.config.meta_service_url == "tcp://[2001:db8::8]:50088"
    assert captured.config.config_store_url == "tcp://[2001:db8::8]:50089"
    assert captured.config.metrics_url == "http://[2001:db8::8]:50090"


def test_sync_mmc_local_config_brackets_ipv6_master(tmp_path):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available")

    configmap_path = tmp_path / "configmap"
    config_path = tmp_path / "config"
    configmap_path.mkdir()
    config_path.mkdir()
    source_config = DEPLOYER_ROOT / "startup" / "roles" / "kv_store_backends" / "memcache" / "mmc-local-inprocess.conf"
    (configmap_path / "kv_store_backends.memcache.mmc-local-inprocess.conf").write_text(
        source_config.read_text(encoding="utf-8"), encoding="utf-8"
    )

    env = os.environ | {
        "CONFIGMAP_PATH": str(configmap_path),
        "CONFIG_PATH": str(config_path),
        "KVS_MASTER_SERVICE": "2001:db8::9",
    }
    result = subprocess.run(
        [
            bash,
            "-c",
            'source "$1"; sync_mmc_local_config; cat "$MMC_LOCAL_CONFIG_PATH"',
            "bash",
            str(DEPLOYER_ROOT / "startup" / "common.sh"),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "tcp://[2001:db8::9]:50088" in result.stdout
    assert "tcp://[2001:db8::9]:50089" in result.stdout
