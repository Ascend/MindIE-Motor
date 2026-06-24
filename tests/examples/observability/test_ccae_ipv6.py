# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import importlib
from pathlib import Path


def _add_ccae_path(monkeypatch):
    repo_root = Path(__file__).resolve().parents[3]
    ccae_root = repo_root / "examples" / "features" / "observability"
    monkeypatch.syspath_prepend(str(ccae_root))


def test_get_local_ip_prefers_pod_ip_for_ipv6(monkeypatch):
    _add_ccae_path(monkeypatch)
    util = importlib.import_module("ccae_reporter.common.util")
    monkeypatch.setenv("POD_IP", "2001:db8::2")

    assert util.get_local_ip() == "2001:db8::2"


def test_format_address_brackets_ipv6_for_kafka_bootstrap(monkeypatch):
    _add_ccae_path(monkeypatch)
    util = importlib.import_module("ccae_reporter.common.util")

    assert ",".join([util.format_address("2001:db8::1", port) for port in [9092, "9093"]]) == (
        "[2001:db8::1]:9092,[2001:db8::1]:9093"
    )
