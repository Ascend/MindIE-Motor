# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""End-to-end tests for ConfigResolver: CP config parsing, hyphen/underscore
compatibility, and engine-specific defaults.
"""

import pytest

from motor.config.resolver import (
    VLLMConfigResolver,
    SGLangConfigResolver,
    ConfigResolver,
)

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _vllm_section(engine_config=None):
    """Build a minimal vLLM engine section dict."""
    return {
        "engine_type": "vllm",
        "engine_config": engine_config or {},
    }


def _sglang_section(engine_config=None):
    """Build a minimal SGLang engine section dict."""
    return {
        "engine_type": "sglang",
        "engine_config": engine_config or {},
    }


# ------------------------------------------------------------------
# Factory
# ------------------------------------------------------------------


def test_factory_returns_vllm_by_default():
    resolver = ConfigResolver({"engine_config": {}})
    assert isinstance(resolver, VLLMConfigResolver)


def test_factory_returns_vllm_for_unknown():
    resolver = ConfigResolver({"engine_type": "unknown", "engine_config": {}})
    assert isinstance(resolver, VLLMConfigResolver)


def test_factory_returns_sglang():
    resolver = ConfigResolver({"engine_type": "sglang", "engine_config": {}})
    assert isinstance(resolver, SGLangConfigResolver)


# ------------------------------------------------------------------
# VLLM — CP config parsing (pcp_size)
# ------------------------------------------------------------------


def test_vllm_cp_underscore():
    resolver = ConfigResolver(_vllm_section({"prefill_context_parallel_size": 2}))
    pc = resolver.get_parallel_config()
    assert pc["pcp_size"] == 2


def test_vllm_cp_hyphen():
    resolver = ConfigResolver(_vllm_section({"prefill-context-parallel-size": 4}))
    pc = resolver.get_parallel_config()
    assert pc["pcp_size"] == 4


def test_vllm_cp_both_forms_are_equivalent():
    """When both underscore and hyphen forms exist, they normalize to the same key.
    The last value wins (dict insertion order).
    """
    resolver = ConfigResolver(
        _vllm_section(
            {
                "prefill_context_parallel_size": 2,
                "prefill-context-parallel-size": 4,
            }
        )
    )
    pc = resolver.get_parallel_config()
    assert pc["pcp_size"] == 4  # normalized, last value wins


# ------------------------------------------------------------------
# SGLang — CP config parsing (pcp_size via context-parallel-size +
# enable-prefill-context-parallel)
# ------------------------------------------------------------------


def test_sglang_cp_hyphen():
    resolver = ConfigResolver(
        _sglang_section(
            {
                "context-parallel-size": 2,
                "enable-prefill-context-parallel": True,
            }
        )
    )
    pc = resolver.get_parallel_config()
    assert pc["pcp_size"] == 2


def test_sglang_cp_underscore():
    resolver = ConfigResolver(
        _sglang_section(
            {
                "context_parallel_size": 2,
                "enable_prefill_context_parallel": True,
            }
        )
    )
    pc = resolver.get_parallel_config()
    assert pc["pcp_size"] == 2


def test_sglang_cp_not_enabled_without_flag():
    """pcp_size is NOT set when enable-prefill-context-parallel is absent."""
    resolver = ConfigResolver(
        _sglang_section(
            {
                "context-parallel-size": 2,
            }
        )
    )
    pc = resolver.get_parallel_config()
    assert pc.get("pcp_size", 1) == 1  # default, key not present in dict


def test_sglang_cp_not_enabled_when_flag_false():
    resolver = ConfigResolver(
        _sglang_section(
            {
                "context-parallel-size": 2,
                "enable-prefill-context-parallel": False,
            }
        )
    )
    pc = resolver.get_parallel_config()
    assert pc.get("pcp_size", 1) == 1


# ------------------------------------------------------------------
# CP affects local_world_size and world_size
# ------------------------------------------------------------------


def test_local_world_size_includes_pcp():
    """local_world_size = pcp * tp * pp"""
    resolver = ConfigResolver(
        _vllm_section(
            {
                "prefill_context_parallel_size": 2,
                "tensor_parallel_size": 2,
                "pipeline_parallel_size": 2,
            }
        )
    )
    pc = resolver.get_parallel_config()
    assert pc["local_world_size"] == 8  # 2 * 2 * 2


def test_local_world_size_pcp_defaults_to_1():
    resolver = ConfigResolver(
        _vllm_section(
            {
                "tensor_parallel_size": 2,
                "pipeline_parallel_size": 3,
            }
        )
    )
    pc = resolver.get_parallel_config()
    assert pc["local_world_size"] == 6  # 1 * 2 * 3


def test_world_size_includes_pcp():
    """world_size = dp * pcp * tp * pp"""
    resolver = ConfigResolver(
        _vllm_section(
            {
                "data_parallel_size": 2,
                "prefill_context_parallel_size": 2,
                "tensor_parallel_size": 2,
                "pipeline_parallel_size": 2,
            }
        )
    )
    pc = resolver.get_parallel_config()
    assert pc["world_size"] == 16  # 2 * 2 * 2 * 2


# ------------------------------------------------------------------
# VLLM — other parallel keys hyphen/underscore compatibility
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,expected",
    [
        ({"data_parallel_size": 3}, ("dp_size", 3)),
        ({"data-parallel-size": 3}, ("dp_size", 3)),
        ({"tensor_parallel_size": 4}, ("tp_size", 4)),
        ({"tensor-parallel-size": 4}, ("tp_size", 4)),
        ({"pipeline_parallel_size": 2}, ("pp_size", 2)),
        ({"pipeline-parallel-size": 2}, ("pp_size", 2)),
        ({"data_parallel_rpc_port": 9100}, ("dp_rpc_port", 9100)),
        ({"data-parallel-rpc-port": 9100}, ("dp_rpc_port", 9100)),
        ({"enable_expert_parallel": True}, ("enable_ep", True)),
        ({"enable-expert-parallel": True}, ("enable_ep", True)),
        ({"cp_kv_cache_interleave_size": 4}, ("cp_kv_cache_interleave_size", 4)),
        ({"cp-kv-cache-interleave-size": 4}, ("cp_kv_cache_interleave_size", 4)),
    ],
)
def test_vllm_parallel_key_variants(key, expected):
    resolver = ConfigResolver(_vllm_section(key))
    pc = resolver.get_parallel_config()
    assert pc[expected[0]] == expected[1]


# ------------------------------------------------------------------
# SGLang — other parallel keys hyphen/underscore compatibility
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,expected",
    [
        ({"dp-size": 3}, ("dp_size", 3)),
        ({"dp_size": 3}, ("dp_size", 3)),
        ({"tp-size": 4}, ("tp_size", 4)),
        ({"tp_size": 4}, ("tp_size", 4)),
        ({"pp-size": 2}, ("pp_size", 2)),
        ({"pp_size": 2}, ("pp_size", 2)),
    ],
)
def test_sglang_parallel_key_variants(key, expected):
    resolver = ConfigResolver(_sglang_section(key))
    pc = resolver.get_parallel_config()
    assert pc[expected[0]] == expected[1]


# ------------------------------------------------------------------
# enable_multi_endpoints — engine-specific defaults
# ------------------------------------------------------------------


def test_vllm_enable_multi_endpoints_default():
    resolver = ConfigResolver(_vllm_section())
    assert resolver.get_enable_multi_endpoints() is True


def test_vllm_enable_multi_endpoints_explicit():
    resolver = ConfigResolver(_vllm_section({"enable_multi_endpoints": False}))
    assert resolver.get_enable_multi_endpoints() is False


def test_sglang_enable_multi_endpoints_default():
    resolver = ConfigResolver(_sglang_section())
    assert resolver.get_enable_multi_endpoints() is False


def test_sglang_enable_multi_endpoints_explicit():
    resolver = ConfigResolver(_sglang_section({"enable_multi_endpoints": True}))
    assert resolver.get_enable_multi_endpoints() is True


# ------------------------------------------------------------------
# Generic keys — hyphen/underscore compatibility
# ------------------------------------------------------------------


def test_vllm_model_name_both_variants():
    r = ConfigResolver(_vllm_section({"served_model_name": "underscore"}))
    assert r.get_model_name() == "underscore"

    r2 = ConfigResolver(_vllm_section({"served-model-name": "hyphen"}))
    assert r2.get_model_name() == "hyphen"


def test_sglang_model_name_both_variants():
    r = ConfigResolver(_sglang_section({"served_model_name": "underscore"}))
    assert r.get_model_name() == "underscore"

    r2 = ConfigResolver(_sglang_section({"served-model-name": "hyphen"}))
    assert r2.get_model_name() == "hyphen"


def test_vllm_npu_mem_utils_both_variants():
    r = ConfigResolver(_vllm_section({"gpu_memory_utilization": 0.8}))
    assert r.get_npu_mem_utils() == 0.8

    r2 = ConfigResolver(_vllm_section({"gpu-memory-utilization": 0.7}))
    assert r2.get_npu_mem_utils() == 0.7


def test_sglang_npu_mem_utils_both_variants():
    r = ConfigResolver(_sglang_section({"mem-fraction-static": 0.8}))
    assert r.get_npu_mem_utils() == 0.8

    r2 = ConfigResolver(_sglang_section({"mem_fraction_static": 0.7}))
    assert r2.get_npu_mem_utils() == 0.7


# ------------------------------------------------------------------
# _get_engine_key helper
# ------------------------------------------------------------------


def test_get_engine_key_returns_first_match():
    r = ConfigResolver(_vllm_section({"a": 1, "b": 2}))
    assert r._get_engine_key("missing", "a", "b") == 1


def test_get_engine_key_skips_missing():
    r = ConfigResolver(_vllm_section({"b": 2}))
    assert r._get_engine_key("a", "b") == 2


def test_get_engine_key_returns_none_when_all_missing():
    r = ConfigResolver(_vllm_section({}))
    assert r._get_engine_key("a", "b") is None
