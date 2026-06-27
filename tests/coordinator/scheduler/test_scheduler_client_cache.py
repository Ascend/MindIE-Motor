# -*- coding: utf-8 -*-
"""AsyncSchedulerClient cache-refresh and cold-start warm-up behavior."""

from unittest.mock import AsyncMock, Mock

import pytest

from motor.common.resources.instance import PDRole
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
