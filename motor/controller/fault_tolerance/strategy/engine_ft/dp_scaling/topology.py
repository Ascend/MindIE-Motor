# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Normalize scale-down evidence and resolve DP and Pod topology."""

from __future__ import annotations

from typing import Any

from motor.controller.fault_tolerance.fault_types import parse_npu_chip_ids


class FaultNormalizer:
    """Normalize fault-manager evidence into original DP-rank coordinates."""

    @staticmethod
    def hardware_fault_affects_instance(instance: Any, pod_ip: str, fault: Any) -> bool:
        """Return whether node hardware evidence belongs to this instance."""
        npu_name = getattr(fault, "npu_name", "")
        if not npu_name:
            return True
        device_ids = parse_npu_chip_ids(npu_name)
        if not device_ids:
            return True

        endpoints_by_pod = getattr(instance, "endpoints", None)
        if not isinstance(endpoints_by_pod, dict):
            return True
        pod_endpoints = endpoints_by_pod.get(pod_ip)
        if not isinstance(pod_endpoints, dict):
            return True
        owned_device_ids = {
            int(str(device.device_id))
            for endpoint in pod_endpoints.values()
            for device in endpoint.device_infos
            if str(device.device_id).isdigit()
        }
        # Preserve conservative node-level behavior for legacy registrations
        # that do not carry endpoint device ownership.
        return not owned_device_ids or bool(device_ids & owned_device_ids)

    @staticmethod
    def software_dp_ranks(faults: list[Any]) -> set[int]:
        """Return definitively dead engine ranks without interpreting engine Mask."""
        dead_ranks = set()
        for fault in faults:
            engine_id = getattr(fault, "engine_id", None)
            engine_status = getattr(fault, "engine_status", None)
            if not isinstance(engine_id, int):
                continue
            if engine_status == 1:
                dead_ranks.add(engine_id)
        # UNHEALTHY identifies an apply-capable engine, not the failed rank:
        # peer survivors can all become UNHEALTHY after a collective failure.
        return dead_ranks

    @staticmethod
    def hardware_dp_ranks(instance: Any, faults: list[tuple[str, str, Any]]) -> tuple[set[int], set[str]]:
        """Map MindCluster node/device evidence to Motor-owned DP endpoint topology."""
        endpoints_by_pod = getattr(instance, "endpoints", None)
        if not isinstance(endpoints_by_pod, dict):
            return set(), set()

        candidates: set[int] = set()
        mapped_fault_keys: set[str] = set()
        for fault_key, pod_ip, fault in faults:
            pod_endpoints = endpoints_by_pod.get(pod_ip)
            if not isinstance(pod_endpoints, dict) or not pod_endpoints:
                continue

            npu_name = getattr(fault, "npu_name", "")
            if not npu_name:
                candidates.update(endpoint.id for endpoint in pod_endpoints.values())
                mapped_fault_keys.add(fault_key)
                continue

            device_ids = parse_npu_chip_ids(npu_name)
            if not device_ids:
                continue
            matched = False
            for endpoint in pod_endpoints.values():
                if any(
                    str(device.device_id).isdigit() and int(str(device.device_id)) in device_ids
                    for device in endpoint.device_infos
                ):
                    candidates.add(endpoint.id)
                    matched = True
            if matched:
                mapped_fault_keys.add(fault_key)

        return candidates, mapped_fault_keys


class TopologyResolver:
    """Resolve stable original DP ranks from the Instance endpoint topology."""

    @staticmethod
    def original_dp_ranks(instance: Any) -> list[int]:
        """Return and validate the immutable external-LB DP rank coordinate space."""
        parallel_config = getattr(instance, "parallel_config", None)
        dp_size = getattr(parallel_config, "dp_size", 0)
        if not isinstance(dp_size, int) or dp_size <= 0:
            raise ValueError("instance DP size is unavailable")
        endpoints = instance.get_all_endpoints(include_headless=False)
        ranks = sorted({endpoint.id for endpoint in endpoints})
        if ranks != list(range(dp_size)):
            raise ValueError("routable endpoint ids do not match original DP ranks: %s" % ranks)
        return ranks

    @staticmethod
    def pod_dp_ranks(instance: Any) -> dict[str, set[int]]:
        """Return the original DP ranks hosted by each Pod IP."""
        endpoints = getattr(instance, "endpoints", None)
        if not isinstance(endpoints, dict):
            raise ValueError("instance endpoint topology is unavailable")

        original_ranks = set(TopologyResolver.original_dp_ranks(instance))
        result: dict[str, set[int]] = {}
        for pod_ip, pod_endpoints in endpoints.items():
            if not isinstance(pod_ip, str) or not isinstance(pod_endpoints, dict):
                raise ValueError("instance endpoint topology is malformed")
            ranks = {endpoint.id for endpoint in pod_endpoints.values() if endpoint.id in original_ranks}
            if ranks:
                result[pod_ip] = ranks
        return result

    @classmethod
    def expand_problem_pod_ranks(cls, instance: Any, seed_ranks: set[int]) -> set[int]:
        """Expand seed ranks to every original DP rank on any touched Pod."""
        if not seed_ranks:
            return set()
        expanded: set[int] = set()
        for pod_ranks in cls.pod_dp_ranks(instance).values():
            if pod_ranks & seed_ranks:
                expanded.update(pod_ranks)
        return expanded

    @staticmethod
    def elastic_pod_cut_target_dp(
        original_ranks: list[int] | set[int],
        expanded_ranks: set[int],
    ) -> int | None:
        """Return keep-size for Elastic EP recovery after emptying whole problem Pods.

        ``expanded`` must already be the union of whole Pods. Any position is
        accepted; survivors keep original DP ids and joiners fill the holes.
        At least one rank must survive.
        """
        if not expanded_ranks:
            return None
        original = set(original_ranks)
        expanded = set(expanded_ranks)
        if not original or not expanded <= original:
            return None
        keep = len(original) - len(expanded)
        return keep if keep >= 1 else None

    @classmethod
    def recyclable_pods(cls, instance: Any, dead_committed: set[int]) -> list[str]:
        """Return Pods whose complete original DP set has been removed."""
        return sorted(
            pod_ip for pod_ip, pod_ranks in cls.pod_dp_ranks(instance).items() if pod_ranks.issubset(dead_committed)
        )

    @classmethod
    def can_stop_all_problem_pods(cls, instance: Any, dead_ranks: set[int]) -> bool:
        """True when every failed rank sits on a Pod that can be stopped entirely."""
        if not dead_ranks:
            return False
        try:
            pod_ranks = cls.pod_dp_ranks(instance)
        except ValueError:
            return False
        covered: set[int] = set()
        for ranks in pod_ranks.values():
            if ranks and ranks.issubset(dead_ranks):
                covered.update(ranks)
        return dead_ranks <= covered
