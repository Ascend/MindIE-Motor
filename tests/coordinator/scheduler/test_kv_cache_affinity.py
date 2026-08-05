# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 license for more details.

"""Tests for KvCacheAffinity"""

import unittest
from unittest.mock import Mock, patch
import json
import os
import tempfile
from pathlib import Path
import pytest
from copy import deepcopy

from motor.common.resources.instance import PDRole
from motor.coordinator.scheduler.policy.kv_cache_affinity import KvCacheAffinityPolicy, TokenizerManager
from motor.coordinator.api_client.conductor_api_client import TENANT_ID
from motor.coordinator.scheduler.policy.utils import (
    preprocess_input,
    exchange_arguments,
    exchange_tool_content,
    exchange_tools,
    content_parts_to_string,
    preprocess_messages_for_standard,
    preprocess_messages_for_dsv4,
)
from motor.common.resources.endpoint import Endpoint, Workload
from motor.common.utils.singleton import ThreadSafeSingleton


def _reset_tokenizer_manager_singleton() -> None:
    """Drop any cached TokenizerManager singleton so each test gets a fresh one."""
    ThreadSafeSingleton._instances.pop(TokenizerManager, None)


def _make_endpoint(ep_id: int, active_tokens: float = 0.0) -> Endpoint:
    """Build a real Endpoint carrying a known workload for load-aware scoring tests."""
    return Endpoint(
        id=ep_id,
        ip="127.0.0.1",
        business_port="8000",
        mgmt_port="8001",
        workload=Workload(active_tokens=active_tokens),
    )


class TestKvCacheAffinityPolicy(unittest.TestCase):
    """Test KvCacheAffinityPolicy Class"""

    def setUp(self):
        """Settings before the test"""
        self.mock_instance_provider = Mock()
        self.policy = KvCacheAffinityPolicy(self.mock_instance_provider)

    def test_init(self):
        """Test initialization."""
        self.assertEqual(self.policy._instance_provider, self.mock_instance_provider)

    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.ConductorApiClient.query_conductor')
    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.TokenizerManager')
    def test_select_endpoint_from_list_with_messages(self, mock_tokenizer_manager, mock_query_conductor):
        """Test select_endpoint_from_list function - use messages"""
        # Preparing Test Data
        mock_instance = Mock()
        mock_instance.id = "instance-1"
        ep = _make_endpoint(1)
        mock_instance.endpoints = {"group": {1: ep}}
        mock_instance.get_all_endpoints.return_value = (ep,)
        instances = [mock_instance]

        mock_req_info = Mock()
        mock_req_info.req_data = {"messages": [{"role": "user", "content": "hello"}]}

        # Mock the return value of TokenizerManager.
        mock_tokenizer = Mock()
        mock_tokenizer.apply_chat_template.return_value = [1, 2, 3]
        mock_tokenizer_manager.return_value = mock_tokenizer

        # Mock ConductorApiClient return value
        mock_query_conductor.return_value = {TENANT_ID: {"vllm-prefill-instance-1": {"GPU": 100, "DP": {"1": 50}}}}

        # Performing the test (default mode = unified)
        result = KvCacheAffinityPolicy.select_endpoint_from_list(instances, mock_req_info)

        # verification result
        self.assertIsNotNone(result)
        self.assertEqual(result[0].id, "instance-1")
        self.assertEqual(result[1].id, 1)

    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.ConductorApiClient.query_conductor')
    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.TokenizerManager')
    def test_select_endpoint_from_list_with_prompt(self, mock_tokenizer_manager, mock_query_conductor):
        """Test select_endpoint_from_list function - 使用 prompt"""
        # Preparing Test Data
        mock_instance = Mock()
        mock_instance.id = "instance-2"
        ep = _make_endpoint(2)
        mock_instance.endpoints = {"group": {2: ep}}
        mock_instance.get_all_endpoints.return_value = (ep,)
        instances = [mock_instance]

        mock_req_info = Mock()
        mock_req_info.req_data = {"prompt": "hello"}

        # Mock the return value of TokenizerManager.
        mock_tokenizer = Mock()
        mock_tokenizer.encode.return_value = [1, 2, 3]
        mock_tokenizer_manager.return_value = mock_tokenizer

        # Mock ConductorApiClient return value
        mock_query_conductor.return_value = {TENANT_ID: {"vllm-prefill-instance-2": {"GPU": 200, "DP": {"2": 100}}}}

        # Performing the test (default mode = unified)
        result = KvCacheAffinityPolicy.select_endpoint_from_list(instances, mock_req_info)

        # verification result
        self.assertIsNotNone(result)
        self.assertEqual(result[0].id, "instance-2")
        self.assertEqual(result[1].id, 2)

    @patch.object(KvCacheAffinityPolicy, '_conductor_block_size', return_value=16)
    @patch('motor.coordinator.api_client.conductor_api_client.ConductorApiClient.query_conductor')
    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.TokenizerManager')
    def test_select_endpoint_from_list_no_messages_or_prompt(
        self, mock_tokenizer_manager, mock_query_conductor, _mock_block_size
    ):
        """No messages/prompt -> empty token ids (< one block) -> fast path routes by load, no query."""
        # Preparing Test Data
        mock_instance = Mock()
        mock_instance.id = "instance-3"
        ep = _make_endpoint(1)
        mock_instance.endpoints = {"group": {1: ep}}
        mock_instance.get_all_endpoints.return_value = (ep,)
        instances = [mock_instance]

        mock_req_info = Mock()
        mock_req_info.req_data = {}

        # Mock the return value of TokenizerManager.
        mock_tokenizer = Mock()
        mock_tokenizer_manager.return_value = mock_tokenizer

        # Performing the test: sub-block prompt -> conductor is not consulted.
        result = KvCacheAffinityPolicy.select_endpoint_from_list(instances, mock_req_info)

        # verification result: routed to the (only) endpoint by load, without a query.
        self.assertIsNotNone(result)
        self.assertEqual(result[1].id, 1)
        mock_query_conductor.assert_not_called()

    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.ConductorApiClient.query_conductor')
    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.TokenizerManager')
    def test_select_endpoint_from_list_no_tenant(self, mock_tokenizer_manager, mock_query_conductor):
        """Test select_endpoint_from_list function - 没有 tenant"""
        # Preparing Test Data
        mock_instance = Mock()
        mock_instance.id = "instance-4"
        instances = [mock_instance]

        mock_req_info = Mock()
        mock_req_info.req_data = {"prompt": "hello"}

        # Mock the return value of TokenizerManager. Prompt >= one block so the conductor is
        # actually queried (the sub-block fast path stays off) and the no-tenant fallback is hit.
        mock_tokenizer = Mock()
        mock_tokenizer.encode.return_value = list(range(2048))
        mock_tokenizer_manager.return_value = mock_tokenizer

        # Mock ConductorApiClient return value（没有 tenant）
        mock_query_conductor.return_value = {}

        # Performing the test
        result = KvCacheAffinityPolicy.select_endpoint_from_list(instances, mock_req_info)

        # verification result
        self.assertIsNone(result)

    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.ConductorApiClient.query_conductor')
    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.TokenizerManager')
    def test_select_endpoint_from_list_no_instance_data(self, mock_tokenizer_manager, mock_query_conductor):
        """Test select_endpoint_from_list function - no instance data"""
        # Preparing Test Data
        mock_instance = Mock()
        mock_instance.id = "instance-5"
        instances = [mock_instance]

        mock_req_info = Mock()
        mock_req_info.req_data = {"prompt": "hello"}

        # Prompt >= one block so the conductor is queried (sub-block fast path off).
        mock_tokenizer = Mock()
        mock_tokenizer.encode.return_value = list(range(2048))
        mock_tokenizer_manager.return_value = mock_tokenizer

        # Mock ConductorApiClient return value
        mock_query_conductor.return_value = {
            TENANT_ID: {"vllm-prefill-instance-6": {"GPU": 100, "DP": {"endpoint-1": 50}}}
        }

        # Performing the test
        result = KvCacheAffinityPolicy.select_endpoint_from_list(instances, mock_req_info)

        # verification result
        self.assertIsNone(result)

    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.ConductorApiClient.query_conductor')
    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.TokenizerManager')
    def test_select_endpoint_from_list_no_selected_instance(self, mock_tokenizer_manager, mock_query_conductor):
        """Test the select_endpoint_from_list method. No instance is selected."""
        # Preparing Test Data
        mock_instance = Mock()
        mock_instance.id = "instance-7"
        instances = [mock_instance]

        mock_req_info = Mock()
        mock_req_info.req_data = {"prompt": "hello"}

        # Prompt >= one block so the conductor is queried (sub-block fast path off).
        mock_tokenizer = Mock()
        mock_tokenizer.encode.return_value = list(range(2048))
        mock_tokenizer_manager.return_value = mock_tokenizer

        # Mock the return value of ConductorApiClient.
        mock_query_conductor.return_value = {TENANT_ID: {"instance-7": {"GPU": 100, "DP": {"endpoint-1": 50}}}}

        # Performing the test
        result = KvCacheAffinityPolicy.select_endpoint_from_list(instances, mock_req_info)

        # verification result
        self.assertIsNone(result)

    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.ConductorApiClient.query_conductor')
    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.TokenizerManager')
    def test_select_endpoint_empty_dp_selects_by_load(self, mock_tokenizer_manager, mock_query_conductor):
        """Empty DP (no cached prefix) no longer returns None: unified picks the endpoint by load."""
        # Preparing Test Data
        mock_instance = Mock()
        mock_instance.id = "instance-8"
        ep = _make_endpoint(1)
        mock_instance.endpoints = {"group": {1: ep}}
        mock_instance.get_all_endpoints.return_value = (ep,)
        instances = [mock_instance]

        mock_req_info = Mock()
        mock_req_info.req_data = {"prompt": "hello"}

        # Mock the return value of TokenizerManager.
        mock_tokenizer = Mock()
        mock_tokenizer.encode.return_value = [1, 2, 3]
        mock_tokenizer_manager.return_value = mock_tokenizer

        # Conductor reports the instance but no cached prefix for any endpoint.
        mock_query_conductor.return_value = {TENANT_ID: {"vllm-prefill-instance-8": {"GPU": 100, "DP": {}}}}

        # Unified scoring still selects the (only) endpoint by load instead of bailing out.
        result = KvCacheAffinityPolicy.select_endpoint_from_list(instances, mock_req_info)

        self.assertIsNotNone(result)
        self.assertEqual(result[1].id, 1)

    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.ConductorApiClient.query_conductor')
    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.TokenizerManager')
    def test_select_endpoint_dpscoring_dict_format(self, mock_tokenizer_manager, mock_query_conductor):
        """New DpBlocks format: DP values are dicts with matched_tokens, not plain ints."""
        ep_a = _make_endpoint(0, active_tokens=50.0)
        ep_b = _make_endpoint(1, active_tokens=50.0)
        mock_instance = Mock()
        mock_instance.id = "inst"
        mock_instance.endpoints = {"group": {0: ep_a, 1: ep_b}}
        mock_instance.get_all_endpoints.return_value = (ep_a, ep_b)
        instances = [mock_instance]

        mock_req_info = Mock()
        mock_req_info.req_data = {"prompt": "hello"}

        mock_tokenizer = Mock()
        mock_tokenizer.encode.return_value = list(range(1000))
        mock_tokenizer_manager.return_value = mock_tokenizer

        # New DpBlocks format: DP values are dicts with matched_tokens
        mock_query_conductor.return_value = {
            TENANT_ID: {
                "vllm-prefill-inst": {
                    "DP": {
                        "0": {"npu_blocks": 6, "cpu_blocks": 0, "disk_blocks": 0, "matched_tokens": 800},
                        "1": {"npu_blocks": 0, "cpu_blocks": 1, "disk_blocks": 0, "matched_tokens": 100},
                    }
                }
            }
        }

        result = KvCacheAffinityPolicy.select_endpoint_from_list(instances, mock_req_info, load_weight=1.0)

        self.assertIsNotNone(result)
        # Both have equal load; ep 0 has longer cached prefix (800 > 100 tokens) → lower prefill cost → chosen.
        self.assertEqual(result[1].id, 0)

    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.ConductorApiClient.query_conductor')
    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.TokenizerManager')
    def test_select_endpoint_mixed_dp_format_old_and_new(self, mock_tokenizer_manager, mock_query_conductor):
        """Backward-compat: old int format and new dict format both work, depending on conductor version."""
        ep_a = _make_endpoint(0, active_tokens=10.0)
        ep_b = _make_endpoint(1, active_tokens=50.0)
        mock_instance = Mock()
        mock_instance.id = "inst"
        mock_instance.endpoints = {"group": {0: ep_a, 1: ep_b}}
        mock_instance.get_all_endpoints.return_value = (ep_a, ep_b)
        instances = [mock_instance]

        mock_req_info = Mock()
        mock_req_info.req_data = {"prompt": "hello"}

        mock_tokenizer = Mock()
        mock_tokenizer.encode.return_value = list(range(500))
        mock_tokenizer_manager.return_value = mock_tokenizer

        # Old conductor: plain int. The matched value is 200 tokens for both endpoints,
        # so affinity ties; then load breaks the tie → ep_a (load=10) wins.
        mock_query_conductor.return_value = {TENANT_ID: {"vllm-prefill-inst": {"DP": {"0": 200, "1": 200}}}}

        result = KvCacheAffinityPolicy.select_endpoint_from_list(instances, mock_req_info, load_weight=1.0)
        self.assertIsNotNone(result)
        self.assertEqual(result[1].id, 0)  # lighter load

    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.ConductorApiClient.query_conductor')
    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.TokenizerManager')
    def test_select_endpoint_load_aware_breaks_tie_by_load(self, mock_tokenizer_manager, mock_query_conductor):
        """load_weight > 0: equally-matched endpoints are tie-broken by lighter workload."""
        ep_a = _make_endpoint(0, active_tokens=100.0)
        ep_b = _make_endpoint(1, active_tokens=10.0)
        mock_instance = Mock()
        mock_instance.id = "inst"
        mock_instance.endpoints = {"group": {0: ep_a, 1: ep_b}}
        mock_instance.get_all_endpoints.return_value = (ep_a, ep_b)
        instances = [mock_instance]

        mock_req_info = Mock()
        mock_req_info.req_data = {"prompt": "hello"}

        mock_tokenizer = Mock()
        mock_tokenizer.encode.return_value = [1, 2, 3]
        mock_tokenizer_manager.return_value = mock_tokenizer

        # Both DP ranks fully cover the (3-token) prompt, so affinity ties and load decides.
        mock_query_conductor.return_value = {TENANT_ID: {"vllm-prefill-inst": {"DP": {"0": 3, "1": 3}}}}

        result = KvCacheAffinityPolicy.select_endpoint_from_list(instances, mock_req_info, load_weight=1.0)

        self.assertIsNotNone(result)
        self.assertEqual(result[0].id, "inst")
        self.assertEqual(result[1].id, 1)  # the less-loaded endpoint

    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.ConductorApiClient.query_conductor')
    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.TokenizerManager')
    def test_select_endpoint_union_conductor_id(self, mock_tokenizer_manager, mock_query_conductor):
        """ROLE_U instances match conductor tenant keys with vllm-union-{id}."""
        ep = _make_endpoint(0, active_tokens=5.0)
        mock_instance = Mock()
        mock_instance.id = "union-1"
        mock_instance.role = PDRole.ROLE_U
        mock_instance.endpoints = {"group": {0: ep}}
        mock_instance.get_all_endpoints.return_value = (ep,)
        instances = [mock_instance]

        mock_req_info = Mock()
        mock_req_info.req_data = {"prompt": "hello"}

        mock_tokenizer = Mock()
        mock_tokenizer.encode.return_value = [1, 2, 3]
        mock_tokenizer_manager.return_value = mock_tokenizer

        mock_query_conductor.return_value = {TENANT_ID: {"vllm-union-union-1": {"DP": {"0": 3}}}}

        result = KvCacheAffinityPolicy.select_endpoint_from_list(instances, mock_req_info, load_weight=1.0)

        self.assertIsNotNone(result)
        self.assertEqual(result[0].id, "union-1")
        self.assertEqual(result[1].id, 0)

    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.ConductorApiClient.query_conductor')
    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.TokenizerManager')
    def test_select_endpoint_load_aware_prefers_more_match_when_load_equal(
        self, mock_tokenizer_manager, mock_query_conductor
    ):
        """load_weight > 0: with equal load, the better-matched endpoint still wins."""
        ep_a = _make_endpoint(0, active_tokens=50.0)
        ep_b = _make_endpoint(1, active_tokens=50.0)
        mock_instance = Mock()
        mock_instance.id = "inst"
        mock_instance.endpoints = {"group": {0: ep_a, 1: ep_b}}
        mock_instance.get_all_endpoints.return_value = (ep_a, ep_b)
        instances = [mock_instance]

        mock_req_info = Mock()
        mock_req_info.req_data = {"prompt": "hello"}

        mock_tokenizer = Mock()
        mock_tokenizer.encode.return_value = list(range(1000))  # long prompt so match matters
        mock_tokenizer_manager.return_value = mock_tokenizer

        # Conductor reports per-DP hits in tokens: rank 0 has 800 of 1000 cached, rank 1 only 100.
        mock_query_conductor.return_value = {TENANT_ID: {"vllm-prefill-inst": {"DP": {"0": 800, "1": 100}}}}

        result = KvCacheAffinityPolicy.select_endpoint_from_list(instances, mock_req_info, load_weight=1.0)

        self.assertIsNotNone(result)
        self.assertEqual(result[1].id, 0)  # endpoint with the longer cached prefix

    def test_select_instance(self):
        """Test _select_instance function"""
        result = self.policy._select_instance()
        self.assertIsNone(result)

    def test_select_endpoint(self):
        """Test _select_endpoint function"""
        mock_instance = Mock()
        result = self.policy._select_endpoint(mock_instance)
        self.assertIsNone(result)


class TestKvCacheAffinityTokenizationUtils(unittest.TestCase):
    def test_content_parts_to_string_extracts_refusal_and_thinking(self):
        content = [
            {"type": "text", "text": "hello"},
            {"type": "refusal", "refusal": "I'm sorry"},
            {"type": "thinking", "thinking": "step1"},
            {"type": "thinking", "reasoning_content": "step2"},
            {"type": "image_url", "image_url": {"url": "x"}},
        ]
        s = content_parts_to_string(content)
        self.assertEqual(
            s,
            "hello\nI'm sorry\nstep1\nstep2\n[Unsupported image_url]",
        )

    def test_preprocess_messages_for_standard_flattens_and_copies(self):
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "content": [{"type": "refusal", "refusal": "no"}]},
        ]
        processed = preprocess_messages_for_standard(messages)
        self.assertIsNot(processed, messages)
        self.assertEqual(processed[0]["content"], "hi")
        self.assertEqual(processed[1]["content"], "no")
        # Original input must remain unchanged (deepcopy semantics).
        self.assertIsInstance(messages[0]["content"], list)

    def test_preprocess_messages_for_dsv4_flattens_messages_and_sorts_tools(self):
        messages = [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "foo", "arguments": "{\"a\": 1}"},
                    }
                ],
            }
        ]
        tools = [
            {
                "type": "function",
                "function": {"parameters": {}, "description": "d", "name": "foo"},
            }
        ]
        processed_messages, processed_tools = preprocess_messages_for_dsv4(messages, tools)
        self.assertEqual(processed_messages[0]["content"], "ok")
        self.assertIsInstance(processed_messages[0]["tool_calls"][0]["function"]["arguments"], dict)
        # Tools should be a copy and function keys should be sorted by priority (name, description, parameters).
        self.assertIsNot(processed_tools, tools)
        self.assertEqual(list(processed_tools[0]["function"].keys()), ["name", "description", "parameters"])


class TestTokenizerManagerDsv4(unittest.TestCase):
    def _make_manager(self, tokenizer: Mock, *, is_dsv4: bool) -> TokenizerManager:
        # Bypass singleton init (which tries to load real tokenizers / config).
        manager = TokenizerManager.__new__(TokenizerManager)
        manager.tokenizer = tokenizer
        manager._is_dsv4 = is_dsv4
        manager.openai_standard = os.environ.get("OPENAI_STANDARD", "STANDARD")
        return manager

    def test_build_dsv4_chat_template_kwargs(self):
        build = TokenizerManager._build_dsv4_chat_template_kwargs

        self.assertEqual(build(None), {"tokenize": True, "drop_thinking": True})

        req_data = {"reasoning_effort": "none"}
        self.assertEqual(
            build(req_data),
            {"tokenize": True, "drop_thinking": True, "reasoning_effort": "none", "enable_thinking": False},
        )

        req_data = {"reasoning_effort": "medium", "chat_template_kwargs": {"foo": 1}}
        self.assertEqual(
            build(req_data),
            {
                "tokenize": True,
                "drop_thinking": True,
                "reasoning_effort": "medium",
                "foo": 1,
                "enable_thinking": True,
            },
        )

        # If caller already supplied enable_thinking, we must not override it.
        req_data = {
            "reasoning_effort": "none",
            "chat_template_kwargs": {"enable_thinking": True},
        }
        self.assertEqual(
            build(req_data),
            {"tokenize": True, "drop_thinking": True, "reasoning_effort": "none", "enable_thinking": True},
        )

    def test_apply_chat_template_dsv4_passes_preprocessed_inputs_and_kwargs(self):
        tokenizer = Mock()
        tokenizer.apply_chat_template.return_value = [1, 2, 3]
        manager = self._make_manager(tokenizer, is_dsv4=True)

        messages = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        tools = [{"type": "function", "function": {"parameters": {}, "description": "d", "name": "foo"}}]
        req_data = {"reasoning_effort": "none"}

        out = manager._apply_chat_template_dsv4(messages, tools, req_data)
        self.assertEqual(out, [1, 2, 3])

        _args, kwargs = tokenizer.apply_chat_template.call_args
        self.assertEqual(kwargs["reasoning_effort"], "none")
        self.assertEqual(kwargs["enable_thinking"], False)
        self.assertEqual(kwargs["tokenize"], True)
        self.assertEqual(kwargs["drop_thinking"], True)
        # Messages content should be flattened to string before reaching tokenizer.
        self.assertEqual(_args[0][0]["content"], "hi")
        # Tools should be forwarded (and preprocessed) as well.
        self.assertEqual(kwargs["tools"][0]["function"]["name"], "foo")

    def test_apply_chat_template_dsv4_encodes_string_result(self):
        tokenizer = Mock()
        tokenizer.apply_chat_template.return_value = "PROMPT"
        tokenizer.encode.return_value = [9, 9]
        manager = self._make_manager(tokenizer, is_dsv4=True)

        out = manager._apply_chat_template_dsv4([{"role": "user", "content": "hi"}], None, None)
        self.assertEqual(out, [9, 9])
        tokenizer.encode.assert_called_once_with("PROMPT", add_special_tokens=False)

    def test_apply_chat_template_dsv4_primary_failure_fail_closed(self):
        tokenizer = Mock()
        tokenizer.apply_chat_template.side_effect = RuntimeError("boom")
        manager = self._make_manager(tokenizer, is_dsv4=True)

        out = manager.apply_chat_template([{"role": "user", "content": "hi"}], None, None)
        self.assertEqual(out, [])
        self.assertEqual(tokenizer.apply_chat_template.call_count, 1)

    def test_is_deepseek_v4_model_detects_from_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg = tmp_path / "config.json"

            cfg.write_text(json.dumps({"model_type": "deepseek_v4"}), encoding="utf-8")
            self.assertTrue(TokenizerManager._is_deepseek_v4_model(str(tmp_path)))

            cfg.write_text(json.dumps({"architectures": ["DeepseekV4ForCausalLM"]}), encoding="utf-8")
            self.assertTrue(TokenizerManager._is_deepseek_v4_model(str(tmp_path)))

        with tempfile.TemporaryDirectory() as tmp2:
            # No config.json
            self.assertFalse(TokenizerManager._is_deepseek_v4_model(tmp2))

    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.ConductorApiClient.query_conductor')
    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.TokenizerManager')
    def test_select_endpoint_load_aware_no_instance_data(self, mock_tokenizer_manager, mock_query_conductor):
        """load_weight > 0: with no matching instance data, fall back (return None)."""
        mock_instance = Mock()
        mock_instance.id = "inst"
        instances = [mock_instance]

        mock_req_info = Mock()
        mock_req_info.req_data = {"prompt": "hello"}

        # Prompt >= one block so the conductor is queried (sub-block fast path off).
        mock_tokenizer = Mock()
        mock_tokenizer.encode.return_value = list(range(2048))
        mock_tokenizer_manager.return_value = mock_tokenizer

        mock_query_conductor.return_value = {TENANT_ID: {"vllm-prefill-other": {"DP": {"0": 1}}}}

        result = KvCacheAffinityPolicy.select_endpoint_from_list(instances, mock_req_info, load_weight=1.0)

        self.assertIsNone(result)

    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.ConductorApiClient.query_conductor')
    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.TokenizerManager')
    def test_select_candidates_returns_topk_ranked_by_score(self, mock_tokenizer_manager, mock_query_conductor):
        """select_endpoint_candidates_from_list returns up to top_k candidates ranked best-first."""
        # Equal cached prefix on all -> affinity ties -> ranked purely by load ascending.
        ep_a = _make_endpoint(0, active_tokens=100.0)
        ep_b = _make_endpoint(1, active_tokens=10.0)
        ep_c = _make_endpoint(2, active_tokens=50.0)
        mock_instance = Mock()
        mock_instance.id = "inst"
        mock_instance.endpoints = {"group": {0: ep_a, 1: ep_b, 2: ep_c}}
        mock_instance.get_all_endpoints.return_value = (ep_a, ep_b, ep_c)
        instances = [mock_instance]

        mock_req_info = Mock()
        mock_req_info.req_data = {"prompt": "hello"}

        mock_tokenizer = Mock()
        mock_tokenizer.encode.return_value = [1, 2, 3]
        mock_tokenizer_manager.return_value = mock_tokenizer

        mock_query_conductor.return_value = {TENANT_ID: {"vllm-prefill-inst": {"DP": {"0": 3, "1": 3, "2": 3}}}}

        ranked = KvCacheAffinityPolicy.select_endpoint_candidates_from_list(
            instances, mock_req_info, load_weight=1.0, top_k=2
        )

        self.assertIsNotNone(ranked)
        self.assertEqual(len(ranked), 2)
        # Best-first by load: ep_b (10) then ep_c (50); ep_a (100) drops off at top_k=2.
        self.assertEqual([ep.id for _inst, ep, _score in ranked], [1, 2])

    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.ConductorApiClient.query_conductor')
    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.TokenizerManager')
    def test_default_mode_is_unified_and_load_aware(self, mock_tokenizer_manager, mock_query_conductor):
        """With no mode/knobs, the default is unified scoring with load_weight=1.0 (load-aware)."""
        # Equal cached prefix on both endpoints -> affinity ties -> load (default weight 1.0) decides.
        ep_a = _make_endpoint(0, active_tokens=100.0)
        ep_b = _make_endpoint(1, active_tokens=10.0)
        mock_instance = Mock()
        mock_instance.id = "inst"
        mock_instance.endpoints = {"group": {0: ep_a, 1: ep_b}}
        mock_instance.get_all_endpoints.return_value = (ep_a, ep_b)
        instances = [mock_instance]

        mock_req_info = Mock()
        mock_req_info.req_data = {"prompt": "hello"}
        mock_tokenizer = Mock()
        mock_tokenizer.encode.return_value = [1, 2, 3]
        mock_tokenizer_manager.return_value = mock_tokenizer
        mock_query_conductor.return_value = {TENANT_ID: {"vllm-prefill-inst": {"DP": {"0": 3, "1": 3}}}}

        # No mode, no load params -> defaults (unified, load_weight=1.0) -> the lighter endpoint wins.
        result = KvCacheAffinityPolicy.select_endpoint_from_list(instances, mock_req_info)

        self.assertIsNotNone(result)
        self.assertEqual(result[1].id, 1)

    @patch.object(KvCacheAffinityPolicy, '_conductor_block_size', return_value=16)
    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.ConductorApiClient.query_conductor')
    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.TokenizerManager')
    def test_short_prompt_skips_conductor_and_routes_by_load(
        self, mock_tokenizer_manager, mock_query_conductor, _mock_block_size
    ):
        """A prompt shorter than one block skips the conductor query and selects by load."""
        ep_a = _make_endpoint(0, active_tokens=100.0)
        ep_b = _make_endpoint(1, active_tokens=10.0)
        mock_instance = Mock()
        mock_instance.id = "inst"
        mock_instance.endpoints = {"group": {0: ep_a, 1: ep_b}}
        mock_instance.get_all_endpoints.return_value = (ep_a, ep_b)
        instances = [mock_instance]

        mock_req_info = Mock()
        mock_req_info.req_data = {"prompt": "hi"}
        mock_tokenizer = Mock()
        mock_tokenizer.encode.return_value = [1, 2, 3]  # 3 tokens < block_size (16)
        mock_tokenizer_manager.return_value = mock_tokenizer

        result = KvCacheAffinityPolicy.select_endpoint_from_list(instances, mock_req_info)

        # No network round-trip, and the all-zero match map leaves load to decide -> lighter wins.
        mock_query_conductor.assert_not_called()
        self.assertIsNotNone(result)
        self.assertEqual(result[1].id, 1)

    @patch.object(KvCacheAffinityPolicy, '_conductor_block_size', return_value=16)
    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.ConductorApiClient.query_conductor')
    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.TokenizerManager')
    def test_full_block_prompt_still_queries_conductor(
        self, mock_tokenizer_manager, mock_query_conductor, _mock_block_size
    ):
        """A prompt of at least one block still consults the conductor (fast path not taken)."""
        ep_a = _make_endpoint(0, active_tokens=100.0)
        ep_b = _make_endpoint(1, active_tokens=100.0)
        mock_instance = Mock()
        mock_instance.id = "inst"
        mock_instance.endpoints = {"group": {0: ep_a, 1: ep_b}}
        mock_instance.get_all_endpoints.return_value = (ep_a, ep_b)
        instances = [mock_instance]

        mock_req_info = Mock()
        mock_req_info.req_data = {"prompt": "hi"}
        mock_tokenizer = Mock()
        mock_tokenizer.encode.return_value = list(range(32))  # 32 tokens >= block_size (16)
        mock_tokenizer_manager.return_value = mock_tokenizer
        # ep_a has the longer cached prefix; with equal load it must win, proving the query ran.
        mock_query_conductor.return_value = {TENANT_ID: {"vllm-prefill-inst": {"DP": {"0": 16, "1": 0}}}}

        result = KvCacheAffinityPolicy.select_endpoint_from_list(instances, mock_req_info)

        mock_query_conductor.assert_called_once()
        self.assertIsNotNone(result)
        self.assertEqual(result[1].id, 0)

    @staticmethod
    def _three_endpoint_instance():
        """One instance, three endpoints with distinct (load, affinity) profiles for gating tests."""
        # id: (active_tokens=load, matched tokens)
        #   0 -> highest affinity (1000) but highest load (100)
        #   1 -> lowest load (10), modest affinity (500)
        #   2 -> 2nd lowest load (20), high affinity (800)
        ep0 = _make_endpoint(0, active_tokens=100.0)
        ep1 = _make_endpoint(1, active_tokens=10.0)
        ep2 = _make_endpoint(2, active_tokens=20.0)
        mock_instance = Mock()
        mock_instance.id = "inst"
        mock_instance.endpoints = {"group": {0: ep0, 1: ep1, 2: ep2}}
        mock_instance.get_all_endpoints.return_value = (ep0, ep1, ep2)
        conductor = {TENANT_ID: {"vllm-prefill-inst": {"DP": {"0": 1000, "1": 500, "2": 800}}}}
        return [mock_instance], conductor

    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.ConductorApiClient.query_conductor')
    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.TokenizerManager')
    def test_select_endpoint_load_gated_picks_best_affinity_among_least_loaded(
        self, mock_tokenizer_manager, mock_query_conductor
    ):
        """load_gate_topn=2: choose among the 2 least-loaded endpoints, then the longest prefix."""
        instances, conductor = self._three_endpoint_instance()
        mock_req_info = Mock()
        mock_req_info.req_data = {"prompt": "hello"}
        mock_tokenizer = Mock()
        mock_tokenizer.encode.return_value = list(range(1000))
        mock_tokenizer_manager.return_value = mock_tokenizer
        mock_query_conductor.return_value = conductor

        result = KvCacheAffinityPolicy.select_endpoint_from_list(
            instances, mock_req_info, mode="load_gated", load_gate_topn=2
        )

        # 2 least-loaded are ep1(10) and ep2(20); ep0 (highest affinity) is gated out by load.
        # Among the survivors ep2 has the longer prefix (800 > 500) -> chosen.
        self.assertIsNotNone(result)
        self.assertEqual(result[1].id, 2)

    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.ConductorApiClient.query_conductor')
    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.TokenizerManager')
    def test_select_endpoint_load_gated_topn1_is_pure_lowest_load(self, mock_tokenizer_manager, mock_query_conductor):
        """load_gate_topn=1: the gate alone decides -> the single least-loaded endpoint wins."""
        instances, conductor = self._three_endpoint_instance()
        mock_req_info = Mock()
        mock_req_info.req_data = {"prompt": "hello"}
        mock_tokenizer = Mock()
        mock_tokenizer.encode.return_value = list(range(1000))
        mock_tokenizer_manager.return_value = mock_tokenizer
        mock_query_conductor.return_value = conductor

        result = KvCacheAffinityPolicy.select_endpoint_from_list(
            instances, mock_req_info, mode="load_gated", load_gate_topn=1
        )

        self.assertIsNotNone(result)
        self.assertEqual(result[1].id, 1)  # lowest load, affinity irrelevant

    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.ConductorApiClient.query_conductor')
    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.TokenizerManager')
    def test_select_endpoint_load_gated_ignores_load_weight(self, mock_tokenizer_manager, mock_query_conductor):
        """In load_gated mode the gate decides; load_weight (a unified-only knob) is ignored."""
        instances, conductor = self._three_endpoint_instance()
        mock_req_info = Mock()
        mock_req_info.req_data = {"prompt": "hello"}
        mock_tokenizer = Mock()
        mock_tokenizer.encode.return_value = list(range(1000))
        mock_tokenizer_manager.return_value = mock_tokenizer
        mock_query_conductor.return_value = conductor

        # The unified score would pick ep0 (0 + 100 = 100, vs ep1 510, ep2 220). load_gated must
        # still pick ep2 regardless of load_weight, proving the mode selection wins.
        result = KvCacheAffinityPolicy.select_endpoint_from_list(
            instances, mock_req_info, mode="load_gated", load_weight=1.0, load_gate_topn=2
        )

        self.assertIsNotNone(result)
        self.assertEqual(result[1].id, 2)

    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.ConductorApiClient.query_conductor')
    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.TokenizerManager')
    def test_select_candidates_load_gated_returns_topk_within_gate(self, mock_tokenizer_manager, mock_query_conductor):
        """load_gated top_k ranks within the least-loaded set by longest prefix, best-first."""
        instances, conductor = self._three_endpoint_instance()
        mock_req_info = Mock()
        mock_req_info.req_data = {"prompt": "hello"}
        mock_tokenizer = Mock()
        mock_tokenizer.encode.return_value = list(range(1000))
        mock_tokenizer_manager.return_value = mock_tokenizer
        mock_query_conductor.return_value = conductor

        # 2 least-loaded are ep1(load 10, matched 500) and ep2(load 20, matched 800); ep0 (load
        # 100) is gated out. Within the gate, rank by matched desc -> ep2 then ep1.
        ranked = KvCacheAffinityPolicy.select_endpoint_candidates_from_list(
            instances, mock_req_info, mode="load_gated", load_gate_topn=2, top_k=2
        )

        self.assertIsNotNone(ranked)
        self.assertEqual([ep.id for _inst, ep, _score in ranked], [2, 1])

    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.TokenizerManager')
    def test_ensure_token_ids_caches_and_reuses(self, mock_tokenizer_manager):
        """_ensure_token_ids tokenizes once, caches on req_info, and reuses on later calls."""
        mock_tokenizer = Mock()
        mock_tokenizer.encode.return_value = [7, 8, 9]
        mock_tokenizer_manager.return_value = mock_tokenizer

        req = Mock()
        req.req_data = {"prompt": "hi"}
        req.token_ids = None  # not yet tokenized

        ids1 = KvCacheAffinityPolicy._ensure_token_ids(req)
        self.assertEqual(ids1, [7, 8, 9])
        self.assertEqual(req.token_ids, [7, 8, 9])  # cached on req_info for load accounting
        mock_tokenizer.encode.assert_called_once()

        # Second call hits the cache -> tokenizer is not invoked again.
        ids2 = KvCacheAffinityPolicy._ensure_token_ids(req)
        self.assertEqual(ids2, [7, 8, 9])
        mock_tokenizer.encode.assert_called_once()


class TestKvAffinityFallbackConsolidation(unittest.TestCase):
    """Consolidated kv_cache_affinity -> load_balance -> round_robin fallback chain (#5)."""

    _AFFINITY = (
        "motor.coordinator.scheduler.runtime.scheduler_client."
        "KvCacheAffinityPolicy.select_endpoint_candidates_from_list"
    )
    _RR = "motor.coordinator.scheduler.runtime.scheduler_client.RoundRobinPolicy.select_instance_from_list"

    @staticmethod
    def _make_client():
        from motor.coordinator.scheduler.runtime.scheduler_client import (
            AsyncSchedulerClient,
            SchedulerClientConfig,
        )

        return AsyncSchedulerClient(SchedulerClientConfig(scheduler_type="kv_cache_affinity"))

    def test_invalid_mode_warns_and_falls_back_to_unified(self):
        """An invalid kv_affinity_mode logs a warning and falls back to unified (not silent)."""
        from motor.coordinator.scheduler.runtime.scheduler_client import (
            AsyncSchedulerClient,
            SchedulerClientConfig,
        )
        from motor.config.coordinator import KV_AFFINITY_MODE_UNIFIED

        with patch("motor.coordinator.scheduler.runtime.scheduler_client.logger.warning") as warn:
            client = AsyncSchedulerClient(
                SchedulerClientConfig(
                    scheduler_type="kv_cache_affinity",
                    kv_affinity_mode="bogus",
                )
            )
        self.assertEqual(client._kv_affinity_mode, KV_AFFINITY_MODE_UNIFIED)
        self.assertTrue(
            any("kv_affinity_mode" in str(c.args[0]) for c in warn.call_args_list),
            "expected a warning mentioning kv_affinity_mode",
        )

    def test_prefill_affinity_hit_uses_affinity(self):
        """ROLE_P with a conductor match returns the ranked affinity candidates, no fallback."""
        from motor.coordinator.scheduler.runtime.zmq_protocol import (
            CANDIDATE_POLICY_KV_CACHE_AFFINITY,
        )

        client = self._make_client()
        inst, ep = Mock(), Mock()
        req = Mock()
        req.req_data = {"prompt": "x"}
        ranked = [(inst, ep, 0.0)]
        with patch(self._AFFINITY, return_value=ranked):
            cands, policy = client._select_endpoint_candidates_from_list_with_policy(
                [Mock()], PDRole.ROLE_P, req, top_k=1
            )
        self.assertEqual(policy, CANDIDATE_POLICY_KV_CACHE_AFFINITY)
        self.assertEqual(cands, ranked)

    def test_prefill_affinity_miss_falls_back_to_load_balance(self):
        """ROLE_P with no conductor match falls through to the single load_balance fallback."""
        from motor.coordinator.scheduler.runtime.zmq_protocol import CANDIDATE_POLICY_LOAD_BALANCE

        client = self._make_client()
        inst, ep = Mock(), Mock()
        req = Mock()
        req.req_data = {"prompt": "x"}
        with (
            patch(self._AFFINITY, return_value=None),
            patch.object(
                client,
                "_select_endpoint_candidates_by_load_balance",
                return_value=[(inst, ep, 1.0)],
            ) as lb,
        ):
            cands, policy = client._select_endpoint_candidates_from_list_with_policy(
                [Mock()], PDRole.ROLE_P, req, top_k=1
            )
        self.assertEqual(policy, CANDIDATE_POLICY_LOAD_BALANCE)
        self.assertEqual(cands, [(inst, ep, 1.0)])
        lb.assert_called_once()

    def test_non_prefill_role_uses_load_balance_without_affinity(self):
        """Non-prefill roles never consult conductor affinity; they use the same fallback path."""
        from motor.coordinator.scheduler.runtime.zmq_protocol import CANDIDATE_POLICY_LOAD_BALANCE

        client = self._make_client()
        inst, ep = Mock(), Mock()
        req = Mock()
        req.req_data = {}
        with (
            patch(self._AFFINITY) as affinity,
            patch.object(
                client,
                "_select_endpoint_candidates_by_load_balance",
                return_value=[(inst, ep, 2.0)],
            ),
        ):
            cands, policy = client._select_endpoint_candidates_from_list_with_policy(
                [Mock()], PDRole.ROLE_D, req, top_k=1
            )
        affinity.assert_not_called()
        self.assertEqual(policy, CANDIDATE_POLICY_LOAD_BALANCE)
        self.assertEqual(cands, [(inst, ep, 2.0)])

    def test_load_balance_empty_falls_back_to_round_robin(self):
        """When load_balance yields nothing, the chain ends at round_robin (unchanged behavior)."""
        from motor.coordinator.scheduler.runtime.zmq_protocol import CANDIDATE_POLICY_ROUND_ROBIN

        client = self._make_client()
        inst, ep = Mock(), Mock()
        req = Mock()
        req.req_data = {"prompt": "x"}
        with (
            patch(self._AFFINITY, return_value=None),
            patch.object(client, "_select_endpoint_candidates_by_load_balance", return_value=[]),
            patch(self._RR, return_value=(inst, 1)),
            patch.object(client, "_select_endpoint_for_instance", return_value=(inst, ep)),
        ):
            cands, policy = client._select_endpoint_candidates_from_list_with_policy(
                [Mock()], PDRole.ROLE_P, req, top_k=1
            )
        self.assertEqual(policy, CANDIDATE_POLICY_ROUND_ROBIN)
        self.assertEqual(cands, [(inst, ep, 0.0)])


class TestTokenizerManagerFunction(unittest.TestCase):
    """Test TokenizerManager class"""

    def setUp(self):
        _reset_tokenizer_manager_singleton()

    def tearDown(self):
        _reset_tokenizer_manager_singleton()

    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.CoordinatorConfig')
    @patch('transformers.AutoTokenizer')
    def test_init_with_model_path(self, mock_auto_tokenizer, mock_config_class):
        """Test tokenizer manager"""
        mock_config = Mock()
        mock_config.scheduler_config.kv_conductor_config.conductor_service = "test_service"
        mock_config.scheduler_config.kv_conductor_config.model_path = "/path/to/model"
        mock_config_class.return_value = mock_config

        # Mock tokenizer
        mock_tokenizer = Mock()
        mock_tokenizer.apply_chat_template.return_value = [1, 2, 3]
        mock_tokenizer.encode.return_value = [4, 5, 6]
        mock_auto_tokenizer.from_pretrained.return_value = mock_tokenizer

        # Create TokenizerManager
        tokenizer_manager = TokenizerManager(mock_config)

        # Verifying Initialization
        self.assertTrue(hasattr(tokenizer_manager, '_initialized'))
        self.assertEqual(tokenizer_manager.tokenizer, mock_tokenizer)

        # Performing the test
        result = tokenizer_manager.apply_chat_template([{"role": "user", "content": "hello"}])

        # verification result
        self.assertEqual(result, [1, 2, 3])

        # Performing the test
        result = tokenizer_manager.encode("hello")

        # verification result
        self.assertEqual(result, [4, 5, 6])

        # Set tokenizer None
        tokenizer_manager.tokenizer = None

        # Performing the test
        result = tokenizer_manager.apply_chat_template([{"role": "user", "content": "hello"}])

        # verification result
        self.assertEqual(result, [])

        # Performing the test
        result = tokenizer_manager.encode("hello")

        # verification result
        self.assertEqual(result, [])

    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.CoordinatorConfig')
    @patch('transformers.AutoTokenizer')
    def test_dsv4_tokenizer_only_for_vllm_engine(self, mock_auto_tokenizer, mock_config_class):
        """DeepSeek V4 vLLM tokenizer must only be used when engine_type=vllm."""
        mock_config = Mock()
        mock_config.prefill_kv_event_config.conductor_service = "test_service"
        mock_config.prefill_kv_event_config.model_path = "/path/to/model"
        mock_config.prefill_kv_event_config.engine_type = "sglang"
        mock_config.tracer_config.endpoint = ""
        mock_config_class.return_value = mock_config

        # If the code accidentally tries to import vllm.tokenizers.deepseek_v4 on sglang,
        # environments without vllm installed would crash. We assert we fall back to transformers.
        with patch.object(TokenizerManager, "_is_deepseek_v4_model", return_value=True):
            mock_tokenizer = Mock()
            mock_auto_tokenizer.from_pretrained.return_value = mock_tokenizer
            manager = TokenizerManager(mock_config)

        self.assertIs(manager.tokenizer, mock_tokenizer)
        self.assertFalse(manager._is_dsv4)
        mock_auto_tokenizer.from_pretrained.assert_called_once()


class TestTokenizerManagerInitialize(unittest.TestCase):
    """Test TokenizerManager class"""

    def setUp(self):
        """Test setting"""
        _reset_tokenizer_manager_singleton()

    def tearDown(self):
        _reset_tokenizer_manager_singleton()

    @patch('motor.coordinator.scheduler.policy.kv_cache_affinity.CoordinatorConfig')
    def test_init_with_empty_conductor_service(self, mock_config_class):
        """Test initialize - null conductor_service"""
        mock_config = Mock()
        mock_config.scheduler_config.kv_conductor_config.conductor_service = ""
        mock_config_class.return_value = mock_config

        # Create TokenizerManager
        tokenizer_manager = TokenizerManager(mock_config)

        # Verifying Initialization
        self.assertTrue(hasattr(tokenizer_manager, '_initialized'))
        self.assertIsNone(tokenizer_manager.tokenizer)

    def test_singleton_pattern(self):
        """Test singleton instance"""
        # First creation
        instance1 = TokenizerManager()

        # Second creation
        instance2 = TokenizerManager()

        # Verify that the instances are the same.
        self.assertIs(instance1, instance2)


class TestExchangeArguments:
    """Test exchange_arguments function"""

    def test_valid_tool_call_arguments_string(self):
        """Test: Valid tool call parameter string converted to JSON object"""
        message = {"tool_calls": [{"function": {"arguments": '{"city": "Beijing", "temperature": 25}'}}]}
        exchange_arguments(message)

        arguments = message["tool_calls"][0]["function"]["arguments"]
        assert isinstance(arguments, dict)
        assert arguments == {"city": "Beijing", "temperature": 25}

    def test_no_tool_calls_key(self):
        """Test: message not have tool_calls key"""
        message = {"role": "user", "content": "Hello"}
        original = deepcopy(message)
        exchange_arguments(message)
        assert message == original

    def test_tool_calls_missing_function(self):
        """Test: tool_calls not have function key"""
        message = {"tool_calls": [{"id": "call_123", "type": "function"}]}
        original = deepcopy(message)
        exchange_arguments(message)
        assert message == original

    def test_arguments_already_dict(self):
        """Test: arguments is dict"""
        message = {"tool_calls": [{"function": {"arguments": {"city": "Shanghai"}}}]}
        exchange_arguments(message)
        assert message["tool_calls"][0]["function"]["arguments"] == {"city": "Shanghai"}

    def test_invalid_json_string(self):
        """Test: invalid json string"""
        message = {"tool_calls": [{"function": {"arguments": '{"city": "Beijing", invalid json}'}}]}
        with pytest.raises(json.JSONDecodeError):
            exchange_arguments(message)

    def test_multiple_tool_calls(self):
        """Test: multiple tool calls"""
        message = {
            "tool_calls": [
                {"function": {"arguments": '{"tool": "tool1", "value": 1}'}},
                {"function": {"arguments": '{"tool": "tool2", "value": 2}'}},
            ]
        }
        exchange_arguments(message)

        for i, tool in enumerate(message["tool_calls"]):
            assert isinstance(tool["function"]["arguments"], dict)
            assert tool["function"]["arguments"]["tool"] == f"tool{i + 1}"
            assert tool["function"]["arguments"]["value"] == i + 1


class TestExchangeToolContent:
    """Test exchange_tool_content function"""

    def test_tool_role_with_string_content(self):
        """Test: role is tool, content is str"""
        message = {"role": "tool", "content": "Tool execution result"}
        exchange_tool_content(message)

        expected = "{'type': 'text', 'text': 'Tool execution result'}"
        assert message["content"] == expected

    def test_tool_role_with_dict_content(self):
        """Test: role is tool, content is dict"""
        message = {"role": "tool", "content": {"type": "image", "data": "base64data"}}
        original = deepcopy(message)
        exchange_tool_content(message)
        assert message["content"] == original["content"]

    def test_no_role_key(self):
        """Test: message not haverolekey"""
        message = {"content": "Some content"}
        original = deepcopy(message)
        exchange_tool_content(message)
        assert message == original

    def test_role_not_tool(self):
        """Test: role is not tool"""
        message = {"role": "user", "content": "User message"}
        original = deepcopy(message)
        exchange_tool_content(message)
        assert message == original

    def test_no_content_key(self):
        """Test: message not havecontentkey"""
        message = {"role": "tool", "tool_call_id": "call_123"}
        original = deepcopy(message)
        exchange_tool_content(message)
        assert message == original

    def test_empty_string_content(self):
        """Test: content is "" """
        message = {"role": "tool", "content": ""}
        exchange_tool_content(message)
        expected = "{'type': 'text', 'text': ''}"
        assert message["content"] == expected


class TestExchangeTools:
    """Test exchange_tools function"""

    def test_sort_tool_fields_by_priority(self):
        """Test: Sort tool fields by priority"""
        tool = {
            "function": {"parameters": {"type": "object"}, "description": "Test tool description", "name": "test_tool"}
        }
        exchange_tools(tool)

        function_keys = list(tool["function"].keys())
        assert function_keys == ["name", "description", "parameters"]

    def test_partial_fields(self):
        """Test: Only some fields"""
        tool = {"function": {"parameters": {"type": "object"}, "name": "partial_tool"}}
        exchange_tools(tool)

        function_keys = list(tool["function"].keys())
        assert function_keys == ["name", "parameters"]

    def test_no_function_key(self):
        """Test: tool not have function key"""
        tool = {"type": "custom", "id": "tool_123"}
        original = deepcopy(tool)
        exchange_tools(tool)
        assert tool == original

    def test_unknown_fields(self):
        """Test: Case with unknown fields"""
        tool = {"function": {"name": "test", "custom_field": "value", "description": "desc", "another_field": 123}}
        exchange_tools(tool)

        function_keys = list(tool["function"].keys())

        assert function_keys[0] == "name"
        assert function_keys[1] == "description"

    def test_all_priority_fields(self):
        """Test: Includes all priority fields"""
        tool = {
            "function": {
                "extra": "extra_value",
                "name": "test",
                "description": "desc",
                "parameters": {"type": "object"},
            }
        }
        exchange_tools(tool)

        function_keys = list(tool["function"].keys())
        assert function_keys[:3] == ["name", "description", "parameters"]


class TestPreprocessInput:
    """Test preprocess_input function"""

    def test_basic_message_processing(self):
        """Test: basic message processing"""
        messages = [
            {"role": "user", "content": "What's the weather?"},
            {"role": "assistant", "tool_calls": [{"function": {"arguments": '{"city": "Beijing"}'}}]},
            {"role": "tool", "content": "Weather data"},
        ]

        processed_messages, processed_tools = preprocess_input(messages)

        # test tool_calls arguments exchange
        assert isinstance(processed_messages[1]["tool_calls"][0]["function"]["arguments"], dict)
        # test tool role content exchange
        assert processed_messages[2]["content"] == "{'type': 'text', 'text': 'Weather data'}"
        assert processed_tools is None

    def test_with_tools(self):
        "Test: List of included tools"
        messages = [{"role": "user", "content": "Call a tool"}]
        tools = [{"function": {"parameters": {"type": "object"}, "description": "Test tool", "name": "test_tool"}}]

        processed_messages, processed_tools = preprocess_input(messages, tools)

        assert processed_tools is not None
        assert list(processed_tools[0]["function"].keys()) == ["name", "description", "parameters"]

    def test_deep_copy_messages(self):
        """Test: Original message will not be modified"""
        original_messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "tool_calls": [{"function": {"arguments": '{"key": "value"}'}}]},
        ]

        processed_messages, _ = preprocess_input(original_messages)

        assert isinstance(original_messages[1]["tool_calls"][0]["function"]["arguments"], str)
        assert isinstance(processed_messages[1]["tool_calls"][0]["function"]["arguments"], dict)

    def test_deep_copy_tools(self):
        """Test: The original tool list will not be modified"""
        original_tools = [{"function": {"parameters": {"type": "object"}, "description": "desc", "name": "tool"}}]

        _, processed_tools = preprocess_input([{"role": "user", "content": "hi"}], original_tools)

        original_keys = list(original_tools[0]["function"].keys())
        assert original_keys == ["parameters", "description", "name"]

        processed_keys = list(processed_tools[0]["function"].keys())
        assert processed_keys == ["name", "description", "parameters"]

    def test_empty_messages(self):
        """Test: Empty message list"""
        messages = []
        processed_messages, processed_tools = preprocess_input(messages)

        assert processed_messages == []
        assert processed_tools is None

    def test_none_tools(self):
        """Test: tools is None"""
        messages = [{"role": "user", "content": "test"}]
        processed_messages, processed_tools = preprocess_input(messages, None)

        assert processed_messages == messages
        assert processed_tools is None

    def test_empty_tools_list(self):
        """Test: Empty tools list"""
        messages = [{"role": "user", "content": "test"}]
        processed_messages, processed_tools = preprocess_input(messages, [])

        assert processed_messages == messages
        assert processed_tools is None

    def test_complex_scenario(self):
        """Test: Complex Scenario - Multiple messages and multiple tools"""
        messages = [
            {"role": "system", "content": "You are a helpful assistant"},
            {"role": "user", "content": "Get weather and time"},
            {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"arguments": '{"city": "Beijing"}'}},
                    {"function": {"arguments": '{"timezone": "UTC"}'}},
                ],
            },
            {"role": "tool", "content": "Weather: 25°C"},
            {"role": "tool", "content": "Time: 14:00"},
        ]

        tools = [
            {"function": {"parameters": {}, "description": "Weather tool", "name": "get_weather"}},
            {"function": {"parameters": {}, "description": "Time tool", "name": "get_time"}},
        ]

        processed_messages, processed_tools = preprocess_input(messages, tools)

        # test message processe
        for tool_call in processed_messages[2]["tool_calls"]:
            assert isinstance(tool_call["function"]["arguments"], dict)

        for msg in processed_messages[3:]:
            if msg["role"] == "tool":
                assert "type" in msg["content"] and "text" in msg["content"]

        # test tool processe
        for tool in processed_tools:
            assert list(tool["function"].keys())[0] == "name"


# -----------------------------------------------------------------------------
# Tools-aware tokenize: standard / non-standard / fallback paths
# -----------------------------------------------------------------------------


def _build_tokenizer_manager(
    *,
    openai_standard: str = "STANDARD",
    apply_chat_template_side_effect=None,
    encode_side_effect=None,
):
    """Construct a TokenizerManager whose internal tokenizer is a Mock.

    Avoids hitting transformers / disk; the returned ``mock_tokenizer`` is the
    same instance assigned to ``manager.tokenizer``.
    """
    _reset_tokenizer_manager_singleton()
    config = Mock()
    config.tracer_config.endpoint = ""
    config.scheduler_config.kv_conductor_config.conductor_service = "stub-conductor"
    config.scheduler_config.kv_conductor_config.model_path = ""

    with patch.dict("os.environ", {"OPENAI_STANDARD": openai_standard}, clear=False):
        manager = TokenizerManager(config)

    mock_tokenizer = Mock()
    if apply_chat_template_side_effect is not None:
        mock_tokenizer.apply_chat_template.side_effect = apply_chat_template_side_effect
    else:
        mock_tokenizer.apply_chat_template.return_value = []
    if encode_side_effect is not None:
        mock_tokenizer.encode.side_effect = encode_side_effect
    else:
        mock_tokenizer.encode.return_value = []
    manager.tokenizer = mock_tokenizer
    manager.openai_standard = openai_standard
    return manager, mock_tokenizer


class TestApplyChatTemplateStandard(unittest.TestCase):
    """STANDARD-path apply_chat_template must include ``tools`` in the rendered tokens."""

    def setUp(self) -> None:
        _reset_tokenizer_manager_singleton()

    def tearDown(self) -> None:
        _reset_tokenizer_manager_singleton()

    def test_standard_path_passes_tools(self) -> None:
        """tools must be forwarded to tokenizer.apply_chat_template on standard path."""
        manager, mock_tokenizer = _build_tokenizer_manager(openai_standard="STANDARD")
        mock_tokenizer.apply_chat_template.return_value = [11, 22, 33, 44]

        messages = [{"role": "user", "content": "hi"}]
        tools = [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}]

        result = manager.apply_chat_template(messages, tools)

        self.assertEqual(result, [11, 22, 33, 44])
        mock_tokenizer.apply_chat_template.assert_called_once()
        _, kwargs = mock_tokenizer.apply_chat_template.call_args
        self.assertIn("tools", kwargs)
        self.assertEqual(kwargs["tools"], tools)
        self.assertTrue(kwargs.get("add_generation_prompt"))
        self.assertTrue(kwargs.get("tokenize"))

    def test_standard_path_no_tools(self) -> None:
        """When no tools provided, standard path still works and forwards tools=None."""
        manager, mock_tokenizer = _build_tokenizer_manager(openai_standard="STANDARD")
        mock_tokenizer.apply_chat_template.return_value = [1, 2]

        messages = [{"role": "user", "content": "hi"}]
        result = manager.apply_chat_template(messages)
        self.assertEqual(result, [1, 2])
        _, kwargs = mock_tokenizer.apply_chat_template.call_args
        self.assertIsNone(kwargs.get("tools"))

    def test_standard_exception_fallback_still_passes_tools(self) -> None:
        """If primary call raises, fallback retries with the SAME tools (never silently drops it)."""
        side_effects = [RuntimeError("first failure"), [9, 9, 9, 9]]
        manager, mock_tokenizer = _build_tokenizer_manager(
            openai_standard="STANDARD",
            apply_chat_template_side_effect=side_effects,
        )

        messages = [{"role": "user", "content": "hi"}]
        tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
        result = manager.apply_chat_template(messages, tools)
        self.assertEqual(result, [9, 9, 9, 9])

        self.assertEqual(mock_tokenizer.apply_chat_template.call_count, 2)
        for _, kwargs in mock_tokenizer.apply_chat_template.call_args_list:
            self.assertEqual(kwargs.get("tools"), tools)

    def test_total_failure_returns_empty_list(self) -> None:
        """Both primary and fallback failing -> empty list (let scheduler fall back to LB)."""
        side_effects = [RuntimeError("first"), RuntimeError("second")]
        manager, mock_tokenizer = _build_tokenizer_manager(
            openai_standard="STANDARD",
            apply_chat_template_side_effect=side_effects,
        )
        result = manager.apply_chat_template(
            [{"role": "user", "content": "hi"}],
            [{"type": "function", "function": {"name": "f"}}],
        )
        self.assertEqual(result, [])
        self.assertEqual(mock_tokenizer.apply_chat_template.call_count, 2)

    def test_non_standard_path_uses_preprocess(self) -> None:
        """Non-standard path keeps using the preprocess pipeline (tokenize=False then encode)."""
        manager, mock_tokenizer = _build_tokenizer_manager(openai_standard="DEEPSEEK")
        mock_tokenizer.apply_chat_template.return_value = "rendered prompt"
        mock_tokenizer.encode.return_value = [5, 6, 7]

        messages = [{"role": "user", "content": "hi"}]
        tools = [{"type": "function", "function": {"name": "f"}}]
        result = manager.apply_chat_template(messages, tools)

        self.assertEqual(result, [5, 6, 7])
        # Non-standard path calls apply_chat_template with tokenize=False, then encodes the string.
        _, kwargs = mock_tokenizer.apply_chat_template.call_args
        self.assertFalse(kwargs.get("tokenize", True))
        self.assertEqual(kwargs.get("tools"), tools)
        mock_tokenizer.encode.assert_called_once_with("rendered prompt")

    def test_non_standard_exception_fallback_to_standard_path_keeps_tools(self) -> None:
        """If preprocess pipeline raises, we fall back to the tools-aware standard path."""
        # First call (non-standard, tokenize=False) raises; second call (standard, tokenize=True) returns ids.
        side_effects = [RuntimeError("preprocess broken"), [42, 43, 44]]
        manager, mock_tokenizer = _build_tokenizer_manager(
            openai_standard="DEEPSEEK",
            apply_chat_template_side_effect=side_effects,
        )

        messages = [{"role": "user", "content": "hi"}]
        tools = [{"type": "function", "function": {"name": "f"}}]
        result = manager.apply_chat_template(messages, tools)
        self.assertEqual(result, [42, 43, 44])
        self.assertEqual(mock_tokenizer.apply_chat_template.call_count, 2)
        # Both invocations must carry tools.
        for _, kwargs in mock_tokenizer.apply_chat_template.call_args_list:
            self.assertEqual(kwargs.get("tools"), tools)


class TestApplyChatTemplateRenamed(unittest.TestCase):
    """The misspelled method must be removed (no alias kept)."""

    def setUp(self) -> None:
        _reset_tokenizer_manager_singleton()

    def tearDown(self) -> None:
        _reset_tokenizer_manager_singleton()

    def test_correct_method_name_exists(self) -> None:
        manager, _ = _build_tokenizer_manager()
        self.assertTrue(hasattr(manager, "_apply_chat_template_with_preprocess"))

    def test_misspelled_method_name_removed(self) -> None:
        manager, _ = _build_tokenizer_manager()
        self.assertFalse(hasattr(manager, "_apply_chat_template_with_preproces"))


class TestKvCacheAffinityWithToolsEndToEnd(unittest.TestCase):
    """End-to-end: tokens sent to conductor must reflect tools when present."""

    def setUp(self) -> None:
        _reset_tokenizer_manager_singleton()

    def tearDown(self) -> None:
        _reset_tokenizer_manager_singleton()

    def _stub_tokenizer_manager(self, ids_with_tools, ids_without_tools):
        """Patch TokenizerManager().apply_chat_template so it differentiates tools/no-tools."""
        manager, mock_tokenizer = _build_tokenizer_manager(openai_standard="STANDARD")

        def _apply(*args, **kwargs):
            tools = kwargs.get("tools")
            return ids_with_tools if tools else ids_without_tools

        mock_tokenizer.apply_chat_template.side_effect = _apply
        return manager

    @patch.object(KvCacheAffinityPolicy, "_conductor_block_size", return_value=16)
    @patch("motor.coordinator.scheduler.policy.kv_cache_affinity.ConductorApiClient.query_conductor")
    def test_query_conductor_receives_tokens_including_tools(self, mock_query, _mock_block_size) -> None:
        ids_with_tools = list(range(20))
        ids_without_tools = list(range(5))
        self._stub_tokenizer_manager(ids_with_tools, ids_without_tools)

        instance = Mock()
        instance.id = 1
        ep = _make_endpoint(0)
        instance.endpoints = {"pod-0": {0: ep}}
        instance.get_all_endpoints.return_value = (ep,)

        mock_query.return_value = {
            TENANT_ID: {
                "vllm-prefill-1": {
                    "longest_matched": 20,
                    "DP": {"0": 1},
                }
            }
        }

        req_info = Mock()
        req_info.req_data = {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {"type": "function", "function": {"name": "search_order"}},
                {"type": "function", "function": {"name": "refund"}},
            ],
        }

        result = KvCacheAffinityPolicy.select_endpoint_from_list([instance], req_info)
        self.assertIsNotNone(result)
        mock_query.assert_called_once()
        sent_instances, sent_ids = mock_query.call_args[0]
        self.assertEqual(sent_instances, [instance])
        self.assertEqual(sent_ids, ids_with_tools)
        self.assertGreater(len(sent_ids), len(ids_without_tools))

    @patch.object(KvCacheAffinityPolicy, "_conductor_block_size", return_value=0)
    @patch("motor.coordinator.scheduler.policy.kv_cache_affinity.ConductorApiClient.query_conductor")
    def test_tokenize_total_failure_falls_back_to_empty_ids(self, mock_query, _mock_block_size) -> None:
        """If both tokenize attempts fail, encoded_ids must be [] and conductor queried with []."""
        manager, mock_tokenizer = _build_tokenizer_manager(openai_standard="STANDARD")
        mock_tokenizer.apply_chat_template.side_effect = RuntimeError("boom")

        instance = Mock()
        instance.id = 1
        ep = _make_endpoint(0)
        instance.endpoints = {"pod-0": {0: ep}}
        instance.get_all_endpoints.return_value = (ep,)
        mock_query.return_value = {}

        req_info = Mock()
        req_info.req_data = {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "f"}}],
        }
        result = KvCacheAffinityPolicy.select_endpoint_from_list([instance], req_info)
        self.assertIsNone(result)
        mock_query.assert_called_once()
        sent_instances, sent_ids = mock_query.call_args[0]
        self.assertEqual(sent_ids, [])
