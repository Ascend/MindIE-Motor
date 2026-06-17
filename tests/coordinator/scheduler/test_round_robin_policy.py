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

"""Tests for RoundRobinPolicy"""

import unittest
from unittest.mock import Mock

from motor.coordinator.scheduler.policy.round_robin import RoundRobinPolicy
from motor.common.resources.instance import PDRole
from tests.coordinator.scheduler.conftest import MockInstanceProvider, create_mock_instance, create_mock_endpoint


class TestRoundRobinPolicy(unittest.TestCase):
    """Test RoundRobinPolicy Class"""

    def test_select_instance_from_list_normal(self):
        """Test select_instance_from_list with multiple instances and round-robin wrapping."""
        inst1 = create_mock_instance(instance_id=1)
        inst2 = create_mock_instance(instance_id=2)
        inst3 = create_mock_instance(instance_id=3)
        instances = [inst1, inst2, inst3]

        result, counter = RoundRobinPolicy.select_instance_from_list(instances, counter=0)
        self.assertIs(result, inst1)
        self.assertEqual(counter, 1)

        result, counter = RoundRobinPolicy.select_instance_from_list(instances, counter=1)
        self.assertIs(result, inst2)
        self.assertEqual(counter, 2)

        result, counter = RoundRobinPolicy.select_instance_from_list(instances, counter=2)
        self.assertIs(result, inst3)
        self.assertEqual(counter, 3)

        # Wrap around
        result, counter = RoundRobinPolicy.select_instance_from_list(instances, counter=3)
        self.assertIs(result, inst1)
        self.assertEqual(counter, 4)

    def test_select_instance_from_list_empty(self):
        """Test select_instance_from_list with empty list returns (None, counter)."""
        result, counter = RoundRobinPolicy.select_instance_from_list([], counter=0)
        self.assertIsNone(result)
        self.assertEqual(counter, 0)

    def test_select_instance_from_list_single(self):
        """Test select_instance_from_list with single instance always returns that instance."""
        inst1 = create_mock_instance(instance_id=1)

        result, counter = RoundRobinPolicy.select_instance_from_list([inst1], counter=0)
        self.assertIs(result, inst1)
        self.assertEqual(counter, 1)

        result, counter = RoundRobinPolicy.select_instance_from_list([inst1], counter=5)
        self.assertIs(result, inst1)
        self.assertEqual(counter, 6)

    def test_select_endpoint_from_instance_normal(self):
        """Test select_endpoint_from_instance cycles through endpoints."""
        ep1 = create_mock_endpoint(endpoint_id=1)
        ep2 = create_mock_endpoint(endpoint_id=2)
        ep3 = create_mock_endpoint(endpoint_id=3)
        endpoints = {"group": {1: ep1, 2: ep2, 3: ep3}}
        instance = create_mock_instance(endpoints=endpoints)
        counters = {}

        result = RoundRobinPolicy.select_endpoint_from_instance(instance, counters)
        self.assertIs(result, ep1)
        self.assertEqual(counters[instance.id], 1)

        result = RoundRobinPolicy.select_endpoint_from_instance(instance, counters)
        self.assertIs(result, ep2)
        self.assertEqual(counters[instance.id], 2)

    def test_select_endpoint_from_instance_none_instance(self):
        """Test select_endpoint_from_instance with None instance returns None."""
        result = RoundRobinPolicy.select_endpoint_from_instance(None, {})
        self.assertIsNone(result)

    def test_select_endpoint_from_instance_no_endpoints(self):
        """Test select_endpoint_from_instance when instance has no endpoints."""
        instance = Mock()
        instance.id = 1
        instance.get_all_endpoints = Mock(return_value=[])
        result = RoundRobinPolicy.select_endpoint_from_instance(instance, {})
        self.assertIsNone(result)

    def test_select_instance_with_provider(self):
        """Test _select_instance cycles through provider instances."""
        inst1 = create_mock_instance(instance_id=1)
        inst2 = create_mock_instance(instance_id=2)
        provider = MockInstanceProvider({PDRole.ROLE_P: {1: inst1, 2: inst2}})
        policy = RoundRobinPolicy(provider)

        result1 = policy._select_instance(PDRole.ROLE_P)
        self.assertIs(result1, inst1)

        result2 = policy._select_instance(PDRole.ROLE_P)
        self.assertIs(result2, inst2)

        # Third call wraps around
        result3 = policy._select_instance(PDRole.ROLE_P)
        self.assertIs(result3, inst1)

    def test_select_instance_no_instances(self):
        """Test _select_instance with no instances returns None."""
        provider = MockInstanceProvider()
        policy = RoundRobinPolicy(provider)
        result = policy._select_instance(PDRole.ROLE_P)
        self.assertIsNone(result)

    def test_select_endpoint_with_provider(self):
        """Test _select_endpoint returns an endpoint from the instance."""
        instance = create_mock_instance(instance_id=1)
        provider = MockInstanceProvider({PDRole.ROLE_P: {1: instance}})
        policy = RoundRobinPolicy(provider)
        result = policy._select_endpoint(instance)
        self.assertIsNotNone(result)

    def test_select_instance_and_endpoint_combined(self):
        """Test select_instance_and_endpoint returns (instance, endpoint) tuple."""
        instance = create_mock_instance(instance_id=1)
        provider = MockInstanceProvider({PDRole.ROLE_P: {1: instance}})
        policy = RoundRobinPolicy(provider)
        result = policy.select_instance_and_endpoint(PDRole.ROLE_P)
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 2)
        self.assertIs(result[0], instance)
        self.assertIsNotNone(result[1])

    def test_select_instance_and_endpoint_no_instance(self):
        """Test select_instance_and_endpoint returns None when no instances available."""
        provider = MockInstanceProvider()
        policy = RoundRobinPolicy(provider)
        result = policy.select_instance_and_endpoint(PDRole.ROLE_P)
        self.assertIsNone(result)
