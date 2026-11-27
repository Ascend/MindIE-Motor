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

import json
import math
import os
import tempfile
from contextlib import contextmanager
from unittest.mock import AsyncMock, Mock, patch

import pytest

from motor.common.resources.endpoint import Endpoint, Workload
from motor.common.resources.instance import Instance, PDRole
from motor.config.config_utils import ConfigKey
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.models.request import RequestInfo
from motor.coordinator.scheduler.policy.kv_cache_affinity import TokenizerManager
from motor.coordinator.scheduler.policy.dynamic_bucket import (
    DynamicBucketPolicy,
    MultiBucketSelector,
    get_request_token_length,
)
from motor.coordinator.scheduler.runtime.scheduler_client import AsyncSchedulerClient, SchedulerClientConfig
from motor.coordinator.scheduler.runtime.builtin_endpoint_policy import get_builtin_endpoint_policy
from motor.coordinator.scheduler.runtime.workload_shm.native import STATUS_OK
from motor.coordinator.scheduler.runtime.zmq_protocol import (
    CANDIDATE_POLICY_DYNAMIC_BUCKET,
    CANDIDATE_POLICY_LOAD_BALANCE,
)


from tests.coordinator.scheduler.conftest import create_mock_endpoint, create_mock_instance, create_mock_workload


def _selector(*, load_scale: float = 4.0) -> MultiBucketSelector:
    return MultiBucketSelector(
        boundaries=[20],
        medians=[10, 30],
        length_scales=[1.0, 1.0],
        load_scales=[load_scale, load_scale],
    )


@pytest.mark.parametrize(
    ("length", "short_load", "long_load", "expected_bucket"),
    [
        (10, 0, 0, 0),  # Empty buckets keep a short request short.
        (30, 0, 0, 1),  # Empty buckets keep a long request long.
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
    selector.update_bucket_loads([short_load, long_load])

    assert selector.select_bucket(length) == expected_bucket


@pytest.mark.parametrize(("load_scale", "expected_bucket"), [(1.0, 1), (4.0, 0)])
def test_load_scale_changes_short_bucket_switch_threshold(load_scale, expected_bucket):
    selector = _selector(load_scale=load_scale)
    selector.update_bucket_loads([100, 50])

    assert selector.select_bucket(10) == expected_bucket


def test_length_scales_apply_per_bucket():
    default = _multi_selector()
    flatter = _multi_selector(length_scales=(1.0, 0.25, 1.0))

    assert default._length_affinity_for_bucket(1, 22) > flatter._length_affinity_for_bucket(1, 22)
    assert flatter._length_affinity_for_bucket(1, 22) > 0.5


def test_load_scales_apply_per_bucket():
    strict = _multi_selector(load_scales=(4.0, 4.0, 4.0))
    lenient = _multi_selector(load_scales=(4.0, 1.0, 4.0))
    strict.update_bucket_loads([100, 50, 100])
    lenient.update_bucket_loads([100, 50, 100])

    assert strict.select_bucket(10) == 0
    assert lenient.select_bucket(10) == 1


def test_multi_bucket_scales_must_align_with_bucket_count():
    with pytest.raises(ValueError, match="one value per bucket"):
        MultiBucketSelector(boundaries=[20], medians=[10, 30], length_scales=[1.0])
    with pytest.raises(ValueError, match="one value per bucket"):
        MultiBucketSelector(boundaries=[20], medians=[10, 30], load_scales=[4.0])
    with pytest.raises(ValueError, match="finite and positive"):
        MultiBucketSelector(boundaries=[20], medians=[10, 30], load_scales=[4.0, 0.0])


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


def _multi_selector(
    *,
    boundaries: tuple[int, ...] = (20, 40),
    medians: tuple[int, ...] = (10, 30, 50),
    counts: tuple[int, ...] | None = None,
    length_scales: tuple[float, ...] | None = None,
    load_scales: tuple[float, ...] | None = None,
) -> MultiBucketSelector:
    return MultiBucketSelector(
        boundaries=list(boundaries),
        medians=list(medians),
        counts=list(counts) if counts is not None else None,
        length_scales=list(length_scales) if length_scales is not None else None,
        load_scales=list(load_scales) if load_scales is not None else None,
    )


@pytest.mark.parametrize(
    ("length", "expected_bucket"),
    [
        (0, 0),
        (20, 0),  # the boundary belongs to the lower bucket
        (21, 1),
        (40, 1),
        (41, 2),
        (1_000_000, 2),  # the last bucket is open-ended
    ],
)
def test_multi_bucket_boundaries_are_inclusive_upper_bounds(length, expected_bucket):
    assert _multi_selector().bucket_of_length(length) == expected_bucket


def test_multi_bucket_derives_default_medians_when_not_configured():
    assert _multi_selector(medians=()).medians == (10, 30, 80)
    assert _multi_selector(boundaries=(20,), medians=()).medians == (10, 60)


def test_default_two_bucket_layout_matches_legacy_values():
    selector = MultiBucketSelector(boundaries=[32 * 1024])

    assert selector.medians == (16 * 1024, 96 * 1024)


@pytest.mark.parametrize(
    ("total", "expected"),
    [
        (0, [0, 0]),
        (1, [1, 0]),
        (2, [1, 1]),
        (3, [1, 2]),
        (4, [2, 2]),
        (5, [2, 3]),
        (7, [3, 4]),
        (8, [4, 4]),
    ],
)
def test_default_two_bucket_split_matches_legacy_floor_half(total, expected):
    selector = MultiBucketSelector(boundaries=[20], medians=[10, 30])

    assert selector.resolve_bucket_counts(total) == expected


@pytest.mark.parametrize(
    ("length", "loads", "expected_bucket"),
    [
        (0, [100, 100, 100], 0),  # Balanced load keeps a short request short.
        (15, [100, 100, 100], 0),
        (15, [100, 0, 100], 1),  # A lighter next bucket pulls a movable request longer.
        (20, [100, 100, 100], 0),
        (21, [100, 100, 100], 0),  # A request just over the border can switch back short.
        (30, [100, 100, 100], 1),  # The median has exactly 0.5 length affinity, not > 1.
        (31, [100, 100, 100], 1),
        (30, [100, 10, 100], 1),  # A much lighter shorter bucket pulls the request back.
        (50, [100, 100, 100], 2),
        (50, [100, 10, 100], 1),
        (60, [100, 100, 100], 2),
        (50, [100, 100, 0], 2),  # An idle initial bucket keeps the request (load ratio is infinite).
        (15, [100, 1000, 0], 0),  # Only adjacent buckets are evaluated, so bucket 2 is ignored.
    ],
)
def test_multi_bucket_selects_target_bucket_by_length_and_adjacent_load(
    length,
    loads,
    expected_bucket,
):
    selector = _multi_selector()
    selector.update_bucket_loads(loads)

    assert selector.select_bucket(length) == expected_bucket


def test_multi_bucket_skips_empty_adjacent_bucket():
    selector = _multi_selector()
    selector.update_bucket_loads([100, None, 0])

    assert selector.select_bucket(15) == 0


@pytest.mark.parametrize(
    ("total", "counts", "expected"),
    [
        (8, (2, 2, 4), [2, 2, 4]),  # Exact counts when their sum matches the instance pool.
        (16, (2, 2, 4), [4, 4, 8]),  # Otherwise counts are ratio weights.
        (20, (8, 1, 1), [16, 2, 2]),  # Apply weights to the whole pool, not a reserved remainder.
        (10, (8, 1, 1), [8, 1, 1]),
        (11, (8, 1, 1), [9, 1, 1]),  # Growing the pool must not shrink the largest bucket here.
        (6, (8, 1, 1), [4, 1, 1]),
        (8, (0, 0, 1), [1, 1, 6]),
        (5, None, [1, 2, 2]),  # No counts: even split with the remainder going to high buckets.
        (3, None, [1, 1, 1]),
        (2, None, [1, 1, 0]),  # A pool smaller than the bucket count fills low buckets first.
        (4, (1, 0, 0), [2, 1, 1]),  # Every bucket keeps at least one instance.
        (0, None, [0, 0, 0]),
    ],
)
def test_multi_bucket_resolve_bucket_counts(total, counts, expected):
    assert _multi_selector(counts=counts).resolve_bucket_counts(total) == expected


@pytest.mark.parametrize("scheduler_type", ["load_balance", "dynamic_bucket"])
@pytest.mark.parametrize(
    ("legacy_counts", "expected"),
    [
        ({}, [4, 4]),
        ({"dynamic_bucket_short_bucket_count": 2}, [2, 6]),
        ({"dynamic_bucket_long_bucket_count": 2}, [6, 2]),
        ({"dynamic_bucket_short_bucket_count": 20}, [7, 1]),
        ({"dynamic_bucket_long_bucket_count": 20}, [1, 7]),
    ],
)
def test_legacy_layout_loads_and_constructs_client_with_historical_constraints(
    tmp_path,
    scheduler_type,
    legacy_counts,
    expected,
):
    """An upgrade must preserve valid medians and a single fixed-size bucket."""
    path = tmp_path / "legacy-buckets.json"
    path.write_text(
        json.dumps(
            {
                "scheduler_config": {
                    "prefill_scheduler_type": scheduler_type,
                    "decode_scheduler_type": scheduler_type,
                    "dynamic_bucket_border": 4096,
                    "dynamic_bucket_long_median": 8192,
                    **legacy_counts,
                }
            }
        ),
        encoding="utf-8",
    )
    scheduler = CoordinatorConfig.from_json(str(path)).scheduler_config
    settings = {
        name: getattr(scheduler, name)
        for name in (
            "dynamic_bucket_boundaries",
            "dynamic_bucket_medians",
            "dynamic_bucket_counts",
            "dynamic_bucket_length_scales",
            "dynamic_bucket_load_scales",
            "dynamic_bucket_legacy_layout",
        )
    }
    with _open_bucket_client(
        [], prefill_scheduler_type=scheduler_type, decode_scheduler_type=scheduler_type, **settings
    ) as client:
        selector = client._dynamic_bucket_selector
        assert selector.medians == (16384, 8192)
        assert selector.resolve_bucket_counts(8) == expected


def test_explicit_layout_still_rejects_nonincreasing_medians():
    with pytest.raises(ValueError, match="medians"):
        MultiBucketSelector(boundaries=[4096], medians=[16384, 8192])


def test_equal_load_can_route_middle_request_into_short_bucket():
    """Bidirectional soft buckets do not reserve Prefill capacity for short requests."""
    selector = MultiBucketSelector(
        boundaries=[32768, 98304],
        medians=[16384, 65536, 147456],
        counts=[1, 1, 1],
        length_scales=[2.0, 1.0, 1.0],
        load_scales=[2.0, 4.0, 4.0],
    )
    selector.update_bucket_loads([98304, 98304, 98304])
    assert selector.select_bucket(49152) == 0
    selector.update_bucket_loads([98304 + 49152, 98304, 98304])
    assert selector.select_bucket(4096) == 0


def test_multi_bucket_builds_endpoints_in_stable_bucket_order():
    instance_2 = create_mock_instance(
        instance_id=2,
        endpoints={"group": {21: create_mock_endpoint(21)}},
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

    candidates = DynamicBucketPolicy.build_bucket_endpoints_for_selector(
        [instance_2, instance_1],
        _multi_selector(counts=(1, 1, 2)),
    )

    assert [(item.instance.id, item.endpoint.id, item.bucket) for item in candidates] == [
        (1, 10, 0),
        (1, 11, 0),
        (1, 12, 0),
        (2, 21, 1),
    ]


def test_multi_bucket_falls_back_to_nearest_nonempty_bucket():
    endpoint_10 = create_mock_endpoint(10, workload=create_mock_workload(active_tokens=7))
    endpoint_20 = create_mock_endpoint(20, workload=create_mock_workload(active_tokens=3))
    instance_1 = create_mock_instance(instance_id=1, endpoints={"group": {10: endpoint_10}})
    instance_2 = create_mock_instance(instance_id=2, endpoints={"group": {20: endpoint_20}})

    # Two endpoints in three buckets: bucket 2 is empty, so the nearest bucket 1 wins.
    selected = DynamicBucketPolicy.select_endpoint_from_list(
        [instance_1, instance_2],
        req_length=1_000,
        selector=_multi_selector(),
    )

    assert selected == (instance_2, endpoint_20)


def test_multi_bucket_rejects_invalid_layouts():
    with pytest.raises(ValueError, match="at least one boundary"):
        MultiBucketSelector(boundaries=[])
    with pytest.raises(ValueError, match="strictly increasing"):
        MultiBucketSelector(boundaries=[20, 20])
    with pytest.raises(ValueError, match="one value per bucket"):
        MultiBucketSelector(boundaries=[20, 40], medians=[10, 30])
    with pytest.raises(ValueError, match="one value per bucket"):
        MultiBucketSelector(boundaries=[20, 40], counts=[1, 1])
    with pytest.raises(ValueError, match="non-negative"):
        MultiBucketSelector(boundaries=[20, 40], counts=[1, -1, 1])


def test_build_bucket_endpoints_with_two_buckets_keeps_stable_order_and_ratio():
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

    candidates = DynamicBucketPolicy.build_bucket_endpoints_for_selector(
        [instance_2, instance_1],
        MultiBucketSelector(
            boundaries=[20],
            medians=[10, 30],
            counts=[2, 3],
        ),
    )

    assert [(item.instance.id, item.endpoint.id, item.bucket) for item in candidates] == [
        (1, 10, 0),
        (1, 11, 0),
        (1, 12, 0),
        (2, 20, 1),
        (2, 21, 1),
    ]


def test_build_bucket_endpoints_keeps_all_endpoints_of_one_instance_in_the_same_bucket():
    instance_1 = create_mock_instance(
        instance_id=1,
        endpoints={"group": {10: create_mock_endpoint(10), 11: create_mock_endpoint(11), 12: create_mock_endpoint(12)}},
    )
    instance_2 = create_mock_instance(
        instance_id=2,
        endpoints={"group": {20: create_mock_endpoint(20)}},
    )
    instance_3 = create_mock_instance(
        instance_id=3,
        endpoints={"group": {30: create_mock_endpoint(30)}},
    )

    candidates = DynamicBucketPolicy.build_bucket_endpoints_for_selector(
        [instance_3, instance_1, instance_2],
        MultiBucketSelector(boundaries=[20], medians=[10, 30], counts=[1, 3]),
    )
    buckets_by_instance = {}
    for item in candidates:
        buckets_by_instance.setdefault(item.instance.id, set()).add(item.bucket)

    assert buckets_by_instance == {1: {0}, 2: {1}, 3: {1}}
    assert [(item.instance.id, item.endpoint.id, item.bucket) for item in candidates] == [
        (1, 10, 0),
        (1, 11, 0),
        (1, 12, 0),
        (2, 20, 1),
        (3, 30, 1),
    ]


def test_select_endpoint_falls_back_to_nonempty_bucket():
    endpoint = create_mock_endpoint(10, workload=create_mock_workload(active_tokens=7))
    instance = create_mock_instance(instance_id=1, endpoints={"group": {10: endpoint}})

    selected = DynamicBucketPolicy.select_endpoint_from_list([instance], req_length=1_000_000, selector=_selector())

    assert selected == (instance, endpoint)


_SHORT_LENGTH = 16 * 1024
_LONG_LENGTH = 96 * 1024


def _bucket_instance(
    instance_id: int, endpoint_id: int, active_tokens: float = 100.0, role: PDRole = PDRole.ROLE_P
) -> Instance:
    endpoint = Endpoint(
        id=endpoint_id,
        ip="127.0.0.1",
        business_port=str(8000 + endpoint_id),
        workload=Workload(active_tokens=active_tokens),
    )
    return Instance(
        job_name="test-job",
        model_name="test-model",
        id=instance_id,
        role=role.value,
        endpoints={f"pod{instance_id}": {endpoint_id: endpoint}},
    )


@pytest.fixture
def coordinator_json_path():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".coordinator.json", delete=False) as handle:
        path = handle.name
    yield path
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def test_prefill_only_dynamic_bucket_loads_model_metadata_and_tokenizer(coordinator_json_path, monkeypatch):
    user_config = {
        ConfigKey.MOTOR_COORDINATOR.value: {
            "scheduler_config": {
                "prefill_scheduler_type": "dynamic_bucket",
                "decode_scheduler_type": "load_balance",
            }
        },
        "motor_engine_prefill_config": {
            "engine_config": {
                "model": "/test/prefill-model",
                "served_model_name": "test",
                "max-model-len": 1024,
            }
        },
    }
    with open(coordinator_json_path, "w", encoding="utf-8") as config_file:
        json.dump(user_config, config_file)
    config = CoordinatorConfig.from_json(coordinator_json_path)
    monkeypatch.setenv("TORCH_DEVICE_BACKEND_AUTOLOAD", "0")
    manager = object.__new__(TokenizerManager)
    with patch.object(TokenizerManager, "get_tokenizer", return_value=None) as load_tokenizer:
        TokenizerManager.__init__(manager, config)

    assert manager.model_path == "/test/prefill-model"
    assert not config.scheduler_config.kv_conductor_config.conductor_service
    load_tokenizer.assert_called_once_with()


@contextmanager
def _open_bucket_client(
    instances: list[Instance],
    *,
    prefill_scheduler_type: str = "dynamic_bucket",
    decode_scheduler_type: str = "load_balance",
    **client_config,
):
    with (
        patch("motor.coordinator.scheduler.runtime.scheduler_client._SchedulerTransport") as transport_cls,
        patch("motor.coordinator.scheduler.runtime.scheduler_client._SchedulerInstanceCache") as cache_cls,
    ):
        transport = AsyncMock()
        transport.connected = False
        transport_cls.return_value = transport
        cache = Mock()
        cache.replace_all = AsyncMock()
        cache.get_instances.return_value = instances
        cache.is_recovery_paused.return_value = False
        cache_cls.return_value = cache
        client_config.setdefault("dynamic_bucket_boundaries", [32 * 1024])
        client_config.setdefault("dynamic_bucket_medians", [16 * 1024, 96 * 1024])
        client = AsyncSchedulerClient(
            SchedulerClientConfig(
                scheduler_address="ipc:///tmp/test_prefill_dynamic_bucket",
                timeout=5.0,
                prefill_scheduler_type=prefill_scheduler_type,
                decode_scheduler_type=decode_scheduler_type,
                **client_config,
            )
        )
        client._push_subscriber = None
        yield client


@pytest.fixture
def prefill_bucket_client():
    with _open_bucket_client(
        [
            _bucket_instance(1, 1),
            _bucket_instance(2, 2),
        ]
    ) as client:
        yield client


def _request(*, length: int, token_ids: list[int] | None, req_data: dict | None = None) -> RequestInfo:
    return RequestInfo(
        req_id=f"req-{length}",
        req_data={} if req_data is None else req_data,
        req_len=length,
        api="v1/completions",  # gitleaks:allow (public API route, not a credential)
        token_ids=token_ids,
    )


@pytest.mark.parametrize("role", [PDRole.ROLE_P, PDRole.ROLE_U])
@pytest.mark.asyncio
async def test_prefill_and_union_follow_decode_bucket_route(role):
    instances = [_bucket_instance(1, 1, role=role), _bucket_instance(2, 2, role=role)]
    with _open_bucket_client(instances, decode_scheduler_type="dynamic_bucket") as client:
        short_candidates, short_policy = await _select(client, role, _SHORT_LENGTH)
        long_candidates, long_policy = await _select(client, role, _LONG_LENGTH)

    assert short_policy == CANDIDATE_POLICY_DYNAMIC_BUCKET
    assert long_policy == CANDIDATE_POLICY_DYNAMIC_BUCKET
    assert [instance.id for instance, _, _ in short_candidates] == [1]
    assert [instance.id for instance, _, _ in long_candidates] == [2]


async def _select(client: AsyncSchedulerClient, role: PDRole, length: int):
    req_info = Mock(spec=RequestInfo)
    req_info.req_id = f"req-{role}-{length}"
    with patch(
        "motor.coordinator.scheduler.runtime.builtin_endpoint_policy.get_request_token_length",
        return_value=length,
    ):
        return await client._select_endpoint_candidates_with_policy(req_info, role)


@pytest.mark.asyncio
async def test_encode_stays_on_load_balance_when_prefill_uses_dynamic_bucket(prefill_bucket_client):
    candidates, policy = await _select(prefill_bucket_client, PDRole.ROLE_E, _SHORT_LENGTH)

    assert policy == CANDIDATE_POLICY_LOAD_BALANCE
    assert candidates


@pytest.mark.asyncio
async def test_decode_stays_on_load_balance_when_only_prefill_uses_dynamic_bucket(prefill_bucket_client):
    _, policy = await _select(prefill_bucket_client, PDRole.ROLE_D, _LONG_LENGTH)

    assert policy == CANDIDATE_POLICY_LOAD_BALANCE


@pytest.mark.asyncio
async def test_reselect_candidates_stay_inside_the_selected_bucket():
    role = PDRole.ROLE_P
    instances = [
        _bucket_instance(1, 1, role=role),
        _bucket_instance(2, 2, role=role),
        _bucket_instance(3, 3, role=role),
    ]
    with _open_bucket_client(
        instances,
        decode_scheduler_type="dynamic_bucket",
        dynamic_bucket_counts=[1, 2],
    ) as client:
        candidates, policy = await _select(client, role, _LONG_LENGTH)
        assert policy == CANDIDATE_POLICY_DYNAMIC_BUCKET
        selected_instance, _, _ = candidates[0]
        bucket_candidates, _ = get_builtin_endpoint_policy(CANDIDATE_POLICY_DYNAMIC_BUCKET).prepare_candidates(
            client, candidates, role, Mock(spec=RequestInfo), None, None
        )

    assert selected_instance.id == 2
    assert {(item["instance_id"], item["endpoint_id"]) for item in bucket_candidates} == {(2, 2), (3, 3)}


@pytest.mark.asyncio
async def test_scheduler_client_uses_configured_multi_bucket_layout():
    role = PDRole.ROLE_P
    instances = [
        _bucket_instance(1, 1, role=role),
        _bucket_instance(2, 2, role=role),
        _bucket_instance(3, 3, role=role),
    ]
    with _open_bucket_client(
        instances,
        decode_scheduler_type="dynamic_bucket",
        dynamic_bucket_boundaries=[2048, 4096],
        dynamic_bucket_medians=[1024, 3072, 5120],
        dynamic_bucket_counts=[1, 1, 1],
    ) as client:
        assert isinstance(client._dynamic_bucket_selector, MultiBucketSelector)
        selected_instance_ids = []
        for length in (1024, 3072, 5120):
            candidates, policy = await _select(client, role, length)
            assert policy == CANDIDATE_POLICY_DYNAMIC_BUCKET
            selected_instance_ids.append(candidates[0][0].id)

        candidates, _ = await _select(client, role, 3072)
        bucket_candidates, _ = get_builtin_endpoint_policy(CANDIDATE_POLICY_DYNAMIC_BUCKET).prepare_candidates(
            client, candidates, role, Mock(spec=RequestInfo), None, None
        )

    assert selected_instance_ids == [1, 2, 3]
    assert {(item["instance_id"], item["endpoint_id"]) for item in bucket_candidates} == {(2, 2)}


@pytest.mark.asyncio
async def test_selection_uses_token_ids_instead_of_request_body_length():
    role = PDRole.ROLE_P
    request = _request(length=_LONG_LENGTH, token_ids=[1] * _SHORT_LENGTH)
    with _open_bucket_client(
        [
            _bucket_instance(1, 1, role=role),
            _bucket_instance(2, 2, role=role),
        ],
        decode_scheduler_type="dynamic_bucket",
    ) as client:
        candidates, policy = await client._select_endpoint_candidates_with_policy(request, role)

    assert policy == CANDIDATE_POLICY_DYNAMIC_BUCKET
    assert [instance.id for instance, _, _ in candidates] == [1]
    assert get_request_token_length(request) == _SHORT_LENGTH


@pytest.mark.parametrize(
    ("role", "content", "token_ids", "expected_tokens"),
    [
        pytest.param(
            PDRole.ROLE_E,
            [{"type": "video_url", "video_url": {"url": "https://example.com/video.mp4"}}],
            None,
            256.0,
            id="encode-video",
        ),
        pytest.param(PDRole.ROLE_P, "hello", [1, 2, 3], 3.0, id="prefill"),
    ],
)
@pytest.mark.asyncio
async def test_dynamic_bucket_allocation_preserves_role_specific_workload(role, content, token_ids, expected_tokens):
    instance = _bucket_instance(1, 1)
    endpoint = instance.get_all_endpoints()[0]
    request = _request(
        length=8,
        token_ids=token_ids,
        req_data={"messages": [{"role": "user", "content": content}]},
    )
    reader = Mock()
    reader.entry_meta.return_value = {"generation": 1, "flags": 0}
    reader.native.cas_add.return_value = (STATUS_OK, 100.0 + expected_tokens)
    with (
        _open_bucket_client([instance]) as client,
        patch.object(client, "_refresh_cache_from_workload_reader", new_callable=AsyncMock),
        patch(
            "motor.coordinator.scheduler.runtime.scheduler_client.select_authoritative_allocate_candidate",
            return_value=(instance, endpoint, 0.0),
        ),
        patch(
            "motor.coordinator.scheduler.runtime.scheduler_client.select_valid_candidate",
            return_value=(instance, endpoint, 0.0),
        ),
    ):
        client._workload_reader = reader
        result = await client.select_and_allocate(role, request)

    assert result is not None
    assert result[2].active_tokens == expected_tokens
    assert reader.native.cas_add.call_args.args[4] == expected_tokens


@pytest.mark.asyncio
async def test_dynamic_bucket_request_full_chain():
    """Trace one request through bucket selection, candidate preparation, and allocation."""
    role = PDRole.ROLE_P
    instances = [
        _bucket_instance(1, 101),
        _bucket_instance(2, 102),
        _bucket_instance(3, 103),
    ]
    request = _request(length=3072, token_ids=[1] * 3072)
    reader = Mock()
    reader.entry_meta.return_value = {"generation": 1, "flags": 0}
    reader.native.cas_add.return_value = (STATUS_OK, 3072.0)

    with _open_bucket_client(
        instances,
        decode_scheduler_type="dynamic_bucket",
        dynamic_bucket_boundaries=[2048, 4096],
        dynamic_bucket_medians=[1024, 3072, 5120],
        dynamic_bucket_counts=[1, 1, 1],
    ) as client:
        candidates, policy = await client._select_endpoint_candidates_with_policy(request, role)
        selected_instance, selected_endpoint, _ = candidates[0]
        bucket_candidates, _ = get_builtin_endpoint_policy(CANDIDATE_POLICY_DYNAMIC_BUCKET).prepare_candidates(
            client, candidates, role, request, None, None
        )

        selector = client._dynamic_bucket_selector
        bucket_endpoints = DynamicBucketPolicy.build_bucket_endpoints_for_selector(instances, selector)
        bucket_by_endpoint = {(item.instance.id, item.endpoint.id): item.bucket for item in bucket_endpoints}
        token_length = get_request_token_length(request)
        initial_bucket = selector.bucket_of_length(token_length)
        selected_bucket = selector.select_bucket(token_length)

        with (
            patch.object(client, "_refresh_cache_from_workload_reader", new_callable=AsyncMock),
            patch(
                "motor.coordinator.scheduler.runtime.scheduler_client.select_valid_candidate",
                return_value=(selected_instance, selected_endpoint, 0.0),
            ),
        ):
            client._workload_reader = reader
            allocated = await client.select_and_allocate(role, request)

    assert token_length == 3072
    assert initial_bucket == 1
    assert selected_bucket == 1
    assert policy == CANDIDATE_POLICY_DYNAMIC_BUCKET
    assert (selected_instance.id, selected_endpoint.id) == (2, 102)
    assert bucket_by_endpoint == {(1, 101): 0, (2, 102): 1, (3, 103): 2}
    assert bucket_candidates == [{"instance_id": 2, "endpoint_id": 102}]
    assert allocated is not None
    assert (allocated[0].id, allocated[1].id) == (2, 102)
    assert allocated[2].active_tokens == 3072.0
    assert reader.native.cas_add.call_args.args[4] == 3072.0
    assert bucket_by_endpoint[(selected_instance.id, selected_endpoint.id)] == selected_bucket
    assert bucket_by_endpoint[(allocated[0].id, allocated[1].id)] == selected_bucket
