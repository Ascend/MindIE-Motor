# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Direct a registered source NodeManager to evacuate its Memcache provider."""

import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, wait
from typing import TYPE_CHECKING

from motor.common.logger import get_logger
from motor.common.resources.instance import Instance, NodeManagerInfo, ReadOnlyInstance
from motor.common.utils.kv_migration import run_directed_drain
from motor.controller.api_client.node_manager_api_client import NodeManagerApiClient
from motor.controller.fault_tolerance.engine_recovery.runtime import FtPhase, get_ft_runtime_store
from motor.controller.fault_tolerance.fault_types import FaultLevel
from motor.controller.fault_tolerance.scale_in_migration import (
    ScaleInIngress,
    live_migration_targets,
    retained_scale_in_targets,
)

if TYPE_CHECKING:
    from motor.controller.core.instance_manager import InstanceManager

logger = get_logger(__name__)


def _provider_inventory(node: NodeManagerInfo, request) -> dict:
    inventory = request(node, "inventory")
    if not isinstance(inventory, dict):
        raise ValueError(f"Invalid inventory from {node.pod_ip}: expected an object")
    for field in ("uid", "pool", "label"):
        value = inventory.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Invalid {field} in inventory from {node.pod_ip}")
    ranks = inventory.get("ranks")
    if (
        not isinstance(ranks, list)
        or not ranks
        or not all(isinstance(rank, int) and not isinstance(rank, bool) and 0 <= rank < (1 << 32) - 1 for rank in ranks)
    ):
        raise ValueError(f"Invalid ranks in inventory from {node.pod_ip}")
    return inventory


def _select_targets(
    source: NodeManagerInfo, candidates: list[NodeManagerInfo], source_uid: str | None, request
) -> tuple[dict, dict[str, dict]]:
    """Select distinct providers in the source pool. Memcache handles placement and capacity."""
    before = _provider_inventory(source, request)
    if source_uid is not None and before["uid"] != source_uid:
        raise ValueError("Source Pod UID changed")
    allowed = {}
    for node in candidates:
        if node.pod_ip == source.pod_ip:
            continue
        try:
            dest = _provider_inventory(node, request)
        except Exception as exc:
            logger.warning("Skipping unavailable KV target %s: %s", node.pod_ip, exc)
            continue
        if node.pod_uid and node.pod_uid != dest["uid"]:
            continue
        label = dest["label"]
        if label == before["label"] or label in allowed or dest["pool"] != before["pool"]:
            continue
        if set(dest["ranks"]).intersection(before["ranks"]):
            continue
        allowed[label] = {**dest, "registration": (node.pod_ip, node.pod_uid)}
    return before, allowed


def evacuate_provider(
    source: NodeManagerInfo,
    candidates: list[NodeManagerInfo],
    operation_id: str,
    deadline: float,
    source_uid: str | None = None,
    recheck_targets: Callable[[], set[tuple[str, str | None]]] | None = None,
) -> dict:
    """Submit one directed drain and report its native terminal status and counters."""
    request = NodeManagerApiClient.migration_request

    def rpc(node, action, payload=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("KV migration deadline reached")
        rpc_timeout = min(12, remaining)
        if action == "trigger":
            payload = {**payload, "timeout_sec": rpc_timeout}
        return request(node, action, payload, timeout=rpc_timeout)

    candidates = live_migration_targets(candidates, deadline)
    before, allowed = _select_targets(source, candidates, source_uid, rpc)
    if recheck_targets is not None and allowed:
        retained = recheck_targets()
        allowed = {label: dest for label, dest in allowed.items() if dest["registration"] in retained}
    if not allowed:
        raise ValueError("No eligible target KV provider")
    payload = {
        "source_uid": before["uid"],
        "operation_id": operation_id,
        "target_node_labels": sorted(allowed),
    }
    result = run_directed_drain(
        lambda: rpc(source, "trigger", payload),
        deadline,
        query=lambda job, left: request(source, "status", {"drain_id": job}, timeout=min(12, left)),
        cancel=lambda job, left: request(source, "cancel", {"drain_id": job}, timeout=left),
    )
    return {**result, "source_uid": before["uid"]}


def stop_original_member(node: NodeManagerInfo) -> bool:
    """A source address may have been reassigned while its drain was in progress."""
    if not node.pod_uid:
        return NodeManagerApiClient.stop(node)
    try:
        NodeManagerApiClient.migration_request(node, "stop", {"source_uid": node.pod_uid})
        return True
    except Exception as exc:
        response = getattr(exc.__cause__, "response", None)
        if response is not None and response.status_code == 409:
            current_uid = response.headers.get("X-Motor-Pod-UID", "").strip()
            if current_uid and current_uid != node.pod_uid:
                logger.info(
                    "Skipping stop of original Pod %s: %s now serves Pod %s", node.pod_uid, node.pod_ip, current_uid
                )
                return True
        logger.error("Failed to stop original Pod %s at %s: %s", node.pod_uid, node.pod_ip, exc)
        return False


def _eligible_recovery_targets(
    manager: "InstanceManager", peers: Iterable[tuple[ReadOnlyInstance, list[NodeManagerInfo]]]
) -> dict[tuple[int, str, str | None], NodeManagerInfo]:
    from motor.controller.fault_tolerance.fault_manager import FaultManager

    targets = {}
    fault_manager = FaultManager()
    runtime_store = get_ft_runtime_store()
    for peer, nodes in peers:
        if manager.is_instance_separated(peer.id):
            continue
        level = fault_manager.get_instance_fault_level(peer.id)
        if level is not None and level > FaultLevel.L2:
            continue
        runtime = runtime_store.get(peer.id)
        if runtime and (
            runtime["phase"] not in (FtPhase.NORMAL, FtPhase.DISABLED, FtPhase.SCALED_DOWN_RUNNING)
            or runtime["recovery_paused"]
            or not runtime["serving_published"]
        ):
            continue
        for node in nodes:
            targets[peer.id, node.pod_ip, node.pod_uid] = node
    return targets


def evacuate_instance(manager: "InstanceManager", instance: Instance, *, timeout: float) -> None:
    """Run each source independently until the instance migration deadline."""
    deadline = time.monotonic() + timeout
    members = instance.get_node_managers()
    excluded = {node.pod_ip for node in members}
    peers = list(_compatible_peer_members(manager, instance, excluded))
    if not peers:
        logger.info("No compatible KV target for retiring instance %s", instance.id)
        return

    def evacuate(node: NodeManagerInfo, uid: str) -> None:
        try:
            original = _eligible_recovery_targets(manager, peers)
            if not original:
                raise ValueError("No eligible target KV provider")

            def recheck_targets() -> set[tuple[str, str | None]]:
                # A peer may enter recovery or register a replacement while inventories are being read.
                current = _eligible_recovery_targets(manager, _compatible_peer_members(manager, instance, excluded))
                return {(ip, pod_uid) for _, ip, pod_uid in original.keys() & current.keys()}

            result = evacuate_provider(node, list(original.values()), "exit-" + uid, deadline, uid, recheck_targets)
            logger.info("Instance %s KV migration from %s: %s", instance.id, node.pod_ip, result)
        except Exception as exc:
            logger.warning("Instance %s KV migration from %s failed: %s", instance.id, node.pod_ip, exc)

    sources = []
    for node in members:
        if node.pod_uid:
            sources.append((node, node.pod_uid))
        else:
            logger.warning("Skipping KV source %s without a registered Pod UID", node.pod_ip)
    if not sources:
        return

    executor = ThreadPoolExecutor(max_workers=len(sources))
    try:
        futures = [executor.submit(evacuate, node, uid) for node, uid in sources]
        _, pending = wait(futures, timeout=max(0, deadline - time.monotonic()))
        if pending:
            logger.warning(
                "KV migration budget exhausted for instance %s with %s sources pending", instance.id, len(pending)
            )
    finally:
        # A slow RPC must not delay the existing stop dispatch beyond the shared deadline.
        executor.shutdown(wait=False, cancel_futures=True)


def cross_instance_targets(
    manager: "InstanceManager",
    instance: Instance,
    excluded: set[str],
) -> list[NodeManagerInfo]:
    """Find compatible active peers outside all generations of the retiring job."""
    return [node for _, nodes in _compatible_peer_members(manager, instance, excluded) for node in nodes]


def _compatible_peer_members(
    manager: "InstanceManager", instance: Instance, excluded: set[str]
) -> Iterator[tuple[ReadOnlyInstance, list[NodeManagerInfo]]]:
    from motor.common.resources import InsStatus

    for peer in sorted(
        manager.get_instances({InsStatus.ACTIVE}), key=lambda item: item.gathered_workload.active_tokens
    ):
        members = peer.get_node_managers()
        if (
            peer.job_name == instance.job_name
            or peer.model_name != instance.model_name
            or peer.engine_type != instance.engine_type
            or peer.parallel_config != instance.parallel_config
            or any(nm.pod_ip in excluded for nm in members)
        ):
            continue
        yield peer, members


def plan_provider_exit(
    manager: "InstanceManager",
    source_ip: str,
    uid: str,
    *,
    deadline: float | None = None,
    ingress: ScaleInIngress | None = None,
    headless: bool = False,
) -> dict:
    if deadline is None:
        deadline = time.monotonic() + 30
    instance = manager.get_instance_by_podip(source_ip)
    group, ready = ingress.resolve(instance, source_ip, uid, headless, deadline) if ingress else (None, False)
    if headless and not ready:
        return {"source_uid": uid, "wait_for_ingress": True}
    if group is not None:
        instance = group.instance
        members = list(group.members.values())
    else:
        if instance is None:
            raise ValueError("Source is not registered in this controller")
        members = instance.get_node_managers()
    source = next((node for node in members if node.pod_ip == source_ip), None)
    if source is None:
        raise ValueError(f"Source pod {source_ip} has no registered NodeManager")
    if source.pod_uid and source.pod_uid != uid:
        raise ValueError("Registered source Pod UID changed")
    candidates = cross_instance_targets(manager, instance, {node.pod_ip for node in members})
    candidates = retained_scale_in_targets(candidates, uid, deadline, headless=headless)
    candidates_left = len(candidates)

    def inventory(node: NodeManagerInfo, action: str) -> dict:
        nonlocal candidates_left
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("KV migration budget exhausted during exit planning")
        if node is not source:
            # Share remaining time with unqueried peers and leave a share for the drain itself.
            remaining /= candidates_left + 1
            candidates_left -= 1
        return NodeManagerApiClient.migration_request(node, action, timeout=min(12, remaining))

    before, allowed = _select_targets(source, candidates, uid, inventory)
    return {
        "source_uid": uid,
        **({"ingress_drained": True} if headless else {}),
        "src_ranks": before["ranks"],
        "targets": sorted(allowed),
        "dst_ranks": sorted({rank for dest in allowed.values() for rank in dest["ranks"]}),
    }
