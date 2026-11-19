# Copyright (c) 2026 ZhengDeL. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Pure reconciliation decisions for explicitly approved whole-instance slots."""

# JSON booleans must not pass integer resource and process-count validation.
# pylint: disable=unidiomatic-typecheck

import math
from typing import Any


def _fresh(observation: dict, now: float, maximum_age: float = 30) -> bool:
    sampled = observation.get("received_at", observation.get("collected_at"))
    return type(sampled) in {int, float} and math.isfinite(sampled) and 0 <= now - sampled <= maximum_age


def _available(slot: dict, observation: dict, now: float, maximum_age: float = 30) -> str | None:
    if observation.get("ok") is not True:
        return "host_unavailable"
    if not _fresh(observation, now, maximum_age):
        return "observation_expired"
    if not observation.get("machine_id") or not observation.get("daemon_id"):
        return "host_identity_unknown"
    required = set(slot["devices"])
    if required & set(observation.get("occupied_devices", [])):
        return "device_declared_by_container"
    ports = set(slot.get("_reserved_ports", slot.get("ports", {}).values()))
    if ports & set(observation.get("occupied_ports", [])):
        return "port_in_use"
    devices = {item["id"]: item for item in observation.get("devices", []) if "id" in item}
    minimum = slot.get("minimum_free_hbm_mib", 0)
    for identifier in required:
        item = devices.get(identifier, {})
        if item.get("healthy") is not True or item.get("processes_known") is not True:
            return "hardware_observation_unknown"
        if type(item.get("process_count")) is not int or item["process_count"] != 0:
            return "device_has_processes"
        utilization = item.get("utilization_percent")
        if type(utilization) not in {int, float} or not math.isfinite(utilization) or utilization != 0:
            return "hardware_not_idle"
        free = item.get("free_hbm_mib")
        if type(free) not in {int, float} or not math.isfinite(free) or free <= 0 or free < minimum:
            return "insufficient_hbm"
    return None


def plan(spec: dict, snapshot: dict, observations: dict, now: float) -> dict[str, Any]:
    """Return actions without reserving resources or changing any state."""
    result: dict[str, Any] = {"actions": [], "blocked": [], "desired": {}}
    maximum_age = spec.get("policy", {}).get("observation_max_age_seconds", 30)
    desired = snapshot.get("desired") or spec["desired"]
    result["desired"] = dict(desired)
    operations = snapshot.get("operations", {})
    pending = [
        key
        for key, value in operations.items()
        if value.get("phase") in {"prepared", "submitted", "unknown"}
        or value.get("phase") == "failed"
        and value.get("may_have_effects") is not False
    ]
    if pending:
        result["blocked"].append({"reason": "unresolved_operation", "operation_ids": sorted(pending)})
        return result
    machines: dict[str, str] = {}
    for host_id, value in observations.items():
        identity = value.get("machine_id")
        if value.get("ok") is not True or not identity:
            continue
        if identity in machines and machines[identity] != host_id:
            result["blocked"].append({"reason": "duplicate_physical_host", "host_id": host_id})
            return result
        machines[identity] = host_id
    instances = snapshot.get("instances", {})
    slots = {item["id"]: item for item in spec["slots"]}
    for slot_id, instance in sorted(instances.items()):
        if instance.get("role") in {"management", "coordinator,controller"}:
            continue
        if instance.get("status") in {"removed", "released"}:
            continue
        slot = slots.get(slot_id)
        if slot is None:
            result["blocked"].append({"slot_id": slot_id, "reason": "instance_slot_missing"})
            continue
        observation = observations.get(slot["host_id"], {})
        if observation.get("ok") is not True:
            result["blocked"].append({"slot_id": slot_id, "reason": "host_unavailable_fencing_required"})
            continue
        if not _fresh(observation, now, maximum_age):
            result["blocked"].append({"slot_id": slot_id, "reason": "observation_expired"})
            continue
        for field, observed in (("host_machine_id", "machine_id"), ("host_daemon_id", "daemon_id")):
            if instance.get(field) and instance[field] != observation.get(observed):
                result["blocked"].append({"slot_id": slot_id, "reason": "host_identity_changed"})
                return result
    if result["blocked"]:
        return result
    held_devices: dict[str, set[int]] = {}
    held_ports: dict[str, set[int]] = {}
    for value in instances.values():
        if value.get("status") in {"removed", "released"}:
            continue
        slot = slots.get(value.get("slot_id"))
        host_id = value.get("host_id") or (slot or {}).get("host_id")
        ports = value.get("reserved_ports")
        if host_id and isinstance(ports, list):
            held_ports.setdefault(host_id, set()).update(ports)
        if slot:
            held_devices.setdefault(slot["host_id"], set()).update(slot["devices"])
            if not isinstance(ports, list):
                held_ports.setdefault(slot["host_id"], set()).update(
                    slot.get("_reserved_ports", slot.get("ports", {}).values())
                )
    for role, count in sorted(desired.items()):
        current = [
            (key, value)
            for key, value in instances.items()
            if value.get("role") == role and value.get("status") not in {"removed", "released"}
        ]
        retiring = [slot_id for slot_id, value in current if value.get("status") == "stopped"]
        if retiring:
            result["actions"].extend({"action": "drain", "slot_id": slot_id} for slot_id in retiring)
            continue
        if len(current) > count:
            candidates = sorted(current, key=lambda row: row[0], reverse=True)
            for slot_id, _ in candidates[: len(current) - count]:
                result["actions"].append({"action": "drain", "slot_id": slot_id})
            continue
        for slot_id, value in current:
            observation = observations.get(slots[slot_id]["host_id"], {})
            containers = {item["id"]: item for item in observation.get("containers", [])}
            identifiers = value.get("container_ids", [])
            observed = [containers[identifier] for identifier in identifiers if identifier in containers]
            if (
                identifiers
                and len(observed) == len(identifiers)
                and all(item.get("status") in {"exited", "dead"} for item in observed)
            ):
                if any(
                    item.get("oom_killed") is True
                    or (type(item.get("exit_code")) is int and item["exit_code"] not in {0, 143})
                    for item in observed
                ):
                    recovery_slot = {
                        **slots[slot_id],
                        "_reserved_ports": value.get("reserved_ports", list(slots[slot_id]["ports"].values())),
                    }
                    error = _available(recovery_slot, observation, now, maximum_age)
                    if error:
                        result["blocked"].append({"reason": error, "slot_id": slot_id})
                    else:
                        result["actions"].append({"action": "recover", "slot_id": slot_id})
                else:
                    result["blocked"].append({"reason": "externally_stopped", "slot_id": slot_id})
        missing = count - len(current)
        for slot in sorted(spec["slots"], key=lambda item: item["id"]):
            if missing <= 0:
                break
            if slot["role"] != role:
                continue
            previous = instances.get(slot["id"])
            if previous and previous.get("status") not in {"removed", "released"}:
                continue
            host_id = slot["host_id"]
            if set(slot["devices"]) & held_devices.get(host_id, set()):
                continue
            candidate_ports = set(slot.get("_reserved_ports", slot.get("ports", {}).values()))
            if candidate_ports & held_ports.get(host_id, set()):
                continue
            error = _available(slot, observations.get(host_id, {}), now, maximum_age)
            if error:
                result["blocked"].append({"slot_id": slot["id"], "reason": error})
                continue
            result["actions"].append({"action": "create", "slot_id": slot["id"]})
            held_devices.setdefault(host_id, set()).update(slot["devices"])
            held_ports.setdefault(host_id, set()).update(candidate_ports)
            missing -= 1
        if missing > 0:
            result["blocked"].append({"role": role, "reason": "insufficient_approved_capacity", "missing": missing})
    return result
