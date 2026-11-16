# Copyright (c) 2026 ZhengDeL. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Short-lived host execution with native preparation and persistent operation receipts.

The worker is trusted administrative code, not a sandbox. It does not listen on a
network socket, collect hardware through a new daemon, or retry an uncertain write.
"""

from __future__ import annotations

# Exact JSON scalar checks intentionally reject bool, which is an int subclass.
# pylint: disable=unidiomatic-typecheck

import contextlib
import copy
import hashlib
import importlib
import ipaddress
import json
import logging
import math
import os
import re
import shlex
import stat
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any


_PREFIX = "io.mindie.motor.orchestration."
_LABELS = {key: _PREFIX + key for key in ("cluster", "slot", "generation", "operation", "digest", "job")}
_IDENTIFIER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,95}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_CID = re.compile(r"[0-9a-f]{64}")
_MAX_JSON = 4 * 1024 * 1024
_MAX_CONTAINERS = 4096
_READ_ACTIONS = frozenset({"observe", "plan", "inspect", "status", "operation", "logs"})
_ROLES = frozenset({"prefill", "decode", "union", "coordinator_controller"})
_TERMINAL = frozenset({"exited", "dead", "created"})


class NativeError(RuntimeError):
    """A refused request or an operation requiring explicit reconciliation."""

    def __init__(self, message: str, *, outcome_unknown: bool = False) -> None:
        super().__init__(message)
        self.outcome_unknown = outcome_unknown


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise NativeError(message)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":")).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _pairs(values: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in values:
        _require(key not in result, "Duplicate JSON key")
        result[key] = value
    return result


def _json(raw: bytes) -> Any:
    _require(len(raw) <= _MAX_JSON, "JSON exceeds its size limit")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_reject_constant)


def _reject_constant(_value: str) -> None:
    raise ValueError("Non-finite JSON")


def _absolute(raw: Any) -> Path:
    _require(
        isinstance(raw, str) and raw.startswith("/") and len(raw) <= 4096 and not any(ord(char) < 32 for char in raw),
        "Expected an absolute POSIX path",
    )
    path = Path(raw)
    _require(".." not in path.parts and path != Path("/"), "Unsafe absolute path")
    for component in (path, *path.parents):
        _require(not component.is_symlink(), "Symlink path is not accepted")
    return path


def _read(path: Path, *, private: bool = False, limit: int = _MAX_JSON) -> bytes:
    _absolute(str(path))
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        _require(stat.S_ISREG(info.st_mode) and info.st_size <= limit, "Invalid bounded regular file")
        if private:
            # The host worker is Linux-only; Windows stubs omit this POSIX API.
            _require(info.st_uid == os.geteuid() and not info.st_mode & 0o077, "File is not private")  # pylint: disable=no-member
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            value = source.read(limit + 1)
        _require(len(value) <= limit, "File exceeds its read limit")
        return value
    finally:
        os.close(descriptor)


def _write(path: Path, value: dict, *, exclusive: bool = False) -> None:
    raw = _canonical(value)
    _require(len(raw) <= _MAX_JSON, "Operation receipt exceeds its limit")
    _absolute(str(path))
    if exclusive:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(raw)
            destination.flush()
            os.fsync(destination.fileno())
    else:
        descriptor, temporary = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
        try:
            os.fchmod(descriptor, 0o600)  # pylint: disable=no-member  # Linux-only POSIX API.
            with os.fdopen(descriptor, "wb") as destination:
                destination.write(raw)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _run(argv: list[str], *, timeout: float = 30, limit: int = _MAX_JSON) -> subprocess.CompletedProcess:
    """Bound subprocess lifetime and memory; never invoke a local shell."""
    from .executor import _read_pipe  # pylint: disable=import-outside-toplevel

    # Explicit bounded wait and timeout kill avoid an unbounded context-manager wait.
    process = subprocess.Popen(  # nosec B603  # pylint: disable=consider-using-with
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    output = {}
    readers = [
        threading.Thread(target=_read_pipe, args=(process.stdout, limit, output, "stdout", process), daemon=True),
        threading.Thread(target=_read_pipe, args=(process.stderr, 65536, output, "stderr", process), daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.wait()
        raise NativeError("Host command timed out") from error
    finally:
        for reader in readers:
            reader.join(timeout=5)
    _require(
        not output.get("overflow") and not any(reader.is_alive() for reader in readers),
        "Host command output exceeds its bounds",
    )
    return subprocess.CompletedProcess(argv, process.returncode, output.get("stdout", b""), output.get("stderr", b""))


def _docker(*args: str, timeout: float = 30, missing: bool = False) -> Any:
    # Never let an inherited Docker context bind a remote daemon to this host's identity.
    response = _run(["docker", "--host", "unix:///var/run/docker.sock", *args], timeout=timeout)
    if response.returncode:
        if missing and len(args) == 2 and args[0] == "inspect":
            diagnostics = response.stderr.decode("utf-8", errors="replace").strip()
            absent = r"(?:Error:\s*No such object:|Error response from daemon:\s*No such container:)\s*"
            if re.fullmatch(absent + re.escape(args[1]), diagnostics):
                return None
        raise NativeError("Docker command failed: " + args[0])
    return response.stdout


def _identity() -> tuple[str, str]:
    machine = _read(Path("/etc/machine-id"), limit=256).decode("ascii").strip().lower()
    boot = _read(Path("/proc/sys/kernel/random/boot_id"), limit=128).decode("ascii").strip()
    _require(re.fullmatch(r"[0-9a-f]{32}", machine), "Host machine identity is unavailable")
    _require(re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", boot), "Host boot identity is unavailable")
    return machine, boot


def _containers(cluster: str) -> list[dict]:
    # Foreign terminal containers neither reserve devices nor need their large
    # inspect documents read on every reconciliation tick.
    active = _docker("ps", "-q", "--no-trunc").decode("ascii").split()
    ours = (
        _docker("ps", "-aq", "--no-trunc", "--filter", "label=" + _LABELS["cluster"] + "=" + cluster)
        .decode("ascii")
        .split()
    )
    identifiers = sorted(set(active) | set(ours))
    _require(
        len(identifiers) <= _MAX_CONTAINERS and all(_CID.fullmatch(value) for value in identifiers),
        "Docker container inventory is invalid or too large",
    )
    result = []
    for offset in range(0, len(identifiers), 16):
        rows = _json(_docker("inspect", *identifiers[offset : offset + 16]))
        _require(isinstance(rows, list), "Docker inspect is not a list")
        result.extend(rows)
    _require({row.get("Id") for row in result} == set(identifiers), "Docker inventory changed during observation")
    return result


def _summary(container: dict) -> dict:
    labels = container.get("Config", {}).get("Labels") or {}
    state = container.get("State") or {}
    return {
        "id": container["Id"],
        "slot_id": labels.get(_LABELS["slot"]),
        "generation": int(labels.get(_LABELS["generation"], "-1")),
        "role": labels.get(_PREFIX + "role"),
        "status": state.get("Status", "unknown"),
        "running": state.get("Running") is True,
        "started_at": state.get("StartedAt", ""),
        "operation_id": labels.get(_LABELS["operation"], ""),
        "job_name": labels.get(_LABELS["job"], ""),
        "spec_digest": labels.get(_LABELS["digest"], ""),
        "exit_code": state.get("ExitCode") if type(state.get("ExitCode")) is int else None,
        "oom_killed": state.get("OOMKilled") is True,
    }


def _claimed_devices(container: dict, requested: set[int]) -> set[int]:
    if container.get("State", {}).get("Status") not in {"running", "restarting", "paused"}:
        return set()
    host = container.get("HostConfig") or {}
    claimed = set()
    if host.get("Privileged") is True:
        # Privileged containers receive access to all host devices even when
        # HostConfig.Devices is empty. An idle process list is not a reservation.
        claimed.update(requested)
    for device in host.get("Devices") or []:
        match = re.fullmatch(r"/dev/davinci([0-9]+)", str(device.get("PathOnHost", "")))
        if match:
            claimed.add(int(match[1]))
    environment = {}
    for item in container.get("Config", {}).get("Env") or []:
        key, _, value = item.partition("=")
        environment[key] = value
    visible = environment.get("ASCEND_VISIBLE_DEVICES")
    if visible not in (None, "", "void", "none"):
        if re.fullmatch(r"[0-9]+(?:,[0-9]+)*", visible):
            claimed.update(int(value) for value in visible.split(","))
        else:
            claimed.update(requested)
    # Ascend/mind-cluster v6.0.0: component/ascend-docker-runtime/runtime/process/process.go:491-495.
    # Unset/empty/void injects no runtime devices; explicit --device mappings above still reserve them.
    # Unrecognized runtime aliases retain the conservative fallback.
    runtime = str(host.get("Runtime", "")).lower()
    if "ascend" in runtime and runtime != "ascend" and visible in (None, "", "all"):
        claimed.update(requested)
    for request in host.get("DeviceRequests") or []:
        if "ascend" not in json.dumps(request).lower():
            continue
        values = request.get("DeviceIDs") or []
        if values and all(isinstance(value, str) and value.isdecimal() for value in values):
            claimed.update(int(value) for value in values)
        else:
            claimed.update(requested)
    return claimed


def _ports() -> set[int]:
    response = _run(["ss", "-H", "-ltn"])
    _require(response.returncode == 0, "Host TCP listener inventory is unavailable")
    result = set()
    for line in response.stdout.decode("utf-8", errors="strict").splitlines():
        fields = line.split()
        _require(len(fields) >= 4, "Unsupported TCP listener inventory")
        address = fields[3]
        port = address.rsplit(":", 1)[-1]
        _require(port.isdecimal() and 0 < int(port) <= 65535, "Unsupported TCP listener address")
        result.add(int(port))
    return result


def _devices(request: dict) -> list[int]:
    value = request.get("devices", request.get("slot", {}).get("devices", []))
    _require(
        isinstance(value, list)
        and len(value) <= 256
        and all(type(item) is int and 0 <= item < 4096 for item in value)
        and len(set(value)) == len(value),
        "Requested devices must be unique physical integer IDs",
    )
    return sorted(value)


def _snapshot_devices(host: dict, requested: list[int], machine: str, boot: str) -> tuple[list[dict], str | None]:
    unknown = [
        {
            "id": value,
            "healthy": False,
            "processes_known": False,
            "process_count": -1,
            "free_hbm_mib": None,
            "utilization_percent": None,
        }
        for value in requested
    ]
    if not requested:
        return [], None
    if not host.get("npu_observation_file"):
        return unknown, "No trusted physical NPU observation source was configured"
    try:
        path = _absolute(host["npu_observation_file"])
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(descriptor)
            _require(
                stat.S_ISREG(info.st_mode)
                and info.st_size <= _MAX_JSON
                and info.st_uid == 0
                and not info.st_mode & 0o022,
                "NPU observation file is not a bounded, root-controlled regular file",
            )
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                value = _json(source.read(_MAX_JSON + 1))
        finally:
            os.close(descriptor)
        expected = "sha256:" + hashlib.sha256(machine.encode("ascii")).hexdigest()
        _require(
            type(value.get("inventory_schema")) is int
            and value["inventory_schema"] == 2
            and value.get("host_identity") == expected,
            "NPU observation host identity/schema does not match",
        )
        clock = value.get("sample_clock")
        _require(
            isinstance(clock, dict)
            and type(clock.get("schema")) is int
            and clock["schema"] == 1
            and clock.get("boot_id") == boot
            and type(clock.get("uptime_ms")) is int
            and 0 <= clock["uptime_ms"] <= 10**15,
            "NPU observation boot clock is invalid",
        )
        uptime = float(_read(Path("/proc/uptime"), limit=512).decode("ascii").split()[0])
        age = (uptime * 1000 - clock["uptime_ms"] + 20) / 1000
        observed = datetime.fromisoformat(value["collected_at"])
        expires = datetime.fromisoformat(value["expires_at"])
        ttl = (expires - observed).total_seconds()
        _require(
            observed.tzinfo is not None
            and expires.tzinfo is not None
            and math.isfinite(ttl)
            and 0 < ttl <= 600
            and 0 <= age < min(ttl, 90),
            "NPU observation is expired or from the future",
        )
        _require(_identity()[1] == boot, "Host rebooted while reading observations")
        telemetry = value.get("npu_telemetry")
        _require(
            isinstance(telemetry, dict)
            and telemetry.get("source") == "host-npu-smi"
            and telemetry.get("status") in {"ready", "partial"}
            and telemetry.get("collected_at") == value["collected_at"]
            and telemetry.get("expires_at") == value["expires_at"],
            "NPU observation source is unsupported",
        )
        rows = telemetry.get("devices")
        _require(isinstance(rows, list) and len(rows) <= 256, "NPU observations are invalid")
        selected, identities, logical_ids = {}, set(), set()
        for row in rows:
            if not isinstance(row, dict) or row.get("device_index") not in requested:
                continue
            identifier = row["device_index"]
            _require(
                identifier not in selected
                and row.get("physical_device_id") == identifier
                and type(identifier) is int
                and row.get("mapping_status") == "verified"
                and all(
                    type(row.get(key)) is int and row[key] >= 0 for key in ("npu_id", "chip_id", "logical_device_id")
                ),
                "Physical NPU identity is unverified",
            )
            pair = (row["npu_id"], row["chip_id"])
            _require(
                pair not in identities and row["logical_device_id"] not in logical_ids,
                "Physical/logical NPU identity is ambiguous",
            )
            identities.add(pair)
            logical_ids.add(row["logical_device_id"])
            processes = row.get("processes")
            known = (
                row.get("process_pid_scope") == "host"
                and row.get("processes_status") == "ready"
                and isinstance(processes, list)
                and len(processes) <= 4096
                and all(
                    isinstance(item, dict) and type(item.get("pid")) is int and item["pid"] > 0 for item in processes
                )
            )
            total, used = row.get("memory_total_bytes"), row.get("memory_used_bytes")
            memory_known = type(total) is int and type(used) is int and 0 <= used <= total and total > 0
            utilization = row.get("utilization_percent")
            utilization_known = (
                type(utilization) in (int, float) and math.isfinite(utilization) and 0 <= utilization <= 100
            )
            selected[identifier] = {
                "id": identifier,
                "healthy": row.get("health") == "healthy",
                "processes_known": bool(known),
                "process_count": len(processes) if known else -1,
                "free_hbm_mib": (total - used) / (1024 * 1024) if memory_known else None,
                "utilization_percent": utilization if utilization_known else None,
            }
        _require(set(selected) == set(requested), "NPU observations do not cover the requested physical devices")
        return [selected[value] for value in requested], None
    except (NativeError, OSError, ValueError, KeyError, TypeError) as error:
        return unknown, str(error)[:256]


def observe(request: dict) -> dict:
    """Read declarations and an existing trusted snapshot; never query another card."""
    requested = _devices(request)
    machine, boot = _identity()
    daemon = _json(_docker("info", "--format", "{{json .}}"))
    _require(
        isinstance(daemon, dict) and isinstance(daemon.get("ID"), str) and daemon["ID"],
        "Docker daemon identity is unavailable",
    )
    containers = _containers(request["cluster"])
    devices, observation_error = _snapshot_devices(request["host"], requested, machine, boot)
    observation_source = "trusted-snapshot" if observation_error is None and requested else "unavailable"
    if requested and observation_error is not None and request["host"].get("observe_npu") is True:
        from .observations import sample_devices  # pylint: disable=import-outside-toplevel

        hardware = request.get("native_config", {}).get("motor_deploy_config", {}).get("hardware_type", "")
        sampled, sampling_error = sample_devices(request["host"], requested, hardware, run=_run)
        valid = (
            isinstance(sampled, list)
            and len(sampled) == len(requested)
            and all(
                isinstance(row, dict)
                and type(row.get("id")) is int
                and row["id"] in requested
                and type(row.get("healthy")) is bool
                and type(row.get("processes_known")) is bool
                and type(row.get("process_count")) is int
                and row["process_count"] >= -1
                and (
                    row.get("free_hbm_mib") is None
                    or type(row["free_hbm_mib"]) in (int, float)
                    and math.isfinite(row["free_hbm_mib"])
                    and row["free_hbm_mib"] >= 0
                )
                and (
                    row.get("utilization_percent") is None
                    or type(row["utilization_percent"]) in (int, float)
                    and math.isfinite(row["utilization_percent"])
                    and 0 <= row["utilization_percent"] <= 100
                )
                for row in sampled
            )
            and len({row["id"] for row in sampled}) == len(requested)
        )
        if valid:
            devices, observation_error = sampled, sampling_error
            observation_source = "npu-smi-one-shot"
        else:
            observation_error = "One-shot physical NPU observation contract is invalid"
    occupied_devices = set()
    occupied_ports = _ports()
    owned = []
    for container in containers:
        occupied_devices.update(_claimed_devices(container, set(requested)))
        if container.get("State", {}).get("Status") in {"running", "restarting", "paused"}:
            for bindings in (container.get("HostConfig", {}).get("PortBindings") or {}).values():
                for binding in bindings or []:
                    port = str(binding.get("HostPort", ""))
                    if port.isdecimal():
                        occupied_ports.add(int(port))
        labels = container.get("Config", {}).get("Labels") or {}
        if labels.get(_LABELS["cluster"]) == request["cluster"]:
            owned.append(_summary(container))
    return {
        "ok": True,
        "machine_id": machine,
        "boot_id": boot,
        "daemon_id": daemon["ID"],
        "collected_at": time.time(),
        "devices": devices,
        "observation_error": observation_error,
        "observation_source": observation_source,
        "occupied_devices": sorted(occupied_devices & set(requested)),
        "occupied_ports": sorted(occupied_ports),
        "containers": owned,
    }


def _modules() -> tuple[Any, Any]:
    return importlib.import_module("lib.docker_utils"), importlib.import_module("docker_deploy")


def _port(value: Any) -> int:
    _require(type(value) is int and 0 < value <= 65535, "Native service port is invalid")
    return value


def _scheme(config: dict, section: dict, field: str = "mgmt_tls_config") -> str:
    # This is the same precedence as native config_utils._update_tls_config.
    settings = section.get(field) or {}
    shared = config.get("motor_deploy_config", {}).get("tls_config") or {}
    if field in shared:
        settings = shared[field]
    _require(isinstance(settings, dict), "Native TLS configuration is not an object")
    _require(type(settings.get("enable_tls", False)) is bool, "Native TLS enablement must be boolean")
    return "https" if settings.get("enable_tls") is True else "http"


def _name(request: dict) -> str:
    fingerprint = _digest([request["cluster"], request["slot"]["id"], request["generation"]])[:12]
    return "motor-" + request["cluster"][:20] + "-" + request["slot"]["id"][:20] + "-" + fingerprint


def plan_instance(request: dict) -> dict:
    """Derive complete native ports and identities without preparing a workspace."""
    from .config import SpecError, validate_native_profile  # pylint: disable=import-outside-toplevel

    native, _ = _modules()
    config = copy.deepcopy(request.get("native_config"))
    environment = request.get("native_env")
    _require(isinstance(config, dict) and isinstance(environment, dict), "Native configuration objects are required")
    slot, host = request["slot"], request["host"]
    role = slot["role"]
    _require(role in _ROLES, "Unsupported native role")
    try:
        worker_ports = validate_native_profile(
            config,
            environment,
            {role} if role != "coordinator_controller" else set(),
            managed=role == "coordinator_controller" or request.get("control_plane", {}).get("mode") == "managed",
        )
    except SpecError as error:
        raise NativeError(str(error)) from error
    native.validate_instance_nums(config)
    _require(not config.get("motor_engine_encode_config"), "Encode orchestration is not supported by this example")
    _require(not native.kv_store_enabled(config), "KV Store orchestration is not supported by this example")
    render = config.get("motor_coordinator_config", {}).get("render_config", {})
    _require(not render.get("enable", False), "Render orchestration is not supported by this example")
    deploy = config.get("motor_deploy_config") or {}
    native._is_a5(deploy.get("hardware_type"))  # Native hardware validation, including unknown-name rejection.
    _require(not native._is_a5(deploy.get("hardware_type")), "The initial orchestration example supports A2/A3 only")
    native.apply_docker_controller_port_defaults(config)
    params = native.derive_params(config)
    _require(
        isinstance(params.image, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@:+-]*", params.image),
        "A valid immutable or explicitly selected native image is required",
    )
    name = _name(request)
    control = request.get("control_plane") or {}
    pod_ip = str(ipaddress.IPv4Address(host["pod_ip"]))
    coordinator_ip = str(ipaddress.IPv4Address(control["coordinator_ip"]))
    controller_ip = str(ipaddress.IPv4Address(control["controller_ip"]))
    identity = native.resolve_runtime_identity(
        config,
        role=role,
        job_name=name if role != "coordinator_controller" else None,
        pod_ip=pod_ip,
        coordinator_ip=coordinator_ip,
        controller_ip=controller_ip,
        kv_store_ip=None,
        kv_store_enabled=False,
        nic_name=host["nic_name"],
    )
    ports = slot.get("ports") or {}
    override = native.EnginePortOverrides(
        node_manager_port=ports.get("node_manager"),
        base_port=ports.get("base"),
        data_parallel_rpc_port=ports.get("dp_rpc"),
        kv_port=ports.get("kv"),
    )
    native.validate_engine_port_overrides(identity, override)
    coordinator = config.get("motor_coordinator_config") or {}
    coord_api = coordinator.get("api_config") or {}
    coord_mgmt = _port(coord_api.get("coordinator_api_mgmt_port", native.DEFAULT_COORD_MGMT_PORT))
    coord_obs = _port(coord_api.get("coordinator_obs_port", native.DEFAULT_COORD_OBS_PORT))
    controller = config.get("motor_controller_config") or {}
    ctrl_port, ctrl_obs = native.docker_controller_ports(config)
    result = {
        "ok": True,
        "phase": "planned",
        "container_name": name,
        "job_name": name,
        "coordinator_url": f"{_scheme(config, coordinator)}://{coordinator_ip}:{coord_mgmt}",
        "coordinator_metrics_url": f"{_scheme(config, coordinator)}://{coordinator_ip}:{coord_obs}/metrics",
        "controller_url": f"{_scheme(config, controller)}://{controller_ip}:{_port(ctrl_port)}",
        "node_manager_url": "",
        "engine_metrics_targets": [],
        "reserved_ports": [],
    }
    devices = _devices(request)
    if role == "coordinator_controller":
        _require(not devices and not ports, "Management containers must not request NPU devices or engine ports")
        management_ports = [
            _port(coord_api.get("coordinator_api_infer_port", native.DEFAULT_COORD_INFER_PORT)),
            coord_mgmt,
            coord_obs,
            _port(ctrl_port),
            _port(ctrl_obs),
            *worker_ports,
        ]
        _require(len(set(management_ports)) == len(management_ports), "Native management ports overlap")
        result["reserved_ports"] = sorted(management_ports)
    else:
        key = {
            "prefill": "single_p_instance_pod_num",
            "decode": "single_d_instance_pod_num",
            "union": "single_hybrid_instance_pod_num",
        }[role]
        _require(
            type(deploy.get(key)) is int and deploy[key] == 1,
            "Only complete single-host native instances are supported",
        )
        needed = native.validate_role_pod_layout(config, role)
        _require(len(devices) == needed, "Selected devices do not match the native per-instance NPU requirement")
        _require(
            all(value < native.npu_docker_card_count(params.hardware_type) for value in devices),
            "Device ID is outside the native hardware template",
        )
        native.apply_engine_port_overrides(config, role, override)
        section = config["motor_engine_" + role + "_config"]
        endpoints = native.collect_engine_listen_ports(config, role)
        _require(endpoints, "Native engine did not expose a complete port set")
        expected_count = 2 + 2 * native._instance_endpoint_num(config, section, role)
        if native._read_kv_port(section.get("engine_config") or {}) is not None:
            expected_count += needed
        _require(len(endpoints) == expected_count, "Native engine port ranges overlap")
        result["reserved_ports"] = sorted({_port(port) for _label, port in endpoints})
        nm_section = section.get(native.C.MOTOR_NODEMANAGER_CONFIG) or {}
        nm_api = nm_section.get("api_config") or {}
        nm_port = _port(nm_api.get("node_manager_port", 1026))
        result["node_manager_url"] = f"{_scheme(config, nm_section)}://{pod_ip}:{nm_port}"
    return result


def _root(request: dict, *, create: bool) -> Path:
    parent = _absolute(request["host"]["work_dir"])
    root = parent / ".docker-orchestration" / request["cluster"]
    _absolute(str(root))
    if create:
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = root.stat()
        _require(info.st_uid == os.geteuid() and not info.st_mode & 0o077, "Worker state directory is not private")  # pylint: disable=no-member  # Linux-only POSIX API.
        for suffix in ("operations", "instances"):
            child = root / suffix
            child.mkdir(mode=0o700, exist_ok=True)
            _absolute(str(child))
            _require(
                child.stat().st_uid == os.geteuid() and not child.stat().st_mode & 0o077,  # pylint: disable=no-member
                "Worker state subdirectory is not private",
            )
    return root


def _receipt_path(root: Path, operation_id: str) -> Path:
    _require(isinstance(operation_id, str) and _IDENTIFIER.fullmatch(operation_id), "Invalid operation ID")
    return root / "operations" / (operation_id + ".json")


def _owned(request: dict) -> list[dict]:
    result = []
    candidates = _containers(request["cluster"])
    named = _docker("inspect", _name(request), missing=True)
    if named is not None:
        exact = _json(named)
        _require(isinstance(exact, list) and len(exact) == 1, "Named container inspection was ambiguous")
        if exact[0]["Id"] not in {row["Id"] for row in candidates}:
            candidates.append(exact[0])
    for container in candidates:
        labels = container.get("Config", {}).get("Labels") or {}
        if container.get("Name", "").lstrip("/") == _name(request):
            _require(
                all(
                    labels.get(_LABELS[key]) == str(value)
                    for key, value in (
                        ("cluster", request["cluster"]),
                        ("slot", request["slot"]["id"]),
                        ("generation", request["generation"]),
                        ("digest", request["spec_digest"]),
                    )
                ),
                "Native container name belongs to a different operation or configuration",
            )
        if all(
            labels.get(_LABELS[key]) == str(value)
            for key, value in (
                ("cluster", request["cluster"]),
                ("slot", request["slot"]["id"]),
                ("generation", request["generation"]),
            )
        ):
            _require(
                labels.get(_LABELS["digest"]) == request["spec_digest"]
                and labels.get(_PREFIX + "role") == request["slot"]["role"]
                and container.get("Name", "").lstrip("/") == _name(request)
                and labels.get(_LABELS["job"]) == _name(request),
                "Owned container configuration drifted",
            )
            recorded_image = labels.get(_PREFIX + "image")
            _require(
                isinstance(recorded_image, str) and recorded_image == container.get("Image"),
                "Owned container image identity drifted",
            )
            if request["slot"]["role"] == "coordinator_controller":
                host = container.get("HostConfig") or {}
                visible = [
                    value
                    for value in container.get("Config", {}).get("Env") or []
                    if value.startswith("ASCEND_VISIBLE_DEVICES=")
                ]
                _require(
                    host.get("Runtime") == "runc"
                    and not host.get("Devices")
                    and not host.get("DeviceRequests")
                    and not host.get("Privileged")
                    and visible == ["ASCEND_VISIBLE_DEVICES=void"],
                    "Management container is not confirmed free of NPU injection",
                )
            result.append(container)
    _require(len(result) <= 1, "Multiple containers claim the same complete instance")
    expected = request.get("container_ids")
    if expected is not None:
        _require(
            isinstance(expected, list)
            and all(isinstance(value, str) and _CID.fullmatch(value) for value in expected)
            and set(expected) == {row["Id"] for row in result},
            "Exact owned container IDs changed",
        )
    return result


def _check_identity(request: dict, observation: dict) -> None:
    expected_machine = request.get("expected_machine_id")
    expected_daemon = request.get("expected_daemon_id")
    _require(expected_machine is None or observation["machine_id"] == expected_machine, "Host machine identity changed")
    _require(expected_daemon is None or observation["daemon_id"] == expected_daemon, "Docker daemon identity changed")
    expected = request.get("expected_host") or {}
    _require(
        not expected
        or expected.get("machine_id") == observation["machine_id"]
        and expected.get("daemon_id") == observation["daemon_id"],
        "Expected host identity changed",
    )


def _admit(request: dict, plan: dict) -> dict:
    observation = observe(request)
    _check_identity(request, observation)
    _require(not observation["occupied_devices"], "Target NPU is declared by a running container")
    _require(not set(observation["occupied_ports"]) & set(plan["reserved_ports"]), "Native port is occupied")
    for device in observation["devices"]:
        _require(
            device["healthy"] is True
            and device["processes_known"] is True
            and device["process_count"] == 0
            and type(device.get("utilization_percent")) in (int, float)
            and device["utilization_percent"] == 0
            and type(device.get("free_hbm_mib")) in (int, float)
            and math.isfinite(device["free_hbm_mib"])
            and device["free_hbm_mib"] > 0,
            "Target NPU health, physical mapping, processes, utilization or HBM cannot be admitted",
        )
    return observation


def _prepare(request: dict, plan: dict, workspace: Path) -> list[str]:
    native, deployer = _modules()
    repository = _absolute(request["host"]["repo_dir"])
    _require(repository.resolve() == Path(__file__).resolve().parents[4], "Worker repository identity does not match")
    deployer_dir = repository / "examples" / "deployer"
    workspace.mkdir(parents=True, mode=0o700, exist_ok=False)
    user_path, env_path = workspace / "user_config.json", workspace / "env.json"
    _write(user_path, request["native_config"], exclusive=True)
    _write(env_path, request["native_env"], exclusive=True)
    role, host = request["slot"]["role"], request["host"]
    slot_ports = request["slot"].get("ports") or {}
    override = native.EnginePortOverrides(
        node_manager_port=slot_ports.get("node_manager"),
        base_port=slot_ports.get("base"),
        data_parallel_rpc_port=slot_ports.get("dp_rpc"),
        kv_port=slot_ports.get("kv"),
    )
    configmap = workspace / "configmap"
    native.prepare_configmap(
        str(deployer_dir), str(configmap), str(user_path), str(env_path), role=role, engine_ports=override
    )
    effective = _json(_read(configmap / "user_config.json"))
    params = native.derive_params(effective)
    identity = native.resolve_runtime_identity(
        effective,
        role=role,
        job_name=plan["job_name"] if role != "coordinator_controller" else None,
        pod_ip=host["pod_ip"],
        coordinator_ip=request["control_plane"]["coordinator_ip"],
        controller_ip=request["control_plane"]["controller_ip"],
        kv_store_ip=None,
        kv_store_enabled=False,
        nic_name=host["nic_name"],
    )
    start = workspace / "start_motor.sh"
    script = native.render_start_motor_sh(
        params,
        str(configmap),
        identity=identity,
        nic_name=host["nic_name"],
        deployer_dir=str(deployer_dir),
        container_name=plan["container_name"],
    )
    descriptor = os.open(start, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
        destination.write(script)
        destination.flush()
        os.fsync(destination.fileno())
    template = deployer.enter_docker_run_template(role, params.hardware_type)
    template = deployer.apply_enter_devices(
        template, ",".join(str(value) for value in _devices(request)), attach_npu=role != "coordinator_controller"
    )
    template = deployer.insert_create_binds(template, [str(workspace)])
    template = deployer.apply_enter_weight(template, params.weight_mount_path)
    from lib.container_ipc import host_ipc_enabled  # pylint: disable=import-outside-toplevel

    ipc_host = host_ipc_enabled(request["native_env"])
    template = deployer.apply_enter_shm(template, params.dshm_size, ipc_host=ipc_host)
    substitutions = {
        "NAME": plan["container_name"],
        "IMAGE": params.image,
        "EXAMPLES": str(repository / "examples"),
        "WEIGHT": params.weight_mount_path,
    }
    deployer._require_host_binds(template, substitutions)
    template = deployer.drop_duplicate_bind_mounts(template, substitutions)
    arguments = shlex.split(template.replace("\\\n", " "))
    _require(
        arguments[:3] == ["docker", "run", "-it"] and arguments[-2:] == ["$IMAGE", "bash"],
        "Native Docker template format is unsupported",
    )
    arguments = [
        re.sub(r"\$(NAME|IMAGE|EXAMPLES|WEIGHT)\b", lambda match: substitutions[match[1]], value)
        for value in arguments[3:-2]
    ]
    _require(not any("$" in value for value in arguments), "Unresolved native Docker template variable")
    labels = {
        "cluster": request["cluster"],
        "slot": request["slot"]["id"],
        "generation": request["generation"],
        "operation": request["operation_id"],
        "digest": request["spec_digest"],
        "job": plan["job_name"],
    }
    images = _json(_docker("image", "inspect", params.image))
    _require(
        isinstance(images, list)
        and len(images) == 1
        and _DIGEST.fullmatch(str(images[0].get("Id", ""))[7:])
        and images[0]["Id"].startswith("sha256:"),
        "Native image is not available with a content identity",
    )
    image_id = images[0]["Id"]
    _require(
        "--rm" not in arguments and not any(value.startswith("--restart") for value in arguments),
        "Native template cannot erase terminal evidence or own a competing restart policy",
    )
    result = ["run", "-d", "--restart=no"]
    if role == "coordinator_controller":
        result += ["--runtime=runc", "-e", "ASCEND_VISIBLE_DEVICES=void"]
    for key, value in labels.items():
        result += ["--label", _LABELS[key] + "=" + str(value)]
    # Keep the native process-tree supervisor as PID 1. Running start_motor.sh
    # directly would bypass its SIGTERM forwarding, orphan cleanup and exit 143
    # semantics. Its prepared workspace remains outside the source checkout.
    supervisor = (
        "import sys;sys.path.insert(0,sys.argv[1]);"
        "import docker_deploy;raise SystemExit(docker_deploy._start_in_place(sys.argv[2]))"
    )
    _require(
        not any(value == "--pid" or value.startswith("--pid=") for value in arguments),
        "Native supervisor requires a private container PID namespace",
    )
    result += [
        "--label",
        _PREFIX + "role=" + role,
        "--label",
        _PREFIX + "image=" + image_id,
        *arguments,
        image_id,
        "python3",
        "-c",
        supervisor,
        str(deployer_dir),
        str(workspace),
    ]
    return result


def _host_binding() -> dict:
    machine, boot = _identity()
    daemon = _json(_docker("info", "--format", "{{json .}}"))
    _require(
        isinstance(daemon, dict) and isinstance(daemon.get("ID"), str) and daemon["ID"],
        "Docker daemon identity is unavailable",
    )
    return {"machine_id": machine, "daemon_id": daemon["ID"], "boot_id": boot}


def _failed_without_effects(request: dict, binding: dict, error: Exception) -> dict:
    message = (
        str(error)[:512] if isinstance(error, NativeError) else "Native preparation failed: " + type(error).__name__
    )
    return {
        "ok": False,
        "phase": "failed_no_effect",
        "status": "not_created" if request["action"] == "deploy" else "failed",
        "operation_id": request["operation_id"],
        "spec_digest": request["spec_digest"],
        "slot_id": request["slot"]["id"],
        "generation": request["generation"],
        "cluster": request["cluster"],
        "action": request["action"],
        "host_id": request["host"]["id"],
        **binding,
        "container_ids": list(request.get("container_ids", [])),
        "executor_terminal": True,
        "may_have_effects": False,
        "outcome_unknown": False,
        "error": message,
    }


def _previous_operation(receipt: Path, intent: str) -> dict | None:
    if not receipt.exists():
        return None
    try:
        previous = _json(_read(receipt, private=True))
    except Exception as error:
        raise NativeError("Existing operation receipt is not verifiable", outcome_unknown=True) from error
    if previous.get("intent_digest") != intent:
        raise NativeError("Operation ID was reused with different input", outcome_unknown=True)
    if previous.get("phase") in {"completed", "failed_no_effect"} and isinstance(previous.get("result"), dict):
        return previous["result"]
    raise NativeError("Operation was already submitted; reconcile its receipt", outcome_unknown=True)


def _mutate(request: dict, root: Path) -> dict:
    import fcntl  # pylint: disable=import-outside-toplevel

    # All clusters using this executor must share a host-level lock, regardless of work_dir.
    lock_path = Path("/run/lock/mindie-motor-docker-orchestration.lock")
    _absolute(str(lock_path))
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(descriptor)
        _require(
            stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077,  # pylint: disable=no-member
            "Host orchestration lock is not private and owned",
        )
        receipt = _receipt_path(root, request["operation_id"])
        intent = _digest(request)
        binding = _host_binding()
        _check_identity(request, binding)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            previous = _previous_operation(receipt, intent)
            if previous is not None:
                return previous
            result = _failed_without_effects(request, binding, NativeError("Another operation owns this host"))
            try:
                _write(
                    receipt,
                    {
                        "version": 1,
                        "phase": "failed_no_effect",
                        "intent_digest": intent,
                        "result": result,
                        "finished_at": time.time(),
                    },
                    exclusive=True,
                )
            except Exception as error:
                raise NativeError("Host lock refusal receipt could not be persisted", outcome_unknown=True) from error
            return result
        previous = _previous_operation(receipt, intent)
        if previous is not None:
            return previous
        action = request["action"]
        state = {
            "version": 1,
            "phase": "prepared",
            "intent_digest": intent,
            "action": action,
            "operation_id": request["operation_id"],
            "cluster": request["cluster"],
            "slot_id": request["slot"]["id"],
            "generation": request["generation"],
            "spec_digest": request["spec_digest"],
            **binding,
            "started_at": time.time(),
            "may_have_effects": False,
        }
        try:
            _write(receipt, state, exclusive=True)
        except Exception as error:
            raise NativeError("Operation intent could not be durably recorded", outcome_unknown=True) from error
        effect_started = False
        try:
            plan = plan_instance(request)
            owned = _owned(request)
            if action == "deploy":
                _require(not owned, "Owned container already exists; inspect instead of recreating")
            if action in {"restart", "remove"}:
                for container in owned:
                    status = container.get("State") or {}
                    _require(
                        status.get("Status") in _TERMINAL and status.get("Running") is False and status.get("Pid") == 0,
                        "Operation requires a confirmed terminal owned container",
                    )
            if action == "restart":
                _require(len(owned) == 1, "Restart requires one exact owned container")
            observed = _admit(request, plan) if action in {"deploy", "restart"} else observe(request)
            _check_identity(request, observed)
            identifiers = [container["Id"] for container in owned]
            if action == "deploy":
                workspace = (
                    root
                    / "instances"
                    / (request["slot"]["id"] + "-g" + str(request["generation"]) + "-" + request["operation_id"])
                )
                command = _prepare(request, plan, workspace)
                _admit(request, plan)
                effect_started = True
                state.update(phase="inflight", may_have_effects=True)
                _write(receipt, state)
                identifier = _docker(*command, timeout=90).decode("ascii").strip()
                _require(_CID.fullmatch(identifier), "Docker did not confirm an exact container ID")
                identifiers = [identifier]
            elif action == "restart":
                _admit(request, plan)
                effect_started = True
                state.update(phase="inflight", may_have_effects=True)
                _write(receipt, state)
                _docker("start", identifiers[0], timeout=90)
            elif action == "stop":
                effect_started = True
                state.update(phase="inflight", may_have_effects=True)
                _write(receipt, state)
                for identifier in identifiers:
                    _docker("stop", "-t", "30", identifier, timeout=45)
            elif action == "remove":
                effect_started = True
                state.update(phase="inflight", may_have_effects=True)
                _write(receipt, state)
                for identifier in identifiers:
                    _docker("rm", identifier, timeout=45)
            remaining = _owned({key: value for key, value in request.items() if key != "container_ids"})
            if action == "remove":
                _require(not remaining, "Owned container removal was not confirmed")
                status = "removed"
            elif action == "stop":
                _require(
                    all(
                        row.get("State", {}).get("Running") is False
                        and row.get("State", {}).get("Pid") == 0
                        and row.get("State", {}).get("Status") in _TERMINAL
                        for row in remaining
                    ),
                    "Owned container stop was not confirmed",
                )
                status = "stopped"
            else:
                _require(
                    len(remaining) == 1 and remaining[0]["Id"] in identifiers,
                    "Created/restarted container identity was not confirmed",
                )
                status = "starting"
            result = {
                **plan,
                "phase": "submitted" if status == "starting" else status,
                "status": status,
                "container_ids": [row["Id"] for row in remaining],
                "affected_container_ids": identifiers,
                "remaining_container_ids": [row["Id"] for row in remaining],
                "containers": [_summary(row) for row in remaining],
                "may_have_effects": status == "starting",
                "released": status == "removed",
                "machine_id": observed["machine_id"],
                "daemon_id": observed["daemon_id"],
                "operation_id": request["operation_id"],
                "spec_digest": request["spec_digest"],
                "slot_id": request["slot"]["id"],
                "generation": request["generation"],
                "cluster": request["cluster"],
                "action": action,
            }
            if action in {"stop", "remove"}:
                result["container_ids"] = identifiers
            state.update(
                phase="completed", result=result, finished_at=time.time(), may_have_effects=result["may_have_effects"]
            )
            _write(receipt, state)
            return result
        except Exception as error:
            if not effect_started:
                result = _failed_without_effects(request, binding, error)
                state.update(phase="failed_no_effect", result=result, finished_at=time.time(), may_have_effects=False)
                try:
                    _write(receipt, state)
                except Exception as receipt_error:
                    raise NativeError(
                        "Known refusal could not be durably recorded", outcome_unknown=True
                    ) from receipt_error
                return result
            state.update(phase="unknown", error=type(error).__name__, finished_at=time.time())
            try:
                _write(receipt, state)
            except Exception as receipt_error:
                raise NativeError(
                    "Host mutation and its durable receipt require reconciliation", outcome_unknown=True
                ) from receipt_error
            raise NativeError(
                "Host mutation was not fully confirmed; reconcile operation", outcome_unknown=True
            ) from error
    finally:
        os.close(descriptor)


def _validate_request(request: dict) -> None:
    _require(isinstance(request, dict) and len(_canonical(request)) <= _MAX_JSON, "Invalid worker request")
    _require(
        isinstance(request.get("cluster"), str) and _IDENTIFIER.fullmatch(request["cluster"]),
        "Invalid cluster identifier",
    )
    _require(isinstance(request.get("host"), dict), "Host object is required")
    _absolute(request["host"].get("work_dir"))
    _absolute(request["host"].get("repo_dir"))
    action = request.get("action")
    _require(action in _READ_ACTIONS | {"deploy", "stop", "remove", "restart"}, "Unsupported worker action")
    if action in {"deploy", "plan", "inspect", "status", "stop", "remove", "restart", "logs"}:
        slot = request.get("slot")
        _require(
            isinstance(slot, dict)
            and isinstance(slot.get("id"), str)
            and _IDENTIFIER.fullmatch(slot["id"])
            and slot.get("role") in _ROLES,
            "Invalid complete-instance slot",
        )
        _require(type(request.get("generation")) is int and 0 <= request["generation"] <= 10**9, "Invalid generation")
        _require(
            isinstance(request.get("spec_digest"), str) and _DIGEST.fullmatch(request["spec_digest"]),
            "Invalid specification digest",
        )
    if action not in _READ_ACTIONS:
        _require(
            isinstance(request.get("operation_id"), str) and _IDENTIFIER.fullmatch(request["operation_id"]),
            "Mutation requires a unique operation ID",
        )
        _require(
            all(
                isinstance(request.get(key), str) and 0 < len(request[key]) <= 256
                for key in ("expected_machine_id", "expected_daemon_id")
            ),
            "Mutation requires the reviewed host and daemon identities",
        )
    if action in {"stop", "remove", "restart"}:
        identifiers = request.get("container_ids")
        _require(
            isinstance(identifiers, list)
            and len(identifiers) == 1
            and isinstance(identifiers[0], str)
            and _CID.fullmatch(identifiers[0]),
            "Lifecycle operation requires the exact owned container ID",
        )


def handle_worker(request: dict) -> dict:
    """Handle exactly one trusted host request without starting a daemon."""
    try:
        # Native preparation may print user-controlled values. Keep those values off
        # the SSH protocol/diagnostic streams; model logs remain in owned containers.
        with (
            open(os.devnull, "w", encoding="utf-8") as sink,
            contextlib.redirect_stdout(sink),
            contextlib.redirect_stderr(sink),
        ):
            previous_logging = logging.root.manager.disable
            logging.disable(logging.CRITICAL)
            try:
                return _dispatch(request)
            finally:
                logging.disable(previous_logging)
    except (NativeError, OSError, ValueError, KeyError, TypeError, ImportError) as error:
        message = (
            str(error)[:512]
            if isinstance(error, NativeError)
            else "Worker precondition failed: " + type(error).__name__
        )
        return {
            "ok": False,
            "error": message,
            "outcome_unknown": bool(getattr(error, "outcome_unknown", False)),
            "may_have_effects": bool(getattr(error, "outcome_unknown", False)),
        }


def _dispatch(request: dict) -> dict:
    """Run one already bounded worker request."""
    _validate_request(request)
    action = request["action"]
    if action == "observe":
        return observe(request)
    if action == "plan":
        return plan_instance(request)
    root = _root(request, create=action not in _READ_ACTIONS)
    if action == "operation":
        receipt = _receipt_path(root, request.get("operation_id"))
        if not receipt.exists():
            return {
                "ok": True,
                "phase": "not_found",
                "operation_id": request.get("operation_id"),
                "may_have_effects": True,
            }
        stored = _json(_read(receipt, private=True))
        if stored.get("phase") in {"completed", "failed_no_effect"}:
            return stored["result"]
        return {
            "ok": True,
            "phase": stored.get("phase", "unknown"),
            "may_have_effects": True,
            "operation_id": request["operation_id"],
            "outcome_unknown": True,
        }
    if action in {"inspect", "status", "logs"}:
        owned = _owned(request)
        result = {
            "ok": True,
            "containers": [_summary(row) for row in owned],
            "container_ids": [row["Id"] for row in owned],
            "job_name": _name(request),
        }
        if action == "logs":
            tail = request.get("tail", 200)
            _require(type(tail) is int and 1 <= tail <= 1000, "Log tail must be between 1 and 1000")
            result["logs"] = []
            for row in owned:
                response = _run(
                    ["docker", "--host", "unix:///var/run/docker.sock", "logs", "--tail", str(tail), row["Id"]],
                    limit=256000,
                )
                _require(response.returncode == 0, "Owned container log read failed")
                stdout = response.stdout.decode("utf-8", errors="replace")
                stderr = response.stderr.decode("utf-8", errors="replace")
                result["logs"].append(
                    {
                        "id": row["Id"],
                        "stdout": stdout,
                        "stderr": stderr,
                        "text": stdout + ("\n[stderr]\n" + stderr if stderr else ""),
                    }
                )
        return result
    return _mutate(request, root)
