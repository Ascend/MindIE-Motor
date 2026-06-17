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

"""Tests for Scheduling policy factory"""

import unittest
from unittest.mock import Mock

from motor.coordinator.scheduler.policy.factory import (
    create,
    register,
    SchedulingPolicyFactory,
    _REGISTRY,
)
from motor.config.coordinator import SchedulerType
from motor.coordinator.scheduler.policy.base import BaseSchedulingPolicy
from motor.coordinator.scheduler.policy.round_robin import RoundRobinPolicy
from motor.coordinator.scheduler.policy.load_balance import LoadBalancePolicy
from tests.coordinator.scheduler.conftest import MockInstanceProvider


class TestPolicyFactory(unittest.TestCase):
    """Tests for Scheduling policy factory."""

    def test_create_round_robin(self):
        """create returns a RoundRobinPolicy for ROUND_ROBIN type."""
        policy = create(SchedulerType.ROUND_ROBIN, MockInstanceProvider())
        self.assertIsInstance(policy, RoundRobinPolicy)

    def test_create_load_balance(self):
        """create returns a LoadBalancePolicy for LOAD_BALANCE type."""
        policy = create(SchedulerType.LOAD_BALANCE, MockInstanceProvider())
        self.assertIsInstance(policy, LoadBalancePolicy)

    def test_create_unknown_type_raises(self):
        """create raises ValueError for unregistered type."""
        with self.assertRaises(ValueError):
            create("UNKNOWN", MockInstanceProvider())

    def test_scheduling_policy_factory_create(self):
        """SchedulingPolicyFactory.create delegates to the module-level create()."""
        policy = SchedulingPolicyFactory.create(
            SchedulerType.ROUND_ROBIN, MockInstanceProvider()
        )
        self.assertIsInstance(policy, RoundRobinPolicy)

    def test_register_custom_policy(self):
        """Register a custom policy type and instantiate it via create()."""
        custom_mock = Mock(spec=BaseSchedulingPolicy)

        def mock_factory(provider):
            return custom_mock

        register("CUSTOM_TYPE", mock_factory)
        result = create("CUSTOM_TYPE", MockInstanceProvider())
        self.assertIs(result, custom_mock)

    def test_register_overwrites_existing(self):
        """Registering the same type twice overwrites with the second factory."""
        factory_a = Mock(return_value=Mock(spec=BaseSchedulingPolicy))
        factory_b = Mock(return_value=Mock(spec=BaseSchedulingPolicy))

        register("OVERWRITE_TYPE", factory_a)
        register("OVERWRITE_TYPE", factory_b)
        create("OVERWRITE_TYPE", MockInstanceProvider())

        factory_a.assert_not_called()
        factory_b.assert_called_once()
