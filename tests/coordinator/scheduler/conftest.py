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

"""Shared test fixtures for scheduler tests."""

from unittest.mock import MagicMock, AsyncMock, Mock
import pytest

from motor.common.resources.instance import Instance, PDRole
from motor.common.resources.endpoint import Endpoint, Workload, WorkloadAction


def create_mock_workload(active_tokens: float = 0.0, active_kv_cache: float = 0.0) -> Mock:
    """Create a mock Workload with given values."""
    wl = Mock(spec=Workload)
    wl.active_tokens = active_tokens
    wl.active_kv_cache = active_kv_cache
    wl.calculate_workload_score = Mock(return_value=active_tokens + active_kv_cache)
    return wl


def create_mock_endpoint(
    endpoint_id: int = 1,
    status: str = "NORMAL",
    workload: Mock | None = None,
) -> Mock:
    """Create a mock Endpoint."""
    if workload is None:
        workload = create_mock_workload()
    ep = Mock(spec=Endpoint)
    ep.id = endpoint_id
    ep.get_status_value = Mock(return_value=status)
    ep.workload = workload
    return ep


def create_mock_instance(
    instance_id: int = 1,
    role: PDRole = PDRole.ROLE_P,
    endpoints: dict | None = None,
    gathered_workload: Mock | None = None,
) -> Mock:
    """Create a mock Instance with optional endpoints."""
    inst = Mock(spec=Instance)
    inst.id = instance_id
    inst.role = role
    if endpoints is None:
        endpoints = {"group": {1: create_mock_endpoint(1)}}
    inst.endpoints = endpoints
    inst.get_all_endpoints = Mock(
        return_value=[ep for pod in endpoints.values() for ep in pod.values()]
    )
    if gathered_workload is None:
        gathered_workload = create_mock_workload()
    inst.gathered_workload = gathered_workload
    return inst


class MockInstanceProvider:
    """Mock InstanceProvider for policy tests."""

    def __init__(self, instances: dict[PDRole, dict[int, Instance]] | None = None):
        self._instances = instances or {}
        self._workload_updates = []

    def get_available_instances(self, role: PDRole = None):
        if role is None:
            result = {}
            for r, insts in self._instances.items():
                result.update(insts)
            return result
        return self._instances.get(role, {})

    async def update_instance_workload(self, instance_id, endpoint_id, workload_change):
        self._workload_updates.append((instance_id, endpoint_id, workload_change))

    def get_workload_updates(self):
        return self._workload_updates
