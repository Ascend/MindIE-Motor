# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from types import SimpleNamespace

from motor.engine_server.constants import constants
from motor.engine_server.core.sglang.sglang_config import SGLangConfig
from motor.engine_server.core.vllm.vllm_config import VLLMConfig


class _DeployConfig:
    def __init__(self, engine_configs: dict | None = None, parallel_config: SimpleNamespace | None = None):
        self.engine_config = SimpleNamespace(configs=engine_configs or {})
        self.model_config = SimpleNamespace()
        self._parallel_config = parallel_config or SimpleNamespace(local_world_size=1, dp_rpc_port=9000)

    def get_parallel_config(self, _role):
        return self._parallel_config


def test_vllm_d2d_sources_bracket_ipv6_peer_ips():
    endpoint_config = SimpleNamespace(
        d2d_peer_ips="2001:db8::1,10.0.0.2",
        deploy_config=_DeployConfig({"model_loader_extra_config": {"source": "auto", "listen_port": 5000}}),
        role=constants.PREFILL_ROLE,
        dp_rank=0,
    )
    config = VLLMConfig(endpoint_config=endpoint_config)

    config._process_d2d_config()

    assert config._d2d_source == [
        {
            "device_id": 0,
            "sources": ["[2001:db8::1]:5000", "10.0.0.2:5000"],
        }
    ]


def test_sglang_dist_init_addr_brackets_ipv6_master_ip():
    endpoint_config = SimpleNamespace(
        deploy_config=_DeployConfig({"nnodes": 2}, SimpleNamespace(dp_rpc_port=9100)),
        role=constants.PREFILL_ROLE,
        host="::1",
        port=8000,
        master_dp_ip="2001:db8::10",
        dp_rank=1,
    )
    config = SGLangConfig(endpoint_config=endpoint_config)

    flattened = config._flatten_config()

    assert flattened["dist-init-addr"] == "[2001:db8::10]:9100"
    assert flattened["node-rank"] == 1
