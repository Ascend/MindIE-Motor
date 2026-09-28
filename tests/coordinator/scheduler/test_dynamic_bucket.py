# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Tests for dynamic long/short sequence bucket scheduling."""

import math

import pytest

from motor.coordinator.scheduler.policy.dynamic_bucket import DynamicBucketPolicy, DynamicBucketSelector
from tests.coordinator.scheduler.conftest import create_mock_endpoint, create_mock_instance, create_mock_workload


def _selector(*, load_scale: float = 4.0) -> DynamicBucketSelector:
    return DynamicBucketSelector(
        short_bucket_median=10,
        long_bucket_median=30,
        bucket_border=20,
        length_scale=1.0,
        load_scale=load_scale,
    )


@pytest.mark.parametrize(
    ("length", "short_load", "long_load", "expected_bucket"),
    [
        (10, 100, 100, 0),  # Short median stays in the short bucket under balanced load.
        (30, 100, 100, 1),  # Long median stays in the long bucket at the strict > 1 threshold.
        (19, 100, 100, 0),  # Just below the border starts and stays short.
        (20, 100, 100, 0),  # The border belongs to the short bucket.
        (21, 100, 100, 0),  # Just above the border may switch back under balanced load.
        (19, 100, 0, 1),  # An overloaded short bucket sends a movable short request long.
        (21, 100, 0, 1),  # An empty long bucket keeps a long request long.
        (19, 0, 100, 0),  # An empty short bucket keeps a short request short.
        (31, 0, 100, 0),  # An overloaded long bucket sends a movable long request short.
    ],
)
def test_select_bucket_length_and_load_matrix(length, short_load, long_load, expected_bucket):
    selector = _selector()
    selector.update_bucket_loads([short_load], [long_load])

    assert selector.select_bucket(length) == expected_bucket


@pytest.mark.parametrize(("load_scale", "expected_bucket"), [(1.0, 1), (4.0, 0)])
def test_load_scale_changes_short_bucket_switch_threshold(load_scale, expected_bucket):
    selector = _selector(load_scale=load_scale)
    selector.update_bucket_loads([100], [50])

    assert selector.select_bucket(10) == expected_bucket


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, 0.5),
        (math.inf, 0.0),
        (-math.inf, 1.0),
        (1_000_000.0, 0.0),
        (-1_000_000.0, 1.0),
    ],
)
def test_sigmoid_affinity_is_stable_at_extreme_values(value, expected):
    assert _selector()._sigmoid_affinity(value) == expected


@pytest.mark.parametrize(
    ("total", "short_count", "long_count", "expected_short"),
    [
        (1, 0, 0, 1),
        (2, 0, 0, 1),
        (5, 0, 0, 2),
        (6, 1, 2, 2),
        (5, 3, 1, 4),
        (4, 99, 0, 3),
        (4, 0, 99, 1),
    ],
)
def test_resolve_short_bucket_count(total, short_count, long_count, expected_short):
    assert DynamicBucketPolicy.resolve_short_bucket_count(total, short_count, long_count) == expected_short


def test_build_bucket_endpoints_uses_stable_order_and_configured_ratio():
    instance_2 = create_mock_instance(
        instance_id=2,
        endpoints={
            "group": {
                21: create_mock_endpoint(21),
                20: create_mock_endpoint(20),
            }
        },
    )
    instance_1 = create_mock_instance(
        instance_id=1,
        endpoints={
            "group": {
                12: create_mock_endpoint(12),
                10: create_mock_endpoint(10),
                11: create_mock_endpoint(11),
            }
        },
    )

    candidates = DynamicBucketPolicy.build_bucket_endpoints(
        [instance_2, instance_1],
        configured_short_count=2,
        configured_long_count=3,
    )

    assert [(item.instance.id, item.endpoint.id, item.bucket) for item in candidates] == [
        (1, 10, 0),
        (1, 11, 0),
        (1, 12, 1),
        (2, 20, 1),
        (2, 21, 1),
    ]


def test_select_endpoint_falls_back_to_nonempty_bucket():
    endpoint = create_mock_endpoint(10, workload=create_mock_workload(active_tokens=7))
    instance = create_mock_instance(instance_id=1, endpoints={"group": {10: endpoint}})

    selected = DynamicBucketPolicy.select_endpoint_from_list([instance], req_length=1_000_000, selector=_selector())

    assert selected == (instance, endpoint)
