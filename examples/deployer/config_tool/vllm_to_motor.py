# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
#
# 独立脚本：不依赖 motor 包，可直接拷贝到任意目录运行。
# 推荐通过 examples/deployer/deploy.py --mode general_config 调用本脚本。
#
# 用法:
#   PD 混部: 同目录放置 run_dp_template_hybrid.sh 后执行
#     python deploy.py --mode general_config --deploy-scenario hybrid --hardware-type A3
#   PD 分离: 同目录放置 run_dp_template_prefill.sh / run_dp_template_decode.sh 后执行
#     python deploy.py --mode general_config --deploy-scenario separate --hardware-type A3
#   硬件类型: A2 / A3 / A5（A5 按每节点 8 卡，输出 hardware_type=850-Atlas-8p-8）
#   可选: --weight-path <路径>  --image-name <镜像>
#   输出: output_config/user_config.json、output_config/env.json
#
# 并行度：脚本 kv extra 提供 world_size；--hardware-type 仅在 tp 超过单节点上限时重算 engine dp/tp。
# deploy Pod 切分：按 world_size 打包到节点（pod 数 = world/cards，每 pod 卡数 = cards 或 world）。
# env.json：仅转换脚本中字面量 export。部署/运行时在 shell 中展开的环境变量不写入 env.json：
#   - 显式跳过：HCCL_IF_IP、网卡名、LD_PRELOAD、ASCEND_RT_VISIBLE_DEVICES 等（见 SKIP_ENV_KEYS）
#   - 值含 $ 引用：如 LD_PRELOAD=...:$LD_PRELOAD、LD_LIBRARY_PATH=...:$LD_LIBRARY_PATH、$1/$nic_name 等

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# converter core (standalone copy, no motor import)
# ---------------------------------------------------------------------------

SKIP_RUNTIME_KEYS = frozenset(
    {
        "host",
        "port",
        "data_parallel_rank",
        "data_parallel_address",
    }
)

# 并行度以 kv_connector_extra_config 为准；CLI 中的以下字段忽略
SKIP_PARALLEL_CLI_KEYS = frozenset(
    {
        "data_parallel_size",
        "tensor_parallel_size",
        "data_parallel_rpc_port",
    }
)

DEFAULT_DP_RPC_PORT = 9000

# Motor 部署/运行时自行注入，不转换到 env.json
SKIP_ENV_KEYS = frozenset(
    {
        "HCCL_IF_IP",
        "GLOO_SOCKET_IFNAME",
        "TP_SOCKET_IFNAME",
        "HCCL_SOCKET_IFNAME",
        "LD_PRELOAD",
        "ASCEND_RT_VISIBLE_DEVICES",
    }
)

# export 值中的 shell 变量引用（$VAR / ${VAR} / $1），表示运行时拼接，不转换
_SHELL_VAR_REF = re.compile(r"\$(?:\{[^}]+\}|[A-Za-z_][A-Za-z0-9_]*|\d+)")

DEFAULT_ENV_COMMON = {
    "CANN_INSTALL_PATH": "/usr/local/Ascend",
    "MOTOR_LOG_ROOT_PATH": "/root/ascend/log",
}

DEFAULT_OUTPUT_DIR = "output_config"
DEFAULT_USER_CONFIG_NAME = "user_config.json"
DEFAULT_ENV_NAME = "env.json"
DEPLOY_SCENARIO_HYBRID = "hybrid"
DEPLOY_SCENARIO_SEPARATE = "separate"

AUTO_HYBRID_SCRIPT = "run_dp_template_hybrid.sh"
AUTO_PREFILL_SCRIPT = "run_dp_template_prefill.sh"
AUTO_DECODE_SCRIPT = "run_dp_template_decode.sh"
MANUAL_FILL_WEIGHT_MOUNT_PATH = "<请按实际情况填写模型权重文件的访问路径>"
MANUAL_FILL_IMAGE_NAME = "<请按实际情况填写镜像名称>"
MANUAL_FILL_PARALLEL_FIELD = "<请手动填写该参数，因为kv-transfer-config中未识别到dp_size和tp_size>"
MANUAL_FILL_DEPLOY_POD_FIELD = "<请手动填写该参数，未获取到dp_size和tp_size，无法推断pod切分情况>"

_ANSI_BLUE = "\033[34m"
_ANSI_RESET = "\033[0m"

ENV_CONFIG_KEY_ORDER = (
    "version",
    "motor_common_env",
    "motor_controller_env",
    "motor_coordinator_env",
    "motor_engine_prefill_env",
    "motor_engine_decode_env",
    "motor_kv_cache_store_env",
)

HYBRID_ENV_CONFIG_KEY_ORDER = (
    "version",
    "motor_common_env",
    "motor_controller_env",
    "motor_coordinator_env",
    "motor_engine_union_env",
    "motor_kv_cache_store_env",
)

UNION_ENV_KEY_ORDER = (
    "VLLM_RPC_TIMEOUT",
    "VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS",
    "HCCL_EXEC_TIMEOUT",
    "HCCL_CONNECT_TIMEOUT",
    "OMP_PROC_BIND",
    "OMP_NUM_THREADS",
    "PYTORCH_NPU_ALLOC_CONF",
    "HCCL_BUFFSIZE",
    "TASK_QUEUE_ENABLE",
    "HCCL_OP_EXPANSION_MODE",
    "VLLM_ASCEND_ENABLE_FLASHCOMM1",
    "VLLM_ASCEND_ENABLE_FUSED_MC2",
    "DYNAMIC_EPLB",
    "VLLM_TORCH_PROFILER_DIR",
    "VLLM_TORCH_PROFILER_WITH_STACK",
)

PREFILL_ENV_KEY_ORDER = (
    "VLLM_RPC_TIMEOUT",
    "VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS",
    "HCCL_EXEC_TIMEOUT",
    "HCCL_CONNECT_TIMEOUT",
    "OMP_PROC_BIND",
    "OMP_NUM_THREADS",
    "PYTORCH_NPU_ALLOC_CONF",
    "HCCL_BUFFSIZE",
    "TASK_QUEUE_ENABLE",
    "HCCL_OP_EXPANSION_MODE",
    "VLLM_ASCEND_ENABLE_FLASHCOMM1",
)

DECODE_ENV_KEY_ORDER = (
    "HCCL_OP_EXPANSION_MODE",
    "TASK_QUEUE_ENABLE",
    "VLLM_RPC_TIMEOUT",
    "VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS",
    "HCCL_EXEC_TIMEOUT",
    "HCCL_CONNECT_TIMEOUT",
    "VLLM_ASCEND_ENABLE_FUSED_MC2",
    "OMP_PROC_BIND",
    "OMP_NUM_THREADS",
    "PYTORCH_NPU_ALLOC_CONF",
    "HCCL_BUFFSIZE",
    "VLLM_ASCEND_ENABLE_FLASHCOMM1",
    "DYNAMIC_EPLB",
    "VLLM_TORCH_PROFILER_DIR",
    "VLLM_TORCH_PROFILER_WITH_STACK",
)

UNDERSCORE_KEYS = frozenset(
    {
        "data_parallel_size",
        "tensor_parallel_size",
        "pipeline_parallel_size",
        "data_parallel_rpc_port",
        "enable_expert_parallel",
        "gpu_memory_utilization",
        "no_disable_hybrid_kv_cache_manager",
        "served_model_name",
        "model",
        "kv_transfer_config",
    }
)

DEFAULT_PROFILER_CONFIG = {
    "profiler": "torch",
    "torch_profiler_dir": "./vllm_profile",
    "torch_profiler_with_stack": False,
}

DEFAULT_DEPLOY_CONFIG = {
    "p_instances_num": 1,
    "d_instances_num": 1,
    "single_p_instance_pod_num": 1,
    "single_d_instance_pod_num": 1,
    "p_pod_npu_num": 16,
    "d_pod_npu_num": 16,
    "image_name": "",
    "job_id": "mindie-motor",
    "hardware_type": "800I_A3",
    "weight_mount_path": "/mnt/weight/",
}

# 各硬件平台默认部署参数（其余字段可生成后手工修改）
HARDWARE_PRESETS: dict[str, dict[str, Any]] = {
    "800I_A2": {
        "hardware_type": "800I_A2",
        "cards_per_node": 8,
        "image_name": "<请手动填写镜像名称，例如：mindie-motor-vllm:dev-26.1.0.B081-800I-A3-py311-Ubuntu24.04-lts-aarch64>",
        "weight_mount_path": "/data01/models/",
        "job_id": "mindie-motor",
    },
    "800I_A3": {
        "hardware_type": "800I_A3",
        "cards_per_node": 16,
        "image_name": "<请手动填写镜像名称，例如：mindie-motor-vllm:dev-26.1.0.B081-800I-A3-py311-Ubuntu24.04-lts-aarch64>",
        "weight_mount_path": "/mnt/weight/",
        "job_id": "mindie-motor",
    },
    "A5": {
        "hardware_type": "850-Atlas-8p-8",
        "cards_per_node": 8,
        "image_name": "<请手动填写镜像名称>",
        "weight_mount_path": "/mnt/weight/",
        "job_id": "mindie-motor",
    },
}

USER_CONFIG_KEY_ORDER = (
    "version",
    "motor_deploy_config",
    "motor_controller_config",
    "motor_coordinator_config",
    "motor_engine_prefill_config",
    "motor_engine_decode_config",
)

HYBRID_USER_CONFIG_KEY_ORDER = (
    "version",
    "motor_deploy_config",
    "motor_controller_config",
    "motor_coordinator_config",
    "motor_engine_union_config",
)

DEPLOY_CONFIG_KEY_ORDER = (
    "p_instances_num",
    "d_instances_num",
    "single_p_instance_pod_num",
    "single_d_instance_pod_num",
    "p_pod_npu_num",
    "d_pod_npu_num",
    "image_name",
    "job_id",
    "hardware_type",
    "weight_mount_path",
)

HYBRID_DEPLOY_CONFIG_KEY_ORDER = (
    "deploy_mode",
    "hybrid_instances_num",
    "single_hybrid_instance_pod_num",
    "hybrid_pod_npu_num",
    "image_name",
    "job_id",
    "hardware_type",
    "weight_mount_path",
)

ENGINE_ROLE_KEY_ORDER = ("engine_type", "engine_config")

HYBRID_ENGINE_CONFIG_KEY_ORDER = (
    "served_model_name",
    "model",
    "gpu_memory_utilization",
    "data_parallel_size",
    "tensor_parallel_size",
    "pipeline_parallel_size",
    "enable_expert_parallel",
    "data_parallel_rpc_port",
    "seed",
    "max-model-len",
    "max-num-batched-tokens",
    "max-num-seqs",
    "block-size",
    "enforce-eager",
    "async-scheduling",
    "enable-prefix-caching",
    "no-enable-prefix-caching",
    "trust-remote-code",
    "quantization",
    "safetensors-load-strategy",
    "model-loader-extra-config",
    "tokenizer-mode",
    "tool-call-parser",
    "enable-auto-tool-choice",
    "reasoning-parser",
    "speculative-config",
    "compilation-config",
    "profiler-config",
    "additional-config",
)

PREFILL_ENGINE_CONFIG_KEY_ORDER = (
    "data_parallel_size",
    "tensor_parallel_size",
    "pipeline_parallel_size",
    "data_parallel_rpc_port",
    "served_model_name",
    "model",
    "seed",
    "enable_expert_parallel",
    "max-model-len",
    "max-num-batched-tokens",
    "max-num-seqs",
    "block-size",
    "enforce-eager",
    "async-scheduling",
    "no_disable_hybrid_kv_cache_manager",
    "enable-prefix-caching",
    "trust-remote-code",
    "gpu_memory_utilization",
    "quantization",
    "safetensors-load-strategy",
    "model-loader-extra-config",
    "tokenizer-mode",
    "tool-call-parser",
    "enable-auto-tool-choice",
    "reasoning-parser",
    "speculative-config",
    "profiler-config",
    "additional-config",
    "kv_transfer_config",
)

DECODE_ENGINE_CONFIG_KEY_ORDER = (
    "data_parallel_size",
    "tensor_parallel_size",
    "pipeline_parallel_size",
    "data_parallel_rpc_port",
    "served_model_name",
    "model",
    "seed",
    "enable_expert_parallel",
    "max-model-len",
    "max-num-batched-tokens",
    "max-num-seqs",
    "async-scheduling",
    "block-size",
    "no-enable-prefix-caching",
    "no_disable_hybrid_kv_cache_manager",
    "safetensors-load-strategy",
    "model-loader-extra-config",
    "trust-remote-code",
    "tokenizer-mode",
    "tool-call-parser",
    "enable-auto-tool-choice",
    "reasoning-parser",
    "gpu_memory_utilization",
    "quantization",
    "compilation-config",
    "speculative-config",
    "profiler-config",
    "additional-config",
    "kv_transfer_config",
)

NESTED_KEY_ORDERS: dict[str, tuple[str, ...]] = {
    "profiler-config": (
        "profiler",
        "torch_profiler_dir",
        "torch_profiler_with_stack",
    ),
    "kv_transfer_config": (
        "kv_connector",
        "kv_role",
        "kv_port",
        "engine_id",
    ),
    "compilation-config": ("cudagraph_mode",),
    "ascend_compilation_config": (
        "enable_npugraph_ex",
        "enable_static_kernel",
    ),
}


def reorder_dict(
    data: dict[str, Any],
    key_order: tuple[str, ...] | list[str],
    *,
    nested_orders: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, Any]:
    """Reorder dict keys; unknown keys are appended at the end."""
    nested_orders = nested_orders or NESTED_KEY_ORDERS
    ordered: dict[str, Any] = {}
    for key in key_order:
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, dict) and key in nested_orders:
            value = reorder_dict(value, nested_orders[key], nested_orders=nested_orders)
        elif isinstance(value, dict) and key == "additional-config":
            value = _reorder_additional_config(value)
        ordered[key] = value
    for key, value in data.items():
        if key in ordered:
            continue
        if isinstance(value, dict) and key in nested_orders:
            value = reorder_dict(value, nested_orders[key], nested_orders=nested_orders)
        elif isinstance(value, dict) and key == "additional-config":
            value = _reorder_additional_config(value)
        ordered[key] = value
    return ordered


def _reorder_additional_config(data: dict[str, Any]) -> dict[str, Any]:
    preferred = (
        "enable_cpu_binding",
        "enable_shared_expert_dp",
        "enable_dsa_cp",
        "multistream_overlap_shared_expert",
        "recompute_scheduler_enable",
        "ascend_compilation_config",
    )
    ordered = reorder_dict(data, preferred, nested_orders=NESTED_KEY_ORDERS)
    return ordered


def format_engine_config(engine_config: dict[str, Any], *, role: str) -> dict[str, Any]:
    if role == "hybrid":
        key_order = HYBRID_ENGINE_CONFIG_KEY_ORDER
    elif role == "prefill":
        key_order = PREFILL_ENGINE_CONFIG_KEY_ORDER
    else:
        key_order = DECODE_ENGINE_CONFIG_KEY_ORDER
    return reorder_dict(engine_config, key_order)


def format_engine_role_config(role_config: dict[str, Any], *, role: str) -> dict[str, Any]:
    formatted = reorder_dict(role_config, ENGINE_ROLE_KEY_ORDER)
    if "engine_config" in formatted:
        formatted["engine_config"] = format_engine_config(formatted["engine_config"], role=role)
    return formatted


def format_user_config(config: dict[str, Any]) -> dict[str, Any]:
    """Apply canonical key order for user_config.json output."""
    if "motor_engine_union_config" in config:
        ordered = reorder_dict(config, HYBRID_USER_CONFIG_KEY_ORDER)
        if "motor_deploy_config" in ordered:
            ordered["motor_deploy_config"] = reorder_dict(
                ordered["motor_deploy_config"],
                HYBRID_DEPLOY_CONFIG_KEY_ORDER,
            )
        if "motor_engine_union_config" in ordered:
            ordered["motor_engine_union_config"] = format_engine_role_config(
                ordered["motor_engine_union_config"],
                role="hybrid",
            )
        return ordered

    if "motor_engine_prefill_config" not in config and "version" not in config:
        role = "prefill"
        kv = config.get("kv_transfer_config") or {}
        if isinstance(kv, dict) and str(kv.get("kv_role", "")).lower() == "kv_consumer":
            role = "decode"
        return format_engine_config(config, role=role)

    ordered = reorder_dict(config, USER_CONFIG_KEY_ORDER)
    if "motor_deploy_config" in ordered:
        ordered["motor_deploy_config"] = reorder_dict(
            ordered["motor_deploy_config"],
            DEPLOY_CONFIG_KEY_ORDER,
        )
    if "motor_engine_prefill_config" in ordered:
        ordered["motor_engine_prefill_config"] = format_engine_role_config(
            ordered["motor_engine_prefill_config"],
            role="prefill",
        )
    if "motor_engine_decode_config" in ordered:
        ordered["motor_engine_decode_config"] = format_engine_role_config(
            ordered["motor_engine_decode_config"],
            role="decode",
        )
    return ordered


def normalize_hardware_type(value: str) -> str:
    """Normalize ``A2`` / ``800I-A2`` -> ``800I_A2``; ``A5`` -> internal ``A5`` preset."""
    text = value.strip().upper().replace("-", "_")
    if text in {"A2", "800I_A2", "910B"}:
        return "800I_A2"
    if text in {"A3", "800I_A3"}:
        return "800I_A3"
    if text in {"A5", "850_ATLAS_8P_8"}:
        return "A5"
    if text in HARDWARE_PRESETS:
        return text
    raise ValueError(f"unsupported hardware type: {value!r}, use A2, A3 or A5")


def _get_kv_config(cli_args: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("kv-transfer-config", "kv_transfer_config"):
        raw = cli_args.get(key)
        if raw is None:
            continue
        if isinstance(raw, str):
            return json.loads(raw)
        if isinstance(raw, dict):
            return raw
    return None


def _as_optional_int(value: Any) -> int | None:
    if value is None or value is True or value is False:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _read_prefill_decode_parallel(extra: dict[str, Any]) -> dict[str, int] | None:
    """Read P/D dp/tp from one dict that contains prefill/decode, if complete."""
    prefill = extra.get("prefill") or {}
    decode = extra.get("decode") or {}
    if not isinstance(prefill, dict) or not isinstance(decode, dict):
        return None

    prefill_dp = _as_optional_int(prefill.get("dp_size"))
    prefill_tp = _as_optional_int(prefill.get("tp_size"))
    decode_dp = _as_optional_int(decode.get("dp_size"))
    decode_tp = _as_optional_int(decode.get("tp_size"))
    if not all([prefill_dp, prefill_tp, decode_dp, decode_tp]):
        return None
    return {
        "prefill_dp": prefill_dp,
        "prefill_tp": prefill_tp,
        "decode_dp": decode_dp,
        "decode_tp": decode_tp,
    }


def _find_prefill_decode_parallel(node: Any) -> dict[str, int] | None:
    """Recursively find the first complete prefill/decode dp/tp in a nested structure."""
    if isinstance(node, dict):
        parallel = _read_prefill_decode_parallel(node)
        if parallel is not None:
            return parallel
        for value in node.values():
            found = _find_prefill_decode_parallel(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_prefill_decode_parallel(item)
            if found is not None:
                return found
    return None


def try_extract_parallel_from_cli_args(cli_args: dict[str, Any]) -> dict[str, int] | None:
    """Return P/D dp/tp from kv config when present; otherwise None (no error)."""
    kv_config = _get_kv_config(cli_args)
    if not kv_config:
        return None
    return _find_prefill_decode_parallel(kv_config)


def extract_parallel_from_cli_args(cli_args: dict[str, Any]) -> dict[str, int]:
    """Read P/D dp/tp from kv_connector_extra_config (preferred).

    Supports top-level prefill/decode and nested structures such as MultiConnector
    ``connectors[].kv_connector_extra_config``.
    """
    parallel = try_extract_parallel_from_cli_args(cli_args)
    if parallel is not None:
        return parallel

    if not _get_kv_config(cli_args):
        raise ValueError("脚本中缺少 kv-transfer-config / kv_connector_extra_config，无法推断 P/D 并行度。")
    raise ValueError(
        "kv_connector_extra_config 中需包含完整的 prefill/decode dp_size 与 tp_size。"
        "（支持写在 MultiConnector 等任意嵌套层级）"
    )


def _as_positive_int(value: Any, *, field: str) -> int:
    if value is None or value is True or value is False:
        raise ValueError(f"脚本中缺少有效的 {field}。")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"脚本中 {field} 无法解析为整数: {value!r}") from exc
    if parsed <= 0:
        raise ValueError(f"脚本中 {field} 必须为正整数，当前为 {parsed}。")
    return parsed


def extract_hybrid_parallel_from_cli_args(cli_args: dict[str, Any]) -> tuple[int, int]:
    """Read dp/tp from hybrid script CLI flags; omitted values default to 1."""
    dp_raw = cli_args.get("data-parallel-size", cli_args.get("data_parallel_size"))
    tp_raw = cli_args.get("tensor-parallel-size", cli_args.get("tensor_parallel_size"))
    dp = 1 if dp_raw is None else _as_positive_int(dp_raw, field="--data-parallel-size")
    tp = 1 if tp_raw is None else _as_positive_int(tp_raw, field="--tensor-parallel-size")
    return dp, tp


def remap_hybrid_parallel_for_hardware(dp: int, tp: int, hardware_type: str) -> tuple[int, int]:
    """Remap hybrid dp/tp while preserving world_size (same rules as PD remap)."""
    cards = cards_per_node(hardware_type)
    world = dp * tp
    new_tp = min(tp, cards)
    if new_tp == tp:
        return dp, tp
    if world % new_tp != 0:
        raise ValueError(
            f"hybrid: world_size={world} 在 tp 上限 {cards} 下无法整除为 tp={new_tp}，"
            "请检查脚本中的 --data-parallel-size / --tensor-parallel-size。"
        )
    return world // new_tp, new_tp


def cards_per_node(hardware_type: str) -> int:
    return int(HARDWARE_PRESETS[normalize_hardware_type(hardware_type)]["cards_per_node"])


def remap_parallel_for_hardware(
    parallel: dict[str, int],
    hardware_type: str,
) -> dict[str, int]:
    """Remap dp/tp while preserving world_size.

    Script tp is kept when already <= cards-per-node; only values above the
    hardware cap (8 for A2/A5, 16 for A3) are reduced and dp is increased accordingly.
    """
    cards = cards_per_node(hardware_type)

    def _remap_role(dp: int, tp: int, role: str) -> tuple[int, int]:
        world = dp * tp
        new_tp = min(tp, cards)
        if new_tp == tp:
            return dp, tp
        if world % new_tp != 0:
            raise ValueError(
                f"{role}: world_size={world} 在 tp 上限 {cards} 下无法整除为 tp={new_tp}，"
                "请检查 kv_connector_extra_config 中的 dp_size/tp_size。"
            )
        return world // new_tp, new_tp

    p_dp, p_tp = _remap_role(parallel["prefill_dp"], parallel["prefill_tp"], "prefill")
    d_dp, d_tp = _remap_role(parallel["decode_dp"], parallel["decode_tp"], "decode")
    return {
        "prefill_dp": p_dp,
        "prefill_tp": p_tp,
        "decode_dp": d_dp,
        "decode_tp": d_tp,
    }


def apply_engine_parallel(
    engine_config: dict[str, Any],
    *,
    role: str,
    parallel: dict[str, int],
) -> None:
    """Write data_parallel_size / tensor_parallel_size from kv extra; rpc port fixed."""
    if role == "prefill":
        engine_config["data_parallel_size"] = parallel["prefill_dp"]
        engine_config["tensor_parallel_size"] = parallel["prefill_tp"]
    elif role == "decode":
        engine_config["data_parallel_size"] = parallel["decode_dp"]
        engine_config["tensor_parallel_size"] = parallel["decode_tp"]
    else:
        raise ValueError(f"unsupported engine role: {role}")
    engine_config["data_parallel_rpc_port"] = DEFAULT_DP_RPC_PORT
    engine_config["pipeline_parallel_size"] = 1


def _infer_role_from_cli_args(cli_args: dict[str, Any]) -> str:
    kv_config = _get_kv_config(cli_args) or {}
    role = str(kv_config.get("kv_role", "")).lower()
    if role == "kv_producer":
        return "prefill"
    if role == "kv_consumer":
        return "decode"
    return "prefill"


def _infer_pod_layout(dp: int, tp: int, cards: int, *, role: str) -> tuple[int, int]:
    """Pack dp×tp world_size onto nodes with ``cards`` NPUs each."""
    world = dp * tp
    if tp > cards:
        raise ValueError(f"{role}: tp={tp} 超过单节点上限 {cards} 卡。")
    if world <= cards:
        return 1, world
    if world % cards != 0:
        raise ValueError(f"{role}: world_size={world} 无法按每节点 {cards} 卡整除切分 Pod。")
    return world // cards, cards


def infer_motor_deploy_config(
    parallel: dict[str, int],
    hardware_type: str,
    *,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Infer motor_deploy_config by packing remapped dp×tp onto nodes."""
    hw = normalize_hardware_type(hardware_type)
    preset = dict(HARDWARE_PRESETS[hw])
    cards = int(preset.pop("cards_per_node"))

    p_pods, p_npu = _infer_pod_layout(
        parallel["prefill_dp"],
        parallel["prefill_tp"],
        cards,
        role="prefill",
    )
    d_pods, d_npu = _infer_pod_layout(
        parallel["decode_dp"],
        parallel["decode_tp"],
        cards,
        role="decode",
    )

    deploy = {
        "p_instances_num": 1,
        "d_instances_num": 1,
        "single_p_instance_pod_num": p_pods,
        "single_d_instance_pod_num": d_pods,
        "p_pod_npu_num": p_npu,
        "d_pod_npu_num": d_npu,
    }

    deploy.update(preset)
    if overrides:
        deploy.update(overrides)
    return deploy


def infer_hybrid_motor_deploy_config(
    dp: int,
    tp: int,
    hardware_type: str,
    *,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Infer hybrid motor_deploy_config by packing dp×tp onto nodes."""
    hw = normalize_hardware_type(hardware_type)
    preset = dict(HARDWARE_PRESETS[hw])
    cards = int(preset.pop("cards_per_node"))
    pods, npu = _infer_pod_layout(dp, tp, cards, role="hybrid")
    deploy = {
        "deploy_mode": "infer_service_set",
        "hybrid_instances_num": 1,
        "single_hybrid_instance_pod_num": pods,
        "hybrid_pod_npu_num": npu,
    }
    deploy.update(preset)
    if overrides:
        deploy.update(overrides)
    return deploy


def resolve_hybrid_script(directory: Path) -> Path:
    """Read hybrid template script from *directory*."""
    script_path = directory / AUTO_HYBRID_SCRIPT
    if not script_path.is_file():
        raise FileNotFoundError(
            f"在目录 {directory} 未找到: {AUTO_HYBRID_SCRIPT}。\n"
            f"PD 混部场景请使用 --deploy-scenario hybrid;"
            f"PD 分离场景请使用 --deploy-scenario separate。"
        )
    return script_path


def resolve_pd_scripts(directory: Path) -> tuple[Path, Path]:
    """Read P/D template scripts from *directory*."""
    prefill_path = directory / AUTO_PREFILL_SCRIPT
    decode_path = directory / AUTO_DECODE_SCRIPT
    missing = [p.name for p in (prefill_path, decode_path) if not p.is_file()]
    if missing:
        raise FileNotFoundError(
            f"在目录 {directory} 未找到: {', '.join(missing)}。\n请放置 {AUTO_PREFILL_SCRIPT} 与 {AUTO_DECODE_SCRIPT}。"
        )
    return prefill_path, decode_path


def apply_manual_fill_placeholders(
    user_config: dict[str, Any],
    *,
    hybrid: bool = False,
) -> None:
    """Mark deploy/model paths that must be edited manually after generation."""
    user_config["motor_deploy_config"]["weight_mount_path"] = MANUAL_FILL_WEIGHT_MOUNT_PATH
    if hybrid:
        engine_config = user_config["motor_engine_union_config"]["engine_config"]
        if "model" in engine_config:
            engine_config["model"] = MANUAL_FILL_WEIGHT_MOUNT_PATH
        return
    for role_key in ("motor_engine_prefill_config", "motor_engine_decode_config"):
        engine_config = user_config[role_key]["engine_config"]
        if "model" in engine_config:
            engine_config["model"] = MANUAL_FILL_WEIGHT_MOUNT_PATH


def apply_parallel_manual_fill_placeholders(user_config: dict[str, Any]) -> None:
    """Mark parallel-related fields that could not be inferred from kv-transfer-config."""
    deploy = user_config["motor_deploy_config"]
    for key in (
        "single_p_instance_pod_num",
        "single_d_instance_pod_num",
        "p_pod_npu_num",
        "d_pod_npu_num",
    ):
        deploy[key] = MANUAL_FILL_DEPLOY_POD_FIELD

    parallel_engine_keys = (
        "data_parallel_size",
        "tensor_parallel_size",
        "pipeline_parallel_size",
        "data_parallel_rpc_port",
    )
    for role_key in ("motor_engine_prefill_config", "motor_engine_decode_config"):
        engine_config = user_config[role_key]["engine_config"]
        for key in parallel_engine_keys:
            engine_config[key] = MANUAL_FILL_PARALLEL_FIELD


def _try_parse_json_text(raw: str) -> Any | None:
    """Parse CLI JSON blob; tolerate leading spaces and shlex escape artifacts."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or text[0] not in "{[":
        return None
    if '\\"' in text:
        text = text.replace('\\"', '"')
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _coerce(raw: str) -> Any:
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    parsed_json = _try_parse_json_text(raw)
    if parsed_json is not None:
        return parsed_json
    for caster in (int, float):
        try:
            return caster(raw)
        except ValueError:
            pass
    return raw


def _set_config(config: dict[str, Any], key: str, value: Any) -> None:
    if "." not in key:
        config[key] = value
        return
    node = config
    for part in key.split(".")[:-1]:
        name = part.replace("-", "_")
        if not isinstance(node.get(name), dict):
            node[name] = {}
        node = node[name]
    node[key.rsplit(".", 1)[-1].replace("-", "_")] = value


def cli_to_config(tokens: list[str]) -> dict[str, Any]:
    config: dict[str, Any] = {}
    rest = list(tokens)
    while rest:
        head, *rest = rest
        if not head.startswith("--"):
            config.setdefault("model", head)
            continue
        key = head[2:]
        if rest and not rest[0].startswith("--"):
            value = rest[0]
            _set_config(config, key, _coerce(value) if isinstance(value, str) else value)
            rest = rest[1:]
        else:
            _set_config(config, key, True)
    return config


def _unquote_shell_value(raw: str) -> str:
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def _coerce_env_value(raw: str) -> Any:
    """Coerce export values for env.json (bools stay lowercase strings)."""
    lowered = raw.lower()
    if lowered in ("true", "false"):
        return lowered
    if raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit()):
        return int(raw)
    return raw


def _value_has_shell_var_ref(raw: str) -> bool:
    """True when export value depends on shell expansion (lib path append, $1, etc.)."""
    return _SHELL_VAR_REF.search(raw) is not None


def parse_script_exports(text: str) -> dict[str, Any]:
    """Parse ``export KEY=VALUE`` lines before ``vllm serve``.

    Skips ``SKIP_ENV_KEYS`` and any value containing ``$`` shell references.
    Export 段不做 shell 变量展开。
    """
    exports: dict[str, Any] = {}
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        if re.search(r"\bvllm\s+serve\b", stripped):
            break
        if re.match(r"^(nic_name|local_ip)=", stripped, re.IGNORECASE):
            continue
        match = re.match(r"^export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$", stripped)
        if not match:
            continue
        key, raw_value = match.group(1), match.group(2).strip()
        if key in SKIP_ENV_KEYS or _value_has_shell_var_ref(raw_value):
            continue
        exports[key] = _coerce_env_value(_unquote_shell_value(raw_value))
    return exports


def extract_vllm_command_text(text: str) -> str:
    """Keep only the ``vllm serve ...`` portion of a shell script."""
    chunks: list[str] = []
    started = False
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        if re.search(r"\bvllm\s+serve\b", stripped):
            started = True
        if started:
            chunks.append(stripped)
    if not chunks:
        return text
    return "\n".join(chunks)


def format_role_env(env: dict[str, Any], *, role: str) -> dict[str, Any]:
    if role == "hybrid":
        key_order = UNION_ENV_KEY_ORDER
    elif role == "prefill":
        key_order = PREFILL_ENV_KEY_ORDER
    else:
        key_order = DECODE_ENV_KEY_ORDER
    return reorder_dict(env, key_order)


def build_env_config(
    prefill_env: dict[str, Any],
    decode_env: dict[str, Any],
) -> dict[str, Any]:
    return reorder_dict(
        {
            "version": "2.0.0",
            "motor_common_env": dict(DEFAULT_ENV_COMMON),
            "motor_controller_env": {},
            "motor_coordinator_env": {},
            "motor_engine_prefill_env": format_role_env(prefill_env, role="prefill"),
            "motor_engine_decode_env": format_role_env(decode_env, role="decode"),
            "motor_kv_cache_store_env": {},
        },
        ENV_CONFIG_KEY_ORDER,
    )


def build_hybrid_env_config(script_env: dict[str, Any]) -> dict[str, Any]:
    return reorder_dict(
        {
            "version": "2.0.0",
            "motor_common_env": dict(DEFAULT_ENV_COMMON),
            "motor_controller_env": {},
            "motor_coordinator_env": {},
            "motor_engine_union_env": format_role_env(script_env, role="hybrid"),
            "motor_kv_cache_store_env": {},
        },
        HYBRID_ENV_CONFIG_KEY_ORDER,
    )


def _normalize_script_text(text: str) -> str:
    text = re.sub(r"\\\s*\r?\n", " ", text)
    text = re.sub(r"\s*\\\s*$", "", text, flags=re.MULTILINE)
    lines = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped:
            lines.append(stripped)
    return " ".join(lines)


def _fold_multiline_quoted_json_flags(text: str) -> str:
    """Collapse multiline single-quoted JSON values (e.g. --kv-transfer-config) to one line."""

    def _collapse(match: re.Match[str]) -> str:
        flag = match.group(1)
        body = match.group(2)
        compact = " ".join(body.split())
        return f"{flag} '{compact}'"

    return re.sub(
        r"(--[\w-]+)\s*'\s*(\{.*?\})\s*'",
        _collapse,
        text,
        flags=re.DOTALL,
    )


def _neutralize_shell_vars_for_parse(text: str) -> str:
    """Replace non-numeric shell vars so shlex can tokenize tutorial scripts."""

    def _replace_braced(match: re.Match[str]) -> str:
        name = match.group(1)
        if name.isdigit():
            return match.group(0)
        if name == "MODEL_PATH":
            return "/placeholder/model"
        return "placeholder"

    def _replace_bare(match: re.Match[str]) -> str:
        name = match.group(1)
        if name.isdigit():
            return match.group(0)
        return "placeholder"

    text = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", _replace_braced, text)
    text = re.sub(r"\$([A-Za-z_][A-Za-z0-9_]*)", _replace_bare, text)
    return text


def _prepare_command_text_for_parse(text: str) -> str:
    normalized = _normalize_script_text(text)
    normalized = _fold_multiline_quoted_json_flags(normalized)
    return _neutralize_shell_vars_for_parse(normalized)


def _substitute_shell_vars(text: str, variables: dict[str, str] | None = None) -> str:
    variables = variables or {}

    def replacer(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return str(variables[name]) if name in variables else match.group(0)

    return re.sub(r"\$(\d+)|\$\{(\d+)\}", replacer, text)


def _expand_inline_assignments(tokens: list[str]) -> list[str]:
    expanded: list[str] = []
    for token in tokens:
        if token.startswith("--") and "=" in token[2:]:
            key, value = token[2:].split("=", 1)
            expanded.extend([f"--{key}", value])
        else:
            expanded.append(token)
    return expanded


def _substitute_tokens(tokens: list[str], variables: dict[str, str] | None) -> list[str]:
    if not variables:
        return tokens
    joined = _substitute_shell_vars(" ".join(shlex.quote(t) for t in tokens), variables)
    return shlex.split(joined, posix=True)


def script_to_cli_tokens(text: str, *, variables: dict[str, str] | None = None) -> list[str]:
    command_text = extract_vllm_command_text(text)
    normalized = _prepare_command_text_for_parse(command_text)
    normalized = _substitute_shell_vars(normalized, variables)
    tokens = shlex.split(normalized, posix=True)
    if tokens and tokens[0] == "vllm":
        tokens = tokens[1:]
    if tokens and tokens[0] == "serve":
        tokens = tokens[1:]
    return _expand_inline_assignments(tokens)


def parse_vllm_serve_command(text: str, *, variables: dict[str, str] | None = None) -> dict[str, Any]:
    tokens = script_to_cli_tokens(text, variables=variables)
    if not tokens:
        raise ValueError("empty vLLM command")
    return cli_to_config(tokens)


def _motor_config_key(cli_key: str) -> str:
    underscored = cli_key.replace("-", "_")
    return underscored if underscored in UNDERSCORE_KEYS else cli_key


def _should_skip_key(cli_key: str, *, include_parallel: bool = False) -> bool:
    normalized = cli_key.replace("-", "_")
    if normalized in SKIP_RUNTIME_KEYS:
        return True
    if not include_parallel and normalized in SKIP_PARALLEL_CLI_KEYS:
        return True
    return False


_KV_EXTRA_PARALLEL_KEYS = frozenset({"dp_size", "tp_size"})


def _strip_parallel_from_tree(node: Any) -> Any:
    """Recursively drop prefill/decode dp_size/tp_size; keep all other nested fields."""
    if isinstance(node, list):
        return [_strip_parallel_from_tree(item) for item in node]
    if not isinstance(node, dict):
        return node
    stripped: dict[str, Any] = {}
    for key, value in node.items():
        if key in ("prefill", "decode"):
            if not isinstance(value, dict):
                continue
            role_extra = {
                field: field_value for field, field_value in value.items() if field not in _KV_EXTRA_PARALLEL_KEYS
            }
            if role_extra:
                stripped[key] = role_extra
            continue
        stripped[key] = _strip_parallel_from_tree(value)
    return stripped


def _strip_parallel_from_kv_extra_config(extra: Any) -> dict[str, Any]:
    """Drop prefill/decode dp_size/tp_size at any nesting depth under kv extra."""
    stripped = _strip_parallel_from_tree(extra)
    return stripped if isinstance(stripped, dict) else {}


def _convert_kv_transfer_config(raw_value: Any) -> dict[str, Any]:
    if isinstance(raw_value, str):
        kv_config = json.loads(raw_value)
    elif isinstance(raw_value, dict):
        kv_config = dict(raw_value)
    else:
        raise ValueError("kv_transfer_config must be a JSON object")
    motor_kv: dict[str, Any] = {
        "kv_connector": kv_config.get("kv_connector"),
        "kv_role": kv_config.get("kv_role"),
        "engine_id": str(kv_config.get("engine_id", "0")),
    }
    if kv_config.get("kv_port") not in (None, ""):
        motor_kv["kv_port"] = str(kv_config.get("kv_port"))
    for key, value in kv_config.items():
        if key in {"kv_connector", "kv_role", "kv_port", "engine_id"}:
            continue
        if key == "kv_connector_extra_config":
            extra = _strip_parallel_from_kv_extra_config(value)
            if extra:
                motor_kv[key] = extra
            continue
        motor_kv[key] = value
    return {k: v for k, v in motor_kv.items() if v is not None}


_JSON_CONFIG_SUFFIXES = ("-config", "_config")


def _normalize_json_config_fields(engine_config: dict[str, Any]) -> None:
    """Coerce stringified JSON blobs (e.g. multiline --additional-config) to objects."""
    for key, value in list(engine_config.items()):
        if not isinstance(value, str):
            continue
        if not any(key.endswith(suffix) for suffix in _JSON_CONFIG_SUFFIXES):
            continue
        parsed = _try_parse_json_text(value)
        if parsed is not None:
            engine_config[key] = parsed


def cli_args_to_engine_config(
    cli_args: dict[str, Any],
    *,
    weight_mount_path: str | None = None,
    overrides: dict[str, Any] | None = None,
    add_profiler_config: bool = True,
    strip_kv_extra_config: bool = True,
    role: str | None = None,
    parallel: dict[str, int] | None = None,
    infer_parallel: bool = True,
    include_parallel_cli: bool = False,
    skip_kv_transfer: bool = False,
) -> dict[str, Any]:
    engine_config: dict[str, Any] = {}
    for cli_key, value in cli_args.items():
        if _should_skip_key(cli_key, include_parallel=include_parallel_cli):
            continue
        config_key = _motor_config_key(cli_key)
        if config_key == "kv_transfer_config":
            if skip_kv_transfer:
                continue
            if strip_kv_extra_config:
                engine_config[config_key] = _convert_kv_transfer_config(value)
                if role == "prefill":
                    engine_config[config_key]["kv_role"] = "kv_producer"
                elif role == "decode":
                    engine_config[config_key]["kv_role"] = "kv_consumer"
            continue
        engine_config[config_key] = value

    _normalize_json_config_fields(engine_config)

    if weight_mount_path:
        engine_config["model"] = weight_mount_path
    elif "model" in engine_config:
        engine_config["model"] = str(engine_config["model"])

    if parallel is None and infer_parallel:
        parallel = try_extract_parallel_from_cli_args(cli_args)

    if parallel is not None:
        engine_role = role or _infer_role_from_cli_args(cli_args)
        apply_engine_parallel(engine_config, role=engine_role, parallel=parallel)

    if add_profiler_config and "profiler-config" not in engine_config:
        engine_config["profiler-config"] = dict(DEFAULT_PROFILER_CONFIG)
    if overrides:
        engine_config.update(overrides)
    return engine_config


def cli_tokens_to_engine_config(
    tokens: list[str],
    *,
    variables: dict[str, str] | None = None,
    weight_mount_path: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if tokens[:1] == ["--"]:
        tokens = tokens[1:]
    tokens = _expand_inline_assignments(_substitute_tokens(tokens, variables))
    return cli_args_to_engine_config(
        cli_to_config(tokens),
        weight_mount_path=weight_mount_path,
        overrides=overrides,
    )


def build_engine_role_config(
    engine_config: dict[str, Any],
    *,
    engine_type: str = "vllm",
    minimal: bool = False,
) -> dict[str, Any]:
    role_cfg: dict[str, Any] = {
        "engine_type": engine_type,
        "engine_config": engine_config,
    }
    if not minimal:
        role_cfg["motor_nodemanger_config"] = {}
    return role_cfg


def _coerce_deploy_value(value: str) -> Any:
    if value.isdigit():
        return int(value)
    try:
        if "." in value:
            return float(value)
    except ValueError:
        pass
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return value


def build_user_config(
    prefill_engine_config: dict[str, Any],
    decode_engine_config: dict[str, Any],
    *,
    deploy_config: dict[str, Any] | None = None,
    minimal_template: bool = False,
) -> dict[str, Any]:
    deploy = dict(DEFAULT_DEPLOY_CONFIG)
    if deploy_config:
        for key, value in deploy_config.items():
            deploy[key] = _coerce_deploy_value(value) if isinstance(value, str) else value
    model_path = (
        deploy.get("weight_mount_path")
        if deploy.get("weight_mount_path") not in (None, "", "/mnt/weight/", "/data01/models/")
        else prefill_engine_config.get("model")
        or decode_engine_config.get("model")
        or deploy.get("weight_mount_path")
        or "/mnt/weight/"
    )
    deploy["weight_mount_path"] = model_path
    return format_user_config(
        {
            "version": "v2.0",
            "motor_deploy_config": deploy,
            "motor_controller_config": {},
            "motor_coordinator_config": {},
            "motor_engine_prefill_config": build_engine_role_config(
                prefill_engine_config,
                minimal=minimal_template,
            ),
            "motor_engine_decode_config": build_engine_role_config(
                decode_engine_config,
                minimal=minimal_template,
            ),
        }
    )


def build_hybrid_user_config(
    engine_config: dict[str, Any],
    *,
    deploy_config: dict[str, Any] | None = None,
    minimal_template: bool = False,
) -> dict[str, Any]:
    deploy = {
        "deploy_mode": "infer_service_set",
        "hybrid_instances_num": 1,
        "single_hybrid_instance_pod_num": 1,
        "hybrid_pod_npu_num": 1,
        "image_name": "",
        "job_id": "mindie-motor",
        "hardware_type": "800I_A3",
        "weight_mount_path": "/mnt/weight/",
    }
    if deploy_config:
        for key, value in deploy_config.items():
            deploy[key] = _coerce_deploy_value(value) if isinstance(value, str) else value
    model_path = (
        deploy.get("weight_mount_path")
        if deploy.get("weight_mount_path") not in (None, "", "/mnt/weight/", "/data01/models/")
        else engine_config.get("model") or deploy.get("weight_mount_path") or "/mnt/weight/"
    )
    deploy["weight_mount_path"] = model_path
    return format_user_config(
        {
            "version": "v2.0",
            "motor_deploy_config": deploy,
            "motor_controller_config": {},
            "motor_coordinator_config": {
                "scheduler_config": {
                    "deploy_mode": "single_node",
                },
            },
            "motor_engine_union_config": build_engine_role_config(
                engine_config,
                minimal=minimal_template,
            ),
        }
    )


def convert_vllm_hybrid_script_to_user_config(
    script: str,
    *,
    variables: dict[str, str] | None = None,
    weight_mount_path: str | None = None,
    hardware_type: str | None = None,
    deploy_config: dict[str, Any] | None = None,
    engine_overrides: dict[str, Any] | None = None,
    minimal_template: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    cli = parse_vllm_serve_command(script, variables=variables)
    dp, tp = extract_hybrid_parallel_from_cli_args(cli)
    if hardware_type:
        dp, tp = remap_hybrid_parallel_for_hardware(dp, tp, hardware_type)

    env_config = build_hybrid_env_config(parse_script_exports(script))
    engine_config = cli_args_to_engine_config(
        cli,
        weight_mount_path=weight_mount_path,
        overrides=engine_overrides,
        include_parallel_cli=True,
        skip_kv_transfer=True,
        add_profiler_config=False,
    )
    engine_config["data_parallel_size"] = dp
    engine_config["tensor_parallel_size"] = tp
    engine_config.setdefault("pipeline_parallel_size", 1)
    engine_config.setdefault("data_parallel_rpc_port", DEFAULT_DP_RPC_PORT)

    merged_deploy = dict(deploy_config or {})
    if weight_mount_path:
        merged_deploy["weight_mount_path"] = weight_mount_path
    if hardware_type:
        merged_deploy = infer_hybrid_motor_deploy_config(
            dp,
            tp,
            hardware_type,
            overrides=merged_deploy or None,
        )
    elif merged_deploy:
        merged_deploy = {
            "deploy_mode": "infer_service_set",
            "hybrid_instances_num": 1,
            "single_hybrid_instance_pod_num": 1,
            "hybrid_pod_npu_num": dp * tp,
            **merged_deploy,
        }

    user_config = build_hybrid_user_config(
        engine_config,
        deploy_config=merged_deploy or None,
        minimal_template=minimal_template,
    )
    if not weight_mount_path:
        apply_manual_fill_placeholders(user_config, hybrid=True)
    return user_config, env_config


def _print_parallel_manual_fill_hint() -> None:
    print(
        f"{_blue_text('[提示]')} 未从 kv-transfer-config 推断出 prefill/decode 的 dp_size/tp_size；"
        "已在 user_config.json 对应字段写入手动填写说明，请按实际情况修改。",
        file=sys.stderr,
    )


def convert_vllm_scripts_to_user_config(
    prefill_script: str,
    decode_script: str,
    *,
    variables: dict[str, str] | None = None,
    weight_mount_path: str | None = None,
    hardware_type: str | None = None,
    deploy_config: dict[str, Any] | None = None,
    prefill_overrides: dict[str, Any] | None = None,
    decode_overrides: dict[str, Any] | None = None,
    minimal_template: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    prefill_cli = parse_vllm_serve_command(prefill_script, variables=variables)
    decode_cli = parse_vllm_serve_command(decode_script, variables=variables)
    script_parallel = try_extract_parallel_from_cli_args(prefill_cli)
    if script_parallel is None:
        script_parallel = try_extract_parallel_from_cli_args(decode_cli)

    parallel: dict[str, int] | None = None
    if script_parallel is not None:
        parallel = remap_parallel_for_hardware(script_parallel, hardware_type) if hardware_type else script_parallel
    else:
        _print_parallel_manual_fill_hint()

    env_config = build_env_config(
        parse_script_exports(prefill_script),
        parse_script_exports(decode_script),
    )

    prefill_engine = cli_args_to_engine_config(
        prefill_cli,
        weight_mount_path=weight_mount_path,
        overrides=prefill_overrides,
        role="prefill",
        parallel=parallel,
        infer_parallel=False,
    )
    decode_engine = cli_args_to_engine_config(
        decode_cli,
        weight_mount_path=weight_mount_path,
        overrides=decode_overrides,
        role="decode",
        parallel=parallel,
        infer_parallel=False,
    )

    merged_deploy = dict(deploy_config or {})
    if weight_mount_path:
        merged_deploy["weight_mount_path"] = weight_mount_path

    if parallel is not None and hardware_type:
        inferred = infer_motor_deploy_config(parallel, hardware_type, overrides=merged_deploy or None)
        merged_deploy = inferred
    elif merged_deploy:
        merged_deploy = {**DEFAULT_DEPLOY_CONFIG, **merged_deploy}
    elif hardware_type:
        hw = normalize_hardware_type(hardware_type)
        preset = dict(HARDWARE_PRESETS[hw])
        preset.pop("cards_per_node", None)
        merged_deploy = {**DEFAULT_DEPLOY_CONFIG, **preset}

    user_config = build_user_config(
        prefill_engine,
        decode_engine,
        deploy_config=merged_deploy or None,
        minimal_template=minimal_template,
    )
    if parallel is None:
        apply_parallel_manual_fill_placeholders(user_config)
        user_config = format_user_config(user_config)
    if not weight_mount_path:
        apply_manual_fill_placeholders(user_config)
    return user_config, env_config


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_output_paths() -> tuple[Path, Path]:
    user_path = Path(DEFAULT_OUTPUT_DIR) / DEFAULT_USER_CONFIG_NAME
    env_path = Path(DEFAULT_OUTPUT_DIR) / DEFAULT_ENV_NAME
    return user_path, env_path


def _blue_text(text: str, *, stream: Any = sys.stderr) -> str:
    """Wrap *text* in blue ANSI codes when writing to a TTY."""
    if hasattr(stream, "isatty") and stream.isatty():
        return f"{_ANSI_BLUE}{text}{_ANSI_RESET}"
    return text


def _print_optional_arg_reminders(
    *,
    weight_path: str | None,
    image_name: str | None,
    deploy_scenario: str,
    hardware_type: str,
) -> None:
    if weight_path and image_name:
        return

    print(
        f"\n{_blue_text('[提示]')} 配置文件生成成功，请基于 user_config.json 内的提示补充两项参数（env.json文件无需修改），完成后可以正常使用。",
        file=sys.stderr,
    )
    print(file=sys.stderr)
    print(
        f"{_blue_text('[推荐]')} 如果您不希望二次修改 user_config.json，可以执行以下全量生成命令：",
        file=sys.stderr,
    )
    print(file=sys.stderr)
    cmd = (
        f"python3 deploy.py --mode general_config --deploy-scenario {deploy_scenario} "
        f"--hardware-type {hardware_type} "
        f"--weight-path <权重路径> --image-name <镜像名称>"
    )
    print(cmd, file=sys.stderr)

    example_weight = "/home/weights/DeepSeek-V4-Flash-w8a8-mtp"
    hw = normalize_hardware_type(hardware_type)
    example_images = {
        "800I_A2": "mindie-motor-vllm:r0.17.0rc1-800I-A2-py311-lts-aarch64",
        "800I_A3": "mindie-motor-vllm:dev-26.1.0.B081-800I-A3-py311-Ubuntu24.04-lts-aarch64",
        "A5": "mindie-motor-vllm:<请填写 A5 镜像名称>",
    }
    example_image = example_images.get(hw, example_images["800I_A3"])
    example_cmd = (
        f"python3 deploy.py --mode general_config --deploy-scenario {deploy_scenario} "
        f"--hardware-type {hardware_type} "
        f"--weight-path {example_weight} "
        f"--image-name {example_image}"
    )
    print(file=sys.stderr)
    print(f"例如：\n{example_cmd}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="从 vLLM 启动脚本生成 Motor 配置。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            f"PD 混部输入: {AUTO_HYBRID_SCRIPT} (--deploy-scenario hybrid)\n"
            f"PD 分离输入: {AUTO_PREFILL_SCRIPT}, {AUTO_DECODE_SCRIPT} "
            f"(--deploy-scenario separate)\n"
            f"输出: {DEFAULT_OUTPUT_DIR}/{DEFAULT_USER_CONFIG_NAME}, "
            f"{DEFAULT_OUTPUT_DIR}/{DEFAULT_ENV_NAME}"
        ),
    )
    parser.add_argument(
        "--deploy-scenario",
        required=True,
        choices=[DEPLOY_SCENARIO_HYBRID, DEPLOY_SCENARIO_SEPARATE],
        help="部署场景: hybrid=PD混部(单脚本); separate=PD分离(prefill+decode双脚本)",
    )
    parser.add_argument(
        "--hardware-type",
        required=True,
        help="硬件类型: A2、A3 或 A5（A5 按每节点 8 卡，输出 hardware_type=850-Atlas-8p-8）",
    )
    parser.add_argument(
        "--weight-path",
        default=None,
        help="模型权重挂载路径（可选，未指定时写入占位说明）",
    )
    parser.add_argument(
        "--image-name",
        default=None,
        help="容器镜像名称（可选，未指定时写入占位说明）",
    )
    args = parser.parse_args(argv)

    deploy_overrides: dict[str, str] = {}
    if args.image_name:
        deploy_overrides["image_name"] = args.image_name

    workdir = Path.cwd()
    user_path, env_path = _default_output_paths()
    try:
        if args.deploy_scenario == DEPLOY_SCENARIO_HYBRID:
            script_path = resolve_hybrid_script(workdir)
            user_config, env_config = convert_vllm_hybrid_script_to_user_config(
                script_path.read_text(encoding="utf-8"),
                hardware_type=args.hardware_type,
                weight_mount_path=args.weight_path,
                deploy_config=deploy_overrides or None,
            )
            print(
                f"已读取: {script_path.name} (scenario=hybrid, hardware={normalize_hardware_type(args.hardware_type)})",
                file=sys.stderr,
            )
        else:
            prefill_path, decode_path = resolve_pd_scripts(workdir)
            user_config, env_config = convert_vllm_scripts_to_user_config(
                prefill_path.read_text(encoding="utf-8"),
                decode_path.read_text(encoding="utf-8"),
                hardware_type=args.hardware_type,
                weight_mount_path=args.weight_path,
                deploy_config=deploy_overrides or None,
            )
            print(
                f"已读取: {prefill_path.name}, {decode_path.name} "
                f"(scenario=separate, hardware={normalize_hardware_type(args.hardware_type)})",
                file=sys.stderr,
            )
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from exc

    if not args.image_name:
        user_config["motor_deploy_config"]["image_name"] = MANUAL_FILL_IMAGE_NAME

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(
        json.dumps(env_config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"已生成: {env_path}", file=sys.stderr)

    user_path.parent.mkdir(parents=True, exist_ok=True)
    user_path.write_text(
        json.dumps(format_user_config(user_config), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"已生成: {user_path}")
    _print_optional_arg_reminders(
        weight_path=args.weight_path,
        image_name=args.image_name,
        deploy_scenario=args.deploy_scenario,
        hardware_type=args.hardware_type,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
