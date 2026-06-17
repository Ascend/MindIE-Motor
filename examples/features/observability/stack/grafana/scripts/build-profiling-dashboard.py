#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Build `motor-vllm-profiling.json` from Prometheus `vllm_profiling_*` metric families.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)

CORE_METRICS = [
    "vllm_profiling_forward_duration_seconds_bucket",
    "vllm_profiling_execute_model_duration_seconds_bucket",
    "vllm_profiling_scheduler_duration_seconds_bucket",
    "vllm_profiling_batch_size",
    "vllm_profiling_running_queue_size",
]


def fetch_metric_names(prometheus_url: str) -> List[str]:
    endpoint = urllib.parse.urljoin(prometheus_url.rstrip("/") + "/", "api/v1/label/__name__/values")
    req = urllib.request.Request(endpoint, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("status") != "success":
        raise RuntimeError(f"prometheus response is not success: {payload}")
    values = payload.get("data", [])
    return sorted(name for name in values if isinstance(name, str) and name.startswith("vllm_profiling_"))


def infer_query(metric_name: str) -> str:
    if metric_name.endswith("_bucket"):
        metric = metric_name
        return f"histogram_quantile(0.95, sum(rate({metric}[5m])) by (le, pd_role, role, instance_id))"
    if metric_name.endswith("_total") or metric_name.endswith("_count"):
        return f"sum(rate({metric_name}[5m])) by (pd_role, role, instance_id)"
    if metric_name.endswith("_sum"):
        return f"sum(rate({metric_name}[5m])) by (pd_role, role, instance_id)"
    return f"avg({metric_name}) by (pd_role, role, instance_id)"


def make_timeseries_panel(panel_id: int, title: str, expr: str, y_unit: str = "short") -> Dict:
    return {
        "id": panel_id,
        "type": "timeseries",
        "title": title,
        "datasource": {"type": "prometheus", "uid": "prometheus"},
        "targets": [
            {
                "refId": "A",
                "expr": expr,
                "legendFormat": "{{pd_role}}/{{instance_id}}",
            }
        ],
        "fieldConfig": {
            "defaults": {
                "unit": y_unit,
            },
            "overrides": [],
        },
        "gridPos": {"h": 8, "w": 12, "x": 0, "y": 0},
        "options": {
            "legend": {
                "displayMode": "list",
                "placement": "bottom",
            }
        },
    }


def place_panels(panels: List[Dict], start_y: int) -> int:
    x = 0
    y = start_y
    for idx, panel in enumerate(panels):
        panel["gridPos"]["x"] = x
        panel["gridPos"]["y"] = y
        x += 12
        if idx % 2 == 1:
            x = 0
            y += 8
    if len(panels) % 2 == 1:
        y += 8
    return y


def build_dashboard(metric_names: List[str], title: str) -> Dict:
    core_names = [name for name in CORE_METRICS if name in metric_names]
    detail_names = [name for name in metric_names if name not in core_names]

    panel_id = 1
    top_panels: List[Dict] = []
    detail_panels: List[Dict] = []

    for metric in core_names:
        panel = make_timeseries_panel(
            panel_id=panel_id,
            title=f"{metric} (Core)",
            expr=infer_query(metric),
        )
        top_panels.append(panel)
        panel_id += 1

    for metric in detail_names:
        panel = make_timeseries_panel(
            panel_id=panel_id,
            title=metric,
            expr=infer_query(metric),
        )
        detail_panels.append(panel)
        panel_id += 1

    current_y = 0
    current_y = place_panels(top_panels, current_y)

    detail_row = {
        "id": panel_id,
        "type": "row",
        "title": "指标明细 Metric Details",
        "collapsed": True,
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": current_y},
        "panels": [],
    }
    panel_id += 1
    detail_y = current_y + 1
    place_panels(detail_panels, detail_y)
    detail_row["panels"] = detail_panels

    templating_list = [
        {
            "name": "cluster",
            "type": "query",
            "datasource": {"type": "prometheus", "uid": "prometheus"},
            "query": "label_values(up, cluster)",
            "refresh": 1,
            "includeAll": True,
            "multi": True,
            "current": {"text": "All", "value": "$__all"},
            "label": "集群 Cluster",
        },
        {
            "name": "pd_role",
            "type": "query",
            "datasource": {"type": "prometheus", "uid": "prometheus"},
            "query": "label_values(up, pd_role)",
            "refresh": 1,
            "includeAll": True,
            "multi": True,
            "current": {"text": "All", "value": "$__all"},
            "label": "角色 Role",
        },
        {
            "name": "instance_id",
            "type": "query",
            "datasource": {"type": "prometheus", "uid": "prometheus"},
            "query": "label_values(up, instance_id)",
            "refresh": 1,
            "includeAll": True,
            "multi": True,
            "current": {"text": "All", "value": "$__all"},
            "label": "实例 Instance",
        },
    ]

    return {
        "uid": "motor-vllm-profiling",
        "title": title,
        "tags": ["pymotor", "profiling", "ms_service_metric"],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "refresh": "10s",
        "editable": True,
        "graphTooltip": 1,
        "time": {"from": "now-30m", "to": "now"},
        "templating": {"list": templating_list},
        "panels": [*top_panels, detail_row],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate vLLM profiling dashboard JSON.")
    parser.add_argument(
        "--prometheus-url",
        default="http://localhost:9090",
        help="Prometheus base URL.",
    )
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parents[1] / "dashboards" / "motor-vllm-profiling.json"),
        help="Output dashboard path.",
    )
    parser.add_argument(
        "--title",
        default="引擎性能剖析",
        help="Dashboard title.",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(name)s] %(message)s",
    )
    args = parse_args()
    try:
        metric_names = fetch_metric_names(args.prometheus_url)
    except (urllib.error.URLError, RuntimeError, json.JSONDecodeError) as exc:
        logger.error("failed to query Prometheus: %s", exc)
        return 1

    if not metric_names:
        logger.error("no vllm_profiling_* metrics found.")
        return 1

    dashboard = build_dashboard(metric_names=metric_names, title=args.title)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(dashboard, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info("generated: %s", output)
    logger.info("metrics: %d", len(metric_names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
