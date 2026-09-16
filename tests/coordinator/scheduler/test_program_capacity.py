# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You may obtain a copy of the License at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING, BUT NOT LIMITED TO, THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# See the Mulan PSL v2 for more details.

"""Tests for vLLM metrics to Program capacity conversion."""

from motor.coordinator.scheduler.runtime.program_capacity import parse_vllm_capacity


def test_parse_vllm_capacity_uses_blocks_and_usage_ratio():
    metrics = """
vllm:cache_config_info{block_size="128",num_gpu_blocks="1000",engine="0"} 1.0
vllm:kv_cache_usage_perc{engine="0"} 0.25
"""

    capacity = parse_vllm_capacity(metrics)

    assert capacity is not None
    assert capacity.total_kv_tokens == 128000
    assert capacity.native_used_kv_tokens == 32000


def test_parse_vllm_capacity_falls_back_when_cache_metadata_is_missing():
    capacity = parse_vllm_capacity("vllm:kv_cache_usage_perc{engine=\"0\"} 0.5", 4096)

    assert capacity is not None
    assert capacity.total_kv_tokens == 4096
    assert capacity.native_used_kv_tokens == 2048


def test_parse_vllm_capacity_returns_none_without_any_capacity_source():
    assert parse_vllm_capacity("# HELP vllm:ready ready") is None
