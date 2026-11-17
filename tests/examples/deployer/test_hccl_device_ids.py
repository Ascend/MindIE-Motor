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
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml


DEPLOYER_ROOT = Path(__file__).resolve().parents[3] / "examples/deployer"


@pytest.mark.parametrize("allocation,expected", [("Ascend910-8,Ascend910-10", ["8", "10"]), ("", ["0", "2", "10"])])
def test_ranktable_devices_follow_allocation_before_privileged_visibility(monkeypatch, allocation, expected):
    """Seeing every host device must not register chips outside the scheduled allocation."""
    monkeypatch.setenv("ASCEND_ALLOCATED_DEVICES", allocation)
    monkeypatch.delenv("ASCEND_RT_VISIBLE_DEVICES", raising=False)
    monkeypatch.delenv("ASCEND_VISIBLE_DEVICES", raising=False)
    spec = importlib.util.spec_from_file_location("hccl_device_test", DEPLOYER_ROOT / "startup/hccl_tools.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with patch.object(module, "_davinci_ids", return_value=["10", "2", "0"]):
        assert module.get_visible_devices() == expected


@pytest.mark.parametrize("template,expected_count", [("engine_template.yaml", 1), ("infer_service_template.yaml", 4)])
def test_engine_templates_expose_scheduler_allocation(template, expected_count):
    """Both workload APIs supply allocation to ranktable generation and native launch."""

    def allocations(value):
        if isinstance(value, dict):
            if value.get("name") == "ASCEND_ALLOCATED_DEVICES":
                yield value["valueFrom"]["fieldRef"]["fieldPath"]
            for child in value.values():
                yield from allocations(child)
        elif isinstance(value, list):
            for child in value:
                yield from allocations(child)

    documents = list(yaml.safe_load_all((DEPLOYER_ROOT / "yaml_template" / template).read_text()))
    assert list(allocations(documents)) == ["metadata.annotations['huawei.com/Ascend910']"] * expected_count
