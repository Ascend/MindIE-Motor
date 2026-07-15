# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import asyncio
import threading

import pytest

from motor.common.resources.dispatch import DispatchPlan
from motor.common.resources import Instance, PDRole, Workload, Endpoint, EventType
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.domain.instance_manager import InstanceManager, UpdateInstanceMode


class TestInstanceManager:
    """Test cases for InstanceManager"""

    # pylint: disable=attribute-defined-outside-init
    def setup_method(self):
        """Setup for each test method"""
        self.config = CoordinatorConfig()
        self.instance_manager = InstanceManager(self.config)

        # Test data
        self.prefill_instance = Instance(
            job_name="test-prefill",
            model_name="test-model",
            id=1,
            role=PDRole.ROLE_P,
            dispatch_capabilities=[DispatchPlan.CONCURRENT_ENGINE_SYNC.value],
            endpoints={},
        )

        self.decode_instance = Instance(
            job_name="test-decode",
            model_name="test-model",
            id=2,
            role=PDRole.ROLE_D,
            dispatch_capabilities=[DispatchPlan.CONCURRENT_ENGINE_SYNC.value],
            endpoints={},
        )

        self.hybrid_instance = Instance(
            job_name="test-hybrid", model_name="test-model", id=3, role=PDRole.ROLE_U, endpoints={}
        )

        self.endpoint = Endpoint(id=1, ip="127.0.0.1", business_port="8080", mgmt_port="8080")

    def test_init(self):
        """Test InstanceManager initialization"""
        assert isinstance(self.instance_manager._prefill_pool, dict)
        assert isinstance(self.instance_manager._decode_pool, dict)
        assert isinstance(self.instance_manager._hybrid_pool, dict)
        assert isinstance(self.instance_manager._unavailable_pool, dict)
        assert len(self.instance_manager._prefill_pool) == 0
        assert len(self.instance_manager._decode_pool) == 0
        assert len(self.instance_manager._hybrid_pool) == 0
        assert len(self.instance_manager._unavailable_pool) == 0

    def test_has_required_instances_from_role_topology(self):
        """Readiness is inferred from roles instead of a configured deploy mode."""
        assert self.instance_manager.has_required_instances() is False

        self.instance_manager._add_instance_to_available_pool(self.prefill_instance)
        assert self.instance_manager.has_required_instances() is True

        self.instance_manager._add_instance_to_available_pool(self.decode_instance)
        assert self.instance_manager.has_required_instances() is True

    def test_has_required_instances_rejects_incompatible_pd_pair(self):
        self.decode_instance.dispatch_capabilities = [DispatchPlan.PREFILL_HANDOFF_DECODE.value]
        self.instance_manager._add_instance_to_available_pool(self.prefill_instance)
        self.instance_manager._add_instance_to_available_pool(self.decode_instance)

        assert self.instance_manager.has_required_instances() is False

    def test_has_required_instances_hybrid(self):
        result = self.instance_manager._add_instance_to_available_pool(self.hybrid_instance)
        assert result is True
        assert self.instance_manager.has_required_instances() is True

    def test_get_available_instances(self):
        """Test get_available_instances method"""
        # Add instances to pools
        self.instance_manager._add_instance_to_available_pool(self.prefill_instance)
        self.instance_manager._add_instance_to_available_pool(self.decode_instance)
        self.instance_manager._add_instance_to_available_pool(self.hybrid_instance)

        # Test getting prefill instances
        prefill_instances = self.instance_manager.get_available_instances(PDRole.ROLE_P)
        assert len(prefill_instances) == 1
        assert 1 in prefill_instances
        assert prefill_instances[1] == self.prefill_instance

        # Test getting decode instances
        decode_instances = self.instance_manager.get_available_instances(PDRole.ROLE_D)
        assert len(decode_instances) == 1
        assert 2 in decode_instances
        assert decode_instances[2] == self.decode_instance

        # Test getting hybrid instances
        hybrid_instances = self.instance_manager.get_available_instances(PDRole.ROLE_U)
        assert len(hybrid_instances) == 1
        assert 3 in hybrid_instances
        assert hybrid_instances[3] == self.hybrid_instance

        # Test getting instances with unknown role
        unknown_instances = self.instance_manager.get_available_instances("unknown")
        assert unknown_instances == {}

    @pytest.mark.asyncio
    async def test_stop_instance(self):
        """Test stop  method"""
        # Add instances to pools
        self.instance_manager._add_instance_to_available_pool(self.prefill_instance)
        self.instance_manager._add_instance_to_available_pool(self.decode_instance)
        self.instance_manager._add_instance_to_available_pool(self.hybrid_instance)

        # Test getting prefill instances
        prefill_instances = self.instance_manager.get_available_instances(PDRole.ROLE_P)
        assert prefill_instances[1] == self.prefill_instance

        # Test getting decode instances
        decode_instances = self.instance_manager.get_available_instances(PDRole.ROLE_D)
        assert decode_instances[2] == self.decode_instance

        # Test getting hybrid instances
        hybrid_instances = self.instance_manager.get_available_instances(PDRole.ROLE_U)
        assert hybrid_instances[3] == self.hybrid_instance

        assert self.instance_manager.has_required_instances() is True

        # Stop instance, delete all info
        await self.instance_manager.stop()

        assert self.instance_manager.has_required_instances() is False
        assert self.instance_manager.get_available_instances(PDRole.ROLE_D) == {}
        assert self.instance_manager.get_available_instances(PDRole.ROLE_P) == {}
        assert self.instance_manager.get_available_instances(PDRole.ROLE_U) == {}

    @pytest.mark.asyncio
    async def test_get_all_instances(self):
        """Test get_all_instances method"""
        # Add instances to pools
        self.instance_manager._add_instance_to_available_pool(self.prefill_instance)
        self.instance_manager._add_instance_to_available_pool(self.decode_instance)

        # Add instance to unavailable pool
        unavailable_instance = Instance(
            job_name="test-unavailable", model_name="test-model", id=3, role=PDRole.ROLE_U, endpoints={}
        )
        self.instance_manager._unavailable_pool[3] = unavailable_instance

        # Get all instances
        available_pool, unavailable_pool = await self.instance_manager.get_all_instances()

        # Verify available pool contents
        assert len(available_pool) == 2
        assert 1 in available_pool
        assert 2 in available_pool
        assert available_pool[1].id == 1
        assert available_pool[2].id == 2

        # Verify unavailable pool contents
        assert len(unavailable_pool) == 1
        assert 3 in unavailable_pool
        assert unavailable_pool[3].id == 3

        # Verify that returned dictionaries are copies (modifying them doesn't affect original pools)
        available_pool[4] = self.hybrid_instance
        unavailable_pool[4] = self.hybrid_instance

        # Original pools should not be affected
        assert len(self.instance_manager._available_pool) == 2
        assert len(self.instance_manager._unavailable_pool) == 1
        assert 4 not in self.instance_manager._available_pool
        assert 4 not in self.instance_manager._unavailable_pool

    def test_find_available_pool(self):
        """Test _find_available_pool method"""
        # Add instances to pools
        self.instance_manager._add_instance_to_available_pool(self.prefill_instance)
        self.instance_manager._add_instance_to_available_pool(self.decode_instance)
        self.instance_manager._add_instance_to_available_pool(self.hybrid_instance)

        # Test finding prefill instance
        prefill_pool = self.instance_manager._find_available_pool(1)
        assert prefill_pool is not None
        assert prefill_pool == self.instance_manager._prefill_pool

        # Test finding decode instance
        decode_pool = self.instance_manager._find_available_pool(2)
        assert decode_pool is not None
        assert decode_pool == self.instance_manager._decode_pool

        # Test finding hybrid instance
        hybrid_pool = self.instance_manager._find_available_pool(3)
        assert hybrid_pool is not None
        assert hybrid_pool == self.instance_manager._hybrid_pool

        # Test finding non-existent instance
        none_pool = self.instance_manager._find_available_pool(999)
        assert none_pool is None

    @pytest.mark.asyncio
    async def test_update_instance_workload_success(self, caplog):
        """Test update_instance_workload method with success case"""
        # Instance must have endpoints so _endpoint_id_cache can resolve endpoint_id
        self.prefill_instance.add_endpoints("127.0.0.1", {self.endpoint.id: self.endpoint})
        self.instance_manager._add_instance_to_available_pool(self.prefill_instance)

        workload_change = Workload(active_tokens=10, active_kv_cache=20)
        await self.instance_manager.update_instance_workload(1, self.endpoint.id, workload_change)

        assert self.prefill_instance.gathered_workload.active_tokens == 10
        assert self.prefill_instance.gathered_workload.active_kv_cache == 20
        assert self.endpoint.workload.active_tokens == 10
        assert self.endpoint.workload.active_kv_cache == 20

    @pytest.mark.asyncio
    async def test_update_instance_workload_floors_negative_and_warns(self, caplog):
        """A release exceeding prior allocation floors load to 0 (never negative) and warns.

        The ledger is an unbounded signed accumulator; a duplicated/late release could otherwise
        drive an endpoint below zero, turning it into a permanent minimum-score scheduling magnet.
        """
        self.prefill_instance.add_endpoints("127.0.0.1", {self.endpoint.id: self.endpoint})
        self.instance_manager._add_instance_to_available_pool(self.prefill_instance)
        await self.instance_manager.update_instance_workload(
            1, self.endpoint.id, Workload(active_tokens=10, active_kv_cache=20)
        )
        caplog.clear()
        # Over-release: subtract more than was allocated -> would go negative without the floor.
        await self.instance_manager.update_instance_workload(
            1, self.endpoint.id, Workload(active_tokens=-30, active_kv_cache=-50)
        )

        assert self.endpoint.workload.active_tokens == 0
        assert self.endpoint.workload.active_kv_cache == 0
        assert self.prefill_instance.gathered_workload.active_tokens == 0
        assert self.prefill_instance.gathered_workload.active_kv_cache == 0
        assert "floor" in caplog.text.lower()
        assert "endpoint_id=1" in caplog.text

    @pytest.mark.asyncio
    async def test_update_instance_workload_balanced_release_no_floor_no_warn(self, caplog):
        """A balanced allocate/release settles at exactly 0 with no floor warning."""
        self.prefill_instance.add_endpoints("127.0.0.1", {self.endpoint.id: self.endpoint})
        self.instance_manager._add_instance_to_available_pool(self.prefill_instance)
        await self.instance_manager.update_instance_workload(
            1, self.endpoint.id, Workload(active_tokens=10, active_kv_cache=20)
        )
        caplog.clear()
        await self.instance_manager.update_instance_workload(
            1, self.endpoint.id, Workload(active_tokens=-10, active_kv_cache=-20)
        )

        assert self.endpoint.workload.active_tokens == 0
        assert self.endpoint.workload.active_kv_cache == 0
        assert "floor" not in caplog.text.lower()

    @pytest.mark.asyncio
    async def test_update_instance_workload_rebuilds_aggregate_after_sibling_overrelease(self):
        """An over-release on one endpoint must not hide load on sibling endpoints."""
        sibling = Endpoint(id=2, ip="127.0.0.2", business_port="8080", mgmt_port="8080")
        self.prefill_instance.add_endpoints("127.0.0.1", {self.endpoint.id: self.endpoint})
        self.prefill_instance.add_endpoints("127.0.0.2", {sibling.id: sibling})
        self.instance_manager._add_instance_to_available_pool(self.prefill_instance)

        await self.instance_manager.update_instance_workload(1, 1, Workload(active_tokens=100))
        await self.instance_manager.update_instance_workload(1, 2, Workload(active_tokens=100))
        await self.instance_manager.update_instance_workload(1, 1, Workload(active_tokens=-150))

        assert self.endpoint.workload.active_tokens == 0
        assert sibling.workload.active_tokens == 100
        assert self.prefill_instance.gathered_workload.active_tokens == 100

    @pytest.mark.asyncio
    async def test_update_instance_workload_instance_not_found(self, caplog):
        """Test update_instance_workload method when instance not found"""
        # Create workload change
        workload_change = Workload(active_tokens=10, active_kv_cache=20)

        # Try to update workload for non-existent instance
        await self.instance_manager.update_instance_workload(999, self.endpoint.id, workload_change)

        # Verify warning was logged (implementation: "not found in available pool while updating workload")
        assert "not found in available pool" in caplog.text and "999" in caplog.text

    @pytest.mark.asyncio
    async def test_update_instance_workload_instance_none(self, caplog):
        """Test update_instance_workload method when instance is None"""
        # Add None instance to pool
        self.instance_manager._prefill_pool[1] = None
        # Also need to add to _available_pool for consistency
        self.instance_manager._available_pool[1] = None

        # Create workload change
        workload_change = Workload(active_tokens=10, active_kv_cache=20)

        # Try to update workload for None instance (instance_id 1 maps to None in pool)
        await self.instance_manager.update_instance_workload(1, self.endpoint.id, workload_change)

        # Implementation logs "not found in available pool while updating workload"
        assert "Instance ID 1" in caplog.text and "not found in available pool" in caplog.text

    @pytest.mark.asyncio
    async def test_delete_unavailable_instance_success(self, caplog):
        """Test delete_unavailable_instance method with success case"""
        # Add instance to unavailable pool
        self.instance_manager._unavailable_pool[1] = self.prefill_instance

        # Delete instance
        await self.instance_manager.delete_unavailable_instance(1)

        # Verify instance was deleted
        assert 1 not in self.instance_manager._unavailable_pool
        assert "Deleted unavailable instance with ID 1 successfully" in caplog.text

    @pytest.mark.asyncio
    async def test_delete_unavailable_instance_not_found(self, caplog):
        """Test delete_unavailable_instance method when instance not found"""
        # Try to delete non-existent instance
        await self.instance_manager.delete_unavailable_instance(999)

        # Verify warning was logged
        assert "Instance ID 999 not found in unavailable instance pool yet" in caplog.text

    @pytest.mark.asyncio
    async def test_update_instance_state_to_available_success(self, caplog):
        """Test update_instance_state method to make instance available"""
        # Add instance to unavailable pool
        unavailable_instance = Instance(
            job_name="test-unavailable", model_name="test-model", id=1, role=PDRole.ROLE_P, endpoints={}
        )
        self.instance_manager._unavailable_pool[1] = unavailable_instance

        # Update instance state to available
        await self.instance_manager.update_instance_state(1, UpdateInstanceMode.AVAILABLE)

        # Verify instance was moved to available pool
        assert 1 not in self.instance_manager._unavailable_pool
        assert 1 in self.instance_manager._prefill_pool
        assert self.instance_manager._prefill_pool[1] == unavailable_instance
        assert "Instance ID 1 updated to available successfully" in caplog.text

    @pytest.mark.asyncio
    async def test_update_instance_state_to_available_not_found(self, caplog):
        """Test update_instance_state method when instance not found in unavailable pool"""
        # Try to update non-existent instance to available
        await self.instance_manager.update_instance_state(999, UpdateInstanceMode.AVAILABLE)

        # Verify warning was logged
        assert "Instance ID 999 not found in unavailable instance pool" in caplog.text

    @pytest.mark.asyncio
    async def test_update_instance_state_to_unavailable_success(self, caplog):
        """Test update_instance_state method to make instance unavailable"""
        # Add instance to available pool properly
        self.instance_manager._add_instance_to_available_pool(self.prefill_instance)

        # Update instance state to unavailable
        await self.instance_manager.update_instance_state(1, UpdateInstanceMode.UNAVAILABLE)

        # Verify instance was moved to unavailable pool
        assert 1 not in self.instance_manager._prefill_pool
        assert 1 in self.instance_manager._unavailable_pool
        assert self.instance_manager._unavailable_pool[1] == self.prefill_instance
        assert "Instance ID 1 updated to unavailable successfully" in caplog.text

    @pytest.mark.asyncio
    async def test_update_instance_state_to_unavailable_not_found(self, caplog):
        """Test update_instance_state method when instance not found in available pool"""
        # Try to update non-existent instance to unavailable
        await self.instance_manager.update_instance_state(999, UpdateInstanceMode.UNAVAILABLE)

        # Verify warning was logged
        assert "Instance ID 999 not found in available instance pool, cannot update to unavailable" in caplog.text

    @pytest.mark.asyncio
    async def test_refresh_instances_add_success(self, caplog):
        """Test refresh_instances method with ADD event"""
        # Create instances to add
        instances = [self.prefill_instance, self.decode_instance]

        # Refresh instances with ADD event
        await self.instance_manager.refresh_instances(EventType.ADD, instances)

        # Verify instances were added
        assert 1 in self.instance_manager._prefill_pool
        assert self.instance_manager._prefill_pool[1] == self.prefill_instance
        assert 2 in self.instance_manager._decode_pool
        assert self.instance_manager._decode_pool[2] == self.decode_instance
        # Implementation logs "with N endpoints to available pool successfully"
        assert "Added instance ID 1" in caplog.text and "to available pool successfully" in caplog.text
        assert "Added instance ID 2" in caplog.text

    @pytest.mark.asyncio
    async def test_refresh_instances_add_duplicate_in_available_pool(self, caplog):
        """Test refresh_instances method with ADD event for duplicate instance in available pool"""
        # Add instance first properly
        self.instance_manager._add_instance_to_available_pool(self.prefill_instance)

        # Try to add the same instance again
        instances = [self.prefill_instance]
        await self.instance_manager.refresh_instances(EventType.ADD, instances)

        # Verify warning was logged
        assert "Instance ID 1 (role: prefill, job_name: test-prefill) already exists in available pool" in caplog.text

    @pytest.mark.asyncio
    async def test_refresh_instances_add_duplicate_in_unavailable_pool(self, caplog):
        """Test refresh_instances method with ADD event for duplicate instance in unavailable pool"""
        # Add instance to unavailable pool
        self.instance_manager._unavailable_pool[1] = self.prefill_instance

        # Try to add the same instance again
        instances = [self.prefill_instance]
        await self.instance_manager.refresh_instances(EventType.ADD, instances)

        # Verify warning was logged
        assert "Instance ID 1 (role: prefill, job_name: test-prefill) already exists in unavailable pool" in caplog.text

    @pytest.mark.asyncio
    async def test_refresh_instances_add_unknown_role(self):
        """Test refresh_instances with ADD event for unknown role: PDRole('unknown_role') raises ValueError."""
        unknown_instance = Instance(
            job_name="test-unknown", model_name="test-model", id=1, role="unknown_role", endpoints={}
        )
        with pytest.raises(ValueError, match="unknown_role|valid PDRole"):
            await self.instance_manager.refresh_instances(EventType.ADD, [unknown_instance])

    @pytest.mark.asyncio
    async def test_refresh_instances_del_success(self, caplog):
        """Test refresh_instances method with DEL event"""
        # Add both instances to available pool first, then DEL removes only prefill
        self.instance_manager._add_instance_to_available_pool(self.prefill_instance)
        self.instance_manager._add_instance_to_available_pool(self.decode_instance)

        instances = [self.prefill_instance]
        await self.instance_manager.refresh_instances(EventType.DEL, instances)

        # Verify instance was deleted
        assert 1 not in self.instance_manager._prefill_pool
        assert 2 in self.instance_manager._decode_pool
        assert (
            "Deleted instance ID 1 (role: prefill, job_name: test-prefill) from available pool successfully"
            in caplog.text
        )

    @pytest.mark.asyncio
    async def test_refresh_instances_del_from_unavailable(self, caplog):
        """Test refresh_instances method with DEL event for unavailable instance"""
        # Add instance to unavailable pool
        self.instance_manager._unavailable_pool[1] = self.prefill_instance

        # Refresh instances with DEL event
        instances = [self.prefill_instance]
        await self.instance_manager.refresh_instances(EventType.DEL, instances)

        # Verify instance was deleted from unavailable pool
        assert 1 not in self.instance_manager._unavailable_pool
        assert (
            "Deleted instance ID 1 (role: prefill, job_name: test-prefill) from unavailable pool successfully"
            in caplog.text
        )

    @pytest.mark.asyncio
    async def test_refresh_instances_del_not_found(self, caplog):
        """Test refresh_instances method with DEL event for non-existent instance"""
        # Try to delete non-existent instance
        instances = [self.prefill_instance]
        await self.instance_manager.refresh_instances(EventType.DEL, instances)

        # Verify warning was logged
        assert "Instance ID 1 (role: prefill, job_name: test-prefill) not found in instance pool" in caplog.text

    @pytest.mark.asyncio
    async def test_refresh_instances_set_success(self, caplog):
        """Test refresh_instances method with SET event"""
        # Create new instances for setting
        new_prefill_instance = Instance(
            job_name="new-prefill", model_name="test-model", id=10, role=PDRole.ROLE_P, endpoints={}
        )
        new_decode_instance = Instance(
            job_name="new-decode", model_name="test-model", id=20, role=PDRole.ROLE_D, endpoints={}
        )

        instances = [new_prefill_instance, new_decode_instance]
        changed = await self.instance_manager.refresh_instances(EventType.SET, instances)
        assert changed is True
        # Verify pools were set correctly
        assert len(self.instance_manager._prefill_pool) == 1
        assert len(self.instance_manager._decode_pool) == 1
        assert 10 in self.instance_manager._prefill_pool
        assert 20 in self.instance_manager._decode_pool
        assert "Added instance ID 10" in caplog.text and "to available pool successfully" in caplog.text
        assert "Added instance ID 20" in caplog.text

    @pytest.mark.asyncio
    async def test_refresh_instances_set_with_existing_instances(self, caplog):
        """Test refresh_instances with SET when pools have instances: SET applies diff (remove P, add D)."""
        self.instance_manager._add_instance_to_available_pool(self.prefill_instance)
        instances = [self.decode_instance]
        changed = await self.instance_manager.refresh_instances(EventType.SET, instances)
        assert changed is True
        assert "SET: removing" in caplog.text and "adding" in caplog.text
        assert len(self.instance_manager._prefill_pool) == 0
        assert len(self.instance_manager._decode_pool) == 1
        assert self.decode_instance.id in self.instance_manager._decode_pool

    @pytest.mark.asyncio
    async def test_refresh_instances_unknown_event(self, caplog):
        """Test refresh_instances method with unknown event type"""
        # Try to refresh with unknown event type
        instances = [self.prefill_instance]
        await self.instance_manager.refresh_instances("unknown", instances)

        # Verify error was logged
        assert "Unknown event type: unknown" in caplog.text

    def test_add_instances_unknown_role(self):
        """Test _add_instances with unknown role: PDRole('unknown_role') raises ValueError."""
        unknown_instance = Instance(
            job_name="test-unknown", model_name="test-model", id=1, role="unknown_role", endpoints={}
        )

        # _add_instance_to_available_pool uses _role_to_pdrole(instance.role); invalid role raises
        with pytest.raises(ValueError, match="unknown_role|valid PDRole"):
            self.instance_manager._add_instances([unknown_instance])

    @pytest.mark.asyncio
    async def test_delete_instances_empty_pools(self):
        """Test _delete_instances path when pools are empty (via refresh_instances DEL)"""
        await self.instance_manager.refresh_instances(EventType.DEL, [self.prefill_instance])
        assert True

    @pytest.mark.asyncio
    async def test_set_instances_empty_pools(self):
        """Test _apply_set_diff when pools are empty: SET adds new instance."""
        new_prefill_instance = Instance(
            job_name="new-prefill", model_name="test-model", id=10, role=PDRole.ROLE_P, endpoints={}
        )
        changed = await self.instance_manager.refresh_instances(EventType.SET, [new_prefill_instance])
        assert changed is True
        assert len(self.instance_manager._prefill_pool) == 1
        assert 10 in self.instance_manager._prefill_pool

    @pytest.mark.asyncio
    async def test_set_instances_no_diff_returns_false(self, caplog):
        """SET with same instance set as current: no change, returns False."""
        self.instance_manager._add_instance_to_available_pool(self.prefill_instance)
        self.instance_manager._add_instance_to_available_pool(self.decode_instance)
        instances = [self.prefill_instance, self.decode_instance]
        changed = await self.instance_manager.refresh_instances(EventType.SET, instances)
        assert changed is False
        assert len(self.instance_manager._prefill_pool) == 1 and len(self.instance_manager._decode_pool) == 1

    @pytest.mark.asyncio
    async def test_set_instances_only_add(self, caplog):
        """SET adds one new instance, keeps existing."""
        self.instance_manager._add_instance_to_available_pool(self.prefill_instance)
        new_decode = Instance(job_name="new-decode", model_name="test-model", id=20, role=PDRole.ROLE_D, endpoints={})
        changed = await self.instance_manager.refresh_instances(EventType.SET, [self.prefill_instance, new_decode])
        assert changed is True
        assert len(self.instance_manager._prefill_pool) == 1
        assert len(self.instance_manager._decode_pool) == 1
        assert 20 in self.instance_manager._decode_pool

    @pytest.mark.asyncio
    async def test_set_instances_only_remove(self, caplog):
        """SET removes one instance, keeps the other."""
        self.instance_manager._add_instance_to_available_pool(self.prefill_instance)
        self.instance_manager._add_instance_to_available_pool(self.decode_instance)
        changed = await self.instance_manager.refresh_instances(EventType.SET, [self.prefill_instance])
        assert changed is True
        assert len(self.instance_manager._prefill_pool) == 1
        assert len(self.instance_manager._decode_pool) == 0

    @pytest.mark.asyncio
    async def test_set_instances_empty_list_clears_all(self):
        """SET with empty list removes all instances."""
        self.instance_manager._add_instance_to_available_pool(self.prefill_instance)
        self.instance_manager._add_instance_to_available_pool(self.decode_instance)
        changed = await self.instance_manager.refresh_instances(EventType.SET, [])
        assert changed is True
        assert len(self.instance_manager._prefill_pool) == 0
        assert len(self.instance_manager._decode_pool) == 0
        assert len(self.instance_manager._available_pool) == 0


class TestInstanceManagerThreadSafety:
    """Thread safety test cases for InstanceManager"""

    # pylint: disable=attribute-defined-outside-init
    def setup_method(self):
        """Setup for each test method"""
        self.config = CoordinatorConfig()
        self.instance_manager = InstanceManager(self.config)

        # Test data
        self.prefill_instance = Instance(
            job_name="test-prefill",
            model_name="test-model",
            id=1,
            role=PDRole.ROLE_P,
            dispatch_capabilities=[DispatchPlan.CONCURRENT_ENGINE_SYNC.value],
            endpoints={},
        )

        self.decode_instance = Instance(
            job_name="test-decode",
            model_name="test-model",
            id=2,
            role=PDRole.ROLE_D,
            dispatch_capabilities=[DispatchPlan.CONCURRENT_ENGINE_SYNC.value],
            endpoints={},
        )

        self.hybrid_instance = Instance(
            job_name="test-hybrid", model_name="test-model", id=3, role=PDRole.ROLE_U, endpoints={}
        )

        self.endpoint = Endpoint(id=1, ip="127.0.0.1", business_port="8080", mgmt_port="8080")

    def test_concurrent_add_and_delete_instances(self):
        """Test concurrent add and delete operations on instances with enhanced concurrency"""
        # Add initial instances
        self.instance_manager._add_instance_to_available_pool(self.prefill_instance)
        self.instance_manager._add_instance_to_available_pool(self.decode_instance)

        # Thread-safe counters for tracking results
        add_results = []
        delete_results = []
        lock = threading.Lock()
        loop = asyncio.new_event_loop()

        def run_loop():
            loop.run_forever()

        loop_thread = threading.Thread(target=run_loop, daemon=True)
        loop_thread.start()

        def add_instance_task(iteration):
            try:
                instance = Instance(
                    job_name=f"test-add-{iteration}",
                    model_name="test-model",
                    id=1000 + iteration,
                    role=PDRole.ROLE_U,
                    endpoints={},
                )
                asyncio.run_coroutine_threadsafe(
                    self.instance_manager.refresh_instances(EventType.ADD, [instance]), loop
                ).result(timeout=10)
                with lock:
                    add_results.append(f"add_success_{iteration}")
            except Exception as e:
                with lock:
                    add_results.append(f"add_error_{iteration}: {str(e)}")

        def delete_instance_task(iteration):
            try:
                instance = Instance(
                    job_name=f"test-del-{iteration}",
                    model_name="test-model",
                    id=2000 + iteration,
                    role=PDRole.ROLE_P,
                    endpoints={},
                )
                asyncio.run_coroutine_threadsafe(
                    self.instance_manager.refresh_instances(EventType.ADD, [instance]), loop
                ).result(timeout=10)
                asyncio.run_coroutine_threadsafe(
                    self.instance_manager.refresh_instances(EventType.DEL, [instance]), loop
                ).result(timeout=10)
                with lock:
                    delete_results.append(f"delete_success_{iteration}")
            except Exception as e:
                with lock:
                    delete_results.append(f"delete_error_{iteration}: {str(e)}")

        # Run multiple concurrent operations
        threads = []

        for i in range(5):
            thread = threading.Thread(target=lambda i=i: [add_instance_task(j) for j in range(i * 50, (i + 1) * 50)])
            threads.append(thread)
            thread.start()

        for i in range(3):
            thread = threading.Thread(target=lambda i=i: [delete_instance_task(j) for j in range(i * 50, (i + 1) * 50)])
            threads.append(thread)
            thread.start()

        # Wait for all threads to complete
        for thread in threads:
            thread.join()

        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2)

        # Verify that operations succeeded
        add_success_count = sum(1 for r in add_results if "add_success" in r)
        delete_success_count = sum(1 for r in delete_results if "delete_success" in r)

        assert add_success_count > 0, f"Expected some add operations to succeed, got results: {add_results}"
        assert delete_success_count > 0, f"Expected some delete operations to succeed, got results: {delete_results}"

        # Verify final state consistency
        available_count = (
            len(self.instance_manager._prefill_pool)
            + len(self.instance_manager._decode_pool)
            + len(self.instance_manager._hybrid_pool)
        )
        unavailable_count = len(self.instance_manager._unavailable_pool)

        # Total instances should be reasonable (original 2 + added ones - deleted ones)
        total_instances = available_count + unavailable_count
        assert total_instances >= 0, f"Invalid instance count: {total_instances}"

    def test_concurrent_update_instance_state(self):
        """Test concurrent update_instance_state operations with enhanced concurrency"""
        # Add multiple instances to available pool
        instances = []
        for i in range(10):
            instance = Instance(
                job_name=f"test-{i}",
                model_name="test-model",
                id=100 + i,
                role=PDRole.ROLE_P if i % 2 == 0 else PDRole.ROLE_D,
                endpoints={},
            )
            self.instance_manager._add_instance_to_available_pool(instance)
            instances.append(instance)

        # Thread-safe counters for tracking results
        unavailable_results = []
        available_results = []
        lock = threading.Lock()
        loop = asyncio.new_event_loop()

        def run_loop():
            loop.run_forever()

        loop_thread = threading.Thread(target=run_loop, daemon=True)
        loop_thread.start()

        def make_unavailable_task(instance_id, iteration):
            try:
                asyncio.run_coroutine_threadsafe(
                    self.instance_manager.update_instance_state(instance_id, UpdateInstanceMode.UNAVAILABLE), loop
                ).result(timeout=10)
                with lock:
                    unavailable_results.append(f"to_unavailable_success_{iteration}")
            except Exception as e:
                with lock:
                    unavailable_results.append(f"to_unavailable_error_{iteration}: {str(e)}")

        def make_available_task(instance_id, iteration):
            try:
                asyncio.run_coroutine_threadsafe(
                    self.instance_manager.update_instance_state(instance_id, UpdateInstanceMode.AVAILABLE), loop
                ).result(timeout=10)
                with lock:
                    available_results.append(f"to_available_success_{iteration}")
            except Exception as e:
                with lock:
                    available_results.append(f"to_available_error_{iteration}: {str(e)}")

        # Run concurrent operations
        threads = []

        for i in range(4):
            thread = threading.Thread(
                target=lambda i=i: [make_unavailable_task(100 + (j % 10), j) for j in range(i * 30, (i + 1) * 30)]
            )
            threads.append(thread)
            thread.start()

        for i in range(4):
            thread = threading.Thread(
                target=lambda i=i: [make_available_task(100 + (j % 10), j) for j in range(i * 30, (i + 1) * 30)]
            )
            threads.append(thread)
            thread.start()

        # Wait for all threads to complete
        for thread in threads:
            thread.join()

        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2)

        # Verify that operations succeeded
        unavailable_success_count = sum(1 for r in unavailable_results if "to_unavailable_success" in r)
        available_success_count = sum(1 for r in available_results if "to_available_success" in r)

        assert unavailable_success_count > 0, (
            f"Expected some unavailable operations to succeed, got results: {unavailable_results}"
        )
        assert available_success_count > 0, (
            f"Expected some available operations to succeed, got results: {available_results}"
        )

        # Verify internal consistency
        available_count = (
            len(self.instance_manager._prefill_pool)
            + len(self.instance_manager._decode_pool)
            + len(self.instance_manager._hybrid_pool)
        )
        unavailable_count = len(self.instance_manager._unavailable_pool)

        # Total instances should equal the original count
        total_instances = available_count + unavailable_count
        assert total_instances == 10, f"Instance count mismatch: expected 10, got {total_instances}"

    def test_concurrent_update_workload(self):
        """Test concurrent update_instance_workload operations with enhanced concurrency"""
        # Add instance to available pool
        self.instance_manager._add_instance_to_available_pool(self.prefill_instance)

        # Add endpoint to instance
        self.prefill_instance.endpoints[1] = {1: self.endpoint}

        # Thread-safe counter for tracking results
        results = []
        lock = threading.Lock()
        loop = asyncio.new_event_loop()

        def run_loop():
            loop.run_forever()

        loop_thread = threading.Thread(target=run_loop, daemon=True)
        loop_thread.start()

        def update_workload_task(iteration, tokens, kv_cache):
            try:
                workload_change = Workload(active_tokens=tokens, active_kv_cache=kv_cache)
                asyncio.run_coroutine_threadsafe(
                    self.instance_manager.update_instance_workload(1, self.endpoint.id, workload_change), loop
                ).result(timeout=10)
                with lock:
                    results.append(f"update_success_{iteration}")
            except Exception as e:
                with lock:
                    results.append(f"update_error_{iteration}: {str(e)}")

        # Run concurrent update operations
        threads = []

        for i in range(5):
            thread = threading.Thread(
                target=lambda i=i: [update_workload_task(j, 10, 20) for j in range(i * 40, (i + 1) * 40)]
            )
            threads.append(thread)
            thread.start()

        for i in range(5):
            thread = threading.Thread(
                target=lambda i=i: [update_workload_task(j, -5, -10) for j in range(i * 40, (i + 1) * 40)]
            )
            threads.append(thread)
            thread.start()

        # Wait for all threads to complete
        for thread in threads:
            thread.join()

        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2)

        # Verify that operations succeeded
        success_count = sum(1 for r in results if "update_success" in r)
        assert success_count > 0, f"Expected some update operations to succeed, got results: {results}"

        # Verify that workload was updated (exact value depends on execution order)
        assert self.prefill_instance.gathered_workload.active_tokens >= 0, "Negative token count"
        assert self.prefill_instance.gathered_workload.active_kv_cache >= 0, "Negative KV cache count"
        assert self.endpoint.workload.active_tokens >= 0, "Negative endpoint token count"
        assert self.endpoint.workload.active_kv_cache >= 0, "Negative endpoint KV cache count"

    def test_concurrent_has_required_instances_calls(self):
        """Test concurrent has_required_instances calls under different conditions with enhanced concurrency"""
        self.instance_manager = InstanceManager(self.config)

        # Thread-safe counter for tracking results
        results = []
        lock = threading.Lock()

        def check_availability_task(iteration):
            try:
                result = self.instance_manager.has_required_instances()
                with lock:
                    results.append((iteration, result))
            except Exception as e:
                with lock:
                    results.append((iteration, f"error: {str(e)}"))

        # Run multiple concurrent availability checks when not available
        threads = []
        for i in range(8):
            thread = threading.Thread(
                target=lambda i=i: [check_availability_task(j) for j in range(i * 20, (i + 1) * 20)]
            )
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

        # All checks should return False (no instances added yet)
        false_count = sum(1 for _, r in results if r is False)
        assert false_count == 160, f"Expected all checks to return False, got {false_count} False out of {len(results)}"

        # Add instances to make it available
        self.instance_manager._add_instance_to_available_pool(self.prefill_instance)
        self.instance_manager._add_instance_to_available_pool(self.decode_instance)

        # Run multiple concurrent availability checks when available
        results = []
        threads = []
        for i in range(8):
            thread = threading.Thread(
                target=lambda i=i: [check_availability_task(j) for j in range(i * 20, (i + 1) * 20)]
            )
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

        # All checks should return True
        true_count = sum(1 for _, r in results if r is True)
        assert true_count == 160, f"Expected all checks to return True, got {true_count} True out of {len(results)}"

    def test_concurrent_get_available_instances(self):
        """Test concurrent get_available_instances calls with enhanced concurrency"""
        # Add instances
        for i in range(20):
            instance = Instance(
                job_name=f"test-{i}",
                model_name="test-model",
                id=100 + i,
                role=PDRole.ROLE_P if i % 2 == 0 else PDRole.ROLE_D,
                endpoints={},
            )
            self.instance_manager._add_instance_to_available_pool(instance)

        # Thread-safe counter for tracking results
        prefill_results = []
        decode_results = []
        lock = threading.Lock()

        def get_prefill_instances_task(iteration):
            try:
                instances = self.instance_manager.get_available_instances(PDRole.ROLE_P)
                with lock:
                    prefill_results.append((iteration, len(instances)))
            except Exception as e:
                with lock:
                    prefill_results.append((iteration, f"error: {str(e)}"))

        def get_decode_instances_task(iteration):
            try:
                instances = self.instance_manager.get_available_instances(PDRole.ROLE_D)
                with lock:
                    decode_results.append((iteration, len(instances)))
            except Exception as e:
                with lock:
                    decode_results.append((iteration, f"error: {str(e)}"))

        # Run concurrent get operations
        threads = []

        # Start 6 threads for prefill instances, each performing 30 iterations
        for i in range(6):
            thread = threading.Thread(
                target=lambda i=i: [get_prefill_instances_task(j) for j in range(i * 30, (i + 1) * 30)]
            )
            threads.append(thread)
            thread.start()

        # Start 6 threads for decode instances, each performing 30 iterations
        for i in range(6):
            thread = threading.Thread(
                target=lambda i=i: [get_decode_instances_task(j) for j in range(i * 30, (i + 1) * 30)]
            )
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

        # All operations should succeed and return correct counts
        prefill_success_count = sum(1 for _, r in prefill_results if isinstance(r, int))
        decode_success_count = sum(1 for _, r in decode_results if isinstance(r, int))

        assert prefill_success_count == 180, f"Expected 180 prefill operations to succeed, got {prefill_success_count}"
        assert decode_success_count == 180, f"Expected 180 decode operations to succeed, got {decode_success_count}"

        # Each should return 10 instances (20 total instances, half prefill, half decode)
        prefill_counts = [r for _, r in prefill_results if isinstance(r, int)]
        decode_counts = [r for _, r in decode_results if isinstance(r, int)]

        assert all(r == 10 for r in prefill_counts), f"Expected all prefill counts to be 10, got {prefill_counts}"
        assert all(r == 10 for r in decode_counts), f"Expected all decode counts to be 10, got {decode_counts}"

    def test_concurrent_get_all_instances(self):
        """Test concurrent get_all_instances calls with enhanced concurrency"""
        # Add instances to available pool
        for i in range(10):
            instance = Instance(
                job_name=f"test-available-{i}",
                model_name="test-model",
                id=100 + i,
                role=PDRole.ROLE_P if i % 2 == 0 else PDRole.ROLE_D,
                endpoints={},
            )
            self.instance_manager._add_instance_to_available_pool(instance)

        # Add instances to unavailable pool
        for i in range(5):
            instance = Instance(
                job_name=f"test-unavailable-{i}", model_name="test-model", id=200 + i, role=PDRole.ROLE_U, endpoints={}
            )
            self.instance_manager._unavailable_pool[200 + i] = instance

        # Thread-safe counter for tracking results
        results = []
        errors = []
        lock = threading.Lock()
        loop = asyncio.new_event_loop()

        def run_loop():
            loop.run_forever()

        loop_thread = threading.Thread(target=run_loop, daemon=True)
        loop_thread.start()

        def get_all_instances_task(iteration):
            try:
                available_pool, unavailable_pool = asyncio.run_coroutine_threadsafe(
                    self.instance_manager.get_all_instances(), loop
                ).result(timeout=10)
                with lock:
                    results.append((iteration, len(available_pool), len(unavailable_pool)))
            except Exception as e:
                with lock:
                    errors.append((iteration, str(e)))

        def modify_instances_task(iteration):
            try:
                if iteration % 2 == 0:
                    instance = Instance(
                        job_name=f"test-modify-available-{iteration}",
                        model_name="test-model",
                        id=300 + iteration,
                        role=PDRole.ROLE_P,
                        endpoints={},
                    )
                    asyncio.run_coroutine_threadsafe(
                        self.instance_manager.refresh_instances(EventType.ADD, [instance]), loop
                    ).result(timeout=10)
                else:
                    instance = Instance(
                        job_name=f"test-modify-unavailable-{iteration}",
                        model_name="test-model",
                        id=400 + iteration,
                        role=PDRole.ROLE_D,
                        endpoints={},
                    )
                    self.instance_manager._unavailable_pool[400 + iteration] = instance
            except Exception as e:
                with lock:
                    errors.append((iteration, str(e)))

        # Run concurrent get and modify operations
        threads = []

        for i in range(3):
            thread = threading.Thread(
                target=lambda i=i: [get_all_instances_task(j) for j in range(i * 40, (i + 1) * 40)]
            )
            threads.append(thread)
            thread.start()

        for i in range(3):
            thread = threading.Thread(
                target=lambda i=i: [modify_instances_task(j) for j in range(i * 40, (i + 1) * 40)]
            )
            threads.append(thread)
            thread.start()

        # Wait for all threads to complete
        for thread in threads:
            thread.join()

        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2)

        # Verify that operations succeeded
        assert len(errors) == 0, f"Expected no errors, got {len(errors)} errors: {errors}"

        # Verify that get operations returned valid results
        assert len(results) == 120, f"Expected 120 get operations to succeed, got {len(results)}"

        # Each get operation should return at least the initial counts
        # (10 available + added ones, 5 unavailable + added ones)
        for _, available_count, unavailable_count in results:
            assert available_count >= 10, f"Available count should be at least 10, got {available_count}"
            assert unavailable_count >= 5, f"Unavailable count should be at least 5, got {unavailable_count}"

    def test_large_scale_concurrent_operations(self):
        """Test large scale concurrent operations to stress test thread safety"""
        loop = asyncio.new_event_loop()

        def run_loop():
            loop.run_forever()

        loop_thread = threading.Thread(target=run_loop, daemon=True)
        loop_thread.start()

        def add_instance_task(instance_id, iteration):
            instance = Instance(
                job_name=f"test-add-{instance_id}-{iteration}",
                model_name="test-model",
                id=instance_id,
                role=PDRole.ROLE_P if instance_id % 2 == 0 else PDRole.ROLE_D,
                endpoints={},
            )
            try:
                asyncio.run_coroutine_threadsafe(
                    self.instance_manager.refresh_instances(EventType.ADD, [instance]), loop
                ).result(timeout=10)
                return f"add_{instance_id}_{iteration}_success"
            except Exception as e:
                return f"add_{instance_id}_{iteration}_error: {str(e)}"

        def delete_instance_task(instance_id, iteration):
            instance = Instance(
                job_name=f"test-del-{instance_id}-{iteration}",
                model_name="test-model",
                id=instance_id,
                role=PDRole.ROLE_P if instance_id % 2 == 0 else PDRole.ROLE_D,
                endpoints={},
            )
            try:
                asyncio.run_coroutine_threadsafe(
                    self.instance_manager.refresh_instances(EventType.DEL, [instance]), loop
                ).result(timeout=10)
                return f"del_{instance_id}_{iteration}_success"
            except Exception as e:
                return f"del_{instance_id}_{iteration}_error: {str(e)}"

        # Thread-safe counter for tracking results
        results = []
        lock = threading.Lock()

        # First add some instances (run in loop from main thread)
        for i in range(50):
            instance = Instance(
                job_name=f"initial-{i}",
                model_name="test-model",
                id=1000 + i,
                role=PDRole.ROLE_P if i % 2 == 0 else PDRole.ROLE_D,
                endpoints={},
            )
            asyncio.run_coroutine_threadsafe(
                self.instance_manager.refresh_instances(EventType.ADD, [instance]), loop
            ).result(timeout=10)

        def add_worker(start_id):
            local_results = []
            for i in range(start_id, start_id + 20):
                result = add_instance_task(2000 + i, i)
                local_results.append(result)
            with lock:
                results.extend(local_results)

        def delete_worker(start_id):
            local_results = []
            for i in range(start_id, start_id + 10):
                result = delete_instance_task(1000 + (i % 50), i)
                local_results.append(result)
            with lock:
                results.extend(local_results)

        # Run concurrent add/delete operations
        threads = []

        for i in range(10):
            thread = threading.Thread(target=add_worker, args=(i * 20,))
            threads.append(thread)
            thread.start()

        for i in range(10):
            thread = threading.Thread(target=delete_worker, args=(i * 10,))
            threads.append(thread)
            thread.start()

        # Wait for all threads to complete
        for thread in threads:
            thread.join()

        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2)

        # Verify that operations completed
        success_count = sum(1 for r in results if "success" in r)
        assert success_count > 0, f"Expected some operations to succeed, got results: {results}"

        # Verify internal consistency
        available_count = (
            len(self.instance_manager._prefill_pool)
            + len(self.instance_manager._decode_pool)
            + len(self.instance_manager._hybrid_pool)
        )
        unavailable_count = len(self.instance_manager._unavailable_pool)

        # Total instances should be reasonable
        total_instances = available_count + unavailable_count
        assert total_instances >= 0, f"Invalid instance count: {total_instances}"
        assert total_instances <= 300, f"Unexpectedly high instance count: {total_instances}"
