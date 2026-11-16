# Copyright (c) 2026 ZhengDeL. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Load bounded orchestration input without changing the native Motor schema."""

# Exact JSON scalar types deliberately reject bool where integers are required.
# pylint: disable=unidiomatic-typecheck

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlsplit


class SpecError(ValueError):
    """An orchestration input cannot be executed safely."""


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_TARGET = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@:-]{0,254}\Z")
_ROLES = frozenset({"prefill", "decode", "union"})
_POLICY = {
    "interval_seconds": 10,
    "ready_timeout_seconds": 900,
    "drain_timeout_seconds": 300,
    "drain_grace_seconds": 5,
    "retry_limit": 3,
    "retry_backoff_seconds": 30,
    "observation_max_age_seconds": 30,
}


def digest(value: object) -> str:
    """Hash the complete JSON value; never coerce native integers through floats."""
    try:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, OverflowError, RecursionError) as error:
        raise SpecError("Input must contain finite JSON values") from error
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _object(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise SpecError(name + " must be an object")
    return value


def _keys(value: dict, allowed: set, name: str) -> None:
    if set(value) - allowed:
        raise SpecError(name + " contains unsupported orchestration fields")


def _text(value: object, name: str, maximum: int = 2048) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise SpecError(name + " must be bounded text without control characters")
    return value


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise SpecError(name + " must be a simple identifier")
    return value


def _integer(value: object, name: str, lower: int, upper: int) -> int:
    if type(value) is not int or not lower <= value <= upper:
        raise SpecError(name + " is outside its supported integer range")
    return value


def _number(value: object, name: str, lower: float, upper: float) -> float:
    if type(value) not in {int, float} or not lower <= value <= upper or not math.isfinite(value):
        raise SpecError(name + " is outside its supported finite range")
    return value


def _remote_path(value: object, name: str) -> str:
    text = _text(value, name)
    path = PurePosixPath(text)
    if not path.is_absolute() or ".." in path.parts or text == "/":
        raise SpecError(name + " must be an absolute non-root remote path without parent traversal")
    return text


def _ipv4(value: object, name: str) -> str:
    try:
        address = ipaddress.IPv4Address(value)
    except (ValueError, TypeError) as error:
        raise SpecError(name + " must be an IPv4 address") from error
    if address.is_unspecified or address.is_multicast or address.is_link_local:
        raise SpecError(name + " must identify a reachable host")
    return str(address)


def _read_object(path: Path) -> dict:
    def pairs(values: list) -> dict:
        result = {}
        for key, value in values:
            if key in result:
                raise SpecError("JSON contains a duplicate field")
            result[key] = value
        return result

    def constant(_: str) -> None:
        raise SpecError("JSON contains a non-finite constant")

    try:
        with path.open("rb") as source:
            raw = source.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise SpecError("Configuration exceeds its byte limit")
        return _object(json.loads(raw, object_pairs_hook=pairs, parse_constant=constant), "Configuration")
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        raise SpecError("Configuration cannot be read as a bounded JSON object") from error


def _hosts(raw: object) -> dict:
    if not isinstance(raw, list) or not 1 <= len(raw) <= 128:
        raise SpecError("hosts must contain between one and 128 approved hosts")
    result = {}
    for item in raw:
        host = dict(_object(item, "host"))
        _keys(
            host,
            {
                "id",
                "ssh_target",
                "pod_ip",
                "nic_name",
                "repo_dir",
                "work_dir",
                "npu_observation_file",
                "observe_npu",
                "python_executable",
            },
            "host",
        )
        identifier = _identifier(host.get("id"), "host.id")
        if identifier in result:
            raise SpecError("host.id is duplicated")
        target = host.get("ssh_target")
        if not isinstance(target, str) or not _TARGET.fullmatch(target):
            raise SpecError("host.ssh_target must be a trusted OpenSSH target without options or whitespace")
        host["pod_ip"] = _ipv4(host.get("pod_ip"), "host.pod_ip")
        if not isinstance(host.get("nic_name"), str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", host["nic_name"]):
            raise SpecError("host.nic_name must identify a Linux network interface")
        for key in ("repo_dir", "work_dir"):
            host[key] = _remote_path(host.get(key), "host." + key)
        if "npu_observation_file" in host:
            host["npu_observation_file"] = _remote_path(host["npu_observation_file"], "host.npu_observation_file")
        if type(host.get("observe_npu", False)) is not bool:
            raise SpecError("host.observe_npu must be an explicit boolean")
        host["observe_npu"] = host.get("observe_npu", False)
        executable = host.get("python_executable", "python3")
        host["python_executable"] = (
            executable if executable == "python3" else _remote_path(executable, "host.python_executable")
        )
        result[identifier] = host
    return result


def _slots(raw: object, hosts: dict) -> list:
    if not isinstance(raw, list) or not 1 <= len(raw) <= 256:
        raise SpecError("slots must contain approved complete instance resource groups")
    result, identifiers = [], set()
    for item in raw:
        slot = dict(_object(item, "slot"))
        _keys(slot, {"id", "role", "host_id", "devices", "ports"}, "slot")
        identifier = _identifier(slot.get("id"), "slot.id")
        if identifier == "management":
            raise SpecError("The management slot name is reserved for the managed control plane")
        if identifier in identifiers:
            raise SpecError("slot.id is duplicated")
        identifiers.add(identifier)
        host_id = _identifier(slot.get("host_id"), "slot.host_id")
        role = slot.get("role")
        if host_id not in hosts or not isinstance(role, str) or role not in _ROLES:
            raise SpecError("slot must reference an approved host and supported role")
        devices = slot.get("devices")
        if not isinstance(devices, list) or not 1 <= len(devices) <= 256:
            raise SpecError("slot.devices must be a nonempty explicit device list")
        for number in devices:
            _integer(number, "slot.devices", 0, 65535)
        if len(set(devices)) != len(devices):
            raise SpecError("slot.devices contains duplicates")
        slot["devices"] = sorted(devices)
        ports = _object(slot.get("ports"), "slot.ports")
        _keys(ports, {"node_manager", "base", "kv", "dp_rpc"}, "slot.ports")
        if not {"node_manager", "base", "dp_rpc"} <= set(ports):
            raise SpecError("slot.ports must explicitly define node_manager, base and dp_rpc")
        for number in ports.values():
            _integer(number, "slot.ports", 1, 65535)
        if len(set(ports.values())) != len(ports):
            raise SpecError("slot.ports contains a port collision")
        result.append(slot)
    roles = {slot["role"] for slot in result}
    if "union" in roles and len(roles) != 1:
        raise SpecError("Union slots cannot be mixed with Prefill/Decode slots")
    return result


def _control(raw: object, hosts: dict) -> dict:
    value = dict(_object(raw, "control_plane"))
    _keys(value, {"mode", "host_id", "controller_ip", "coordinator_ip", "coordinator_url"}, "control_plane")
    if not isinstance(value.get("mode"), str) or value["mode"] not in {"managed", "external"}:
        raise SpecError("control_plane.mode must be managed or external")
    if value.get("mode") == "managed" and value.get("host_id") not in hosts:
        raise SpecError("Managed control plane must reference an approved host")
    for key in ("controller_ip", "coordinator_ip"):
        value[key] = _ipv4(value.get(key), "control_plane." + key)
    try:
        parsed = urlsplit(_text(value.get("coordinator_url"), "control_plane.coordinator_url"))
        _ = parsed.port
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and parsed.path in {"", "/"}
        )
    except ValueError as error:
        raise SpecError("Coordinator management origin is invalid") from error
    if not valid:
        raise SpecError("coordinator_url must be a management HTTP(S) origin without credentials or API paths")
    value["coordinator_url"] = value["coordinator_url"].rstrip("/")
    return value


def _desired(raw: object, roles: set) -> dict:
    values = _object(raw, "desired")
    if not values or set(values) - roles:
        raise SpecError("desired must name only the configured instance roles")
    return {role: _integer(values.get(role, 0), "desired." + role, 0, 128) for role in sorted(roles)}


def _native_check(config: dict, roles: set) -> None:
    deployment = _object(config.get("motor_deploy_config"), "motor_deploy_config")
    for role in roles:
        prefix = {"prefill": "p", "decode": "d", "union": "hybrid"}[role]
        members = deployment.get("single_" + prefix + "_instance_pod_num", 1)
        if type(members) is not int or members != 1:
            raise SpecError("This orchestrator supports only complete single-host instances")
        count = deployment.get(prefix + "_pod_npu_num")
        _integer(count, prefix + "_pod_npu_num", 1, 256)


def validate_native_profile(config: dict, environment: dict, roles: set[str], *, managed: bool) -> list[int]:
    """Require explicit fixed listeners without replacing native defaults or fields.

    A managed profile must declare its worker metaserver range. Returning this
    range avoids importing Motor runtime modules in the short-lived host worker.
    """
    coordinator = _object(config.get("motor_coordinator_config", {}), "motor_coordinator_config")
    scheduler = _object(coordinator.get("scheduler_config", {}), "motor_coordinator_config.scheduler_config")
    conductor_sections = (
        config.get("kv_conductor_config", {}),
        scheduler.get("kv_conductor_config", {}),
        coordinator.get("prefill_kv_event_config", {}),
    )
    if any(
        _object(section, "KV Conductor configuration").get("conductor_service") not in (None, "")
        for section in conductor_sections
    ):
        raise SpecError("KV Conductor orchestration is not supported by this example")
    for key in ("motor_common_env", "motor_coordinator_env"):
        if _object(environment.get(key, {}), key).get("KV_CONDUCTOR_SERVICE") not in (None, ""):
            raise SpecError("KV_CONDUCTOR_SERVICE is not supported by this orchestration profile")
    sections = []
    if managed:
        sections.extend(
            (name, _object(config.get(name), name)) for name in ("motor_coordinator_config", "motor_controller_config")
        )
    for role in sorted(roles):
        name = "motor_engine_" + role + "_config"
        engine = _object(config.get(name), name)
        name += ".motor_nodemanger_config"
        sections.append((name, _object(engine.get("motor_nodemanger_config"), name)))
    for name, section in sections:
        allocator = _object(section.get("port_allocator_config", {}), name + ".port_allocator_config")
        if allocator.get("enable") is not False:
            raise SpecError(name + ".port_allocator_config.enable must explicitly be false for fixed reservations")
    if not managed:
        return []
    name = "motor_coordinator_config.inference_workers_config"
    workers = _object(coordinator.get("inference_workers_config"), name)
    # Five API listeners plus the worker span must fit the ledger's 1024-port claim bound.
    count = _integer(workers.get("num_workers"), name + ".num_workers", 1, 1019)
    base = _integer(workers.get("worker_metaserver_base_port"), name + ".worker_metaserver_base_port", 0, 65535)
    if base == 0:
        return []
    if base + count - 1 > 65535:
        raise SpecError(name + ": worker_metaserver_base_port + num_workers - 1 exceeds 65535")
    return list(range(base, base + count))


def load_spec(path: str | Path) -> dict:
    """Load one approved cluster and retain arbitrary native configuration fields."""
    source = Path(path).expanduser().resolve()
    spec = dict(_read_object(source))
    _keys(
        spec,
        {
            "version",
            "name",
            "config_dir",
            "hosts",
            "slots",
            "control_plane",
            "desired",
            "policy",
            "autoscaling",
            "tls",
            "management_headers",
        },
        "orchestration",
    )
    if type(spec.get("version")) is not int or spec["version"] != 1:
        raise SpecError("Unsupported orchestration configuration version")
    _identifier(spec.get("name"), "name")
    directory = Path(_text(spec.get("config_dir"), "config_dir")).expanduser()
    if not directory.is_absolute():
        directory = source.parent / directory
    spec["config_dir"] = str(directory.resolve())
    spec["hosts"] = _hosts(spec.get("hosts"))
    spec["slots"] = _slots(spec.get("slots"), spec["hosts"])
    for host_id in {slot["host_id"] for slot in spec["slots"]}:
        host = spec["hosts"][host_id]
        if not host.get("npu_observation_file") and not host["observe_npu"]:
            raise SpecError("Engine hosts require a trusted NPU snapshot or explicit observe_npu permission")
    roles = {slot["role"] for slot in spec["slots"]}
    spec["desired"] = _desired(spec.get("desired"), roles)
    if any(count > sum(slot["role"] == role for slot in spec["slots"]) for role, count in spec["desired"].items()):
        raise SpecError("Desired count exceeds the approved candidate slots")
    spec["control_plane"] = _control(spec.get("control_plane"), spec["hosts"])
    policy = dict(_object(spec.get("policy", {}), "policy"))
    _keys(policy, set(_POLICY), "policy")
    for key, default in _POLICY.items():
        policy[key] = _number(
            policy.get(key, default),
            "policy." + key,
            0 if key in {"retry_limit", "drain_grace_seconds"} else 0.1,
            86400,
        )
    _integer(policy["retry_limit"], "policy.retry_limit", 0, 100)
    spec["policy"] = policy
    autoscaling = dict(_object(spec.get("autoscaling", {"enabled": False}), "autoscaling"))
    _keys(autoscaling, {"enabled", "cooldown_seconds", "stable_samples", "roles"}, "autoscaling")
    if type(autoscaling.get("enabled", False)) is not bool:
        raise SpecError("autoscaling.enabled must be a boolean")
    autoscaling["enabled"] = autoscaling.get("enabled", False)
    autoscaling["cooldown_seconds"] = _number(autoscaling.get("cooldown_seconds", 120), "cooldown_seconds", 1, 86400)
    autoscaling["stable_samples"] = _integer(autoscaling.get("stable_samples", 3), "stable_samples", 1, 1000)
    scaling_roles = _object(autoscaling.get("roles", {}), "autoscaling.roles")
    if set(scaling_roles) - roles or autoscaling["enabled"] and not set(roles) <= set(scaling_roles):
        raise SpecError("Autoscaling must refer to configured roles with explicit limits")
    for role, raw in scaling_roles.items():
        rule = _object(raw, "autoscaling role")
        _keys(rule, {"min", "max", "up_running", "down_running"}, "autoscaling role")
        lower = _integer(rule.get("min"), "autoscaling.min", 1 if autoscaling["enabled"] else 0, 128)
        upper = _integer(rule.get("max"), "autoscaling.max", lower, 128)
        down = _number(rule.get("down_running", 0), "down_running", 0, 1000000)
        up = _number(rule.get("up_running"), "up_running", 0, 1000000)
        if down >= up or autoscaling["enabled"] and not lower <= spec["desired"][role] <= upper:
            raise SpecError("Autoscaling thresholds or desired count are inconsistent")
    autoscaling["roles"] = scaling_roles
    spec["autoscaling"] = autoscaling
    tls = dict(_object(spec.get("tls", {}), "tls"))
    _keys(tls, {"ca_file", "cert_file", "key_file"}, "tls")
    if bool(tls.get("cert_file")) != bool(tls.get("key_file")):
        raise SpecError("Management client certificate and key must be configured together")
    for key, value in tls.items():
        local = Path(_text(value, "tls." + key)).expanduser()
        tls[key] = str((source.parent / local).resolve() if not local.is_absolute() else local.resolve())
    spec["tls"] = tls
    headers = _object(spec.get("management_headers", {}), "management_headers")
    for key, value in headers.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}", key):
            raise SpecError("Management header name is invalid")
        _text(value, "management header value", 8192)
    spec["management_headers"] = headers
    spec["_native_config"] = _read_object(directory.resolve() / "user_config.json")
    spec["_native_env"] = _read_object(directory.resolve() / "env.json")
    _native_check(spec["_native_config"], roles)
    validate_native_profile(
        spec["_native_config"], spec["_native_env"], roles, managed=spec["control_plane"]["mode"] == "managed"
    )
    spec["digest"] = digest({key: value for key, value in spec.items() if key != "config_dir"})
    return spec
