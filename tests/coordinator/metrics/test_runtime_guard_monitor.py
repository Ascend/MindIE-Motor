# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Confirmed-delta behavior for Runtime Guard anomaly alarms."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from motor.coordinator.metrics.metrics_collector import MetricsCollector, parse_metric_text
from motor.coordinator.metrics.runtime_guard_monitor import _read_anomaly_samples

_METRIC = "vllm_profiling_runtime_guard:anomaly:count_total"


def _text(*samples: tuple[str, float], generation: float | None = None) -> str:
    lines = [
        "# HELP %s Runtime guard anomaly count" % _METRIC,
        "# TYPE %s counter" % _METRIC,
    ]
    for incident_type, value in samples:
        lines.append('%s{dp="0",incident_type="%s",phase="decode",role="decode"} %s' % (_METRIC, incident_type, value))
    if generation is not None:
        lines.extend(
            [
                "# HELP process_start_time_seconds Process start",
                "# TYPE process_start_time_seconds gauge",
                "process_start_time_seconds %s" % generation,
            ]
        )
    return "\n".join(lines) + "\n"


def test_parse_multiple_incident_types_and_process_start():
    samples, generation, readable = _read_anomaly_samples(
        _text(("repeat", 2), ("finite", 4), generation=123), parse_metric_text
    )
    assert readable
    assert samples == {"repeat": 2, "finite": 4}
    assert generation == 123


@pytest.mark.asyncio
async def test_partial_endpoint_scrape_is_forwarded_to_pipeline():
    import asyncio
    from unittest.mock import AsyncMock
    from motor.common.resources.instance import PDRole

    good = SimpleNamespace(id=1, ip="10.0.0.2", business_port="9000")
    bad = SimpleNamespace(id=2, ip="10.0.0.3", business_port="9001")
    instance = SimpleNamespace(id=9, role=PDRole.ROLE_D, model_name="test-model", get_all_endpoints=lambda: (good, bad))
    collector = object.__new__(MetricsCollector)
    collector._infer_tls_config = None
    collector._runtime_guard_pipeline = SimpleNamespace(ingest=AsyncMock())
    collector._loop = asyncio.get_running_loop()
    metrics = _text(("token_repeat", 4))
    with patch(
        "motor.coordinator.metrics.metrics_collector.query_endpoint_metrics",
        side_effect=lambda endpoint, tls: metrics if endpoint.id == good.id else "",
    ):
        result = await asyncio.to_thread(collector._fetch_endpoint_metrics, instance)
        await asyncio.sleep(0)
    assert result == {}
    collector._runtime_guard_pipeline.ingest.assert_awaited_once_with(
        9, "test-model", [(1, "9000", metrics), (2, "9001", None)]
    )
