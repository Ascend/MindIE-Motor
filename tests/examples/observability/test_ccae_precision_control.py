# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from __future__ import annotations

import importlib
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _add_ccae_path(monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    ccae_root = repo_root / "examples" / "features" / "observability"
    monkeypatch.syspath_prepend(str(ccae_root))


def _make_reporter(monkeypatch: pytest.MonkeyPatch):
    _add_ccae_path(monkeypatch)
    ccae_mod = importlib.import_module("ccae_reporter.reporters.ccae_reporter")
    reporter_cls = ccae_mod.CCAEReporter
    task_cls = ccae_mod._PrecisionControlTask
    limit = ccae_mod.PRECISION_COMPLETED_REPORT_LIMIT
    window_sec = ccae_mod.PRECISION_CONTROL_REPORT_WINDOW_SEC

    reporter = reporter_cls.__new__(reporter_cls)
    reporter.identity = "Controller"
    reporter.logger = MagicMock()
    reporter._precision_lock = threading.Lock()
    reporter._precision_tasks = {}
    reporter._expired_control_codes = {}
    reporter.model_id_period = {"model-1": [False, 1, 0.0]}
    reporter.backend = MagicMock()
    reporter.backend.is_alive.return_value = True
    reporter.backend.terminate_instance.return_value = True
    reporter.backend.check_instance_exists.return_value = True
    reporter._model_name = MagicMock(return_value="qwen")
    reporter.send_precision_control = MagicMock(return_value={"retCode": 0, "reqList": []})
    reporter._parse_precision_response = MagicMock()
    return reporter, task_cls, limit, window_sec, ccae_mod.MODEL_ID_STR


class TestPrecisionCompletedReporting:
    def test_control_status_respond_does_not_remove_task(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reporter, task_cls, _, _, _ = _make_reporter(monkeypatch)
        reporter._precision_tasks["model-1"] = task_cls(
            control_code="code-pId=1-dId=2",
            status="Completed",
            completed_report_count=2,
        )

        reporter._apply_req_list_item(
            {
                "modelID": "model-1",
                "controlStatusRespond": True,
            }
        )

        assert "model-1" in reporter._precision_tasks
        assert reporter._precision_tasks["model-1"].completed_report_count == 2

    def test_completed_report_success_increments_until_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reporter, task_cls, limit, _, _ = _make_reporter(monkeypatch)
        reporter._precision_tasks["model-1"] = task_cls(
            control_code="code-pId=1-dId=2",
            status="Completed",
        )

        for i in range(limit - 1):
            reporter._record_completed_report_success(["model-1"])
            assert "model-1" in reporter._precision_tasks
            assert reporter._precision_tasks["model-1"].completed_report_count == i + 1

        reporter._record_completed_report_success(["model-1"])
        assert "model-1" not in reporter._precision_tasks
        assert reporter._expired_control_codes["model-1"] == "code-pId=1-dId=2"

    def test_http_failure_does_not_increment_completed_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reporter, task_cls, _, _, model_id_str = _make_reporter(monkeypatch)
        reporter._precision_tasks["model-1"] = task_cls(
            control_code="code-pId=1-dId=2",
            status="Completed",
        )
        reporter.send_precision_control.return_value = None

        reporter.precision_control_periodic()

        assert reporter._precision_tasks["model-1"].completed_report_count == 0
        reporter.send_precision_control.assert_called_once()
        body = reporter.send_precision_control.call_args.args[0]
        assert body["modelServiceInfo"][0]["controlStatus"] == "Completed"
        assert body["modelServiceInfo"][0][model_id_str] == "model-1"
        reporter._parse_precision_response.assert_not_called()

    def test_periodic_success_counts_only_completed_tasks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reporter, task_cls, limit, _, _ = _make_reporter(monkeypatch)
        reporter._precision_tasks["model-1"] = task_cls(
            control_code="code-pId=1-dId=2",
            status="Completed",
        )

        for _ in range(limit):
            reporter.precision_control_periodic()

        assert "model-1" not in reporter._precision_tasks
        assert reporter.send_precision_control.call_count == limit
        assert reporter._parse_precision_response.call_count == limit


class TestPrecisionControlValidation:
    def test_invalid_control_code_marks_task_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reporter, _, _, _, _ = _make_reporter(monkeypatch)

        reporter._apply_req_list_item(
            {
                "modelID": "model-1",
                "precisionCommand": "precision_detection",
                "controlCode": "not-a-valid-code",
            }
        )

        task = reporter._precision_tasks["model-1"]
        assert task.status == "Failed"
        reporter.backend.terminate_instance.assert_not_called()

    def test_missing_instance_marks_task_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reporter, _, _, _, _ = _make_reporter(monkeypatch)
        reporter.backend.check_instance_exists.side_effect = lambda iid: iid == 2

        reporter._apply_req_list_item(
            {
                "modelID": "model-1",
                "precisionCommand": "precision_detection",
                "controlCode": "code-pId=1-dId=2",
            }
        )

        task = reporter._precision_tasks["model-1"]
        assert task.status == "Failed"
        reporter.backend.terminate_instance.assert_not_called()

    def test_pure_p_control_code_can_pass_validation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reporter, _, _, _, _ = _make_reporter(monkeypatch)
        reporter.backend.check_instance_exists.side_effect = lambda iid: iid == 1

        reporter._apply_req_list_item(
            {
                "modelID": "model-1",
                "precisionCommand": "precision_detection",
                "controlCode": "code-pId=1-dId=0",
            }
        )

        task = reporter._precision_tasks["model-1"]
        assert task.status != "Failed"
        reporter.backend.terminate_instance.assert_called_once_with(
            1,
            "ccae_precision_control",
            precision_alarm_clear=True,
        )

    def test_failed_status_reports_until_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reporter, task_cls, limit, _, model_id_str = _make_reporter(monkeypatch)
        reporter._precision_tasks["model-1"] = task_cls(
            control_code="code-pId=1-dId=2",
            status="Failed",
        )

        for _ in range(limit):
            reporter.precision_control_periodic()

        assert "model-1" not in reporter._precision_tasks
        last_body = reporter.send_precision_control.call_args.args[0]
        assert last_body["modelServiceInfo"][0]["controlStatus"] == "Failed"
        assert last_body["modelServiceInfo"][0][model_id_str] == "model-1"


class TestPrecisionReportWindow:
    def test_task_within_window_keeps_reporting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reporter, task_cls, _, window_sec, model_id_str = _make_reporter(monkeypatch)
        start = 1000.0
        monkeypatch.setattr(
            "ccae_reporter.reporters.ccae_reporter.time.monotonic",
            lambda: start + window_sec - 1,
        )
        reporter._precision_tasks["model-1"] = task_cls(
            control_code="code-pId=1-dId=2",
            status="Initial",
            started_at=start,
        )

        reporter.precision_control_periodic()

        assert "model-1" in reporter._precision_tasks
        body = reporter.send_precision_control.call_args.args[0]
        assert body["modelServiceInfo"][0]["controlCode"] == "code-pId=1-dId=2"
        assert body["modelServiceInfo"][0]["controlStatus"] == "Initial"
        assert body["modelServiceInfo"][0][model_id_str] == "model-1"

    def test_task_expires_after_report_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reporter, task_cls, _, window_sec, model_id_str = _make_reporter(monkeypatch)
        start = 1000.0
        monkeypatch.setattr(
            "ccae_reporter.reporters.ccae_reporter.time.monotonic",
            lambda: start + window_sec,
        )
        reporter._precision_tasks["model-1"] = task_cls(
            control_code="code-pId=1-dId=2",
            status="Initial",
            started_at=start,
        )

        reporter.precision_control_periodic()

        assert "model-1" not in reporter._precision_tasks
        assert reporter._expired_control_codes["model-1"] == "code-pId=1-dId=2"
        body = reporter.send_precision_control.call_args.args[0]
        assert "controlCode" not in body["modelServiceInfo"][0]
        assert "controlStatus" not in body["modelServiceInfo"][0]
        assert body["modelServiceInfo"][0][model_id_str] == "model-1"

    def test_same_control_code_suppressed_after_expiry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reporter, _, _, _, _ = _make_reporter(monkeypatch)
        reporter._expired_control_codes["model-1"] = "code-pId=1-dId=2"

        reporter._apply_req_list_item(
            {
                "modelID": "model-1",
                "precisionCommand": "precision_detection",
                "controlCode": "code-pId=1-dId=2",
            }
        )

        assert "model-1" not in reporter._precision_tasks
        reporter.backend.terminate_instance.assert_not_called()

    def test_new_control_code_restarts_after_expiry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reporter, _, _, _, _ = _make_reporter(monkeypatch)
        reporter._expired_control_codes["model-1"] = "code-pId=1-dId=2"

        reporter._apply_req_list_item(
            {
                "modelID": "model-1",
                "precisionCommand": "precision_detection",
                "controlCode": "code-pId=3-dId=4",
            }
        )

        assert "model-1" in reporter._precision_tasks
        assert reporter._precision_tasks["model-1"].control_code == "code-pId=3-dId=4"
        assert "model-1" not in reporter._expired_control_codes
        assert reporter.backend.terminate_instance.call_count == 1


class TestPrecisionAlarmClearAfterTerminate:
    def test_successful_terminate_requests_pd_group_alarm_clear(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        reporter, _, _, _, _ = _make_reporter(monkeypatch)

        reporter._apply_req_list_item(
            {
                "modelID": "model-1",
                "precisionCommand": "precision_detection",
                "controlCode": "code-pId=1-dId=2",
            }
        )

        reporter.backend.terminate_instance.assert_called_once_with(
            2,
            "ccae_precision_control",
            p_instance_id=1,
            precision_alarm_clear=True,
        )

    def test_failed_pd_group_terminate_keeps_task_initial(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        reporter, _, _, _, _ = _make_reporter(monkeypatch)
        reporter.backend.terminate_instance.return_value = False

        reporter._apply_req_list_item(
            {
                "modelID": "model-1",
                "precisionCommand": "precision_detection",
                "controlCode": "code-pId=1-dId=2",
            }
        )

        assert reporter._precision_tasks["model-1"].status == "Initial"

    def test_motor_backend_posts_group_terminate_with_precision_alarm_clear(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _add_ccae_path(monkeypatch)
        backend_mod = importlib.import_module("ccae_reporter.backends.motor_backend")
        backend = backend_mod.MotorBackend.__new__(backend_mod.MotorBackend)
        backend.logger = MagicMock()
        backend.is_alive = MagicMock(return_value=True)
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"data": {"precision_alarm_cleared": True}}
        backend.probe_client = MagicMock()
        backend.probe_client.do_post.return_value = response

        assert (
            backend.terminate_instance(
                2,
                "ccae_precision_control",
                p_instance_id=1,
                precision_alarm_clear=True,
            )
            is True
        )
        backend.probe_client.do_post.assert_called_once_with(
            "/controller/terminate_instance",
            {
                "instance_id": 2,
                "reason": "ccae_precision_control",
                "p_instance_id": 1,
                "precision_alarm_clear": True,
            },
        )
