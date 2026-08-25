# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import copy

import pytest

from lib.update_config_whitelist import apply_whitelist_update


_CCAE_USER_CONFIG = {
    "motor_controller_config": {
        "observability_config": {
            "observability_enable": True,
        },
        "api_config": {
            "observability_api_port": 1027,
        },
    },
    "motor_deploy_config": {
        "job_id": "mindie-motor",
        "deploy_mode": "multi_deployment_yaml",
        "tls_config": {
            "north_tls_config": {
                "enable_tls": True,
                "ca_file": "",
                "cert_file": "",
                "key_file": "",
                "passwd_file": "",
            }
        },
    },
    "north_config": {
        "name": "ccae_reporter",
        "ip": "10.226.81.122",
        "port": 26335,
    },
}


@pytest.mark.parametrize(
    "baseline",
    [
        {
            "motor_controller_config": {},
            "motor_deploy_config": {"job_id": "mindie-motor"},
        },
        {
            "motor_controller_config": {"observability_config": None},
            "motor_deploy_config": {"job_id": "mindie-motor", "tls_config": None},
            "north_config": None,
        },
    ],
    ids=["missing_sections", "null_intermediate_nodes"],
)
def test_apply_whitelist_update_creates_missing_nested_ccae_sections(baseline):
    """--update_config must create CCAE/observability intermediates absent from baseline."""
    result = apply_whitelist_update(_CCAE_USER_CONFIG, baseline)

    assert result["motor_controller_config"]["observability_config"]["observability_enable"] is True
    assert "api_config" not in result["motor_controller_config"]
    assert result["motor_deploy_config"]["tls_config"]["north_tls_config"]["enable_tls"] is True
    assert result["motor_deploy_config"]["tls_config"]["north_tls_config"]["ca_file"] == ""
    assert result["north_config"] == {
        "name": "ccae_reporter",
        "ip": "10.226.81.122",
        "port": 26335,
    }
    assert result["motor_deploy_config"]["job_id"] == "mindie-motor"
    assert "deploy_mode" not in result["motor_deploy_config"]


def test_apply_whitelist_update_overwrites_existing_leaf_without_mutating_baseline():
    """Existing whitelist leaves are updated in-place; baseline dict must stay unchanged."""
    baseline = {
        "motor_controller_config": {
            "observability_config": {"observability_enable": False, "metrics_ttl": 60},
        },
        "motor_deploy_config": {"job_id": "mindie-motor"},
    }
    baseline_snapshot = copy.deepcopy(baseline)
    user_config = copy.deepcopy(baseline)
    user_config["motor_controller_config"]["observability_config"]["observability_enable"] = True

    result = apply_whitelist_update(user_config, baseline)

    assert result["motor_controller_config"]["observability_config"]["observability_enable"] is True
    assert result["motor_controller_config"]["observability_config"]["metrics_ttl"] == 60
    assert baseline == baseline_snapshot
