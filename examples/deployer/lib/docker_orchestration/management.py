# Copyright (c) 2026 ZhengDeL. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Bounded native management requests; no changes to Motor's routing protocol."""

import json
import math
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class ManagementError(RuntimeError):
    """A native observation or lifecycle precondition could not be established."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        raise ManagementError("Management redirects are not accepted")


def request_counts(text: str) -> dict[str, float] | None:
    """Require complete, finite queue and running gauges; missing never means zero."""
    names = {
        "vllm:num_requests_waiting": "waiting",
        "vllm:num_requests_running": "running",
        "sglang:num_waiting_reqs": "waiting",
        "sglang:num_running_reqs": "running",
    }
    families: dict[str, dict[str, float]] = {}
    for line in text.splitlines():
        prefix = re.split(r"[\s{]", line.strip(), maxsplit=1)[0]
        match = re.fullmatch(r"([^\s{]+)(?:\{.*\})?\s+([^\s]+)(?:\s+[0-9]+)?", line.strip())
        if prefix in names and not match:
            return None
        if not match or match[1] not in names:
            continue
        try:
            value = float(match[2])
        except ValueError:
            return None
        if not math.isfinite(value) or value < 0:
            return None
        totals = families.setdefault(match[1].split(":", 1)[0], {})
        key = names[match[1]]
        totals[key] = totals.get(key, 0) + value
    if len(families) != 1:
        return None
    totals = next(iter(families.values()))
    return totals if set(totals) == {"waiting", "running"} else None


class NativeManagement:
    """Use native instance views and pause/resume with verified HTTP/TLS transport."""

    def __init__(self, spec: dict) -> None:
        self.spec = spec
        self.origin = spec["control_plane"]["coordinator_url"].rstrip("/")
        self.metrics_url: str | None = None
        tls = spec.get("tls", {})
        context = ssl.create_default_context(cafile=tls.get("ca_file"))
        if tls.get("cert_file"):
            context.load_cert_chain(tls["cert_file"], tls["key_file"])
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context), _NoRedirect()
        )

    def request(self, url: str, *, method: str = "GET", metrics: bool = False) -> Any:
        """Return metric text or an untrusted JSON value for caller validation."""
        parsed = urllib.parse.urlsplit(url)
        try:
            valid = parsed.scheme in {"http", "https"} and parsed.hostname and (parsed.port is None or parsed.port > 0)
        except ValueError:
            valid = False
        if not valid or parsed.username or parsed.password or parsed.fragment:
            raise ManagementError("Native management URL is invalid")
        headers = {"Accept": "text/plain" if metrics else "application/json"}
        if not metrics:
            headers.update(self.spec.get("management_headers", {}))
        data = b"{}" if method == "POST" else None
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=10) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise ManagementError("Native management response exceeds its limit")
        except urllib.error.HTTPError as error:
            raise ManagementError("Native management returned HTTP " + str(error.code)) from None
        except (OSError, urllib.error.URLError, TimeoutError) as error:
            raise ManagementError("Native management transport failed: " + type(error).__name__) from None
        try:
            return raw.decode("utf-8") if metrics else json.loads(raw)
        except (UnicodeError, ValueError):
            raise ManagementError("Native management response has an invalid encoding") from None

    def instances(self) -> list[dict]:
        value = self.request(self.origin + "/instances")
        if not isinstance(value, dict) or not isinstance(value.get("instances"), list):
            raise ManagementError("Native instance view is incomplete")
        if len(value["instances"]) > 4096 or any(not isinstance(row, dict) for row in value["instances"]):
            raise ManagementError("Native instance view is invalid")
        return value["instances"]

    def ready(self, instance: dict) -> bool:
        rows = [row for row in self.instances() if row.get("job_name") == instance["job_name"]]
        return bool(rows) and all(
            str(row.get("pool", "")).lower() == "available"
            and str(row.get("status", "")).lower() == "active"
            and row.get("healthy") is True
            for row in rows
        )

    def pause(self, instance: dict) -> list[str]:
        base = instance.get("node_manager_url", "").rstrip("/")
        value = self.request(base + "/node-manager/pause", method="POST")
        targets = value.get("engine_metrics_targets") if isinstance(value, dict) else None
        if not isinstance(value, dict) or value.get("status") != "ok" or not isinstance(targets, list) or not targets:
            raise ManagementError("Native pause did not return complete engine metric targets")
        host = self.spec["hosts"][instance["host_id"]]["pod_ip"]
        ports = set(instance["reserved_ports"])
        for target in targets:
            if not isinstance(target, str):
                raise ManagementError("Engine metric target is invalid")
            parsed = urllib.parse.urlsplit(target)
            try:
                valid_port = parsed.port in ports
            except ValueError:
                valid_port = False
            owned_origin = parsed.scheme in {"http", "https"} and parsed.hostname == host and valid_port
            metric_path = parsed.path == "/metrics" and not parsed.fragment and not parsed.query
            if not owned_origin or not metric_path or parsed.username or parsed.password:
                raise ManagementError("Engine metric target is outside the owned instance")
        return targets

    def resume(self, instance: dict) -> None:
        base = instance.get("node_manager_url", "").rstrip("/")
        result = self.request(base + "/node-manager/resume", method="POST")
        if not isinstance(result, dict) or result.get("status") != "ok":
            raise ManagementError("Native resume was not confirmed")

    def drain(self, instance: dict) -> None:
        """Refuse stopping when routing exclusion or complete queue observations are missing."""
        deadline = time.monotonic() + self.spec["policy"]["drain_timeout_seconds"]
        quiet_since: float | None = None
        try:
            targets = self.pause(instance)
            while time.monotonic() < deadline:
                rows = [row for row in self.instances() if row.get("job_name") == instance["job_name"]]
                paused = bool(rows) and all(str(row.get("pool", "")).lower() == "paused" for row in rows)
                counts = [request_counts(self.request(target, metrics=True)) for target in targets]
                quiet = paused and all(
                    value is not None and value["waiting"] == value["running"] == 0 for value in counts
                )
                now = time.monotonic()
                quiet_since = now if quiet and quiet_since is None else quiet_since if quiet else None
                if quiet_since is not None and now - quiet_since >= self.spec["policy"]["drain_grace_seconds"]:
                    return
                time.sleep(0.5)
            raise ManagementError("Drain deadline expired; instance was not stopped")
        except (ManagementError, KeyboardInterrupt):
            try:
                self.resume(instance)
            except ManagementError as resume_error:
                raise ManagementError(
                    "Drain failed and resume is unconfirmed; retain the instance resources"
                ) from resume_error
            raise

    def role_counts(self, role: str) -> dict[str, float] | None:
        if not self.metrics_url:
            return None
        query = {"type": "role"}
        if role != "union":
            query["role"] = role
        separator = "&" if "?" in self.metrics_url else "?"
        return request_counts(self.request(self.metrics_url + separator + urllib.parse.urlencode(query), metrics=True))
