# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Verify that CLI/TUI entry points call the RBG lifecycle and scaling functions directly."""

import subprocess
import sys
import threading
from copy import deepcopy
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize(
    "operation,function,owner",
    [
        ("deploy", "deploy_services_rbg", "lifecycle"),
        ("runtime", "update_rbg_runtime_config", "lifecycle"),
        ("scale", "update_rbg_instance_num", "lifecycle"),
    ],
)
def test_cli_calls_concrete_operation_without_infer_fallback(monkeypatch, tmp_path, operation, function, owner):
    import deploy

    config = {"motor_deploy_config": {"deploy_mode": "rbg", "job_id": "demo"}}
    calls = []
    baseline = deepcopy(config)
    monkeypatch.setattr("lib.rbg.lifecycle.get_rbg_baseline_config", lambda _: baseline)
    module = ModuleType("lib.rbg." + owner)
    setattr(module, function, lambda *args, **kwargs: calls.append((args, kwargs)))
    module.get_rbg_baseline_config = lambda _: baseline
    monkeypatch.setitem(sys.modules, "lib.rbg." + owner, module)
    monkeypatch.setattr(deploy, "deploy_services_infer_service_set", lambda *a: pytest.fail("Infer fallback"))
    env_path = tmp_path / "env.json"
    env_path.write_text("{}", encoding="utf-8")
    if operation == "deploy":
        deploy.deploy_services(config, str(env_path), dry_run=True)
    elif operation == "runtime":
        deploy.handle_update_config(config)
    else:
        deploy.handle_update_instance_num(config, str(env_path))
    assert len(calls) == 1
    assert calls[0][0][0] == config
    if operation == "deploy":
        assert calls[0][0][1] == str(env_path)
        assert calls[0][1]["dry_run"] is True
    if operation == "scale":
        assert calls[0][0] == (config, baseline)
        assert calls[0][1] == {}


def test_missing_lifecycle_function_fails_instead_of_falling_back(monkeypatch, tmp_path):
    import deploy

    monkeypatch.setitem(sys.modules, "lib.rbg.lifecycle", ModuleType("lib.rbg.lifecycle"))
    config = {"motor_deploy_config": {"deploy_mode": "rbg", "job_id": "demo"}}
    env_path = tmp_path / "env.json"
    env_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ImportError):
        deploy.deploy_services(config, str(env_path), dry_run=True)


def test_tui_failed_delete_preserves_deployed_state(monkeypatch):
    from lib.tui.actions import _DeployActionsMixin

    def fail(*args, **kwargs):
        raise RuntimeError("API unavailable")

    module = ModuleType("lib.rbg.lifecycle")
    module.delete_rbg = fail
    monkeypatch.setitem(sys.modules, "lib.rbg.lifecycle", module)
    statuses = []
    session = SimpleNamespace(
        user_config={"motor_deploy_config": {"deploy_mode": "rbg", "job_id": "demo"}},
        _deployed=True,
        _set_status=lambda text, *_: statuses.append(text),
        _reset_after_delete=lambda: pytest.fail("failed delete reset state"),
    )
    _DeployActionsMixin._exec_delete(session, "demo")
    assert session._deployed and "API unavailable" in statuses[0]


def test_tui_same_name_new_uid_restarts_monitor():
    from lib.tui.actions import _DeployActionsMixin

    restarted = []
    session = SimpleNamespace(
        _rbg_pods=lambda: {"model-prefill-0-engine-0": "new"},
        _rbg_pod_uids={"model-prefill-0-engine-0": "old"},
        _start_progress_monitor=lambda: restarted.append(True),
    )
    _DeployActionsMixin._rescan_pods(session)
    assert restarted == [True]


@pytest.mark.parametrize("flag", ["--update_resources", "--delete_deployment", "--wait", "--timeout"])
def test_removed_resource_operation_flags_are_rejected(monkeypatch, flag):
    import deploy

    monkeypatch.setattr(sys, "argv", ["deploy.py", flag])
    with pytest.raises(SystemExit) as exc:
        deploy.parse_arguments()
    assert exc.value.code == 2


@pytest.mark.parametrize("flag", ["--update_config", "--update_instance_num"])
def test_existing_update_operations_remain_available(monkeypatch, flag):
    import deploy

    monkeypatch.setattr(sys, "argv", ["deploy.py", flag])
    assert getattr(deploy.parse_arguments(), flag[2:]) is True


@pytest.mark.parametrize("dry_run", [False, True])
def test_rbg_uses_common_checks_without_rewriting_startup(monkeypatch, tmp_path, dry_run):
    import deploy

    config = {"motor_deploy_config": {"deploy_mode": "rbg", "job_id": "demo"}}
    env_path = tmp_path / "env.json"
    env_path.write_text("{}", encoding="utf-8")
    calls = []
    module = ModuleType("lib.rbg.lifecycle")
    module.deploy_services_rbg = lambda *a, **kw: calls.append("rbg")
    monkeypatch.setitem(sys.modules, "lib.rbg.lifecycle", module)
    monkeypatch.setattr(deploy, "validate_kv_cache_store_config", lambda _: calls.append("kv"))
    monkeypatch.setattr(deploy, "validate_node_selectors", lambda _: calls.append("nodes"))
    monkeypatch.setattr(deploy, "ensure_namespace", lambda _: None)
    monkeypatch.setattr(deploy, "warn_existing_service", lambda _: None)
    monkeypatch.setattr(deploy, "set_env_to_shell", lambda *a: pytest.fail("RBG must not rewrite source scripts"))
    monkeypatch.setattr(
        deploy, "resolve_nodeports_for_yaml_files", lambda *a, **kw: pytest.fail("RBG must check its own Service files")
    )
    deploy.deploy_services(config, str(env_path), dry_run=dry_run)
    assert calls == (["kv", "rbg"] if dry_run else ["kv", "nodes", "rbg"])


@pytest.mark.parametrize(
    "error", [subprocess.CalledProcessError(1, "kubectl"), subprocess.TimeoutExpired("kubectl", 15)]
)
def test_rbg_monitor_retries_query_failure_before_starting(monkeypatch, error):
    from lib.tui import step

    queries, displayed, started = [], [], []

    def discover(namespace, rbg_name):
        queries.append((namespace, rbg_name))
        if len(queries) == 1:
            raise error
        return {"engine-0": "uid-0"}

    monkeypatch.setattr(step, "engine_pods", discover)
    monkeypatch.setattr(step.time, "sleep", lambda _: None)
    monkeypatch.setattr(step, "update_log_display", lambda *args: displayed.append(args))
    monkeypatch.setattr(
        step, "VLLMProgressMonitor", lambda **kwargs: SimpleNamespace(start=lambda *args: started.append(args))
    )
    step.start_monitor("ns", 1, "model")
    assert queries == [("ns", "model"), ("ns", "model")]
    assert displayed == [(1, 1)]
    assert started == [(["engine-0"], "ns")]


@pytest.mark.parametrize("name", ["", " "])
@pytest.mark.parametrize("entry", ["shell_get_pod", "start_monitor"])
def test_empty_rbg_name_is_rejected_without_legacy_fallback(monkeypatch, name, entry):
    from lib.tui import step

    monkeypatch.setattr(step.time, "sleep", lambda _: None)
    monkeypatch.setattr(step.subprocess, "Popen", lambda *a, **kw: pytest.fail("Legacy discovery must not run"))
    args = ("ns", 1, name) if entry == "start_monitor" else ("ns", name)
    with pytest.raises(ValueError, match="rbg_name must be nonempty"):
        getattr(step, entry)(*args)


@pytest.mark.parametrize("handler", ["handle_update_config", "handle_update_instance_num"])
@pytest.mark.parametrize("baseline", [None, {"motor_deploy_config": {"deploy_mode": "infer_service_set"}}])
def test_rbg_updates_reject_missing_or_different_mode_baseline(monkeypatch, handler, baseline):
    import deploy

    config = {"motor_deploy_config": {"deploy_mode": "rbg", "job_id": "demo"}}
    monkeypatch.setattr("lib.rbg.lifecycle.get_rbg_baseline_config", lambda _: baseline)
    with pytest.raises(FileNotFoundError if baseline is None else ValueError):
        getattr(deploy, handler)(config)


def test_rbg_runtime_update_filters_non_whitelisted_changes(monkeypatch):
    import deploy

    baseline = {
        "motor_deploy_config": {"deploy_mode": "rbg", "job_id": "demo", "image_name": "old"},
        "motor_coordinator_config": {"logging_config": {"log_level": "INFO"}},
    }
    config = deepcopy(baseline)
    config["motor_deploy_config"]["image_name"] = "new"
    config["motor_coordinator_config"]["logging_config"]["log_level"] = "DEBUG"
    updated = []
    module = ModuleType("lib.rbg.lifecycle")
    module.update_rbg_runtime_config = lambda config, baseline_config: updated.append(config)
    module.get_rbg_baseline_config = lambda _: baseline
    monkeypatch.setitem(sys.modules, "lib.rbg.lifecycle", module)
    monkeypatch.setattr("lib.rbg.lifecycle.get_rbg_baseline_config", lambda _: baseline)
    deploy.handle_update_config(config)
    assert updated[0]["motor_deploy_config"]["image_name"] == "old"
    assert updated[0]["motor_coordinator_config"]["logging_config"]["log_level"] == "DEBUG"


def test_tui_discovery_failure_keeps_current_log_monitors():
    from lib.tui.actions import _DeployActionsMixin

    def unavailable():
        raise subprocess.CalledProcessError(1, "kubectl")

    statuses = []
    session = SimpleNamespace(
        _ctx=object(),
        _rbg_pods=unavailable,
        _monitor_threads=[],
        _progress_monitor=SimpleNamespace(cancel=lambda: pytest.fail("Existing monitor was stopped")),
        _cleanup_monitors=lambda: pytest.fail("Existing monitors were cleaned up"),
        _set_status=lambda *args: statuses.append(args),
        _rbg_pod_uids={"engine-0": "old"},
    )
    _DeployActionsMixin._start_progress_monitor(session)
    assert "RBG Pod discovery failed" in statuses[0][0]
    assert session._rbg_pod_uids == {"engine-0": "old"}


def test_tui_rbg_waits_for_old_log_monitors_before_restarting(monkeypatch):
    from lib.tui.actions import _DeployActionsMixin

    old_thread = Mock(is_alive=Mock(return_value=True))
    threads = [old_thread]
    cancel = threading.Event()

    def cleanup():
        cancel.set()
        threads.clear()

    session = SimpleNamespace(
        _ctx=object(),
        _rbg_pods=lambda: {"model-prefill-0-engine-0": "new"},
        _monitor_threads=threads,
        _progress_monitor=Mock(),
        _cleanup_monitors=cleanup,
        _cancel_event=cancel,
        _set_status=Mock(),
    )
    start_thread = Mock()
    monkeypatch.setattr("lib.tui.actions.threading.Thread", start_thread)

    _DeployActionsMixin._start_progress_monitor(session)

    session._progress_monitor.cancel.assert_called_once_with()
    start_thread.assert_not_called()
    assert threads == [old_thread] and cancel.is_set()
