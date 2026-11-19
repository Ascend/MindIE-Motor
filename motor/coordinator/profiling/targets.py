# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Resolve a profiling selector into concrete DP endpoints.

Selection semantics match the Coordinator metrics dimensions:
service = instance ``model_name``, ``instance_id`` = ``Instance.id``,
``role`` = instance role, ``dp_rank`` = ``Endpoint.id``.
"""

import re
from collections.abc import Mapping

from motor.common.resources.endpoint import Endpoint
from motor.common.resources.instance import Instance
from motor.common.utils.net import format_address
from motor.coordinator.profiling.models import ProfilingTarget, ProfilingTargetSelector, canonical_role

# Keep profile prefixes filesystem-safe and readable in trace file names.
_UNSAFE_PREFIX_CHARS = re.compile(r"[^0-9A-Za-z_.-]+")
_PROFILE_PREFIX_PATTERN = re.compile(r"[0-9A-Za-z][0-9A-Za-z_.-]{0,127}")
_PROFILE_PREFIX_LIMIT = 128


class ProfilingTargetError(ValueError):
    """The selector cannot be resolved into a non-empty profiling target set."""


def build_profile_prefix(target: ProfilingTarget, override: str | None = None) -> str:
    """Build the dimension-tagged result prefix for one target.

    The prefix reuses the metrics dimensions so trace files collected from many
    instances/DPs stay distinguishable: ``motor_profile_svc-<...>_ins-<id>_role-<role>_dp-<rank>``.
    """
    suffix = f"_ins-{target.instance_id}_role-{target.role}_dp-{target.dp_rank}"
    if override is None:
        service = _UNSAFE_PREFIX_CHARS.sub("_", target.service).strip("_") or "service"
        base = f"motor_profile_svc-{service}"[: max(0, _PROFILE_PREFIX_LIMIT - len(suffix))]
    else:
        base = override
    prefix = base if base.endswith(suffix) else base + suffix
    if not _PROFILE_PREFIX_PATTERN.fullmatch(prefix):
        raise ProfilingTargetError(
            "profile_prefix including its target suffix must be 1-128 ASCII characters, start with "
            "a letter or number, and contain only letters, numbers, '.', '_' or '-'"
        )
    return prefix


def _endpoints(instance: Instance) -> dict[int, Endpoint]:
    """Return the routable (non-headless) DP endpoints of one instance keyed by DP rank."""
    endpoints: dict[int, Endpoint] = {}
    for endpoint in instance.get_all_endpoints():
        endpoints[endpoint.id] = endpoint
    return endpoints


def _instance_role(instance: Instance) -> str:
    role = instance.role
    value = getattr(role, "value", role)
    return str(value)


def _describe_ids(ids: list[int]) -> str:
    """Show a bounded sample of selector errors."""
    sample = "[" + ", ".join(str(value)[:32] for value in ids[:8]) + "]"
    return sample if len(ids) <= 8 else f"{sample} ({len(ids)} total)"


def resolve_targets(
    instances: Mapping[int, Instance],
    selector: ProfilingTargetSelector,
    max_targets: int = 0,
) -> tuple[str, list[ProfilingTarget]]:
    """Resolve one selector against the live instance view.

    Args:
        instances: Available instances keyed by instance id, as returned by the
            scheduler instance view.
        selector: Service / role / instance / DP filters.
        max_targets: Upper bound for the resolved target count; ``<= 0`` disables
            the bound.

    Returns:
        ``(service, targets)`` where ``service`` is the selected service name, or
        an empty string when the selector spans multiple services.

    Raises:
        ProfilingTargetError: The selector matches no target, names unknown
            instances/DPs/services, or exceeds ``max_targets``.
    """
    if not instances:
        raise ProfilingTargetError("no available inference instances to profile")

    known_instance_ids = set(instances)
    requested_service = selector.service
    if requested_service is not None:
        services = {instance.model_name for instance in instances.values()}
        if requested_service not in services:
            available = ", ".join(sorted(services))
            raise ProfilingTargetError(
                f"service {requested_service!r} is not an available inference service; available: {available}"
            )

    requested_roles = set(selector.roles or [])
    candidates = [
        instance
        for instance in instances.values()
        if (requested_service is None or instance.model_name == requested_service)
        and (not requested_roles or canonical_role(_instance_role(instance)) in requested_roles)
    ]

    if selector.instances is not None:
        requested_instances = set(selector.instances)
        unknown_instances = sorted(requested_instances - known_instance_ids)
        if unknown_instances:
            raise ProfilingTargetError(f"unknown instance_id(s): {_describe_ids(unknown_instances)}")
        candidate_ids = {instance.id for instance in candidates}
        filtered_out = sorted(requested_instances - candidate_ids)
        if filtered_out:
            raise ProfilingTargetError(
                f"instance_id(s) {_describe_ids(filtered_out)} do not match the service/roles filter"
            )
        candidates = [instance for instance in candidates if instance.id in requested_instances]

    if not candidates:
        raise ProfilingTargetError("no profiling targets matched the service/roles/instances selector")
    candidates.sort(key=lambda instance: instance.id)
    candidate_ids = {instance.id for instance in candidates}

    dp_selector = selector.dps
    if isinstance(dp_selector, dict):
        unknown_instances = sorted(set(dp_selector) - known_instance_ids)
        if unknown_instances:
            raise ProfilingTargetError(f"dps specified for unknown instance_id(s): {_describe_ids(unknown_instances)}")
        outside_selection = sorted(set(dp_selector) - candidate_ids)
        if outside_selection:
            raise ProfilingTargetError(
                f"dps specified for instance_id(s) {_describe_ids(outside_selection)} that are not selected"
            )
        # An explicit instance -> rank mapping means "profile exactly these pairs".
        candidates = [instance for instance in candidates if instance.id in dp_selector]

    targets: list[ProfilingTarget] = []
    by_address: dict[str, ProfilingTarget] = {}
    for instance in candidates:
        endpoints = _endpoints(instance)
        if isinstance(dp_selector, dict):
            # Exact-pairs semantics: only the instances/ranks listed in the mapping
            # are profiled; the pre-filter above intentionally drops other candidates.
            requested_ranks = dp_selector[instance.id]
            explicit_instance = True
        elif isinstance(dp_selector, list):
            requested_ranks = list(dp_selector)
            explicit_instance = True
        else:
            requested_ranks = sorted(endpoints)
            explicit_instance = selector.instances is not None

        if not requested_ranks:
            if explicit_instance:
                raise ProfilingTargetError(f"instance {instance.id} has no routable DP endpoint to profile")
            continue

        missing_ranks = sorted(set(requested_ranks) - set(endpoints))
        if missing_ranks:
            raise ProfilingTargetError(
                f"instance {instance.id} has no endpoint for DP rank(s) {_describe_ids(missing_ranks)}; "
                f"available DP ranks: {_describe_ids(sorted(endpoints))}"
            )

        for dp_rank in requested_ranks:
            endpoint = endpoints[dp_rank]
            address = format_address(endpoint.ip, endpoint.business_port)
            previous = by_address.get(address)
            if previous is not None:
                raise ProfilingTargetError(
                    f"instance {previous.instance_id} DP {previous.dp_rank} and instance "
                    f"{instance.id} DP {dp_rank} share profiling address {address}; narrow the selection"
                )
            target = ProfilingTarget(
                service=instance.model_name,
                instance_id=instance.id,
                role=_instance_role(instance),
                dp_rank=dp_rank,
                pod_ip=endpoint.ip,
                address=address,
            )
            by_address[address] = target
            targets.append(target)

    if not targets:
        raise ProfilingTargetError("no profiling targets matched: every selected instance has no routable DP endpoint")
    if 0 < max_targets < len(targets):
        raise ProfilingTargetError(
            f"resolved {len(targets)} profiling targets exceed max_targets={max_targets}; "
            "narrow the service/roles/instances/dps selection"
        )

    selected_services = {target.service for target in targets}
    return (targets[0].service if len(selected_services) == 1 else ""), targets
