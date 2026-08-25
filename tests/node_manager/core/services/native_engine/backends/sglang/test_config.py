# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You may use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.


from motor.config.endpoint import DeployConfig, EndpointConfig, EngineConfig, ModelConfig, ParallelConfig
from motor.node_manager.core.services.native_engine.backends.sglang.config import SGLangConfig


def _make_endpoint_config(engine_cfg: dict) -> EndpointConfig:
    deploy_config = DeployConfig(
        engine_type="sglang",
        model_config=ModelConfig(
            model_name="deepseek-v4-flash",
            model_path="",
            npu_mem_utils=0.93,
            encode_parallel_config=ParallelConfig(),
            prefill_parallel_config=ParallelConfig(dp_size=16, tp_size=16, pp_size=1),
            decode_parallel_config=ParallelConfig(dp_size=16, tp_size=16, pp_size=1),
        ),
        engine_config=EngineConfig.from_dict(engine_cfg),
        mgmt_tls_config=None,
        infer_tls_config=None,
        enable_multi_endpoints=False,
    )
    return EndpointConfig(
        deploy_config=deploy_config,
        host="192.168.196.59",
        port=10000,
        role="union",
        node_rank=0,
        master_dp_ip="192.168.196.59",
        dp_rank=0,
    )


def test_flatten_uses_engine_config_only_for_model_path():
    engine_cfg = {
        "model-path": "/home/weights/DeepSeek-V4-Flash-w8a8-mtp/",
        "served-model-name": "deepseek-v4-flash",
    }
    flattened = SGLangConfig(endpoint_config=_make_endpoint_config(engine_cfg))._flatten_config()

    assert flattened["model_path"] == "/home/weights/DeepSeek-V4-Flash-w8a8-mtp/"
    assert "model-path" not in flattened


def test_param_list_includes_multi_node_native_launch_args():
    endpoint = _make_endpoint_config({"nnodes": 2, "model-path": "/m"})
    endpoint.node_rank = 1
    endpoint.dp_rank = 3
    arg_list = SGLangConfig(endpoint_config=endpoint)._get_param_list()

    assert arg_list[arg_list.index("--dist-init-addr") + 1] == "192.168.196.59:9000"
    assert arg_list[arg_list.index("--node-rank") + 1] == "1"
