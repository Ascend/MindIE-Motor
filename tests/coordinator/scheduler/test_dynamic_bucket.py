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
    DynamicBucketSelector,
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
def _open_bucket_client(instances: list[Instance], *, decode_scheduler_type: str = "load_balance"):
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
        client = AsyncSchedulerClient(
            SchedulerClientConfig(
                scheduler_address="ipc:///tmp/test_prefill_dynamic_bucket",
                timeout=5.0,
                prefill_scheduler_type="dynamic_bucket",
                decode_scheduler_type=decode_scheduler_type,
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
    with _open_bucket_client(instances, decode_scheduler_type="dynamic_bucket") as client:
        candidates, policy = await _select(client, role, _LONG_LENGTH)
        assert policy == CANDIDATE_POLICY_DYNAMIC_BUCKET
        selected_instance, _, _ = candidates[0]
        bucket_candidates, _ = get_builtin_endpoint_policy(CANDIDATE_POLICY_DYNAMIC_BUCKET).prepare_candidates(
            client, candidates, role, Mock(spec=RequestInfo), None, None
        )

    assert selected_instance.id == 2
    assert {(item["instance_id"], item["endpoint_id"]) for item in bucket_candidates} == {(2, 2), (3, 3)}


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
