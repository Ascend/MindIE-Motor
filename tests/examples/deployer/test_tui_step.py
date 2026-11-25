# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from lib.tui.step import PROGRESS_TOTAL, engine_pod_name_pattern, parse_log_line


@pytest.mark.parametrize("previous_status", ["INITIAL", "WAIT2START"])
def test_normal_endpoint_transition_completes_startup_progress(previous_status: str) -> None:
    """Normal and snapshot startup paths must both complete the progress bar."""
    line = f"Native engine rank 0, status change from EndpointStatus.{previous_status} to EndpointStatus.NORMAL"

    assert parse_log_line(line) == PROGRESS_TOTAL


def test_engine_pod_pattern_follows_assigned_service() -> None:
    """A named service is selected by its service prefix; other modes keep vllm-."""
    assert engine_pod_name_pattern("service2") == "^service2-"
    assert engine_pod_name_pattern("") == "vllm-"


@pytest.mark.parametrize("mode", ["infer_service_set", "multi_deployment", "single_container"])
def test_legacy_monitor_restart_keeps_original_behavior(monkeypatch, mode):
    from lib.tui.actions import _DeployActionsMixin

    old_thread = Mock(is_alive=Mock(return_value=True))
    old_monitor = Mock()
    cancel = threading.Event()
    threads = [old_thread]

    def cleanup():
        cancel.set()
        threads.clear()

    session = SimpleNamespace(
        user_config={"motor_deploy_config": {"deploy_mode": mode}},
        _ctx=object(),
        namespace="demo",
        _rbg_pods=lambda: None,
        _monitor_threads=threads,
        _progress_monitor=old_monitor,
        _cleanup_monitors=cleanup,
        _cancel_event=cancel,
        _set_status=Mock(),
        _progress_active=False,
    )
    worker = Mock()
    monkeypatch.setattr("lib.tui.actions.threading.Thread", Mock(return_value=worker))
    monkeypatch.setattr("lib.tui.step.shell_get_pod", lambda _: ["vllm-p0"])

    _DeployActionsMixin._start_progress_monitor(session)

    old_monitor.cancel.assert_not_called()
    worker.start.assert_called_once_with()
    assert session._progress_active and not cancel.is_set()


@pytest.mark.parametrize("mode", ["infer_service_set", "multi_deployment", "single_container"])
def test_legacy_delete_failure_keeps_original_menu_reset(monkeypatch, tmp_path, mode):
    from lib.tui.actions import _DeployActionsMixin

    (tmp_path / "delete.sh").write_text("exit 1\n", encoding="utf-8")
    process = MagicMock()
    process.__enter__.return_value = process
    process.stdout.readline.return_value = ""
    process.poll.return_value = 1
    process.returncode = 1
    popen = Mock(return_value=process)
    monkeypatch.setattr("lib.tui.actions.subprocess.Popen", popen)
    session = SimpleNamespace(
        user_config={"motor_deploy_config": {"deploy_mode": mode}},
        _ctx=Mock(),
        _running=True,
        deployer_dir=str(tmp_path),
        _set_status=Mock(),
        _reset_after_delete=Mock(),
    )

    _DeployActionsMixin._exec_delete(session, "demo")

    assert popen.call_args.args[0] == ["/bin/bash", str(tmp_path / "delete.sh"), "demo"]
    session._reset_after_delete.assert_called_once_with()


def test_single_container_hybrid_tui_keeps_original_template_validation(monkeypatch, tmp_path):
    import deploy
    from lib.tui.actions import _DeployActionsMixin

    config = {
        "motor_deploy_config": {
            "deploy_mode": "single_container",
            "hybrid_instances_num": 1,
            "single_hybrid_instance_pod_num": 1,
            "hybrid_pod_npu_num": 1,
        },
        "motor_engine_union_config": {},
    }
    (tmp_path / "user_config.json").write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / "env.json").write_text("{}", encoding="utf-8")
    resolver = Mock(return_value=str(tmp_path / "missing-infer-template.yaml"))
    submit = Mock(side_effect=RuntimeError("deployment boundary"))
    monkeypatch.setattr("lib.constant.OUTPUT_ROOT_PATH", str(tmp_path / "output"))
    monkeypatch.setattr("lib.generator.k8s_utils.set_user_config_path", Mock())
    monkeypatch.setattr("lib.container_snapshot.resolve_infer_service_template", resolver)
    monkeypatch.setattr(deploy, "deploy_services", submit)
    session = SimpleNamespace(
        _ctx=Mock(),
        _set_status=Mock(),
        _read_line=lambda *args, **kwargs: str(tmp_path),
        _last_config_dir="",
        deployer_dir=str(tmp_path),
    )

    _DeployActionsMixin._deploy_service(session)

    resolver.assert_called_once()
    submit.assert_not_called()
