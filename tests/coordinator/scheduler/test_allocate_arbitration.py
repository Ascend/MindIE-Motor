# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
No-ZMQ arbitration golden tests (design §6.2 / §11.3 / R4).

These call the extracted ``allocate_arbitration`` functions directly with an
``ArbitrationContext``. They must select the SAME (instance, endpoint) as the
former ALLOCATE_ONLY authoritative path -- with no ZMQ and no dispatcher.
"""

import pytest

from motor.common.resources.endpoint import Endpoint, EndpointStatus, Workload
from motor.common.resources.http_msg_spec import EventType
from motor.common.resources.instance import Instance, InsStatus, PDRole, ParallelConfig
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.domain.instance_manager import InstanceManager
from motor.coordinator.scheduler import allocate_arbitration
from motor.coordinator.scheduler.allocate_arbitration import ArbitrationContext
from motor.coordinator.scheduler.runtime.zmq_protocol import (
    CANDIDATE_POLICY_KV_CACHE_AFFINITY,
    CANDIDATE_POLICY_LOAD_BALANCE,
)


def _make_instance(
    instance_id: int,
    endpoint_ids: tuple[int, int],
    role: PDRole = PDRole.ROLE_P,
) -> Instance:
    inst = Instance(
        job_name=f"{role.value}-{instance_id}",
        model_name="test_model",
        id=instance_id,
        role=role,
        status=InsStatus.ACTIVE,
        parallel_config=ParallelConfig(dp_size=2),
    )
    inst.add_endpoints(
        f"pod-{instance_id}",
        {
            idx: Endpoint(
                id=endpoint_id,
                ip=f"10.0.0.{instance_id}",
                business_port=f"80{idx}",
                status=EndpointStatus.NORMAL,
                workload=Workload(),
            )
            for idx, endpoint_id in enumerate(endpoint_ids)
        },
    )
    return inst


def _context(
    instance_manager: InstanceManager,
    *,
    is_load_balance: bool,
    blocked: tuple[int, ...] = (),
) -> ArbitrationContext:
    blocked_set = set(blocked)
    return ArbitrationContext(
        get_available_instances=instance_manager.get_available_instances,
        is_instance_circuit_open=lambda instance_id: instance_id in blocked_set,
        endpoint_instance_score_weight=0.0,
        is_load_balance_scheduler=is_load_balance,
    )


async def _two_prefill_pool(loads: dict[tuple[int, int], float], role: PDRole = PDRole.ROLE_P) -> InstanceManager:
    config = CoordinatorConfig()
    config.scheduler_config.endpoint_instance_score_weight = 0.0
    instance_manager = InstanceManager(config)
    inst_a = _make_instance(1, (10, 11), role=role)
    inst_b = _make_instance(2, (20, 21), role=role)
    await instance_manager.refresh_instances(EventType.ADD, [inst_a, inst_b])
    for (iid, eid), tokens in loads.items():
        await instance_manager.update_instance_workload(iid, eid, Workload(active_tokens=tokens))
    return instance_manager


_STD_LOADS = {(1, 10): 20, (1, 11): 30, (2, 20): 1, (2, 21): 40}


@pytest.mark.asyncio
async def test_authoritative_load_balance_selects_global_lowest():
    """LB scheduler, no candidate_policy: scan the whole pool and pick the globally lowest score."""
    im = await _two_prefill_pool(_STD_LOADS)
    ctx = _context(im, is_load_balance=True)

    selected = allocate_arbitration.select_authoritative_allocate_candidate(
        ctx, (1, 10), [(1, 10)], PDRole.ROLE_P, candidate_policy=None
    )

    assert selected is not None
    instance, endpoint, score = selected
    assert (instance.id, endpoint.id) == (2, 20)
    assert score == 1


@pytest.mark.asyncio
async def test_load_balance_policy_scans_all_under_kv_scheduler():
    """candidate_policy=load_balance forces a global scan even under a KV-affinity scheduler."""
    im = await _two_prefill_pool(_STD_LOADS, role=PDRole.ROLE_D)
    ctx = _context(im, is_load_balance=False)

    selected = allocate_arbitration.select_authoritative_allocate_candidate(
        ctx, (1, 10), [(1, 10)], PDRole.ROLE_D, candidate_policy=CANDIDATE_POLICY_LOAD_BALANCE
    )

    assert selected is not None
    instance, endpoint, _ = selected
    assert (instance.id, endpoint.id) == (2, 20)


@pytest.mark.asyncio
async def test_kv_affinity_keeps_proposed_candidate():
    """KV-affinity with a single proposed candidate keeps it (no global LB replacement)."""
    im = await _two_prefill_pool(_STD_LOADS)
    ctx = _context(im, is_load_balance=False)

    selected = allocate_arbitration.select_authoritative_allocate_candidate(
        ctx, (1, 10), [(1, 10)], PDRole.ROLE_P, candidate_policy=CANDIDATE_POLICY_KV_CACHE_AFFINITY
    )

    assert selected is not None
    instance, endpoint, score = selected
    assert (instance.id, endpoint.id) == (1, 10)
    assert score == 20


@pytest.mark.asyncio
async def test_kv_affinity_reselects_least_loaded_among_candidates():
    """On a stale-view burst, re-pick the least-loaded WITHIN the proposed set (not globally)."""
    im = await _two_prefill_pool({(1, 10): 20, (1, 11): 5, (2, 20): 10, (2, 21): 40})
    ctx = _context(im, is_load_balance=False)

    selected = allocate_arbitration.select_authoritative_allocate_candidate(
        ctx,
        (1, 10),
        [(1, 10), (2, 20)],
        PDRole.ROLE_P,
        candidate_policy=CANDIDATE_POLICY_KV_CACHE_AFFINITY,
    )

    assert selected is not None
    instance, endpoint, score = selected
    # ep11 (load 5) is globally lightest but NOT proposed; among {ep10=20, ep20=10} ep20 wins.
    assert (instance.id, endpoint.id) == (2, 20)
    assert score == 10


@pytest.mark.asyncio
async def test_affinity_global_prefers_cache_hit_despite_higher_load():
    """Unified re-rank: a big cache hit (prefill_cost=0) beats lighter-loaded un-cached endpoints."""
    im = await _two_prefill_pool({(1, 10): 1, (1, 11): 1, (2, 20): 50, (2, 21): 1})
    ctx = _context(im, is_load_balance=False)

    selected = allocate_arbitration.select_affinity_global(
        ctx,
        [(1, 10, 10000.0), (1, 11, 10000.0), (2, 20, 0.0), (2, 21, 10000.0)],
        PDRole.ROLE_P,
        prefill_load_scale=1.0,
        load_weight=1.0,
    )

    assert selected is not None
    instance, endpoint, score = selected
    assert (instance.id, endpoint.id) == (2, 20)
    assert score == pytest.approx(50.0)  # 1.0*0 + 1.0*50


@pytest.mark.asyncio
async def test_affinity_global_breaks_equal_affinity_by_fresh_load():
    """All equally cached (prefill_cost=0) -> reduce to lowest fresh load across ALL endpoints."""
    im = await _two_prefill_pool({(1, 10): 20, (1, 11): 30, (2, 20): 5, (2, 21): 40})
    ctx = _context(im, is_load_balance=False)

    selected = allocate_arbitration.select_affinity_global(
        ctx,
        [(1, 10, 0.0), (1, 11, 0.0), (2, 20, 0.0), (2, 21, 0.0)],
        PDRole.ROLE_P,
        prefill_load_scale=1.0,
        load_weight=1.0,
    )

    assert selected is not None
    instance, endpoint, score = selected
    assert (instance.id, endpoint.id) == (2, 20)
    assert score == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_unknown_candidate_policy_falls_back_to_scheduler_type():
    """Unknown candidate_policy must not silently disable LB's global scan under an LB scheduler."""
    im = await _two_prefill_pool(_STD_LOADS)
    ctx = _context(im, is_load_balance=True)

    selected = allocate_arbitration.select_authoritative_allocate_candidate(
        ctx, (1, 10), [(1, 10)], PDRole.ROLE_P, candidate_policy="load-balnace"
    )

    assert selected is not None
    instance, endpoint, _ = selected
    assert (instance.id, endpoint.id) == (2, 20)


@pytest.mark.asyncio
async def test_select_valid_candidate_validates_fast_path_target():
    """Fast path: validate the proposed endpoint and return its current score."""
    im = await _two_prefill_pool(_STD_LOADS)
    ctx = _context(im, is_load_balance=True)

    selected = allocate_arbitration.select_valid_candidate(ctx, (1, 10), PDRole.ROLE_P)

    assert selected is not None
    instance, endpoint, score = selected
    assert (instance.id, endpoint.id) == (1, 10)
    assert score == 20


@pytest.mark.asyncio
async def test_select_valid_candidate_finds_encode_pool():
    """Fast-path validation must locate encode instances in the ROLE_E pool."""
    config = CoordinatorConfig()
    config.scheduler_config.endpoint_instance_score_weight = 0.0
    im = InstanceManager(config)
    await im.refresh_instances(EventType.ADD, [_make_instance(1, (10, 11), role=PDRole.ROLE_E)])
    ctx = _context(im, is_load_balance=True)

    selected = allocate_arbitration.select_valid_candidate(ctx, (1, 10), PDRole.ROLE_E)

    assert selected is not None
    instance, endpoint, _ = selected
    assert (instance.id, endpoint.id) == (1, 10)


@pytest.mark.asyncio
async def test_circuit_open_is_the_final_gate():
    """A circuit-open instance is rejected on the fast path and skipped by the global scan."""
    im = await _two_prefill_pool(_STD_LOADS)
    ctx = _context(im, is_load_balance=True, blocked=(2,))

    # Fast path: proposing the blocked instance returns nothing.
    assert allocate_arbitration.select_valid_candidate(ctx, (2, 20), PDRole.ROLE_P) is None

    # Global scan: the otherwise-lightest instance 2 is skipped; instance 1's lightest wins.
    selected = allocate_arbitration.select_global_load_balance_candidate(ctx, PDRole.ROLE_P)
    assert selected is not None
    instance, endpoint, _ = selected
    assert instance.id == 1
    assert endpoint.id == 10  # ep10=20 < ep11=30


@pytest.mark.asyncio
async def test_global_load_balance_skips_excluded_pair():
    """A pair this CAS round already rejected must not win the re-scan again."""
    im = await _two_prefill_pool(_STD_LOADS)
    ctx = _context(im, is_load_balance=True)

    selected = allocate_arbitration.select_global_load_balance_candidate(ctx, PDRole.ROLE_P, excluded={(2, 20)})

    assert selected is not None
    instance, endpoint, _ = selected
    assert (instance.id, endpoint.id) == (1, 10)  # next-lowest after excluding the global min (2,20)


@pytest.mark.asyncio
async def test_global_load_balance_skips_missing_dispatch_capability():
    """Decode co-location fallback must not re-pick an instance that lacks the required capability."""
    im = await _two_prefill_pool(_STD_LOADS)
    for instance in im.get_available_instances(PDRole.ROLE_P).values():
        instance.dispatch_capabilities = ["decode_colocation"] if instance.id == 1 else []
    ctx = _context(im, is_load_balance=True)

    selected = allocate_arbitration.select_global_load_balance_candidate(
        ctx, PDRole.ROLE_P, required_dispatch_capability="decode_colocation"
    )

    assert selected is not None
    instance, endpoint, _ = selected
    assert instance.id == 1
    assert endpoint.id == 10


@pytest.mark.asyncio
async def test_authoritative_load_balance_skips_excluded_pair():
    """select_authoritative_allocate_candidate must forward excluded into the LB global scan."""
    im = await _two_prefill_pool(_STD_LOADS)
    ctx = _context(im, is_load_balance=True)

    selected = allocate_arbitration.select_authoritative_allocate_candidate(
        ctx, (1, 10), [(1, 10)], PDRole.ROLE_P, candidate_policy=None, excluded={(2, 20)}
    )

    assert selected is not None
    instance, endpoint, _ = selected
    assert (instance.id, endpoint.id) == (1, 10)


@pytest.mark.asyncio
async def test_affinity_global_skips_excluded_pair():
    """Unified affinity re-rank must not keep re-picking a pair this CAS round already rejected."""
    im = await _two_prefill_pool({(1, 10): 1, (1, 11): 1, (2, 20): 50, (2, 21): 1})
    ctx = _context(im, is_load_balance=False)

    selected = allocate_arbitration.select_affinity_global(
        ctx,
        [(1, 10, 10000.0), (1, 11, 10000.0), (2, 20, 0.0), (2, 21, 10000.0)],
        PDRole.ROLE_P,
        prefill_load_scale=1.0,
        load_weight=1.0,
        excluded={(2, 20)},
    )

    assert selected is not None
    instance, endpoint, _ = selected
    assert (instance.id, endpoint.id) != (2, 20)
