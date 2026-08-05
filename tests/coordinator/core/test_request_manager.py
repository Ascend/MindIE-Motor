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
import pytest
import time
from unittest.mock import patch
from unittest.mock import AsyncMock
from motor.common.resources.endpoint import Workload
from motor.common.resources.instance import PDRole
from motor.coordinator.domain.request_manager import RequestManager
from motor.coordinator.models.request import RequestInfo
from motor.config.coordinator import CoordinatorConfig


class TestRequestManager:
    """Test cases for RequestManager class"""

    @pytest.mark.asyncio
    async def test_generate_request_id_format(self):
        """Test that generated ID has correct format"""
        config = CoordinatorConfig()
        manager = RequestManager(config)
        request_id = await manager.generate_request_id()

        assert isinstance(request_id, str)
        assert len(request_id) == 28
        assert all(c in '0123456789abcdef' for c in request_id)

    @pytest.mark.asyncio
    async def test_generate_request_id_uniqueness(self):
        """Test that generated IDs are unique"""
        config = CoordinatorConfig()
        manager = RequestManager(config)
        ids = set()
        for _ in range(1000):
            new_id = await manager.generate_request_id()
            assert new_id not in ids
            ids.add(new_id)
        assert len(ids) == 1000

    @pytest.mark.asyncio
    async def test_generate_request_id_concurrent_async(self):
        """Test ID generation with concurrent async calls"""
        config = CoordinatorConfig()
        manager = RequestManager(config)

        async def generate_many():
            return [await manager.generate_request_id() for _ in range(100)]

        results = await asyncio.gather(*[generate_many() for _ in range(10)])
        generated_ids = {id_ for lst in results for id_ in lst}
        assert len(generated_ids) == 1000

    @pytest.mark.asyncio
    @patch('motor.coordinator.domain.request_manager.uuid.uuid4')
    async def test_fallback_uuid_generation(self, mock_uuid):
        """Test fallback to UUID when main algorithm fails"""
        config = CoordinatorConfig()
        manager = RequestManager(config)
        mock_uuid.return_value.hex = 'fallback1234567890abcdef1234567890'

        with patch.object(manager, '_lock', new_callable=AsyncMock) as mock_lock:
            mock_lock.__aenter__.side_effect = Exception("Test exception")
            request_id = await manager.generate_request_id()
            assert request_id == 'fallback1234567890abcdef1234567890'

    @pytest.mark.asyncio
    async def test_id_structure(self):
        """Test the structural components of generated ID"""
        config = CoordinatorConfig()
        manager = RequestManager(config)
        request_id = await manager.generate_request_id()

        # Extract components
        timestamp_part = request_id[:16]  # First 16 chars: timestamp
        counter_part = request_id[16:20]  # Next 4 chars: counter
        random_part = request_id[20:]  # Last 8 chars: random

        # Timestamp should be a valid microsecond timestamp
        assert timestamp_part.isdigit()
        timestamp_val = int(timestamp_part)
        current_micros = int(time.time() * 1000000)
        # Should be within a reasonable range (last 10 seconds)
        assert current_micros - timestamp_val <= 10000000

        # Counter should be 4-digit number
        assert counter_part.isdigit()
        assert len(counter_part) == 4

        # Random part should be 8 hex characters
        assert len(random_part) == 8
        assert all(c in '0123456789abcdef' for c in random_part)

    @pytest.mark.asyncio
    async def test_consecutive_ids_increment_counter(self):
        """Test that counter increments for same timestamp"""
        config = CoordinatorConfig()
        manager = RequestManager(config)
        fixed_time = 1691234567890123
        with patch('motor.coordinator.domain.request_manager.time.time') as mock_time:
            mock_time.return_value = fixed_time / 1000000
            id1 = await manager.generate_request_id()
            id2 = await manager.generate_request_id()
            id3 = await manager.generate_request_id()
        counter1, counter2, counter3 = int(id1[16:20]), int(id2[16:20]), int(id3[16:20])
        assert counter2 == counter1 + 1
        assert counter3 == counter2 + 1
        assert id1[:16] == id2[:16] == id3[:16]

    @pytest.mark.asyncio
    async def test_counter_reset_on_new_timestamp(self):
        """Test that counter resets when timestamp changes"""
        config = CoordinatorConfig()
        manager = RequestManager(config)
        with patch('motor.coordinator.domain.request_manager.time.time') as mock_time:
            mock_time.return_value = 1691234567.890123
            id1 = await manager.generate_request_id()
        with patch('motor.coordinator.domain.request_manager.time.time') as mock_time:
            mock_time.return_value = 1691234567.890124
            id2 = await manager.generate_request_id()
            counter2 = int(id2[16:20])
        assert counter2 == 0
        assert id1[:16] != id2[:16]

    @pytest.mark.asyncio
    async def test_add_req_info_and_del_req_info(self):
        config = CoordinatorConfig()
        manager = RequestManager(config)
        req_id = await manager.generate_request_id()
        req_info = RequestInfo(req_id=req_id, req_data={"test": "data"}, req_len=100, api="/test/api")
        result = await manager.add_req_info(req_info)
        assert result is True
        async with manager._lock:
            assert req_id in manager._req_info_dict
            assert manager._req_info_dict[req_id].req_id == req_id
        result = await manager.del_req_info(req_id)
        assert result is True
        async with manager._lock:
            assert req_id not in manager._req_info_dict
        result = await manager.del_req_info("non_existent_id")
        assert result is False

    @pytest.mark.asyncio
    async def test_concurrent_async_access(self):
        """Test concurrent async access to add_req_info and del_req_info"""
        config = CoordinatorConfig()
        manager = RequestManager(config)

        async def worker(worker_id: int):
            req_id = await manager.generate_request_id()
            req_info = RequestInfo(
                req_id=req_id,
                req_data={"worker_id": worker_id, "test": "data"},
                req_len=100,
                api=f"/test/api/{worker_id}",
            )
            add_result = await manager.add_req_info(req_info)
            del_result = await manager.del_req_info(req_id)
            return (add_result, del_result)

        tasks = [worker(i) for i in range(10)]
        out = await asyncio.gather(*tasks)
        for i, (add_ok, del_ok) in enumerate(out):
            assert add_ok is True, f"Add failed for worker {i}"
            assert del_ok is True, f"Delete failed for worker {i}"

    @pytest.mark.asyncio
    async def test_request_persistence(self):
        """Test that requests persist in the dictionary until deleted"""
        config = CoordinatorConfig()
        manager = RequestManager(config)
        req_ids = []
        for i in range(5):
            req_id = await manager.generate_request_id()
            req_info = RequestInfo(
                req_id=req_id, req_data={"index": i, "test": "data"}, req_len=100, api=f"/test/api/{i}"
            )
            await manager.add_req_info(req_info)
            req_ids.append(req_id)
        async with manager._lock:
            for req_id in req_ids:
                assert req_id in manager._req_info_dict
            await asyncio.sleep(0)  # allow lock to be released for next block
        await manager.del_req_info(req_ids[1])
        await manager.del_req_info(req_ids[3])
        async with manager._lock:
            assert req_ids[0] in manager._req_info_dict
            assert req_ids[1] not in manager._req_info_dict
            assert req_ids[2] in manager._req_info_dict
            assert req_ids[3] not in manager._req_info_dict
            assert req_ids[4] in manager._req_info_dict


@pytest.fixture
def request_manager():
    """Fixture to provide RequestManager instance"""
    config = CoordinatorConfig()
    return RequestManager(config)


@pytest.mark.asyncio
async def test_with_fixture(request_manager):
    """Test using pytest fixture"""
    id1 = await request_manager.generate_request_id()
    id2 = await request_manager.generate_request_id()
    assert isinstance(id1, str)
    assert isinstance(id2, str)
    assert id1 != id2


def test_update_config():
    """Test RequestManager update_config method"""
    # Create initial config
    initial_config = CoordinatorConfig()

    # Create request manager with initial config
    request_manager = RequestManager(initial_config)

    # Create new config with different values
    new_config = CoordinatorConfig()
    new_config.rate_limit_config.max_requests = 1000

    # Update config
    request_manager.update_config(new_config)

    # Verify config was updated
    assert request_manager._rate_limit_config.max_requests == 1000


# -------- Workload (add/get/update/del_req_workload) tests --------


@pytest.mark.asyncio
async def test_add_get_update_del_req_workload():
    """Test add_req_workload, get_req_workload, update_req_workload, del_req_workload."""
    config = CoordinatorConfig()
    manager = RequestManager(config)
    req_id = await manager.generate_request_id()
    role = PDRole.ROLE_P
    workload = Workload(active_tokens=5.0)

    # add
    ok = await manager.add_req_workload(req_id, role, workload)
    assert ok is True

    # get
    got = await manager.get_req_workload(req_id, role)
    assert got is not None
    assert got.active_tokens == 5.0

    # update (in-place style: pass updated Workload)
    updated = Workload(active_tokens=3.0)
    ok = await manager.update_req_workload(req_id, role, updated)
    assert ok is True
    got2 = await manager.get_req_workload(req_id, role)
    assert got2.active_tokens == 3.0

    # del
    ok = await manager.del_req_workload(req_id, role)
    assert ok is True
    got3 = await manager.get_req_workload(req_id, role)
    assert got3 is None


@pytest.mark.asyncio
async def test_add_req_workload_duplicate_returns_false():
    """Duplicate add_req_workload for same (req_id, role) returns False."""
    config = CoordinatorConfig()
    manager = RequestManager(config)
    req_id = await manager.generate_request_id()
    role = PDRole.ROLE_D
    w = Workload(active_tokens=1.0)

    assert await manager.add_req_workload(req_id, role, w) is True
    assert await manager.add_req_workload(req_id, role, w) is False


@pytest.mark.asyncio
async def test_get_req_workload_missing_returns_none():
    """get_req_workload for non-existent (req_id, role) returns None."""
    config = CoordinatorConfig()
    manager = RequestManager(config)
    got = await manager.get_req_workload("nonexistent", PDRole.ROLE_P)
    assert got is None
