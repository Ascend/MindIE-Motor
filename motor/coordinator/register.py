# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Standalone instance registry for Motor Coordinator.

    python3 -m motor.coordinator.register --prefill 10.10.0.11:8000 --decode 10.10.0.12:8000
    python3 -m motor.coordinator.register add --prefill 10.10.0.13:8000
    python3 -m motor.coordinator.register del --prefill 10.10.0.13:8000
    python3 -m motor.coordinator.register del --id 1234
    python3 -m motor.coordinator.register list

Default action is ``set`` (replace the whole instance list). ``add`` / ``del``
are incremental. ``list`` queries currently registered instances.
Instance ids are derived from role + the complete endpoint group so the same
group can be added and deleted without remembering numeric ids.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path

DEFAULT_BASE_URL = "http://127.0.0.1"
DEFAULT_MGMT_PORT = 1026
MGMT_API_KEY_HEADER = "X-Motor-Management-Key"
HEALTH_TIMEOUT_SEC = 5
STANDALONE_ID_NAMESPACE = 0x40000000
STANDALONE_ID_MASK = 0x3FFFFFFF
ACTIONS = ("set", "add", "del", "list")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Register native vLLM P/D instances to a standalone Motor Coordinator.",
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="set",
        choices=ACTIONS,
        help="set=replace all (default), add=append, del=remove, list=query.",
    )
    parser.add_argument(
        "--prefill",
        action="append",
        default=[],
        metavar="IP:PORT[,IP:PORT...]",
        help="One prefill instance per flag; comma-join multiple endpoints of the same instance. Repeatable.",
    )
    parser.add_argument(
        "--decode",
        action="append",
        default=[],
        metavar="IP:PORT[,IP:PORT...]",
        help="One decode instance per flag; same grouping rule as --prefill.",
    )
    parser.add_argument(
        "--id",
        action="append",
        type=int,
        default=[],
        dest="ids",
        metavar="ID",
        help="Instance id to delete (del only; repeatable).",
    )
    parser.add_argument(
        "--coordinator",
        default=DEFAULT_BASE_URL,
        help=f"Coordinator base URL without port (default: {DEFAULT_BASE_URL}).",
    )
    parser.add_argument(
        "--mgmt-port",
        type=int,
        default=DEFAULT_MGMT_PORT,
        help=f"Coordinator management port (default: {DEFAULT_MGMT_PORT}).",
    )
    parser.add_argument(
        "--mgmt-api-key-file",
        default="",
        help="File containing the management API key; required when management authentication is enabled.",
    )
    parser.add_argument(
        "--model-name",
        default=None,
        help="Registered model name. Default: queried from the first reachable "
        "engine's /v1/models (requires exactly one served model).",
    )
    parser.add_argument("--engine-type", default="vllm", help="Engine family registered on instances (default: vllm).")
    parser.add_argument(
        "--timeout", type=int, default=60, help="Seconds to wait for Coordinator liveness (default: 60)."
    )
    parser.add_argument(
        "--no-health-check", action="store_true", help="Skip probing each endpoint's /v1/models before add/set."
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the instances JSON without POSTing.")
    args = parser.parse_args(argv)
    has_groups = bool(args.prefill or args.decode)
    if args.action == "list":
        if has_groups or args.ids:
            parser.error("list does not take --prefill / --decode / --id")
        return args
    if args.action == "del":
        if not has_groups and not args.ids:
            parser.error("del requires --prefill/--decode and/or --id")
    elif not has_groups:
        parser.error("at least one --prefill or --decode endpoint is required")
    elif args.ids:
        parser.error("--id is only valid with del")
    return args


def http_json(
    method: str,
    url: str,
    body: dict | None = None,
    timeout: int = 10,
    headers: dict[str, str] | None = None,
) -> tuple[int, str]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8")


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def mgmt_json(
    method: str,
    url: str,
    body: dict | None = None,
    timeout: int = 10,
    headers: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Call Coordinator management HTTP and exit with a readable error on failure."""
    try:
        if headers:
            return http_json(method, url, body, timeout, headers)
        return http_json(method, url, body, timeout)
    except urllib.error.HTTPError as exc:
        detail = _http_error_detail(exc)
        suffix = f": {detail}" if detail else ""
        sys.exit(f"ERROR: {method} {url} -> HTTP {exc.code}{suffix}")
    except (urllib.error.URLError, OSError) as exc:
        sys.exit(f"ERROR: {method} {url} failed: {exc}")


def parse_endpoint(raw: str) -> tuple[str, str]:
    raw = raw.strip()
    ip, sep, port = raw.rpartition(":")
    if not sep or not ip or not port.isdigit():
        sys.exit(f"ERROR: invalid endpoint '{raw}', expected IP:PORT")
    return ip, port


def parse_endpoint_group(group: str) -> list[tuple[str, str]]:
    """Parse an endpoint group into a deterministic order independent of CLI input order."""
    endpoints = [parse_endpoint(raw) for raw in group.split(",") if raw.strip()]
    if not endpoints:
        sys.exit(f"ERROR: empty endpoint group '{group}'")
    return sorted(endpoints, key=lambda endpoint: (endpoint[0], int(endpoint[1])))


def first_endpoint(group: str) -> tuple[str, str]:
    return parse_endpoint_group(group)[0]


def derive_instance_id(role: str, group: str) -> int:
    endpoints = parse_endpoint_group(group)
    identity = "|".join([role, *(f"{ip}:{port}" for ip, port in endpoints)])
    digest = zlib.crc32(identity.encode()) & STANDALONE_ID_MASK
    return STANDALONE_ID_NAMESPACE | digest


def iter_engine_endpoints(args: argparse.Namespace) -> list[tuple[str, str]]:
    endpoints: list[tuple[str, str]] = []
    for groups in (args.prefill, args.decode):
        for group in groups:
            endpoints.extend(parse_endpoint_group(group))
    return endpoints


def fetch_engine_model_ids(ip: str, port: str) -> tuple[str, list[str]]:
    url = f"http://{ip}:{port}/v1/models"
    _, body = http_json("GET", url, timeout=HEALTH_TIMEOUT_SEC)
    payload = json.loads(body)
    model_ids = [m.get("id") for m in payload.get("data", []) if m.get("id")]
    return url, model_ids


def resolve_model_name(args: argparse.Namespace) -> str:
    if args.model_name:
        return args.model_name
    last_error = None
    for ip, port in iter_engine_endpoints(args):
        try:
            url, model_ids = fetch_engine_model_ids(ip, port)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last_error = f"{ip}:{port} ({exc})"
            continue
        if len(model_ids) == 1:
            return model_ids[0]
        if not model_ids:
            sys.exit(f"ERROR: {url} returned no models; pass --model-name explicitly")
        sys.exit(f"ERROR: {url} serves multiple models {model_ids}; pass --model-name to choose one")
    hint = f"; last error: {last_error}" if last_error else ""
    sys.exit(f"ERROR: no reachable engine /v1/models{hint}; pass --model-name explicitly")


def probe_endpoint(ip: str, port: str, model_name: str) -> bool:
    try:
        url, model_ids = fetch_engine_model_ids(ip, port)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"WARN: http://{ip}:{port}/v1/models unreachable ({exc})", file=sys.stderr)
        return False
    if not model_ids:
        print(f"WARN: {url} returned no models", file=sys.stderr)
        return False
    if model_name not in model_ids:
        print(f"WARN: {url} serves {sorted(model_ids)}, expected '{model_name}'", file=sys.stderr)
        return False
    return True


def build_endpoints(group: str, model_name: str | None, probe: bool) -> dict[str, dict[int, dict]]:
    by_ip: dict[str, dict[int, dict]] = {}
    for ep_id, (ip, port) in enumerate(parse_endpoint_group(group)):
        if probe and model_name is not None and not probe_endpoint(ip, port, model_name):
            sys.exit(
                f"ERROR: probe failed for {ip}:{port} in group '{group}'; "
                "refusing incomplete instance. Pass --no-health-check to skip probes."
            )
        by_ip.setdefault(ip, {})[ep_id] = {
            "id": ep_id,
            "ip": ip,
            "business_port": port,
            "status": "normal",
            "headless": False,
        }
    return by_ip


def build_instances(args: argparse.Namespace, model_name: str | None) -> list[dict]:
    instances: list[dict] = []
    probe = args.action != "del" and not args.no_health_check
    for role, groups in (("prefill", args.prefill), ("decode", args.decode)):
        for group in groups:
            ip, port = first_endpoint(group)
            by_ip = build_endpoints(group, model_name, probe)
            if not by_ip:
                print(
                    f"WARN: no endpoint in group '{group}' ({role}); instance skipped",
                    file=sys.stderr,
                )
                continue
            name = model_name or "unknown"
            instances.append(
                {
                    "job_name": f"{name}-{role}-{ip}-{port}",
                    "model_name": name,
                    "engine_type": args.engine_type,
                    "id": derive_instance_id(role, group),
                    "role": role,
                    "status": "active",
                    "enable_multi_endpoints": True,
                    "endpoints": by_ip,
                }
            )
    for instance_id in args.ids:
        # These schema placeholders make --dry-run self-contained. Before a live
        # deletion, _prepare_instances_for_refresh replaces them with the identity
        # returned by GET /instances.
        instances.append(
            {
                "job_name": f"id-{instance_id}",
                "model_name": "unknown",
                "id": instance_id,
                "role": "prefill",
                "status": "active",
            }
        )
    instance_ids = [instance["id"] for instance in instances]
    if len(instance_ids) != len(set(instance_ids)):
        duplicates = sorted({instance_id for instance_id in instance_ids if instance_ids.count(instance_id) > 1})
        sys.exit(f"ERROR: duplicate instance ID(s) derived in one request: {duplicates}")
    if not instances:
        sys.exit("ERROR: no instance left to submit; aborting")
    return instances


def wait_liveness(mgmt_url: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            http_json("GET", f"{mgmt_url}/liveness", timeout=5)
            return
        except (urllib.error.URLError, OSError):
            time.sleep(1)
    sys.exit(f"ERROR: Coordinator {mgmt_url} not live after {timeout}s")


def _load_mgmt_headers(api_key_file: str) -> dict[str, str]:
    if not api_key_file:
        return {}
    try:
        api_key = Path(api_key_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        sys.exit(f"ERROR: failed to read management API key file '{api_key_file}': {exc}")
    if not api_key or "\n" in api_key or "\r" in api_key:
        sys.exit("ERROR: management API key file must contain exactly one non-empty line")
    return {MGMT_API_KEY_HEADER: api_key}


def _print_instance_list(mgmt_url: str, headers: dict[str, str]) -> None:
    _, body = mgmt_json("GET", f"{mgmt_url}/instances", headers=headers)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        print(body)
        return
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _fetch_registered_instances(mgmt_url: str, headers: dict[str, str]) -> list[dict]:
    _, body = mgmt_json("GET", f"{mgmt_url}/instances", headers=headers)
    try:
        payload = json.loads(body)
        instances = payload.get("instances")
    except (json.JSONDecodeError, AttributeError) as exc:
        sys.exit(f"ERROR: invalid GET /instances response: {exc}")
    if not isinstance(instances, list) or not all(isinstance(instance, dict) for instance in instances):
        sys.exit("ERROR: invalid GET /instances response: 'instances' must be a list")
    return instances


def _endpoint_signature(instance: dict) -> tuple[tuple[str, str, bool], ...]:
    """Return the order-independent network identity of an endpoint group."""
    endpoints = instance.get("endpoints") or {}
    if isinstance(endpoints, list):
        raw_endpoints = endpoints
    elif isinstance(endpoints, dict):
        raw_endpoints = [
            endpoint
            for pod_endpoints in endpoints.values()
            if isinstance(pod_endpoints, dict)
            for endpoint in pod_endpoints.values()
            if isinstance(endpoint, dict)
        ]
    else:
        return ()
    return tuple(
        sorted(
            (
                str(endpoint.get("ip", "")),
                str(endpoint.get("business_port", "")),
                bool(endpoint.get("headless", False)),
            )
            for endpoint in raw_endpoints
        )
    )


def _prepare_instances_for_refresh(action: str, instances: list[dict], registered: list[dict]) -> None:
    """Detect ID collisions and resolve endpoint-based deletion to the registered ID."""
    registered_by_id: dict[int, dict] = {}
    for existing in registered:
        instance_id = existing.get("id")
        if not isinstance(instance_id, int):
            continue
        if instance_id in registered_by_id:
            sys.exit(f"ERROR: Coordinator returned duplicate registered instance ID {instance_id}")
        registered_by_id[instance_id] = existing

    if action == "del":
        for instance in instances:
            if not instance.get("endpoints"):
                instance_id = instance["id"]
                existing = registered_by_id.get(instance_id)
                if existing is None:
                    sys.exit(f"ERROR: no registered instance with ID {instance_id}")
                for field in ("role", "job_name", "model_name", "engine_type"):
                    if existing.get(field) is not None:
                        instance[field] = existing[field]
                continue
            identity = (instance.get("role"), _endpoint_signature(instance))
            matches = [
                existing for existing in registered if (existing.get("role"), _endpoint_signature(existing)) == identity
            ]
            if not matches:
                sys.exit(
                    f"ERROR: no registered instance matches role={instance.get('role')} "
                    f"endpoints={_endpoint_signature(instance)}"
                )
            if len(matches) > 1:
                sys.exit("ERROR: multiple registered instances match the requested endpoint group")
            existing = matches[0]
            instance["id"] = existing["id"]
            instance["job_name"] = existing.get("job_name", instance["job_name"])
            instance["model_name"] = existing.get("model_name", instance["model_name"])
        return

    for instance in instances:
        existing = registered_by_id.get(instance["id"])
        if existing is None:
            continue
        same_endpoint_identity = existing.get("role") == instance.get("role") and _endpoint_signature(
            existing
        ) == _endpoint_signature(instance)
        same_add_identity = same_endpoint_identity and existing.get("job_name") == instance.get("job_name")
        if not same_endpoint_identity or (action == "add" and not same_add_identity):
            sys.exit(f"ERROR: instance ID {instance['id']} is already registered to a different instance")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    mgmt_url = f"{args.coordinator.rstrip('/')}:{args.mgmt_port}"
    if args.action == "list":
        mgmt_headers = _load_mgmt_headers(args.mgmt_api_key_file)
        wait_liveness(mgmt_url, args.timeout)
        _print_instance_list(mgmt_url, mgmt_headers)
        return

    model_name = None if args.action == "del" else resolve_model_name(args)
    instances = build_instances(args, model_name)
    payload = {"event": args.action, "instances": instances}

    if args.dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    mgmt_headers = _load_mgmt_headers(args.mgmt_api_key_file)
    wait_liveness(mgmt_url, args.timeout)
    registered = _fetch_registered_instances(mgmt_url, mgmt_headers)
    _prepare_instances_for_refresh(args.action, instances, registered)
    payload = {"event": args.action, "instances": instances}
    mgmt_json("POST", f"{mgmt_url}/instances/refresh", payload, headers=mgmt_headers)
    names = ", ".join(f"{i['job_name']}({i['role']}/{i['id']})" for i in instances)
    print(f"{args.action} {len(instances)} instance(s): {names}")
    _, readiness = mgmt_json("GET", f"{mgmt_url}/readiness")
    print(f"readiness: {readiness}")


if __name__ == "__main__":
    main()
