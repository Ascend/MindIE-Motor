# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Profiling target resolution: service / role / instance / DP selection semantics."""

import pytest

from motor.common.resources.endpoint import Endpoint
from motor.common.resources.instance import Instance
from motor.coordinator.profiling.models import ProfilingStartRequest, ProfilingTarget
from motor.coordinator.profiling.targets import (
    ProfilingTargetError,
    build_profile_prefix,
    resolve_targets,
)


def _endpoint(dp_rank: int, ip: str, port: int = 8000, headless: bool = False) -> Endpoint:
    return Endpoint(id=dp_rank, ip=ip, business_port=str(port), headless=headless)


def _instance(
    instance_id: int,
    role: str,
    endpoints: list[Endpoint],
    model_name: str = "deepseek-v3",
) -> Instance:
    pod_ip = f"10.0.0.{instance_id + 1}"
    return Instance(
        job_name=f"inst-{instance_id}",
        model_name=model_name,
        id=instance_id,
        role=role,
        endpoints={pod_ip: {endpoint.id: endpoint for endpoint in endpoints}},
    )


def _two_role_topology() -> dict[int, Instance]:
    return {
        0: _instance(0, "prefill", [_endpoint(0, "10.0.0.1"), _endpoint(1, "10.0.0.2")]),
        1: _instance(1, "decode", [_endpoint(0, "10.0.0.3")]),
    }


def test_default_selector_targets_every_routable_dp_with_metrics_dimensions():
    instances = _two_role_topology()

    service, targets = resolve_targets(instances, ProfilingStartRequest())

    assert service == "deepseek-v3"
    assert [(t.instance_id, t.dp_rank, t.role) for t in targets] == [
        (0, 0, "prefill"),
        (0, 1, "prefill"),
        (1, 0, "decode"),
    ]
    first = targets[0]
    assert (first.service, first.pod_ip, first.address) == ("deepseek-v3", "10.0.0.1", "10.0.0.1:8000")


def test_role_filter_selects_only_matching_instances():
    instances = _two_role_topology()

    _, targets = resolve_targets(instances, ProfilingStartRequest(roles=["decode"]))

    assert {target.role for target in targets} == {"decode"}
    assert [(target.instance_id, target.dp_rank) for target in targets] == [(1, 0)]


@pytest.mark.parametrize(("role", "selector_role"), [("both", "union"), ("hybrid", "both"), ("union", "hybrid")])
def test_role_aliases_match_and_preserve_metrics_label(role: str, selector_role: str) -> None:
    instances = {0: _instance(0, role, [_endpoint(0, "10.0.0.1")])}

    _, targets = resolve_targets(instances, ProfilingStartRequest(roles=[selector_role]))

    assert [(target.instance_id, target.role) for target in targets] == [(0, role)]


def test_unknown_instance_error_shows_a_bounded_sample() -> None:
    with pytest.raises(ProfilingTargetError) as error:
        resolve_targets(_two_role_topology(), ProfilingStartRequest(instances=list(range(100, 200))))

    assert str(error.value) == "unknown instance_id(s): [100, 101, 102, 103, 104, 105, 106, 107] (100 total)"


def test_service_filter_rejects_unknown_service():
    with pytest.raises(ProfilingTargetError, match="not an available inference service"):
        resolve_targets(_two_role_topology(), ProfilingStartRequest(service="other-model"))


def test_service_filter_selects_the_named_service():
    instances = _two_role_topology()
    instances[2] = _instance(2, "decode", [_endpoint(0, "10.0.0.9")], model_name="other-model")

    service, targets = resolve_targets(instances, ProfilingStartRequest(service="other-model"))

    assert service == "other-model"
    assert {target.instance_id for target in targets} == {2}


def test_instance_filter_reports_service_from_selected_targets() -> None:
    instances = _two_role_topology()
    instances[2] = _instance(2, "decode", [_endpoint(0, "10.0.0.9")], model_name="other-model")

    service, _ = resolve_targets(instances, ProfilingStartRequest(instances=[2]))

    assert service == "other-model"


def test_selection_rejects_two_targets_with_the_same_engine_address() -> None:
    instances = {
        0: _instance(0, "prefill", [_endpoint(0, "10.0.0.1")]),
        1: _instance(1, "decode", [_endpoint(0, "10.0.0.1")]),
    }

    with pytest.raises(ProfilingTargetError, match="instance 0 DP 0 and instance 1 DP 0 share profiling address"):
        resolve_targets(instances, ProfilingStartRequest())


def test_instance_filter_limits_targets_and_reports_unknown_ids():
    instances = _two_role_topology()

    _, targets = resolve_targets(instances, ProfilingStartRequest(instances=[0]))

    assert {target.instance_id for target in targets} == {0}
    with pytest.raises(ProfilingTargetError, match=r"unknown instance_id\(s\): \[9\]"):
        resolve_targets(instances, ProfilingStartRequest(instances=[9]))


def test_instance_filter_contradicting_role_filter_is_rejected():
    with pytest.raises(ProfilingTargetError, match="do not match the service/roles filter"):
        resolve_targets(_two_role_topology(), ProfilingStartRequest(instances=[1], roles=["prefill"]))


def test_dp_list_applies_to_every_selected_instance():
    instances = _two_role_topology()

    _, targets = resolve_targets(instances, ProfilingStartRequest(dps=[0]))

    assert [(target.instance_id, target.dp_rank) for target in targets] == [(0, 0), (1, 0)]


def test_dp_list_rejects_a_rank_missing_from_a_selected_instance():
    with pytest.raises(ProfilingTargetError, match=r"DP rank\(s\) \[1\]"):
        resolve_targets(_two_role_topology(), ProfilingStartRequest(dps=[1]))


def test_dp_mapping_targets_only_the_listed_ranks_per_instance():
    instances = _two_role_topology()

    _, targets = resolve_targets(instances, ProfilingStartRequest(dps={0: [1]}))

    assert [(target.instance_id, target.dp_rank) for target in targets] == [(0, 1)]


def test_dp_mapping_rejects_unknown_or_unselected_instances():
    instances = _two_role_topology()

    with pytest.raises(ProfilingTargetError, match=r"unknown instance_id\(s\): \[7\]"):
        resolve_targets(instances, ProfilingStartRequest(dps={7: [0]}))
    with pytest.raises(ProfilingTargetError, match="that are not selected"):
        resolve_targets(instances, ProfilingStartRequest(instances=[0], dps={1: [0]}))


def test_headless_endpoints_are_not_profiling_targets():
    instances = {
        0: _instance(
            0,
            "prefill",
            [_endpoint(0, "10.0.0.1"), _endpoint(1, "10.0.0.2", headless=True)],
        )
    }

    _, targets = resolve_targets(instances, ProfilingStartRequest())

    assert [target.dp_rank for target in targets] == [0]


def test_empty_topology_and_no_intersection_are_rejected():
    with pytest.raises(ProfilingTargetError, match="no available inference instances"):
        resolve_targets({}, ProfilingStartRequest())
    with pytest.raises(ProfilingTargetError, match="do not match the service/roles filter"):
        resolve_targets(
            {0: _instance(0, "prefill", [_endpoint(0, "10.0.0.1")])},
            ProfilingStartRequest(instances=[0], roles=["decode"]),
        )


def test_max_targets_bounds_the_fan_out():
    with pytest.raises(ProfilingTargetError, match="exceed max_targets"):
        resolve_targets(_two_role_topology(), ProfilingStartRequest(), max_targets=2)


def test_build_profile_prefix_carries_service_instance_role_and_dp():
    target = ProfilingTarget(
        service="deepseek v3/1",
        instance_id=2,
        role="decode",
        dp_rank=7,
        pod_ip="10.0.0.1",
        address="10.0.0.1:8000",
    )

    assert build_profile_prefix(target) == "motor_profile_svc-deepseek_v3_1_ins-2_role-decode_dp-7"
    long_name = target.model_copy(update={"service": "a" * 100})
    prefix = build_profile_prefix(long_name)
    assert len(prefix) == 128
    assert prefix.endswith("_ins-2_role-decode_dp-7")


def test_dp_mapping_profiles_only_the_listed_instance_dp_pairs():
    instances = _two_role_topology()

    _, targets = resolve_targets(instances, ProfilingStartRequest(instances=[0, 1], dps={0: [0]}))

    assert [(target.instance_id, target.dp_rank) for target in targets] == [(0, 0)]
