# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 license for more details.

"""Tests for LoadBalancePolicy"""

import asyncio
import unittest
from unittest.mock import Mock

from motor.coordinator.scheduler.policy.load_balance import LoadBalancePolicy
from motor.common.resources.instance import PDRole
from motor.common.resources.endpoint import Workload, WorkloadAction
from tests.coordinator.scheduler.conftest import (
    MockInstanceProvider,
    create_mock_instance,
    create_mock_endpoint,
    create_mock_workload,
)


class TestLoadBalancePolicy(unittest.TestCase):
    """Test LoadBalancePolicy Class"""

    def test_select_instance_from_list_lowest_workload(self):
        """Test select_instance_from_list returns instance with lowest workload score."""
        inst1 = create_mock_instance(
            instance_id=1, gathered_workload=create_mock_workload(active_tokens=10.0)
        )
        inst2 = create_mock_instance(
            instance_id=2, gathered_workload=create_mock_workload(active_tokens=5.0)
        )
        inst3 = create_mock_instance(
            instance_id=3, gathered_workload=create_mock_workload(active_tokens=15.0)
        )
        instances = [inst1, inst2, inst3]

        result = LoadBalancePolicy.select_instance_from_list(instances, role=PDRole.ROLE_P)
        self.assertIs(result, inst2)

    def test_select_instance_from_list_empty(self):
        """Test select_instance_from_list with empty list returns None."""
        result = LoadBalancePolicy.select_instance_from_list([], role=PDRole.ROLE_P)
        self.assertIsNone(result)

    def test_select_instance_from_list_single(self):
        """Test select_instance_from_list with single instance returns that instance."""
        inst1 = create_mock_instance(
            instance_id=1, gathered_workload=create_mock_workload(active_tokens=10.0)
        )
        result = LoadBalancePolicy.select_instance_from_list([inst1], role=PDRole.ROLE_P)
        self.assertIs(result, inst1)

    def test_select_instance_from_list_tie_break_first(self):
        """Test select_instance_from_list returns first encountered when workloads tie."""
        inst1 = create_mock_instance(
            instance_id=1, gathered_workload=create_mock_workload(active_tokens=5.0)
        )
        inst2 = create_mock_instance(
            instance_id=2, gathered_workload=create_mock_workload(active_tokens=5.0)
        )
        instances = [inst1, inst2]

        result = LoadBalancePolicy.select_instance_from_list(instances, role=PDRole.ROLE_P)
        self.assertIs(result, inst1)

    def test_select_instance_from_list_with_start_index(self):
        """Test select_instance_from_list with start_index still picks lowest workload."""
        inst1 = create_mock_instance(
            instance_id=1, gathered_workload=create_mock_workload(active_tokens=10.0)
        )
        inst2 = create_mock_instance(
            instance_id=2, gathered_workload=create_mock_workload(active_tokens=5.0)
        )
        instances = [inst1, inst2]

        result = LoadBalancePolicy.select_instance_from_list(
            instances, role=PDRole.ROLE_P, start_index=1
        )
        self.assertIs(result, inst2)

    def test_select_endpoint_from_instance_lowest_workload(self):
        """Test select_endpoint_from_instance returns endpoint with lowest workload."""
        ep1 = create_mock_endpoint(
            endpoint_id=1, workload=create_mock_workload(active_tokens=10.0)
        )
        ep2 = create_mock_endpoint(
            endpoint_id=2, workload=create_mock_workload(active_tokens=5.0)
        )
        ep3 = create_mock_endpoint(
            endpoint_id=3, workload=create_mock_workload(active_tokens=15.0)
        )
        endpoints = {"group": {1: ep1, 2: ep2, 3: ep3}}
        instance = create_mock_instance(endpoints=endpoints)

        result = LoadBalancePolicy.select_endpoint_from_instance(instance)
        self.assertIs(result, ep2)

    def test_select_endpoint_from_instance_none(self):
        """Test select_endpoint_from_instance with None instance returns None."""
        result = LoadBalancePolicy.select_endpoint_from_instance(None)
        self.assertIsNone(result)

    def test_select_endpoint_from_instance_no_endpoints(self):
        """Test select_endpoint_from_instance when instance has no endpoints."""
        instance = Mock()
        instance.id = 1
        instance.role = PDRole.ROLE_P
        instance.get_all_endpoints = Mock(return_value=[])
        result = LoadBalancePolicy.select_endpoint_from_instance(instance)
        self.assertIsNone(result)

    def test_update_workload_success(self):
        """Test update_workload succeeds and stores the update."""
        provider = MockInstanceProvider()
        policy = LoadBalancePolicy(provider)
        workload_change = Workload(active_tokens=1.0)

        result = asyncio.run(
            policy.update_workload(1, 1, "req1", WorkloadAction.ALLOCATION, workload_change)
        )

        self.assertTrue(result)
        updates = provider.get_workload_updates()
        self.assertEqual(len(updates), 1)
        instance_id, endpoint_id, stored_change = updates[0]
        self.assertEqual(instance_id, 1)
        self.assertEqual(endpoint_id, 1)
        self.assertIs(stored_change, workload_change)

    def test_update_workload_no_provider_support(self):
        """Test update_workload raises RuntimeError when provider lacks support."""
        provider = object()
        policy = LoadBalancePolicy(provider)

        with self.assertRaises(RuntimeError):
            asyncio.run(
                policy.update_workload(1, 1, "req1", WorkloadAction.ALLOCATION, Workload())
            )

    def test_select_instance_with_provider(self):
        """Test _select_instance returns lowest workload instance via provider."""
        inst1 = create_mock_instance(
            instance_id=1, gathered_workload=create_mock_workload(active_tokens=10.0)
        )
        inst2 = create_mock_instance(
            instance_id=2, gathered_workload=create_mock_workload(active_tokens=5.0)
        )
        provider = MockInstanceProvider({PDRole.ROLE_P: {1: inst1, 2: inst2}})
        policy = LoadBalancePolicy(provider)

        result = policy._select_instance(PDRole.ROLE_P)
        self.assertIs(result, inst2)

    def test_select_instance_no_instances(self):
        """Test _select_instance with no instances returns None."""
        provider = MockInstanceProvider()
        policy = LoadBalancePolicy(provider)
        result = policy._select_instance(PDRole.ROLE_P)
        self.assertIsNone(result)

    def test_select_endpoint_with_provider(self):
        """Test _select_endpoint returns lowest workload endpoint via provider."""
        ep1 = create_mock_endpoint(
            endpoint_id=1, workload=create_mock_workload(active_tokens=10.0)
        )
        ep2 = create_mock_endpoint(
            endpoint_id=2, workload=create_mock_workload(active_tokens=5.0)
        )
        endpoints = {"group": {1: ep1, 2: ep2}}
        instance = create_mock_instance(endpoints=endpoints)
        provider = MockInstanceProvider()
        policy = LoadBalancePolicy(provider)

        result = policy._select_endpoint(instance)
        self.assertIs(result, ep2)

    def test_select_instance_and_endpoint_combined(self):
        """Test select_instance_and_endpoint returns (instance, endpoint) tuple."""
        inst1 = create_mock_instance(instance_id=1)
        provider = MockInstanceProvider({PDRole.ROLE_P: {1: inst1}})
        policy = LoadBalancePolicy(provider)

        result = policy.select_instance_and_endpoint(PDRole.ROLE_P)
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 2)
        self.assertIs(result[0], inst1)
        self.assertIsNotNone(result[1])
