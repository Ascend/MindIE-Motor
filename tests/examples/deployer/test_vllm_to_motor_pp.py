# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import sys
from pathlib import Path

import pytest

DEPLOYER_ROOT = Path(__file__).resolve().parents[3] / "examples" / "deployer"
sys.path.insert(0, str(DEPLOYER_ROOT / "config_tool"))

import vllm_to_motor as v2m  # noqa: E402


def _prefill_script(pp: int, *, multi_node_cli: bool = False) -> str:
    multi_node = " --nnodes 2 --node-rank 0 --master-addr placeholder --master-port 7060" if multi_node_cli else ""
    return (
        "vllm serve /mnt/weight/test "
        "--tensor-parallel-size 16 "
        f"--pipeline-parallel-size {pp} "
        f"{multi_node} "
        "--kv-transfer-config '{\"kv_connector\":\"MooncakeConnector\","
        "\"kv_role\":\"kv_producer\","
        "\"kv_connector_extra_config\":{"
        "\"prefill\":{\"dp_size\":1,\"tp_size\":16},"
        "\"decode\":{\"dp_size\":1,\"tp_size\":16}}}'"
    )


def _decode_script() -> str:
    return (
        "vllm serve /mnt/weight/test "
        "--tensor-parallel-size 16 "
        "--pipeline-parallel-size 1 "
        "--kv-transfer-config '{\"kv_connector\":\"MooncakeConnector\","
        "\"kv_role\":\"kv_consumer\","
        "\"kv_connector_extra_config\":{"
        "\"prefill\":{\"dp_size\":1,\"tp_size\":16},"
        "\"decode\":{\"dp_size\":1,\"tp_size\":16}}}'"
    )


def test_separate_prefill_preserves_pp_and_emits_nnodes():
    """TP=16, PP=2, A3(16 cards) -> pp=2, nnodes=2, master-port, pod=2, npu=16."""
    user_config, _env = v2m.convert_vllm_scripts_to_user_config(
        _prefill_script(pp=2),
        _decode_script(),
        hardware_type="A3",
    )

    p_engine = user_config["motor_engine_prefill_config"]["engine_config"]
    assert p_engine["pipeline_parallel_size"] == 2
    assert p_engine["nnodes"] == 2
    assert str(p_engine.get("master-port") or p_engine.get("master_port"))
    # Runtime injects master-addr / node-rank; converter must not write them.
    assert "master-addr" not in p_engine and "master_addr" not in p_engine
    assert "node-rank" not in p_engine and "node_rank" not in p_engine

    deploy = user_config["motor_deploy_config"]
    assert deploy["single_p_instance_pod_num"] == 2
    assert deploy["p_pod_npu_num"] == 16


def test_separate_pp1_does_not_emit_cross_node():
    """PP=1 -> no nnodes/master-port, single-pod layout (backward compatible)."""
    user_config, _env = v2m.convert_vllm_scripts_to_user_config(
        _prefill_script(pp=1),
        _decode_script(),
        hardware_type="A3",
    )

    p_engine = user_config["motor_engine_prefill_config"]["engine_config"]
    assert p_engine["pipeline_parallel_size"] == 1
    assert "nnodes" not in p_engine
    assert "master-port" not in p_engine and "master_port" not in p_engine

    deploy = user_config["motor_deploy_config"]
    assert deploy["single_p_instance_pod_num"] == 1
    assert deploy["p_pod_npu_num"] == 16


def test_infer_pod_layout_folds_pp_into_nnodes():
    """tp*pp spanning multiple nodes yields nnodes and per-node card count."""
    pods, npu, nnodes = v2m._infer_pod_layout(1, 16, 16, role="prefill", pp=2)
    assert (pods, npu, nnodes) == (2, 16, 2)

    pods, npu, nnodes = v2m._infer_pod_layout(1, 16, 16, role="decode", pp=1)
    assert (pods, npu, nnodes) == (1, 16, 1)


def test_infer_pod_layout_rejects_non_divisible_pp():
    """tp*pp not divisible by cards-per-node must fail loudly."""
    # tp=8, pp=3 -> per_dp=24, not divisible by 16 cards/node.
    with pytest.raises(ValueError):
        v2m._infer_pod_layout(1, 8, 16, role="prefill", pp=3)


def test_emit_cross_node_rejects_dp_gt_1_with_nnodes_gt_1():
    """dp>1 and nnodes>1 is unsupported until per-DP-group master_ip exists."""
    engine_config: dict = {}
    with pytest.raises(ValueError, match=r"dp>1.*nnodes>1|nnodes>1.*dp>1"):
        v2m._emit_cross_node_engine_config(
            engine_config,
            dp=2,
            tp=16,
            pp=2,
            cards=16,
            role="prefill",
        )


def _prefill_cli_with_pp(pp: int) -> dict:
    return v2m.parse_vllm_serve_command(_prefill_script(pp=pp))


def test_cli_args_to_engine_config_preserves_cli_pp_when_infer_parallel():
    """Default infer_parallel path must keep --pipeline-parallel-size from CLI."""
    cli = _prefill_cli_with_pp(2)
    engine = v2m.cli_args_to_engine_config(cli, role="prefill")
    assert engine["pipeline_parallel_size"] == 2


def test_cli_skips_runtime_injected_multi_node_keys():
    """Script --nnodes/--node-rank/--master-addr must not leak into engine_config."""
    cli = v2m.parse_vllm_serve_command(_prefill_script(pp=2, multi_node_cli=True))
    engine = v2m.cli_args_to_engine_config(cli, role="prefill")
    assert "nnodes" not in engine
    assert "node_rank" not in engine and "node-rank" not in engine
    assert "master_addr" not in engine and "master-addr" not in engine
    # master-port is kept; runtime still needs a rendezvous port in config.
    assert str(engine.get("master-port") or engine.get("master_port")) == "7060"


def test_convert_strips_script_multi_node_keys_and_emits_nnodes():
    """Passthrough multi-node CLI must not block converter-emitted nnodes."""
    user_config, _env = v2m.convert_vllm_scripts_to_user_config(
        _prefill_script(pp=2, multi_node_cli=True),
        _decode_script(),
        hardware_type="A3",
    )
    p_engine = user_config["motor_engine_prefill_config"]["engine_config"]
    assert p_engine["nnodes"] == 2
    assert "master_addr" not in p_engine and "master-addr" not in p_engine
    assert "node_rank" not in p_engine and "node-rank" not in p_engine
    assert str(p_engine.get("master-port") or p_engine.get("master_port")) == "7060"


def test_cli_tokens_to_engine_config_preserves_cli_pp():
    """cli_tokens_to_engine_config must not overwrite CLI PP to 1."""
    tokens = [
        "serve",
        "/mnt/weight/test",
        "--tensor-parallel-size",
        "16",
        "--pipeline-parallel-size",
        "2",
        "--kv-transfer-config",
        (
            '{"kv_connector":"MooncakeConnector","kv_role":"kv_producer",'
            '"kv_connector_extra_config":{"prefill":{"dp_size":1,"tp_size":16},'
            '"decode":{"dp_size":1,"tp_size":16}}}'
        ),
    ]
    engine = v2m.cli_tokens_to_engine_config(tokens)
    assert engine["pipeline_parallel_size"] == 2
