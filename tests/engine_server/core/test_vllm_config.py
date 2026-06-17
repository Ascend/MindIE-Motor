# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import sys
from unittest.mock import MagicMock

# Mock vllm before importing VLLMConfig (vllm is not available in test environment)
mock_vllm = MagicMock()
mock_vllm.entrypoints = MagicMock()
mock_vllm.entrypoints.openai = MagicMock()
mock_vllm.entrypoints.openai.cli_args = MagicMock()
mock_vllm.entrypoints.openai.cli_args.make_arg_parser = MagicMock()
mock_vllm.entrypoints.openai.cli_args.validate_parsed_serve_args = MagicMock()
sys.modules['vllm'] = mock_vllm
sys.modules['vllm.entrypoints'] = mock_vllm.entrypoints
sys.modules['vllm.entrypoints.openai'] = mock_vllm.entrypoints.openai
sys.modules['vllm.entrypoints.openai.cli_args'] = mock_vllm.entrypoints.openai.cli_args
sys.modules['vllm.utils'] = MagicMock()
sys.modules['vllm.utils.argparse_utils'] = MagicMock()

from motor.engine_server.core.vllm.vllm_config import VLLMConfig
from motor.config.endpoint import EndpointConfig, DeployConfig, ModelConfig, EngineConfig, ParallelConfig


def _make_endpoint_config(
    nnodes=1,
    master_port=None,
    node_rank=0,
    master_dp_ip="192.168.1.1",
    dp_size=1,
    tp_size=4,
    pcp_size=1,
):
    """Build an EndpointConfig with minimal fields for testing _flatten_config."""
    engine_cfg = {
        "tensor_parallel_size": tp_size,
        "data_parallel_size": dp_size,
    }
    if nnodes > 1:
        engine_cfg["nnodes"] = nnodes
    if master_port is not None:
        engine_cfg["master_port"] = master_port

    deploy_config = DeployConfig(
        engine_type="vllm",
        model_config=ModelConfig(
            model_name="test",
            model_path="/path/to/model",
            npu_mem_utils=0.9,
            encode_parallel_config=ParallelConfig(),
            prefill_parallel_config=ParallelConfig(dp_size=dp_size, tp_size=tp_size, pcp_size=pcp_size),
            decode_parallel_config=ParallelConfig(dp_size=dp_size, tp_size=tp_size),
        ),
        engine_config=EngineConfig.from_dict(engine_cfg),
        mgmt_tls_config=None,
        infer_tls_config=None,
    )
    return EndpointConfig(
        deploy_config=deploy_config,
        host="127.0.0.1",
        port=8000,
        mgmt_port=9001,
        role="union",
        node_rank=node_rank,
        master_dp_ip=master_dp_ip,
        dp_rank=0,
    )


def test_no_pcp_params_when_nnodes_is_one():
    """When nnodes=1, no PCP params should be added."""
    endpoint_config = _make_endpoint_config(nnodes=1)
    config = VLLMConfig(endpoint_config=endpoint_config)
    config.initialize()
    flattened = config._flatten_config()

    assert "node_rank" not in flattened
    assert "master_addr" not in flattened
    assert "headless" not in flattened


def test_no_pcp_params_when_nnodes_gt_1_but_no_master_port():
    """When nnodes > 1 but master_port is missing, no PCP params added."""
    endpoint_config = _make_endpoint_config(nnodes=2, master_port=None)
    config = VLLMConfig(endpoint_config=endpoint_config)
    config.initialize()
    flattened = config._flatten_config()

    assert "node_rank" not in flattened
    assert "master_addr" not in flattened
    assert "headless" not in flattened


def test_pcp_params_added_for_master_node():
    """Master node (node_rank=0) gets node_rank and master_addr but NOT headless."""
    endpoint_config = _make_endpoint_config(
        nnodes=2,
        master_port=7001,
        node_rank=0,
        master_dp_ip="10.0.0.1",
    )
    config = VLLMConfig(endpoint_config=endpoint_config)
    config.initialize()
    flattened = config._flatten_config()

    assert flattened.get("nnodes") == 2
    assert flattened.get("master_port") == 7001
    assert flattened.get("node_rank") == 0
    assert flattened.get("master_addr") == "10.0.0.1"
    assert "headless" not in flattened


def test_pcp_params_added_for_slave_node():
    """Slave node (node_rank > 0) gets node_rank, master_addr, AND headless."""
    endpoint_config = _make_endpoint_config(
        nnodes=2,
        master_port=7001,
        node_rank=1,
        master_dp_ip="10.0.0.1",
    )
    config = VLLMConfig(endpoint_config=endpoint_config)
    config.initialize()
    flattened = config._flatten_config()

    assert flattened.get("nnodes") == 2
    assert flattened.get("master_port") == 7001
    assert flattened.get("node_rank") == 1
    assert flattened.get("master_addr") == "10.0.0.1"
    assert flattened.get("headless") is True


def test_pcp_params_engine_config_takes_precedence():
    """Engine_config values for nnodes/master_port take precedence."""
    endpoint_config = _make_endpoint_config(
        nnodes=3,
        master_port=6001,
        node_rank=0,
    )
    config = VLLMConfig(endpoint_config=endpoint_config)
    config.initialize()
    flattened = config._flatten_config()

    assert flattened.get("nnodes") == 3
    assert flattened.get("master_port") == 6001
    assert flattened.get("node_rank") == 0


def test_pcp_params_added_when_nnodes_is_string():
    """When nnodes is a string, int() conversion makes it work."""
    endpoint_config = _make_endpoint_config(nnodes=1, master_port=7001)
    endpoint_config.deploy_config.engine_config.set("nnodes", "2")
    config = VLLMConfig(endpoint_config=endpoint_config)
    config.initialize()
    flattened = config._flatten_config()

    # nnodes="2" → int("2")=2 > 1 → PCP active
    assert flattened.get("node_rank") == 0
    assert flattened.get("master_addr") == "192.168.1.1"
    assert "headless" not in flattened


def test_master_dp_ip_none_handled_gracefully():
    """When master_dp_ip is None, master_addr should not be set."""
    endpoint_config = _make_endpoint_config(
        nnodes=2,
        master_port=7001,
        node_rank=0,
        master_dp_ip=None,
    )
    config = VLLMConfig(endpoint_config=endpoint_config)
    config.initialize()
    flattened = config._flatten_config()

    assert flattened.get("node_rank") == 0
    assert "master_addr" not in flattened
    assert "headless" not in flattened


def test_pcp_params_with_master_port_dash_variant():
    """PCP detection works with master-port (dash) in engine_config."""
    endpoint_config = _make_endpoint_config(
        nnodes=2,
        master_port=None,
        node_rank=0,
    )
    # Simulate user writing "master-port" (vLLM native style) instead of "master_port"
    endpoint_config.deploy_config.engine_config.set("master-port", 7001)
    endpoint_config.deploy_config.engine_config.configs.pop("master_port", None)
    config = VLLMConfig(endpoint_config=endpoint_config)
    config.initialize()
    flattened = config._flatten_config()

    assert flattened.get("node_rank") == 0
    assert flattened.get("master_addr") == "192.168.1.1"
    assert "headless" not in flattened
