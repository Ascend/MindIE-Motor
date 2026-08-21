# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Golden contracts for native-engine CLI configuration.

These tests exercise the relocated runtime config builders and preserve the
pre-move CLI contract.
"""

import copy
import json

import pytest

from motor.config.endpoint import DeployConfig, EndpointConfig, EngineConfig, ModelConfig, ParallelConfig
from motor.common import engine_constants as constants
from motor.node_manager.core.services.native_engine.config_factory import ConfigFactory
from motor.node_manager.core.services.native_engine.backends.sglang.config import SGLangConfig
from motor.node_manager.core.services.native_engine.backends.vllm.config import VLLMConfig


def _endpoint_config(
    *,
    engine_type: str,
    role: str = "union",
    engine_config: dict | None = None,
    host: str = "127.0.0.1",
    port: int = 8000,
    master_dp_ip: str = "10.0.0.1",
    dp_rank: int = 0,
    node_rank: int = 0,
    dp_size: int = 1,
    tp_size: int = 2,
    pp_size: int = 1,
    pcp_size: int = 1,
    dp_rpc_port: int = 9000,
    d2d_peer_ips: str | None = None,
) -> EndpointConfig:
    prefill_parallel = ParallelConfig(
        dp_size=dp_size,
        tp_size=tp_size,
        pp_size=pp_size,
        pcp_size=pcp_size,
        dp_rpc_port=dp_rpc_port,
    )
    decode_parallel = ParallelConfig(
        dp_size=dp_size,
        tp_size=tp_size,
        pp_size=pp_size,
        dp_rpc_port=dp_rpc_port,
    )
    deploy_config = DeployConfig(
        engine_type=engine_type,
        model_config=ModelConfig(
            model_name="glm-test",
            model_path="/models/glm-test",
            npu_mem_utils=0.85,
            encode_parallel_config=ParallelConfig(
                dp_size=dp_size,
                tp_size=tp_size,
                pp_size=pp_size,
                dp_rpc_port=dp_rpc_port,
            ),
            prefill_parallel_config=prefill_parallel,
            decode_parallel_config=decode_parallel,
        ),
        engine_config=EngineConfig.from_dict(engine_config or {}),
        mgmt_tls_config=None,
        infer_tls_config=None,
    )
    return EndpointConfig(
        engine_type=engine_type,
        role=role,
        host=host,
        port=port,
        mgmt_port=9001,
        master_dp_ip=master_dp_ip,
        dp_rank=dp_rank,
        node_rank=node_rank,
        d2d_peer_ips=d2d_peer_ips,
        deploy_config=deploy_config,
    )


def test_vllm_union_cli_golden_preserves_order_types_and_precedence():
    endpoint = _endpoint_config(
        engine_type="vllm",
        engine_config={
            "dtype": "bfloat16",
            "trust_remote_code": True,
            "allowed_local_media_path": ["/data/a", "/data/b"],
            "rope_scaling": {"rope_type": "yarn", "factor": 2.0},
            "tensor_parallel_size": 8,
            "enable_chunked_prefill": False,
        },
    )
    config = VLLMConfig(endpoint_config=endpoint)
    config.initialize()

    assert config.get_cli_args() == [
        "--dtype",
        "bfloat16",
        "--trust-remote-code",
        "--allowed-local-media-path",
        "/data/a",
        "/data/b",
        "--rope-scaling",
        json.dumps({"rope_type": "yarn", "factor": 2.0}),
        "--tensor-parallel-size",
        "8",
        "--disable-access-log-for-endpoints",
        "/health,/metrics,/snapshot/health",
        "--model",
        "/models/glm-test",
        "--served-model-name",
        "glm-test",
        "--gpu-memory-utilization",
        "0.85",
        "--data-parallel-size",
        "1",
        "--pipeline-parallel-size",
        "1",
        "--data-parallel-rpc-port",
        "9000",
        "--cp-kv-cache-interleave-size",
        "1",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
    ]


@pytest.mark.parametrize(
    ("role", "kv_role"),
    [
        ("prefill", "kv_producer"),
        ("decode", "kv_consumer"),
    ],
)
def test_vllm_pd_cli_golden_injects_handoff_metadata(role, kv_role):
    endpoint = _endpoint_config(
        engine_type="vllm",
        role=role,
        engine_config={
            "kv_transfer_config": {
                "kv_connector": "MooncakeConnectorV1",
                "kv_port": "36001",
            }
        },
        dp_size=2,
        dp_rank=1,
    )
    config = VLLMConfig(endpoint_config=endpoint)
    config.initialize()
    expected_kv = {
        "kv_connector": "MooncakeConnectorV1",
        "kv_port": "36001",
        "kv_role": kv_role,
        "engine_id": "0",
        "kv_connector_extra_config": {
            "prefill": {"dp_size": 2, "tp_size": 2, "pp_size": 1},
            "decode": {"dp_size": 2, "tp_size": 2, "pp_size": 1},
        },
    }

    assert config.get_cli_args() == [
        "--kv-transfer-config",
        json.dumps(expected_kv),
        "--disable-access-log-for-endpoints",
        "/health,/metrics,/snapshot/health",
        "--model",
        "/models/glm-test",
        "--served-model-name",
        "glm-test",
        "--gpu-memory-utilization",
        "0.85",
        "--data-parallel-size",
        "2",
        "--tensor-parallel-size",
        "2",
        "--pipeline-parallel-size",
        "1",
        "--data-parallel-rpc-port",
        "9000",
        "--cp-kv-cache-interleave-size",
        "1",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
        "--data-parallel-address",
        "10.0.0.1",
        "--data-parallel-rank",
        "1",
    ]


def test_vllm_cross_node_pcp_headless_cli_golden():
    endpoint = _endpoint_config(
        engine_type="vllm",
        engine_config={"nnodes": 2, "master_port": 7001},
        node_rank=1,
        pcp_size=2,
    )
    config = VLLMConfig(endpoint_config=endpoint)
    config.initialize()

    assert config.get_cli_args()[-11:] == [
        "--prefill-context-parallel-size",
        "2",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
        "--node-rank",
        "1",
        "--master-addr",
        "10.0.0.1",
        "--headless",
    ]


def test_vllm_multi_connector_and_d2d_cli_contract():
    endpoint = _endpoint_config(
        engine_type="vllm",
        role="prefill",
        engine_config={
            "kv_transfer_config": {
                "kv_connector": "MultiConnector",
                "kv_connector_extra_config": {
                    "connectors": [
                        {"kv_connector": "NixlConnector"},
                        {
                            "kv_connector": "AscendStoreConnector",
                            "kv_connector_extra_config": {},
                        },
                    ]
                },
            },
            "model_loader_extra_config": {
                "source": "auto",
                "listen_port": 5000,
            },
        },
        d2d_peer_ips="2001:db8::1,10.0.0.2",
    )
    config = VLLMConfig(endpoint_config=endpoint)
    config.initialize()
    cli_args = config.get_cli_args()

    kv_index = cli_args.index("--kv-transfer-config")
    kv_config = json.loads(cli_args[kv_index + 1])
    assert kv_config == {
        "kv_connector": "MultiConnector",
        "kv_connector_extra_config": {
            "connectors": [
                {
                    "kv_connector": "NixlConnector",
                    "kv_role": "kv_producer",
                    "kv_connector_extra_config": {
                        "prefill": {"dp_size": 1, "tp_size": 2, "pp_size": 1},
                        "decode": {"dp_size": 1, "tp_size": 2, "pp_size": 1},
                    },
                },
                {
                    "kv_connector": "AscendStoreConnector",
                    "kv_connector_extra_config": {"lookup_rpc_port": "0"},
                    "kv_role": "kv_producer",
                },
            ]
        },
        "engine_id": "0",
        "kv_role": "kv_producer",
    }
    loader_index = cli_args.index("--model-loader-extra-config")
    assert json.loads(cli_args[loader_index + 1]) == {
        "LISTEN_PORT": 5000,
        "SOURCE": [
            {
                "device_id": 0,
                "sources": ["[2001:db8::1]:5000", "10.0.0.2:5000"],
            },
            {
                "device_id": 1,
                "sources": ["[2001:db8::1]:5001", "10.0.0.2:5001"],
            },
        ],
        "MODEL": "glm-test",
    }
    assert cli_args[cli_args.index("--load-format") + 1] == "netloader"


def test_vllm_builder_output_is_stable_across_repeated_initialization():
    endpoint = _endpoint_config(
        engine_type="vllm",
        role="prefill",
        engine_config={
            "kv_transfer_config": {
                "kv_connector": "MooncakeHybridConnector",
                "kv_port": "36001",
                "kv_connector_extra_config": {},
            }
        },
    )
    config = VLLMConfig(endpoint_config=endpoint)

    config.initialize()
    first_cli = config.get_cli_args()
    first_endpoint = copy.deepcopy(endpoint)
    config.initialize()

    assert config.get_cli_args() == first_cli
    assert endpoint == first_endpoint


def test_sglang_union_cli_golden():
    endpoint = _endpoint_config(
        engine_type="sglang",
        engine_config={
            "dtype": "bfloat16",
            "trust_remote_code": True,
            "random_seed": 7,
        },
    )
    config = SGLangConfig(endpoint_config=endpoint)
    config.initialize()

    assert config.get_cli_args() == [
        "--dtype",
        "bfloat16",
        "--trust-remote-code",
        "--random-seed",
        "7",
        "--enable-metrics",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
        "--disaggregation-mode",
        "null",
    ]


@pytest.mark.parametrize(
    ("role", "mode"),
    [
        ("prefill", "prefill"),
        ("decode", "decode"),
    ],
)
def test_sglang_pd_multinode_cli_golden(role, mode):
    endpoint = _endpoint_config(
        engine_type="sglang",
        role=role,
        engine_config={"nnodes": 2},
        host="::1",
        master_dp_ip="2001:db8::10",
        dp_rank=1,
        node_rank=7,
        dp_rpc_port=9100,
    )
    config = SGLangConfig(endpoint_config=endpoint)

    assert config.get_cli_args() == [
        "--nnodes",
        "2",
        "--enable-metrics",
        "--host",
        "::1",
        "--port",
        "8000",
        "--dist-init-addr",
        "[2001:db8::10]:9100",
        "--node-rank",
        "7",
        "--disaggregation-mode",
        mode,
    ]


def test_sglang_multinode_accepts_string_nnodes_and_requires_master_address():
    endpoint = _endpoint_config(
        engine_type="sglang",
        role="prefill",
        engine_config={"nnodes": "2"},
        master_dp_ip="",
    )

    with pytest.raises(ValueError, match="master_dp_ip is required"):
        SGLangConfig(endpoint_config=endpoint).get_cli_args()


@pytest.mark.parametrize("nnodes", ["invalid", 0])
def test_sglang_rejects_invalid_nnodes(nnodes):
    endpoint = _endpoint_config(engine_type="sglang", engine_config={"nnodes": nnodes})

    with pytest.raises(ValueError, match="nnodes"):
        SGLangConfig(endpoint_config=endpoint).get_cli_args()


def test_native_cli_omits_none_values():
    endpoint = _endpoint_config(
        engine_type="sglang",
        engine_config={"download-dir": None},
    )

    args = SGLangConfig(endpoint_config=endpoint).get_cli_args()

    assert "--download-dir" not in args
    assert "None" not in args


def test_factory_rejects_unknown_engine_type_before_importing_builder():
    endpoint = _endpoint_config(engine_type="unknown")

    with pytest.raises(ValueError, match="Unsupported engine type: unknown"):
        ConfigFactory(endpoint).build_cli_config()


@pytest.mark.parametrize(
    ("invalid_kv_config", "error_pattern"),
    [
        (None, "kv_transfer_config is None in engine_config"),
        (
            {
                "kv_connector": "MultiConnector",
                "kv_connector_extra_config": {"connectors": [{"kv_connector": "NixlConnector"}]},
            },
            "Failed to process kv_transfer_config",
        ),
        (
            {
                "kv_connector": "MultiConnector",
                "kv_connector_extra_config": {
                    "connectors": [
                        {"kv_connector": "NixlConnector"},
                        {
                            "kv_connector": "UnsupportedStoreConnector",
                            "kv_connector_extra_config": {},
                        },
                    ]
                },
            },
            "Failed to process kv_transfer_config",
        ),
    ],
)
def test_vllm_invalid_pd_connector_contract(invalid_kv_config, error_pattern):
    engine_config = {}
    if invalid_kv_config is not None:
        engine_config[constants.KV_TRANSFER_CONFIG] = invalid_kv_config
    endpoint = _endpoint_config(
        engine_type="vllm",
        role="prefill",
        engine_config=engine_config,
    )

    with pytest.raises(ValueError, match=error_pattern):
        VLLMConfig(endpoint_config=endpoint).initialize()
