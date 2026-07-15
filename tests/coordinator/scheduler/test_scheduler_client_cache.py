# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""AsyncSchedulerClient cache-refresh and cold-start warm-up behavior."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from motor.common.resources.endpoint import Endpoint, EndpointStatus
from motor.common.resources.instance import Instance, PDRole
from motor.coordinator.scheduler.runtime.scheduler_client import (
    AsyncSchedulerClient,
    SchedulerClientConfig,
)


# --- Live workload / instance membership refresh before selection ---


@pytest.mark.asyncio
async def test_refresh_cache_pulls_instances_on_version_change():
    client = AsyncSchedulerClient(SchedulerClientConfig())
    reader = Mock()
    reader.read_and_patch_cache = Mock(return_value=(7, False))  # new version, heartbeat fresh
    client._workload_reader = reader
    client._last_instance_version = 6
    client._on_instance_refreshed = None
    client.get_available_instances = AsyncMock(return_value={})

    await client._refresh_cache_from_workload_reader()

    reader.read_and_patch_cache.assert_called_once()  # live workload patched into cache
    client.get_available_instances.assert_awaited_once()  # membership pulled
    assert client._last_instance_version == 7


@pytest.mark.asyncio
async def test_refresh_cache_patches_without_pull_when_version_unchanged():
    client = AsyncSchedulerClient(SchedulerClientConfig())
    reader = Mock()
    reader.read_and_patch_cache = Mock(return_value=(6, False))
    client._workload_reader = reader
    client._last_instance_version = 6
    client.get_available_instances = AsyncMock(return_value={})

    await client._refresh_cache_from_workload_reader()

    reader.read_and_patch_cache.assert_called_once()  # still patches live workload
    client.get_available_instances.assert_not_awaited()  # but no redundant membership pull


# --- Router cold-start warm-up so a cold cache pulls instead of 503 ---


@pytest.mark.asyncio
async def test_get_available_instance_roles_warms_up_when_cache_cold():
    client = AsyncSchedulerClient(SchedulerClientConfig())
    client.get_available_instances = AsyncMock(return_value={})
    # First read sees an empty cache, second read (after warm-up) sees populated roles.
    client._roles_from_cache = Mock(side_effect=[set(), {PDRole.ROLE_P, PDRole.ROLE_D}])

    roles = await client.get_available_instance_roles()

    client.get_available_instances.assert_awaited_once()
    assert roles == {PDRole.ROLE_P, PDRole.ROLE_D}


@pytest.mark.asyncio
async def test_get_available_instance_roles_skips_warmup_when_cache_warm():
    client = AsyncSchedulerClient(SchedulerClientConfig())
    client.get_available_instances = AsyncMock(return_value={})
    client._roles_from_cache = Mock(return_value={PDRole.ROLE_U})

    roles = await client.get_available_instance_roles()

    client.get_available_instances.assert_not_awaited()
    assert roles == {PDRole.ROLE_U}


# --- Incremental instance-change delta (PUB) vs full-GET fallback ---


def _mk_instance(instance_id: int, role: PDRole, endpoint_id: int | None = None) -> Instance:
    inst = Instance(job_name=f"{role.value}-{instance_id}", model_name="m", id=instance_id, role=role, endpoints={})
    if endpoint_id is not None:
        inst.add_endpoints(
            f"pod-{instance_id}",
            {
                endpoint_id: Endpoint(
                    id=endpoint_id,
                    ip=f"10.0.0.{instance_id}",
                    business_port="8000",
                    mgmt_port="9000",
                    status=EndpointStatus.NORMAL,
                )
            },
        )
    return inst


@pytest.mark.asyncio
async def test_cache_apply_add_inserts_sorted_with_endpoints():
    cache = AsyncSchedulerClient(SchedulerClientConfig())._cache
    assert await cache.apply_add([_mk_instance(2, PDRole.ROLE_P, endpoint_id=20)])
    assert await cache.apply_add([_mk_instance(1, PDRole.ROLE_P, endpoint_id=10)])

    assert [i.id for i in cache.get_instances(PDRole.ROLE_P)] == [1, 2]  # kept sorted by id
    assert cache._endpoint_map.get((1, 10)) is not None
    assert cache._endpoint_map.get((2, 20)) is not None


@pytest.mark.asyncio
async def test_cache_apply_add_rejects_unknown_role_without_mutation(caplog):
    cache = AsyncSchedulerClient(SchedulerClientConfig())._cache
    unknown_instance = SimpleNamespace(id=9, role="role_x", endpoints={})

    assert not await cache.apply_add([unknown_instance])
    assert cache.get_instances(PDRole.ROLE_P) == []
    assert "unknown role" in caplog.text


@pytest.mark.asyncio
async def test_cache_apply_add_replaces_prior_role_and_endpoints():
    cache = AsyncSchedulerClient(SchedulerClientConfig())._cache
    await cache.apply_add([_mk_instance(1, PDRole.ROLE_P, endpoint_id=10)])

    await cache.apply_add([_mk_instance(1, PDRole.ROLE_D, endpoint_id=20)])

    assert cache.get_instances(PDRole.ROLE_P) == []
    assert [instance.id for instance in cache.get_instances(PDRole.ROLE_D)] == [1]
    assert (1, 10) not in cache._endpoint_map
    assert (1, 20) in cache._endpoint_map


@pytest.mark.asyncio
async def test_cache_apply_remove_drops_instance_and_endpoints():
    cache = AsyncSchedulerClient(SchedulerClientConfig())._cache
    await cache.apply_add([_mk_instance(1, PDRole.ROLE_P, endpoint_id=10)])
    await cache.apply_add([_mk_instance(2, PDRole.ROLE_P, endpoint_id=20)])

    await cache.apply_remove([_mk_instance(1, PDRole.ROLE_P)])

    assert [i.id for i in cache.get_instances(PDRole.ROLE_P)] == [2]
    assert (1, 10) not in cache._endpoint_map
    assert (2, 20) in cache._endpoint_map


@pytest.mark.asyncio
async def test_notify_applies_add_delta_without_full_get():
    client = AsyncSchedulerClient(SchedulerClientConfig())
    client._last_instance_version = 5
    client.get_available_instances = AsyncMock(return_value={})  # must NOT be called on a delta
    inst_dict = _mk_instance(7, PDRole.ROLE_P).model_dump(mode="json")

    await client._on_instance_change_notify(6, {"event": "add", "instances": [inst_dict]})

    client.get_available_instances.assert_not_awaited()
    assert client._last_instance_version == 6
    assert [i.id for i in client._cache.get_instances(PDRole.ROLE_P)] == [7]


@pytest.mark.asyncio
async def test_notify_add_delta_rejected_by_cache_falls_back_to_full_get():
    client = AsyncSchedulerClient(SchedulerClientConfig())
    client._last_instance_version = 5
    client.get_available_instances = AsyncMock(return_value={})
    client._cache.apply_add = AsyncMock(return_value=False)
    inst_dict = _mk_instance(7, PDRole.ROLE_P).model_dump(mode="json")

    await client._on_instance_change_notify(6, {"event": "add", "instances": [inst_dict]})

    client.get_available_instances.assert_awaited_once()
    assert client._last_instance_version == 6


@pytest.mark.asyncio
async def test_notify_version_gap_falls_back_to_full_get():
    client = AsyncSchedulerClient(SchedulerClientConfig())
    client._last_instance_version = 5
    client._on_instance_refreshed = None
    client.get_available_instances = AsyncMock(return_value={})
    inst_dict = _mk_instance(7, PDRole.ROLE_P).model_dump(mode="json")

    await client._on_instance_change_notify(7, {"event": "add", "instances": [inst_dict]})

    client.get_available_instances.assert_awaited_once()
    assert client._last_instance_version == 7
    assert client._cache.get_instances(PDRole.ROLE_P) == []


@pytest.mark.asyncio
async def test_notify_partial_decode_failure_falls_back_to_full_get():
    client = AsyncSchedulerClient(SchedulerClientConfig())
    client._last_instance_version = 5
    client._on_instance_refreshed = None
    client.get_available_instances = AsyncMock(return_value={})
    valid_instance = _mk_instance(7, PDRole.ROLE_P).model_dump(mode="json")

    await client._on_instance_change_notify(
        6,
        {"event": "add", "instances": [valid_instance, {"invalid": "instance"}]},
    )

    client.get_available_instances.assert_awaited_once()
    assert client._last_instance_version == 6
    assert client._cache.get_instances(PDRole.ROLE_P) == []


@pytest.mark.asyncio
async def test_notify_applies_del_delta_without_full_get():
    client = AsyncSchedulerClient(SchedulerClientConfig())
    await client._cache.apply_add([_mk_instance(7, PDRole.ROLE_P)])
    client._last_instance_version = 5
    client.get_available_instances = AsyncMock(return_value={})
    inst_dict = _mk_instance(7, PDRole.ROLE_P).model_dump(mode="json")

    await client._on_instance_change_notify(6, {"event": "del", "instances": [inst_dict]})

    client.get_available_instances.assert_not_awaited()
    assert [i.id for i in client._cache.get_instances(PDRole.ROLE_P)] == []
    assert client._last_instance_version == 6


@pytest.mark.asyncio
async def test_notify_without_delta_falls_back_to_full_get():
    client = AsyncSchedulerClient(SchedulerClientConfig())
    client._last_instance_version = 5
    client._on_instance_refreshed = None
    client.get_available_instances = AsyncMock(return_value={})

    await client._on_instance_change_notify(6, None)  # old 2-frame format / no delta

    client.get_available_instances.assert_awaited_once()
    assert client._last_instance_version == 6


@pytest.mark.asyncio
async def test_notify_unhandled_event_falls_back_to_full_get():
    client = AsyncSchedulerClient(SchedulerClientConfig())
    client._last_instance_version = 5
    client._on_instance_refreshed = None
    client.get_available_instances = AsyncMock(return_value={})

    await client._on_instance_change_notify(6, {"event": "set", "instances": []})  # SET -> full pull

    client.get_available_instances.assert_awaited_once()


@pytest.mark.asyncio
async def test_notify_del_delta_to_empty_still_notifies_refreshed():
    """A DEL delta that drains the last instance still fires on_instance_refreshed with an empty
    list, so downstream cleanup (e.g. the HTTP client pool) prunes clients for the removed
    endpoints instead of leaking them until the next non-empty refresh.
    """
    client = AsyncSchedulerClient(SchedulerClientConfig())
    await client._cache.apply_add([_mk_instance(7, PDRole.ROLE_P, endpoint_id=70)])
    client._last_instance_version = 5
    client.get_available_instances = AsyncMock(return_value={})
    refreshed = AsyncMock()
    client._on_instance_refreshed = refreshed
    inst_dict = _mk_instance(7, PDRole.ROLE_P, endpoint_id=70).model_dump(mode="json")

    await client._on_instance_change_notify(6, {"event": "del", "instances": [inst_dict]})

    client.get_available_instances.assert_not_awaited()
    assert client._cache.get_instances(PDRole.ROLE_P) == []
    refreshed.assert_awaited_once_with([])


@pytest.mark.asyncio
async def test_notify_full_pull_with_no_active_endpoints_still_notifies_refreshed():
    """The full-GET fallback fires on_instance_refreshed even when no endpoints remain active, so a
    drain observed via full pull still triggers downstream cleanup.
    """
    client = AsyncSchedulerClient(SchedulerClientConfig())
    client._last_instance_version = 5
    client.get_available_instances = AsyncMock(return_value={})  # leaves the cache empty
    refreshed = AsyncMock()
    client._on_instance_refreshed = refreshed

    await client._on_instance_change_notify(6, None)  # no delta -> full pull path

    client.get_available_instances.assert_awaited_once()
    refreshed.assert_awaited_once_with([])
