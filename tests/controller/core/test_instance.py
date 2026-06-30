#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.


from motor.common.resources.instance import Instance, ReadOnlyInstance, ParallelConfig
from motor.common.resources.endpoint import Endpoint, EndpointStatus


def test_instance_active() -> None:
    parallel_config = ParallelConfig(dp_size=2, tp_size=2)
    pod_ip = "127.0.0.1"
    endpoints = {
        1: Endpoint(id=1, ip=pod_ip, business_port="1001", mgmt_port="9001"),
        2: Endpoint(id=2, ip=pod_ip, business_port="1002", mgmt_port="9002"),
    }
    instance = Instance(
        job_name="test_active", model_name="test_model", id=1, role="prefill", parallel_config=parallel_config
    )
    instance.add_endpoints(pod_ip, endpoints)
    assert instance.get_endpoints_num() == len(endpoints)


def test_add_endpoints() -> None:
    parallel_config = ParallelConfig(dp_size=4, tp_size=2)
    pod_ip1 = "127.0.0.1"
    endpoints1 = {
        1: Endpoint(id=1, ip=pod_ip1, business_port="1001", mgmt_port="9001"),
        2: Endpoint(id=2, ip=pod_ip1, business_port="1002", mgmt_port="9002"),
    }
    instance = Instance(
        job_name="test_add_endpoints", model_name="test_model", id=1, role="prefill", parallel_config=parallel_config
    )
    instance.add_endpoints(pod_ip1, endpoints1)


def test_del_endpoints() -> None:
    parallel_config = ParallelConfig(dp_size=2, tp_size=2)
    pod_ip = "127.0.0.1"
    endpoints = {
        1: Endpoint(id=1, ip=pod_ip, business_port="1001", mgmt_port="9001"),
        2: Endpoint(id=2, ip=pod_ip, business_port="1002", mgmt_port="9002"),
    }
    instance = Instance(
        job_name="test_del_endpoints", model_name="test_model", id=1, role="prefill", parallel_config=parallel_config
    )
    instance.add_endpoints(pod_ip, endpoints)
    assert instance.get_endpoints_num() == len(endpoints)
    instance.del_endpoints(pod_ip)
    assert instance.get_endpoints_num() == 0


def test_readonly_instance_get_instance() -> None:
    """Test ReadOnlyInstance get_instance method"""
    # Create an instance
    instance = Instance(job_name="test_readonly", model_name="test_model", id=1, role="prefill")

    # Wrap it in ReadOnlyInstance
    readonly_instance = ReadOnlyInstance(instance)

    # Test get_instance method
    retrieved_instance = readonly_instance.get_instance()
    assert retrieved_instance is instance
    assert retrieved_instance.job_name == "test_readonly"
    assert retrieved_instance.model_name == "test_model"
    assert retrieved_instance.id == 1
    assert retrieved_instance.role == "prefill"


def test_readonly_instance_delegation() -> None:
    """Test ReadOnlyInstance attribute delegation"""
    # Create an instance with some data
    instance = Instance(job_name="test_delegation", model_name="test_model", id=1, role="prefill")

    # Wrap it in ReadOnlyInstance
    readonly_instance = ReadOnlyInstance(instance)

    # Test attribute access delegation
    assert readonly_instance.job_name == "test_delegation"
    assert readonly_instance.model_name == "test_model"
    assert readonly_instance.id == 1
    assert readonly_instance.role == "prefill"

    # Test method delegation
    assert readonly_instance.get_endpoints_num() == 0


def test_readonly_instance_modification_blocking() -> None:
    """Test ReadOnlyInstance blocks modification methods"""
    # Create an instance
    instance = Instance(job_name="test_blocking", model_name="test_model", id=1, role="prefill")

    # Wrap it in ReadOnlyInstance
    readonly_instance = ReadOnlyInstance(instance)

    # Test that modification methods are blocked
    try:
        readonly_instance.update_instance_status("inactive")
        assert False, "Should have raised AttributeError"
    except AttributeError as e:
        assert "does not allow modification method 'update_instance_status'" in str(e)


def test_readonly_instance_to_instance() -> None:
    """Test ReadOnlyInstance to_instance method"""
    # Create an instance with some data
    instance = Instance(job_name="test_to_instance", model_name="test_model", id=1, role="prefill")
    instance.status = "active"

    # Wrap it in ReadOnlyInstance
    readonly_instance = ReadOnlyInstance(instance)

    # Test to_instance method creates a deep copy
    copied_instance = readonly_instance.to_instance()

    # Verify it's a different object
    assert copied_instance is not instance
    assert copied_instance is not readonly_instance.get_instance()

    # Verify data is copied correctly
    assert copied_instance.job_name == "test_to_instance"
    assert copied_instance.model_name == "test_model"
    assert copied_instance.id == 1
    assert copied_instance.role == "prefill"
    assert copied_instance.status == "active"

    # Verify that modifying the copy doesn't affect the original
    copied_instance.job_name = "modified_job"
    assert instance.job_name == "test_to_instance"
    assert readonly_instance.job_name == "test_to_instance"


def test_is_endpoints_enough_equal_dp_size() -> None:
    """Test is_endpoints_enough returns True when endpoints equal dp size"""
    parallel_config = ParallelConfig(dp_size=2, tp_size=2)
    pod_ip = "127.0.0.1"
    endpoints = {
        1: Endpoint(id=1, ip=pod_ip, business_port="1001", mgmt_port="9001"),
        2: Endpoint(id=2, ip=pod_ip, business_port="1002", mgmt_port="9002"),
    }
    instance = Instance(
        job_name="test_endpoints_equal", model_name="test_model", id=1, role="prefill", parallel_config=parallel_config
    )
    instance.add_endpoints(pod_ip, endpoints)

    assert instance.is_endpoints_enough() is True
    assert instance.get_endpoints_num() == 2


def test_is_endpoints_enough_greater_than_dp_size() -> None:
    """Test is_endpoints_enough returns False when endpoints greater than dp size"""
    parallel_config = ParallelConfig(dp_size=2, tp_size=2)
    pod_ip = "127.0.0.1"
    endpoints = {
        1: Endpoint(id=1, ip=pod_ip, business_port="1001", mgmt_port="9001"),
        2: Endpoint(id=2, ip=pod_ip, business_port="1002", mgmt_port="9002"),
        3: Endpoint(id=3, ip=pod_ip, business_port="1003", mgmt_port="9003"),  # Extra endpoint
    }
    instance = Instance(
        job_name="test_endpoints_greater",
        model_name="test_model",
        id=1,
        role="prefill",
        parallel_config=parallel_config,
    )
    instance.add_endpoints(pod_ip, endpoints)

    assert instance.is_endpoints_enough() is False
    assert instance.get_endpoints_num() == 3


def test_is_endpoints_enough_less_than_dp_size() -> None:
    """Test is_endpoints_enough returns False when endpoints less than dp size"""
    parallel_config = ParallelConfig(dp_size=4, tp_size=2)
    pod_ip = "127.0.0.1"
    endpoints = {
        1: Endpoint(id=1, ip=pod_ip, business_port="1001", mgmt_port="9001"),
        2: Endpoint(id=2, ip=pod_ip, business_port="1002", mgmt_port="9002"),
    }
    instance = Instance(
        job_name="test_endpoints_less", model_name="test_model", id=1, role="prefill", parallel_config=parallel_config
    )
    instance.add_endpoints(pod_ip, endpoints)

    assert instance.is_endpoints_enough() is False
    assert instance.get_endpoints_num() == 2


def test_is_endpoints_enough_no_endpoints() -> None:
    """Test is_endpoints_enough returns False when no endpoints"""
    parallel_config = ParallelConfig(dp_size=2, tp_size=2)
    instance = Instance(
        job_name="test_no_endpoints", model_name="test_model", id=1, role="prefill", parallel_config=parallel_config
    )

    assert instance.is_endpoints_enough() is False
    assert instance.get_endpoints_num() == 0


# ===== Headless Endpoint Filtering Tests (Cross-Node PCP) =====


def test_get_all_endpoints_filters_headless() -> None:
    """get_all_endpoints skips endpoints marked headless (PCP slave nodes)."""
    instance = Instance(
        job_name="test_headless_filter",
        model_name="test_model",
        id=1,
        role="prefill",
        parallel_config=ParallelConfig(dp_size=1, tp_size=4),
        enable_multi_endpoints=True,
    )
    # Master node endpoint
    instance.add_endpoints("10.0.0.1", {0: Endpoint(id=0, ip="10.0.0.1", business_port="8000", mgmt_port="9000")})
    # Slave node endpoint (headless)
    instance.add_endpoints(
        "10.0.0.2",
        {0: Endpoint(id=1, ip="10.0.0.2", business_port="8000", mgmt_port="9000", headless=True)},
    )
    endpoints = instance.get_all_endpoints()
    assert len(endpoints) == 1
    assert endpoints[0].ip == "10.0.0.1"


def test_get_all_endpoints_no_filter_when_no_headless() -> None:
    """All endpoints returned when none are headless."""
    instance = Instance(
        job_name="test_no_headless",
        model_name="test_model",
        id=1,
        role="prefill",
        parallel_config=ParallelConfig(dp_size=2, tp_size=4),
        enable_multi_endpoints=True,
    )
    instance.add_endpoints("10.0.0.1", {0: Endpoint(id=0, ip="10.0.0.1", business_port="8000", mgmt_port="9000")})
    instance.add_endpoints("10.0.0.2", {0: Endpoint(id=1, ip="10.0.0.2", business_port="8000", mgmt_port="9000")})
    endpoints = instance.get_all_endpoints()
    assert len(endpoints) == 2


def test_get_all_endpoints_filters_headless_multi_endpoints_disabled() -> None:
    """headless filter works with enable_multi_endpoints=False (only id=0 + non-headless)."""
    instance = Instance(
        job_name="test_headless_single_endpoint",
        model_name="test_model",
        id=1,
        role="prefill",
        parallel_config=ParallelConfig(dp_size=1, tp_size=4),
        enable_multi_endpoints=False,
    )
    # Master (id=0)
    instance.add_endpoints("10.0.0.1", {0: Endpoint(id=0, ip="10.0.0.1", business_port="8000", mgmt_port="9000")})
    # Slave (id=0, headless) — should be BOTH filtered by id=0 rule AND headless
    instance.add_endpoints(
        "10.0.0.2",
        {0: Endpoint(id=0, ip="10.0.0.2", business_port="8000", mgmt_port="9000", headless=True)},
    )
    endpoints = instance.get_all_endpoints()
    assert len(endpoints) == 1
    assert endpoints[0].ip == "10.0.0.1"


def test_get_all_endpoints_single_pod_all_headless() -> None:
    """If all endpoints are headless, get_all_endpoints returns empty tuple."""
    instance = Instance(
        job_name="test_all_headless",
        model_name="test_model",
        id=1,
        role="prefill",
        parallel_config=ParallelConfig(dp_size=1, tp_size=4),
        enable_multi_endpoints=True,
    )
    instance.add_endpoints(
        "10.0.0.1",
        {0: Endpoint(id=0, ip="10.0.0.1", business_port="8000", mgmt_port="9000", headless=True)},
    )
    endpoints = instance.get_all_endpoints()
    assert len(endpoints) == 0


def test_get_all_endpoints_include_headless() -> None:
    """include_headless=True returns ALL endpoints including headless ones."""
    instance = Instance(
        job_name="test_include_headless",
        model_name="test_model",
        id=1,
        role="prefill",
        parallel_config=ParallelConfig(dp_size=2, tp_size=4),
        enable_multi_endpoints=True,
    )
    instance.add_endpoints(
        "10.0.0.1",
        {0: Endpoint(id=0, ip="10.0.0.1", business_port="8000", mgmt_port="9000")},
    )
    instance.add_endpoints(
        "10.0.0.2",
        {0: Endpoint(id=1, ip="10.0.0.2", business_port="8000", mgmt_port="9000", headless=True)},
    )

    # Default: headless excluded
    assert len(instance.get_all_endpoints()) == 1
    assert instance.get_all_endpoints()[0].id == 0

    # include_headless=True: both endpoints returned
    eps = instance.get_all_endpoints(include_headless=True)
    assert len(eps) == 2
    assert {ep.id for ep in eps} == {0, 1}


def test_endpoint_headless_defaults_to_false() -> None:
    """Endpoint.headless defaults to False for backward compatibility."""
    endpoint = Endpoint(id=0, ip="10.0.0.1", business_port="8000", mgmt_port="9000")
    assert endpoint.headless is False


def test_is_endpoints_enough_counts_all_endpoints() -> None:
    """is_endpoints_enough counts all endpoints regardless of headless flag.
    This is safe because headless is only set when nnodes>1, and in that
    case _assemble_instance bypasses is_endpoints_enough entirely.
    """
    instance = Instance(
        job_name="test_eps_enough_all",
        model_name="test_model",
        id=1,
        role="prefill",
        parallel_config=ParallelConfig(dp_size=2, tp_size=4),
        enable_multi_endpoints=True,
    )
    instance.add_endpoints("10.0.0.1", {0: Endpoint(id=0, ip="10.0.0.1", business_port="8000", mgmt_port="9000")})
    instance.add_endpoints(
        "10.0.0.2",
        {0: Endpoint(id=1, ip="10.0.0.2", business_port="8000", mgmt_port="9000", headless=True)},
    )
    # Both endpoints count (2 == dp_size=2), even with headless
    assert instance.is_endpoints_enough() is True


def test_is_any_endpoint_paused() -> None:
    """Test is_any_endpoint_paused() detects partial and full PAUSED states"""
    parallel_config = ParallelConfig(dp_size=2, tp_size=2)
    instance = Instance(
        job_name="test_paused", model_name="test_model", id=1, role="prefill", parallel_config=parallel_config
    )
    instance.add_endpoints(
        "10.0.0.1",
        {0: Endpoint(id=0, ip="10.0.0.1", business_port="8000", mgmt_port="9000", status=EndpointStatus.NORMAL)},
    )
    instance.add_endpoints(
        "10.0.0.2",
        {0: Endpoint(id=1, ip="10.0.0.2", business_port="8000", mgmt_port="9000", status=EndpointStatus.PAUSED)},
    )

    # Partial PAUSED
    assert instance.is_any_endpoint_paused() is True
    assert instance.is_all_endpoints_paused() is False

    # All PAUSED
    for pod_endpoints in instance.endpoints.values():
        for endpoint in pod_endpoints.values():
            endpoint.status = EndpointStatus.PAUSED
    assert instance.is_any_endpoint_paused() is True
    assert instance.is_all_endpoints_paused() is True

    # No PAUSED
    for pod_endpoints in instance.endpoints.values():
        for endpoint in pod_endpoints.values():
            endpoint.status = EndpointStatus.NORMAL
    assert instance.is_any_endpoint_paused() is False


def test_is_any_endpoint_paused_empty_endpoints() -> None:
    """Test is_any_endpoint_paused() returns False when no endpoints exist"""
    parallel_config = ParallelConfig(dp_size=2, tp_size=2)
    instance = Instance(
        job_name="test_empty", model_name="test_model", id=1, role="prefill", parallel_config=parallel_config
    )
    assert instance.is_any_endpoint_paused() is False
