# Copyright (c) 2026 ZhengDeL. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Opt-in, bounded observations of approved physical A2/A3 devices on the host.

The caller supplies its bounded subprocess runner. There is no background
sampler, broad ``info``/``watch`` query, host CPU/OS collection or driver change.
Physical IDs are resolved by the documented ``phyid-remap -p`` query rather
than inferred from logical IDs or card arithmetic. Unsupported output is unknown.
"""

# Exact JSON scalar types deliberately reject bool as a physical device ID.
# pylint: disable=unidiomatic-typecheck

from __future__ import annotations

import math
import re
import subprocess
import time
from typing import Callable


def _fields(text: str) -> dict[str, list[str]]:
    result = {}
    for line in text.splitlines():
        match = re.fullmatch(r"\s*\|?\s*([A-Za-z][A-Za-z0-9 ()%_/-]*)\s*:\s*(.*?)\s*\|?\s*", line)
        if match:
            key = re.sub(r"[^a-z0-9]", "", match[1].lower())
            result.setdefault(key, []).append(match[2].strip(" |"))
    return result


def _single(fields: dict, *keys: str) -> str | None:
    values = {value for key in keys for value in fields.get(key, [])}
    if len(values) > 1:
        raise ValueError("Observation contains conflicting identity or scalar fields")
    return next(iter(values)) if values else None


def _integer(value: str | None) -> int:
    if value is None or not re.fullmatch(r"[0-9]{1,10}", value):
        raise ValueError("Observation has no exact integer identity")
    return int(value)


def _mapping(text: str, requested: int) -> dict:
    fields = _fields(text)
    physical = _integer(_single(fields, "chipphysicalid", "chipphyid"))
    logical = _integer(_single(fields, "chiplogicid", "chiplogicalid"))
    card = _integer(_single(fields, "npuid"))
    chip = _integer(_single(fields, "chipid"))
    if physical != requested or max(physical, logical, card, chip) > 65535:
        raise ValueError("Physical NPU remap does not identify the requested device")
    return {"physical_id": physical, "logical_id": logical, "npu_id": card, "chip_id": chip}


def _scope(text: str, mapping: dict) -> dict:
    fields = _fields(text)
    for keys, name in (
        (["chipphysicalid", "chipphyid"], "physical_id"),
        (["chiplogicid", "chiplogicalid"], "logical_id"),
        (["npuid"], "npu_id"),
        (["chipid"], "chip_id"),
    ):
        value = _single(fields, *keys)
        if value is not None and _integer(value) != mapping[name]:
            raise ValueError("A chip query returned another device's identity")
    return fields


def _health(text: str, mapping: dict) -> tuple[bool, bool]:
    fields = _scope(text, mapping)
    value = _single(fields, "healthstatus", "health")
    if value is None:
        return False, False
    status = value.upper()
    return status == "OK", status in {"OK", "WARNING", "ALARM", "CRITICAL"}


def _process_count(text: str, mapping: dict) -> tuple[bool, int]:
    fields = _scope(text, mapping)
    identifiers = set()
    for key in ("processid", "pid", "procid"):
        for value in fields.get(key, []):
            identifier = _integer(value)
            if not 1 <= identifier <= 2**31 - 1:
                raise ValueError("Process identity is invalid")
            identifiers.add(identifier)
    header, scoped_table = False, False
    empty = False
    for raw in text.splitlines():
        line = raw.strip(" |\t")
        if re.search(r"\b(?:Process\s+ID|PID)\b", line, re.IGNORECASE) and ":" not in line:
            header = True
            scoped_table = bool(re.search(r"\bNPU\b", line) and re.search(r"\bChip\b", line))
            continue
        no_process = re.fullmatch(
            r"No running processes(?: found)?(?: in NPU ([0-9]+))?"
            r"(?:[, ]+Chip(?: ID)?\s*[: ]\s*([0-9]+))?\.?",
            line,
            re.IGNORECASE,
        )
        if no_process or re.fullmatch(r"No process in device\.?", line, re.IGNORECASE):
            if no_process is None:
                empty = True
                continue
            if no_process[1] is not None and int(no_process[1]) != mapping["npu_id"]:
                raise ValueError("Empty process observation belongs to another NPU")
            if no_process[2] is not None and int(no_process[2]) != mapping["chip_id"]:
                raise ValueError("Empty process observation belongs to another chip")
            empty = True
            continue
        if header and re.match(r"[0-9]", line):
            values = line.replace("|", " ").split()
            if scoped_table:
                if (
                    len(values) < 4
                    or not all(value.isdecimal() for value in values[:3])
                    or (int(values[0]), int(values[1])) != (mapping["npu_id"], mapping["chip_id"])
                ):
                    raise ValueError("Process table contains an unrequested device")
                identifier = int(values[2])
            else:
                if len(values) < 2 or not values[0].isdecimal():
                    raise ValueError("Process table row cannot be identified")
                identifier = int(values[0])
            if not 1 <= identifier <= 2**31 - 1:
                raise ValueError("Process identity is invalid")
            identifiers.add(identifier)
    raw_count = _single(fields, "processcount", "processnum", "proccount", "procnum")
    count = _integer(raw_count) if raw_count is not None else None
    if empty and (identifiers or count not in {None, 0}):
        raise ValueError("Process observations contradict an empty result")
    if count is not None and identifiers and count != len(identifiers):
        raise ValueError("Process count contradicts its returned identities")
    if empty:
        return True, 0
    if count is not None:
        return True, count
    return (True, len(identifiers)) if identifiers else (False, -1)


def _quantity(fields: dict, keys: tuple[str, ...], *, upper: bool = False) -> float | None:
    values = []
    for key in keys:
        text = _single(fields, key)
        if text is None:
            continue
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text):
            raise ValueError("HBM quantity is unavailable")
        number = float(text)
        if not math.isfinite(number) or number > 2**40:
            raise ValueError("HBM quantity is outside its supported range")
        # Treat an MB label as decimal: this remains a lower bound if the HDK
        # actually printed binary MiB. Explicit MiB labels need no conversion.
        values.append(number if key.endswith("mib") or upper else number * 1000000 / (1024 * 1024))
    return (max(values) if upper else min(values)) if values else None


def _memory(text: str, mapping: dict) -> tuple[float | None, str | None]:
    fields = _scope(text, mapping)
    free = _quantity(fields, ("hbmfreemb", "hbmfreemib", "hbmfreememorymb", "hbmfreecapacitymb"))
    capacity = _quantity(fields, ("hbmcapacitymb", "hbmtotalcapacitymb", "hbmcapacitymib", "hbmtotalcapacitymib"))
    used = _quantity(fields, ("hbmusedmb", "hbmusagemb", "hbmusedmib", "hbmusagemib"), upper=True)
    if free is not None:
        if capacity is not None and free > capacity:
            raise ValueError("HBM free amount exceeds capacity")
        return free, "reported-free-MB-conservative-MiB"
    if capacity is not None and used is not None:
        capacity_upper = _quantity(
            fields, ("hbmcapacitymb", "hbmtotalcapacitymb", "hbmcapacitymib", "hbmtotalcapacitymib"), upper=True
        )
        used_lower = _quantity(fields, ("hbmusedmb", "hbmusagemb", "hbmusedmib", "hbmusagemib"))
        if used_lower > capacity_upper:
            raise ValueError("HBM usage is inconsistent with capacity")
        return max(0.0, capacity - used), "capacity-lower-minus-used-upper-bound-MiB"
    rate_text = _single(fields, "hbmusagerate", "hbmusagepercent", "hbmusage")
    if capacity is None or rate_text is None or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", rate_text):
        return None, None
    rate = float(rate_text)
    if not math.isfinite(rate) or not 0 <= rate <= 100:
        raise ValueError("HBM usage rate is invalid")
    return capacity * max(0.0, 1 - (rate + 1) / 100), "capacity-rate-lower-bound-plus-one-percentage-point"


def _utilization(text: str, mapping: dict) -> float | None:
    fields = _scope(text, mapping)
    primary = ("aicoreusagerate", "aicoreusagepercent", "aicoreusage", "npuutilization")
    if not any(key in fields for key in primary):
        return None
    values = []
    for key in (*primary, "aivectorusagerate", "aicubeusagerate"):
        raw = _single(fields, key)
        if raw is None:
            continue
        raw = raw.rstrip("%").strip()
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", raw):
            raise ValueError("Accelerator utilization is unavailable")
        value = float(raw)
        if not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError("Accelerator utilization is invalid")
        values.append(value)
    return max(values)


def sample_devices(
    host: dict, requested: list[int], hardware_type: str, *, run: Callable
) -> tuple[list[dict], str | None]:
    """Read selected host devices only, with a 20s total / 3s command budget.

    ``run`` is the worker's bounded argv runner, accepting timeout/limit and
    returning CompletedProcess. Missing/unsupported commands remain unknown.
    Optional HBM figures can be conservative lower bounds, not model-fit proof.
    """
    if (
        not isinstance(requested, list)
        or len(requested) > 16
        or any(type(value) is not int or not 0 <= value <= 65535 for value in requested)
        or len(set(requested)) != len(requested)
    ):
        raise ValueError("NPU observation needs up to 16 unique approved physical IDs")
    rows = {
        value: {
            "id": value,
            "healthy": False,
            "health_known": False,
            "processes_known": False,
            "process_count": -1,
            "free_hbm_mib": None,
            "free_hbm_basis": None,
            "utilization_percent": None,
            "mapping_verified": False,
        }
        for value in requested
    }
    if not requested:
        return [], None
    if host.get("observe_npu") is not True:
        return list(rows.values()), "One-shot host NPU observation is not explicitly enabled"
    if hardware_type not in {"800I_A2", "800I_A3"}:
        return list(rows.values()), "One-shot NPU observation supports only the selected A2/A3 profiles"
    deadline = time.monotonic() + 20

    def query(*arguments: str) -> str:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("NPU observation time budget expired")
        value = run(["npu-smi", "info", *arguments], timeout=min(3, remaining), limit=256 * 1024)
        if not isinstance(value, subprocess.CompletedProcess) or value.returncode != 0:
            raise ValueError("A requested NPU query was unavailable")
        raw = value.stdout
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        if not isinstance(text, str) or len(text.encode("utf-8")) > 256 * 1024:
            raise ValueError("NPU output exceeds its supported bounds")
        if re.search(r"(?im)^\s*(?:\[ERROR\]|ERROR\s*:|Failed\b|This device does not support\b)", text):
            raise ValueError("A requested NPU query was unsupported")
        return text

    mappings, pairs, logical_ids = {}, set(), set()
    try:
        for identifier in requested:
            try:
                mapping = _mapping(query("-t", "phyid-remap", "-p", str(identifier)), identifier)
            except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
                continue
            pair = mapping["npu_id"], mapping["chip_id"]
            if pair in pairs or mapping["logical_id"] in logical_ids:
                return list(rows.values()), "Requested physical NPU identities are ambiguous"
            pairs.add(pair)
            logical_ids.add(mapping["logical_id"])
            mappings[identifier] = mapping
        for identifier, mapping in mappings.items():
            row = rows[identifier]
            row.update(
                mapping_verified=True,
                npu_id=mapping["npu_id"],
                chip_id=mapping["chip_id"],
                logical_id=mapping["logical_id"],
            )
            selected = ("-i", str(mapping["npu_id"]), "-c", str(mapping["chip_id"]))
            try:
                row["healthy"], row["health_known"] = _health(query("-t", "health", *selected), mapping)
            except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
                pass
            try:
                row["processes_known"], row["process_count"] = _process_count(
                    query("-t", "proc-mem", *selected), mapping
                )
            except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
                pass
            try:
                memory = query("-t", "memory", *selected)
                row["free_hbm_mib"], row["free_hbm_basis"] = _memory(memory, mapping)
            except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
                pass
            try:
                usages = query("-t", "usages", *selected)
                row["utilization_percent"] = _utilization(usages, mapping)
                if row["free_hbm_mib"] is None:
                    row["free_hbm_mib"], row["free_hbm_basis"] = _memory(usages, mapping)
            except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
                # Preserve facts obtained from the selected device; never make
                # a failed process query look like a verified empty process set.
                continue
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
        pass
    complete = all(
        row["mapping_verified"]
        and row["health_known"]
        and row["processes_known"]
        and row["free_hbm_mib"] is not None
        and row["utilization_percent"] is not None
        for row in rows.values()
    )
    return list(rows.values()), None if complete else "One or more requested NPU observations are unavailable"
