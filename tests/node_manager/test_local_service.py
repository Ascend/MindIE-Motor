# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# See the Mulan PSL v2 for more details.

import sys
import threading
from types import SimpleNamespace

from motor.config.node_manager import KVCacheStoreConfig
from motor.node_manager.core.services.local_service import LocalService


def test_standalone_local_service_brackets_ipv6_urls(monkeypatch):
    captured = SimpleNamespace(config=None)

    class FakeLocalConfig:
        pass

    class FakeDistributedObjectStore:
        def setup(self, config):
            captured.config = config
            return 0

        def init(self, _):
            return 0

    monkeypatch.setitem(
        sys.modules,
        "memcache_hybrid",
        SimpleNamespace(LocalConfig=FakeLocalConfig, DistributedObjectStore=FakeDistributedObjectStore),
    )
    monkeypatch.setattr(LocalService, "_scan_node_available_dram_gb", staticmethod(lambda: 10))
    config = KVCacheStoreConfig(
        enable=True,
        backend="memcache",
        service="2001:db8::11",
        local_service_mode="standalone",
        protocol="device_sdma",
        port=51088,
        config_store_port=51089,
    )
    service = LocalService("800T_A3", config)
    service._ls_stop = threading.Event()
    service._ls_stop.set()

    service._run()

    assert captured.config.meta_service_url == "tcp://[2001:db8::11]:51088"
    assert captured.config.config_store_url == "tcp://[2001:db8::11]:51089"
