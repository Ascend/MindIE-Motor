# Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Inject PR170-style error logs (and optional OTLP traces) for observability testing."""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


def _random_trace_id() -> str:
    return secrets.token_hex(16)


def _random_request_id() -> str:
    return f"req-{secrets.token_hex(8)}"


def build_log_lines(
    *,
    service_name: str,
    trace_id: str,
    request_id: str,
    count: int,
) -> list[str]:
    lines: list[str] = []
    for i in range(count):
        lines.append(
            f"HTTP request send failed. url=http://127.0.0.1:8080/v1/chat/completions, "
            f"error=connection refused attempt={i + 1} "
            f"Possible causes: 1) engine not ready 2) network partition 3) timeout "
            f"trace_id={trace_id} x_request_id={request_id} service_name={service_name}"
        )
        lines.append(
            f"error message: upstream engine unavailable for request {request_id} "
            f"trace_id={trace_id} x_request_id={request_id}"
        )
    return lines


def push_loki_logs(
    *,
    loki_url: str,
    service_name: str,
    lines: list[str],
) -> None:
    # Each line needs a distinct timestamp; Loki dedupes same stream + ts + line.
    base_ts_ns = int(time.time() * 1_000_000_000)
    values = [[str(base_ts_ns + i), line] for i, line in enumerate(lines)]
    payload = {
        "streams": [
            {
                "stream": {"service_name": service_name, "job": "inject-pr170"},
                "values": values,
            }
        ]
    }
    req = urllib.request.Request(
        f"{loki_url.rstrip('/')}/loki/api/v1/push",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"Loki push failed: HTTP {resp.status}")


def push_otlp_trace(
    *,
    otlp_http_url: str,
    service_name: str,
    trace_id: str,
    request_id: str,
) -> None:
    # Minimal OTLP/HTTP JSON trace with x_request_id + error.message attributes.
    span_id = secrets.token_hex(8)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    payload = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": service_name}},
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "inject-pr170"},
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": span_id,
                                "name": "router.dispatch",
                                "kind": 1,
                                "startTimeUnixNano": str(int(time.time() * 1_000_000_000)),
                                "endTimeUnixNano": str(int(time.time() * 1_000_000_000) + 50_000_000),
                                "attributes": [
                                    {
                                        "key": "x_request_id",
                                        "value": {"stringValue": request_id},
                                    },
                                    {
                                        "key": "error.message",
                                        "value": {
                                            "stringValue": (
                                                f"error message: upstream engine unavailable for request {request_id}"
                                            )
                                        },
                                    },
                                ],
                                "status": {"code": 2, "message": "upstream engine unavailable"},
                            }
                        ],
                    }
                ],
            }
        ]
    }
    req = urllib.request.Request(
        f"{otlp_http_url.rstrip('/')}/v1/traces",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"OTLP trace push failed: HTTP {resp.status}")
    print(f"[inject] OTLP trace sent trace_id={trace_id} at {now}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("loki", "both"),
        default="loki",
        help="Push logs only, or logs + OTLP trace (default: loki)",
    )
    parser.add_argument("--loki-url", default="http://127.0.0.1:3100")
    parser.add_argument("--otlp-http-url", default="http://127.0.0.1:4318")
    parser.add_argument("--service-name", default="motor-coordinator")
    parser.add_argument("--trace-id", default="")
    parser.add_argument("--request-id", default="")
    parser.add_argument("--count", type=int, default=2, help="Number of error pairs to emit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    trace_id = args.trace_id or _random_trace_id()
    request_id = args.request_id or _random_request_id()
    lines = build_log_lines(
        service_name=args.service_name,
        trace_id=trace_id,
        request_id=request_id,
        count=max(1, args.count),
    )

    try:
        push_loki_logs(
            loki_url=args.loki_url,
            service_name=args.service_name,
            lines=lines,
        )
    except urllib.error.URLError as exc:
        print(f"[inject] Loki push failed: {exc}", file=sys.stderr)
        return 1

    print(f"[inject] pushed {len(lines)} log lines to Loki trace_id={trace_id} x_request_id={request_id}")
    print('[inject] Explore query: {service_name=~"motor-.*"} |= "Possible causes:"')

    if args.mode == "both":
        try:
            push_otlp_trace(
                otlp_http_url=args.otlp_http_url,
                service_name=args.service_name,
                trace_id=trace_id,
                request_id=request_id,
            )
        except urllib.error.URLError as exc:
            print(f"[inject] OTLP trace push failed: {exc}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
