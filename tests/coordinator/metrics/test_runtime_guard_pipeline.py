# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from motor.coordinator.metrics.metrics_collector import parse_metric_text
from motor.coordinator.metrics.runtime_guard_pipeline import RuntimeGuardPipeline


def metrics(value):
    name = "vllm_profiling_runtime_guard:anomaly:count_total"
    return f'# HELP {name} count\n# TYPE {name} counter\n{name}{{incident_type="repeat"}} {value}\n'


class Scheduler:
    def __init__(self):
        self.active = False
        self.normal = 0
        self.calls = []

    async def record_precision_result(self, key, issue, threshold, *, clear_threshold, check_valid):
        self.calls.append(issue)
        raise_hit = issue and not self.active
        if issue:
            self.normal = 0
        elif self.active:
            self.normal += 1
        clear_hit = self.active and self.normal >= clear_threshold
        return SimpleNamespace(
            skip=False,
            threshold_hit=raise_hit,
            clear_threshold_hit=clear_hit,
            action_token="token",
            alarm_moi="saved-moi",
        )

    async def finish_precision_action(self, key, token, *, action_type, success, **kwargs):
        if success:
            self.active = action_type == "raise" and not kwargs.get("auto_recovery_cleared")
            self.normal = 0
        return True


def make_pipeline(scheduler, reports, probe):
    config = SimpleNamespace(
        precision_detection_config=SimpleNamespace(
            precision_clear_threshold=2, probe_max_attempts=3, probe_timeout_seconds=1
        )
    )

    async def report(payload):
        reports.append(payload)
        return {"ok": True}

    return RuntimeGuardPipeline(
        parser=parse_metric_text,
        scheduler_provider=lambda: scheduler,
        probe_provider=lambda: probe,
        config_provider=lambda: config,
        report=report,
        interval=10,
    )


@pytest.mark.asyncio
async def test_combines_ports_probes_exact_endpoints_and_clears_after_valid_normal_rounds():
    scheduler = Scheduler()
    reports = []
    probe = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(failures=1)))
    pipeline = make_pipeline(scheduler, reports, probe)
    batch = [(1, "10000", metrics(2)), (2, "10001", metrics(3))]
    for _ in range(10):
        await pipeline.ingest(9, "model", batch)
    assert len(reports) == 1
    assert reports[0]["instance_id"] == "9"
    assert reports[0]["p_instance_id"] == ""
    assert reports[0]["native_me_dn"]
    assert reports[0]["probable_cause"] == "1:Repeated token-level precision issues detected by sampling"
    assert "abnormal_ports=[10000,10001]" in reports[0]["additional_information"]
    assert "precision_issue_count=5" in reports[0]["additional_information"]
    assert "probe_failure_count=2" in reports[0]["additional_information"]
    assert [c.kwargs["d_endpoint_id"] for c in probe.run.await_args_list] == [1, 2]
    assert all(c.kwargs["max_attempts"] == 1 for c in probe.run.await_args_list)
    for _ in range(10):
        await pipeline.ingest(9, "model", [(1, "10000", metrics(2)), (2, "10001", None)])
    assert len(reports) == 1
    for _ in range(20):
        await pipeline.ingest(9, "model", batch)
    assert len(reports) == 2
    assert reports[1]["moi"] == "saved-moi"


@pytest.mark.asyncio
async def test_disappeared_endpoint_does_not_count_as_normal():
    scheduler = Scheduler()
    reports = []
    probe = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(failures=0)))
    pipeline = make_pipeline(scheduler, reports, probe)
    for _ in range(10):
        await pipeline.ingest(9, "model", [(1, "10000", metrics(1)), (2, "10001", metrics(1))])
    for _ in range(40):
        await pipeline.ingest(9, "model", [(1, "10000", metrics(1))])
    assert len(reports) == 1


@pytest.mark.asyncio
async def test_probe_exception_still_reports_alarm():
    scheduler = Scheduler()
    reports = []
    probe = SimpleNamespace(run=AsyncMock(side_effect=RuntimeError("probe failed")))
    pipeline = make_pipeline(scheduler, reports, probe)
    for _ in range(10):
        await pipeline.ingest(9, "model", [(1, "10000", metrics(1))])
    assert "probe_failure_count=1" in reports[0]["additional_information"]


@pytest.mark.asyncio
async def test_counter_decrease_rebuilds_baseline_and_next_increase_is_detected():
    scheduler = Scheduler()
    reports = []
    probe = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(failures=0)))
    pipeline = make_pipeline(scheduler, reports, probe)
    for value in (5, 2, 3):
        for _ in range(10):
            await pipeline.ingest(9, "model", [(1, "10000", metrics(value))])
    assert scheduler.calls == [True, False, True]
    assert len(reports) == 1


@pytest.mark.asyncio
async def test_failed_alarm_is_retried_without_losing_counter():
    scheduler = Scheduler()
    reports = []
    probe = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(failures=0)))
    pipeline = make_pipeline(scheduler, reports, probe)
    attempts = []

    async def report(payload):
        attempts.append(payload)
        return {"ok": len(attempts) > 1}

    pipeline._report = report
    for _ in range(20):
        await pipeline.ingest(9, "model", [(1, "10000", metrics(2))])
    assert len(attempts) == 2
    assert all("precision_issue_count=2" in p["additional_information"] for p in attempts)


@pytest.mark.asyncio
async def test_synchronous_report_is_run_off_event_loop():
    scheduler = Scheduler()
    reports = []
    probe = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(failures=0)))
    pipeline = make_pipeline(scheduler, reports, probe)
    pipeline._report = lambda payload: reports.append(payload) or {"ok": True}
    for _ in range(10):
        await pipeline.ingest(9, "model", [(1, "10000", metrics(1))])
    assert len(reports) == 1
