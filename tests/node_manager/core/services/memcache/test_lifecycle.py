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
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest
import requests

from motor.config.node_manager import KVCacheStoreConfig
from motor.node_manager.core.services.memcache import migration
from motor.node_manager.core.services.memcache.lifecycle import LocalService
from motor.node_manager.core.services.memcache.migration import DrainSubmissionRejected


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


DRAIN_UUID = "01234567-89ab-cdef-0123-456789abcdef"


def _make_ls(*, migration_enabled=False, **overrides) -> LocalService:
    """Quick one-off LocalService with overrides on top of defaults."""
    cfg = KVCacheStoreConfig()
    cfg.enable = True
    cfg.backend = "memcache"
    cfg.service = "kvs-master:50088"
    cfg.port = 50088
    cfg.local_service_mode = "inprocess"
    cfg.local_config_path = "/tmp/mmc-local-inprocess.conf"
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return LocalService(
        hardware_type="800I_A3",
        kv_cache_store_config=cfg,
        local_world_size=4,
        kv_migration_enabled=migration_enabled,
    )


def _write_conf(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _cleanup(*paths: str) -> None:
    for p in paths:
        if os.path.exists(p):
            os.remove(p)


# ===================================================================
# should_launch
# ===================================================================


def test_should_launch_standalone():
    assert _make_ls(local_service_mode="standalone").should_launch() is True


def test_should_launch_inprocess():
    assert _make_ls(local_service_mode="inprocess").should_launch() is False


def test_should_launch_disabled():
    assert _make_ls(enable=False, local_service_mode="standalone").should_launch() is False


def test_should_launch_not_memcache():
    assert _make_ls(backend="mooncake", local_service_mode="standalone").should_launch() is False


# ===================================================================
# prepare()
# ===================================================================


def test_prepare_sets_mmc_local_config_path_env():
    """prepare() exports MMC_LOCAL_CONFIG_PATH env var pointing to the conf file."""
    ls = _make_ls(local_service_mode="inprocess")
    ls._endpoints_count = 1
    _write_conf("/tmp/mmc-local-inprocess.conf", "")
    try:
        ls.prepare(endpoints_count=1)
        assert os.environ["MMC_LOCAL_CONFIG_PATH"] == "/tmp/mmc-local-inprocess.conf"
    finally:
        _cleanup("/tmp/mmc-local-inprocess.conf")


def test_prepare_no_conf_warns(caplog):
    """When conf file doesn't exist, prepare() warns and returns early."""
    ls = _make_ls(local_service_mode="inprocess")
    ls._kv_cfg.local_config_path = "/tmp/nonexistent.conf"
    ls.prepare(endpoints_count=0)
    assert "not found" in caplog.text


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("custom_label", [None, "storage-a"])
def test_standalone_drain_label_only_on_storage_config(monkeypatch, tmp_path, custom_label, enabled):
    key = "ock.mmc.local_service.node_label"
    backend = "ock.mmc.local_service.backend_id = inference-a\n"
    client = tmp_path / "mmc-local-inprocess.conf"
    storage = tmp_path / "mmc-local-standalone.conf"
    client.write_text(f"{key}=old\n{backend}{key} = old\n", encoding="utf-8")
    storage.write_text(backend, encoding="utf-8")
    monkeypatch.setenv("POD_IP", "10.0.0.4")
    monkeypatch.setenv("MMC_LOCAL_CONFIG_PATH", str(client))
    monkeypatch.delenv("MMC_LOCAL_SERVICE_NODE_LABEL", raising=False)
    if custom_label:
        monkeypatch.setenv("MMC_LOCAL_SERVICE_NODE_LABEL", custom_label)
    service = _make_ls(migration_enabled=enabled, local_service_mode="standalone", local_config_path=str(client))
    service.prepare()
    service.prepare()  # Engine relaunch in the same Pod.
    assert os.environ["MMC_LOCAL_CONFIG_PATH"] == str(client)
    assert client.read_text(encoding="utf-8") == (backend if enabled else f"{key}=old\n{backend}{key} = old\n")
    expected = f"{backend}{key} = {custom_label or '10.0.0.4'}\n" if enabled else backend
    assert storage.read_text(encoding="utf-8") == expected


def test_standalone_rejects_shared_client_and_storage_config(tmp_path):
    shared = tmp_path / "mmc-local-standalone.conf"
    shared.touch()
    service = _make_ls(local_service_mode="standalone", local_config_path=str(shared))
    service.prepare()  # Shared configs are valid when the feature is off.
    service = _make_ls(migration_enabled=True, local_service_mode="standalone", local_config_path=str(shared))
    with pytest.raises(ValueError, match="separate config files"):
        service.prepare()


def test_prepare_a2_a5_force_inprocess(tmp_path, monkeypatch):
    """A2 hardware forces inprocess mode even if standalone is configured."""
    ls = _make_ls(migration_enabled=True, local_service_mode="standalone")
    ls.hardware_type = "800I_A2"
    client = tmp_path / "mmc-local-inprocess.conf"
    client.touch()
    ls._kv_cfg.local_config_path = str(client)
    monkeypatch.setenv("POD_IP", "10.0.0.4")
    monkeypatch.delenv("MMC_LOCAL_SERVICE_NODE_LABEL", raising=False)
    monkeypatch.setenv("MMC_LOCAL_CONFIG_PATH", str(client))
    ls.prepare(endpoints_count=0)
    assert ls._kv_cfg.local_service_mode == "inprocess"
    assert ls.should_launch() is False
    assert client.read_text(encoding="utf-8") == ""


# ===================================================================
# pull() — standalone LS (Popen)
# ===================================================================


@patch("subprocess.Popen")
def test_pull_launches_standalone_as_subprocess(mock_popen):
    """pull() starts standalone LS via Popen with standalone conf in env."""
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None  # process still running
    mock_popen.return_value = mock_proc

    _write_conf("/tmp/mmc-local-standalone.conf", "")
    try:
        ls = _make_ls(local_service_mode="standalone")
        ls._kv_cfg.local_config_path = "/tmp/mmc-local-inprocess.conf"
        ls.pull()

        # Verify Popen was called with sys.executable -m
        mock_popen.assert_called_once()
        call_args, call_kwargs = mock_popen.call_args
        cmd = call_args[0]
        assert cmd == [sys.executable, "-m", "motor.node_manager.core.services.memcache.worker"]
        assert ls.migration is None
        assert call_kwargs["shell"] is False
        # Env should have standalone conf path (os.path.join -> '\' on Windows)
        assert call_kwargs["env"]["MMC_LOCAL_CONFIG_PATH"] == os.path.join("/tmp", "mmc-local-standalone.conf")

        assert ls.is_alive() is True
    finally:
        _cleanup("/tmp/mmc-local-standalone.conf")


@patch("subprocess.Popen")
def test_pull_process_exits_immediately(mock_popen):
    """When the LS process exits immediately, pull() raises and marks dead."""
    mock_proc = MagicMock()
    mock_proc.poll.return_value = 1  # exited with error
    mock_popen.return_value = mock_proc

    _write_conf("/tmp/mmc-local-standalone.conf", "")
    try:
        ls = _make_ls(local_service_mode="standalone")
        ls._kv_cfg.local_config_path = "/tmp/mmc-local-inprocess.conf"
        ls.pull()

        assert ls.is_alive() is False
        assert ls.is_started() is False
    finally:
        _cleanup("/tmp/mmc-local-standalone.conf")


def test_stop_kills_process():
    """stop() sends SIGKILL to the LS process."""
    ls = _make_ls(local_service_mode="standalone")
    mock_proc = MagicMock()
    mock_proc.pid = 12345
    ls._ls_process = mock_proc

    with patch("os.kill") as mock_kill:
        ls.stop()
        mock_kill.assert_called_once_with(12345, 9)  # signal.SIGKILL


def test_stop_process_not_found():
    """stop() handles ProcessLookupError gracefully (process already dead)."""
    ls = _make_ls(local_service_mode="standalone")
    mock_proc = MagicMock()
    mock_proc.pid = 12345
    ls._ls_process = mock_proc

    with patch("os.kill", side_effect=ProcessLookupError):
        ls.stop()

    assert ls._ls_process is None


def _response(body):
    response = MagicMock()
    response.json.return_value = body
    return response


@pytest.mark.parametrize("worker_identity", [None, "123 7", "124 7", "123 9"])
def test_inventory_uses_only_the_running_worker_rank(monkeypatch, tmp_path, worker_identity):
    monkeypatch.setenv("MMC_DRAIN_API_URL", "http://meta:50090")
    monkeypatch.setenv("POD_IP", "10.0.0.4")
    monkeypatch.setenv("POD_UID", "source-uid")
    service = _make_ls(migration_enabled=True, local_service_mode="standalone")
    service._kv_cfg.local_config_path = str(tmp_path / "mmc-local-inprocess.conf")
    (tmp_path / "mmc-local-standalone.conf").touch()
    with patch("subprocess.Popen", return_value=MagicMock(pid=123, poll=MagicMock(return_value=None))) as spawn:
        service.pull()
    rank_file = Path(spawn.call_args.args[0][-1])
    if worker_identity is not None:
        rank_file.write_text(worker_identity, encoding="ascii")
    snapshot = {
        "data_source_ready": True,
        "degraded": False,
        "segments": [{"segment_name": f"rank-{rank}-dram"} for rank in (7, 8)],
    }
    with patch("requests.get", return_value=_response(snapshot)) as get:
        if worker_identity in (None, "124 7"):
            with pytest.raises(RuntimeError, match="standalone storage rank"):
                service.migration.drain_inventory()
            get.assert_not_called()
            return
        if worker_identity == "123 9":
            with pytest.raises(RuntimeError, match="no verifiable segments"):
                service.migration.drain_inventory()
            return
        assert service.migration.drain_inventory() == {
            "uid": "source-uid",
            "label": "10.0.0.4",
            "pool": "http://meta:50090",
            "ranks": [7],
        }
        get.assert_called_once_with("http://meta:50090/api/v1/capacity/segment_remaining", timeout=3)


@patch("motor.node_manager.core.services.memcache.migration.requests.post")
def test_start_drain_submits_node_labels_and_keeps_uuid(mock_post, monkeypatch):
    monkeypatch.setenv("MMC_DRAIN_API_URL", "http://meta:50090")
    monkeypatch.setenv("POD_IP", "10.0.0.4")
    mock_post.return_value = _response({"success": True, "status": "submitted", "drain_id": DRAIN_UUID})
    ls = _make_ls(migration_enabled=True).migration

    result = ls.start_drain(["10.0.0.1"], 3)

    assert result["drain_id"] == DRAIN_UUID
    assert result["state"] == "migrating"
    assert mock_post.call_args.args[0] == "http://meta:50090/api/v1/drain_jobs"
    assert mock_post.call_args.kwargs["json"] == {
        "node_label": "10.0.0.4",
        "target_node_labels": ["10.0.0.1"],
        "max_concurrency": 3,
    }


@patch("motor.node_manager.core.services.memcache.migration.requests.get")
def test_query_drain_uses_explicit_uuid_and_maps_cancelled(mock_get, monkeypatch):
    monkeypatch.setenv("MMC_DRAIN_API_URL", "http://meta:50090")
    mock_get.return_value = _response({"success": True, "status": "cancelled", "drain_id": "uuid-2"})
    ls = _make_ls(migration_enabled=True).migration

    result = ls.query_drain("uuid-2")

    assert result["state"] == "migration_cancelled"
    assert mock_get.call_args.kwargs["params"] == {"drain_id": "uuid-2"}


@pytest.mark.parametrize("counter", ["oom_keys", "cancelled_keys"])
@patch("motor.node_manager.core.services.memcache.migration.requests.get")
def test_done_with_incomplete_keys_is_not_reported_as_success(mock_get, monkeypatch, counter):
    monkeypatch.setenv("MMC_DRAIN_API_URL", "http://meta:50090")
    mock_get.return_value = _response(
        {"success": True, "status": "done", "drain_id": "uuid-3", "failed_keys": 0, counter: 2}
    )
    ls = _make_ls(migration_enabled=True).migration

    assert ls.query_drain("uuid-3")["state"] == "migration_failed"


@patch("motor.node_manager.core.services.memcache.migration.requests.post")
def test_cancel_drain_posts_explicit_uuid(mock_post, monkeypatch):
    monkeypatch.setenv("MMC_DRAIN_API_URL", "http://meta:50090")
    mock_post.return_value = _response({"success": True, "status": "cancelling", "drain_id": "uuid-4"})
    ls = _make_ls(migration_enabled=True).migration

    result = ls.cancel_drain("uuid-4")

    assert result["status"] == "cancelling"
    assert mock_post.call_args.kwargs["json"] == {"drain_id": "uuid-4"}


@pytest.mark.parametrize("operation", ["query", "cancel"])
def test_drain_rejects_a_reply_for_another_uuid(monkeypatch, operation):
    monkeypatch.setenv("MMC_DRAIN_API_URL", "http://meta")
    service = _make_ls(migration_enabled=True).migration
    reply = _response({"success": True, "drain_id": "other-job", "status": "cancelling"})
    with (
        patch.object(migration.requests, "get", return_value=reply) as get,
        patch.object(migration.requests, "post", return_value=reply) as post,
    ):
        with pytest.raises(RuntimeError, match="different drain UUID"):
            if operation == "query":
                service.query_drain("requested-job")
            else:
                service.cancel_drain("requested-job")
    assert get.call_count == int(operation == "query")
    assert post.call_count == int(operation == "cancel")


def test_parallel_submissions_share_one_ack_while_another_job_can_be_queried_and_cancelled(
    monkeypatch,
    tmp_path,
):
    uid = "source"
    monkeypatch.setenv("POD_UID", uid)
    monkeypatch.setenv("POD_IP", "10.0.0.4")
    monkeypatch.setenv("MMC_DRAIN_API_URL", "http://meta")
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    service = _make_ls(migration_enabled=True).migration
    monkeypatch.setattr(service, "drain_inventory", lambda **_: {"ranks": [4]})
    receipt_pending, release_receipt = Event(), Event()
    duplicate_started = Event()

    def post(url, *, json, timeout):
        if url.endswith("/cancel"):
            assert json == {"drain_id": "existing-job"}
            return _response({"success": True, "drain_id": "existing-job", "status": "cancelling"})
        assert url == "http://meta/api/v1/drain_jobs"
        receipt_pending.set()
        assert release_receipt.wait(10)
        return _response({"success": True, "drain_id": DRAIN_UUID})

    def submit_again():
        duplicate_started.set()
        return service.submit_directed_drain("op", uid, ["peer"], None)

    send = MagicMock(side_effect=post)
    monkeypatch.setattr(migration.requests, "post", send)
    get = MagicMock(
        side_effect=[
            _response({"success": True, "drain_id": "existing-job", "status": status})
            for status in ("running", "cancelled")
        ]
    )
    monkeypatch.setattr(migration.requests, "get", get)
    with ThreadPoolExecutor(max_workers=3) as workers:
        first = workers.submit(service.submit_directed_drain, "op", uid, ["peer"], None)
        try:
            assert receipt_pending.wait(5)
            duplicate = workers.submit(submit_again)
            assert duplicate_started.wait(5)
            assert workers.submit(service.query_drain, "existing-job").result(timeout=5)["status"] == "running"
            ack = workers.submit(service.cancel_drain, "existing-job").result(timeout=5)
            assert ack["status"] == "cancelling"
            get.assert_called_once()
        finally:
            release_receipt.set()
        assert first.result(timeout=5) == duplicate.result(timeout=5)
        assert first.result()["drain_id"] == DRAIN_UUID

    assert service.query_drain("existing-job")["status"] == "cancelled"
    assert [call.kwargs["params"] for call in get.call_args_list] == [{"drain_id": "existing-job"}] * 2
    assert [call.args[0] for call in send.call_args_list] == [
        "http://meta/api/v1/drain_jobs",
        "http://meta/api/v1/drain_jobs/cancel",
    ]


@pytest.mark.parametrize("first_outcome", ["http_rejected", "explicit_rejection", "unknown", "invalid_id"])
def test_directed_drain_retries_only_a_definite_submission_rejection(monkeypatch, tmp_path, first_outcome):
    uid = "uid-4"
    monkeypatch.setenv("POD_UID", uid)
    monkeypatch.setenv("POD_IP", "10.0.0.4")
    monkeypatch.setenv("MMC_DRAIN_API_URL", "http://meta:50090")
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    service = _make_ls(migration_enabled=True).migration
    monkeypatch.setattr(service, "drain_inventory", lambda **_: {"ranks": [4]})
    if first_outcome == "http_rejected":
        first = requests.Response()
        first.status_code = 400
        error = requests.HTTPError
    elif first_outcome == "explicit_rejection":
        first = _response({"success": False, "status": "error", "drain_id": ""})
        error = DrainSubmissionRejected
    elif first_outcome == "invalid_id":
        first = _response({"success": True, "drain_id": "not-a-uuid"})
        error = RuntimeError
    else:
        first = requests.Timeout("submission acknowledgement lost")
        error = requests.Timeout
    post = MagicMock(side_effect=[first, _response({"success": True, "drain_id": DRAIN_UUID})])
    monkeypatch.setattr(migration.requests, "post", post)

    with pytest.raises(error):
        service.submit_directed_drain("op", uid, ["peer"], None)
    if first_outcome in ("unknown", "invalid_id"):
        with pytest.raises(RuntimeError, match="unacknowledged"):
            service.submit_directed_drain("op", uid, ["peer"], None)
        post.assert_called_once()
    else:
        first_success = service.submit_directed_drain("op", uid, ["peer"], None)
        assert service.submit_directed_drain("op", uid, ["peer"], None) == first_success
        assert first_success["drain_id"] == DRAIN_UUID
        assert post.call_count == 2


@pytest.fixture
def scale_in_service(monkeypatch, tmp_path):
    # NodeManager loads its config when the transport class is first imported.
    config_file = tmp_path / "user_config.json"
    config_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("ROLE", "decode")
    monkeypatch.setenv("USER_CONFIG_PATH", str(config_file))
    from motor.node_manager.api_client import controller_api_client

    monkeypatch.setenv("POD_UID", "uid-4")
    monkeypatch.setenv("POD_IP", "source")
    monkeypatch.setenv("MMC_DRAIN_API_URL", "http://meta")
    monkeypatch.setenv("MMC_LOCAL_SERVICE_NODE_LABEL", "source")
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    service = _make_ls(migration_enabled=True, local_service_mode="standalone")
    plan = {"source_uid": "uid-4", "src_ranks": [4], "targets": ["peer"], "dst_ranks": [1]}
    service._ls_process = MagicMock(pid=123, poll=MagicMock(return_value=None))
    service._ls_rank_directory = SimpleNamespace(name=str(tmp_path))
    (tmp_path / "rank").write_text("123 4", encoding="ascii")
    transport = MagicMock()
    discovery = transport.__enter__.return_value.post
    discovery.return_value = plan

    def connect(*, address, timeout):
        assert address == "controller" and 0 < timeout <= 30
        discovery.rpc_timeout = timeout
        return transport

    monkeypatch.setattr(
        controller_api_client.ControllerApiClient, "_generate_client_args", lambda: {"address": "controller"}
    )
    monkeypatch.setattr(controller_api_client, "SafeHTTPSClient", connect)
    return service.migration, plan, discovery


@pytest.mark.parametrize(
    "native,state",
    [("done", "migration_completed"), ("failed", "migration_failed"), ("cancelled", "migration_cancelled")],
)
def test_prestop_waits_for_native_terminal_state_without_scanning_keys(monkeypatch, scale_in_service, native, state):
    service, _, discovery = scale_in_service
    post = MagicMock(return_value=_response({"success": True, "drain_id": DRAIN_UUID}))
    replies = iter([requests.Timeout("lost status"), "running", native])

    def query(url, *, params, timeout):
        assert url == "http://meta/api/v1/drain_jobs/query" and params == {"drain_id": DRAIN_UUID}
        assert 0 < timeout <= 5
        value = next(replies)
        if isinstance(value, Exception):
            raise value
        return _response({"status": value, "drain_id": DRAIN_UUID})

    monkeypatch.setattr(migration.requests, "get", query)
    monkeypatch.setattr(migration.requests, "post", post)
    result = service.prepare_local_exit("uid-4", True, 30)
    assert (result["source_uid"], result["drain_id"], result["state"]) == ("uid-4", DRAIN_UUID, state)
    discovery.assert_called_once()
    budget = discovery.rpc_timeout
    assert 10 < budget <= 30
    assert discovery.call_args.args[1] == {"source_ip": "source", "source_uid": "uid-4", "timeout_sec": budget}
    post.assert_called_once()
    assert post.call_args.kwargs["json"] == {"node_label": "source", "target_node_labels": ["peer"]}


@pytest.mark.parametrize("accepted_during_planning", [False, True])
def test_prestop_reuses_a_reconfiguration_receipt(monkeypatch, tmp_path, scale_in_service, accepted_during_planning):
    service, plan, discovery = scale_in_service
    record = tmp_path / "motor-drain-uid-4" / "operation.json"

    def save_receipt(*args):
        record.parent.mkdir()
        record.write_text(json.dumps({"response": {"drain_id": DRAIN_UUID}}), encoding="utf-8")
        return plan

    if accepted_during_planning:
        discovery.side_effect = save_receipt
    else:
        save_receipt()
    query = MagicMock(
        side_effect=[_response({"status": state, "drain_id": DRAIN_UUID}) for state in ("running", "done")]
    )
    monkeypatch.setattr(migration.requests, "get", query)
    monkeypatch.setattr(migration.requests, "post", lambda *args, **kwargs: pytest.fail("unexpected submit or cancel"))
    result = service.prepare_local_exit("uid-4", accepted_during_planning, 5)
    assert (result["drain_id"], result["state"]) == (DRAIN_UUID, "migration_completed")
    assert query.call_count == 2
    assert discovery.call_count == int(accepted_during_planning)


@pytest.mark.parametrize("completes", [True, False])
def test_prestop_preserves_source_for_entire_budget(monkeypatch, tmp_path, scale_in_service, completes):
    from motor.common.utils import kv_migration

    service, _, discovery = scale_in_service
    record = tmp_path / "motor-drain-uid-4" / "operation.json"
    record.parent.mkdir()
    record.write_text(json.dumps({"response": {"drain_id": DRAIN_UUID}}))
    now = [0.0]
    clock = SimpleNamespace(monotonic=lambda: now[0], sleep=lambda sec: now.__setitem__(0, now[0] + sec))
    monkeypatch.setattr(kv_migration, "time", clock)
    monkeypatch.setattr(migration, "time", clock)
    cancelled = Event()

    def query(drain_id, timeout):
        assert drain_id == DRAIN_UUID and 0 < timeout <= 30 - now[0]
        state = "migration_completed" if completes and now[0] >= 29.5 else "migrating"
        return {"state": state, "drain_id": drain_id}

    def cancel(drain_id, timeout):
        assert now[0] == 30 and drain_id == DRAIN_UUID and timeout == 1
        cancelled.set()

    monkeypatch.setattr(service, "query_drain", query)
    monkeypatch.setattr(service, "cancel_drain", cancel)
    if completes:
        assert service.prepare_local_exit("uid-4", False, 30)["state"] == "migration_completed"
        assert 29.5 <= now[0] < 30 and not cancelled.is_set()
    else:
        with pytest.raises(TimeoutError, match=DRAIN_UUID):
            service.prepare_local_exit("uid-4", False, 30)
        assert now[0] == 30 and cancelled.wait(timeout=2)
    discovery.assert_not_called()


def test_invalid_saved_receipt_blocks_both_paths_without_resubmitting(monkeypatch, tmp_path, scale_in_service):
    service, _, discovery = scale_in_service
    record = tmp_path / "motor-drain-uid-4" / "operation.json"
    record.parent.mkdir()
    record.write_text(json.dumps({"intent": {"operation_id": "exit-uid-4"}, "response": {"drain_id": "bad"}}))
    post = MagicMock()
    monkeypatch.setattr(migration.requests, "post", post)
    with pytest.raises(RuntimeError, match="UUID"):
        service.prepare_local_exit("uid-4", True, 5)
    with pytest.raises(RuntimeError, match="UUID"):
        service.submit_directed_drain("exit-uid-4", "uid-4", ["peer"], None)
    post.assert_not_called()
    discovery.assert_not_called()


@pytest.mark.parametrize(
    "uid,drained,changes,error",
    [
        ("another-pod", True, {}, "UID"),
        ("uid-4", False, {}, "until requests drain"),
        ("uid-4", True, {"targets": [], "dst_ranks": []}, "No verified provider exit targets"),
        ("uid-4", True, {"src_ranks": [7]}, "Source identity changed"),
    ],
)
def test_prestop_requires_source_identity_drained_requests_and_verified_targets(
    monkeypatch, scale_in_service, uid, drained, changes, error
):
    service, plan, discovery = scale_in_service
    post = MagicMock()
    monkeypatch.setattr(migration.requests, "post", post)
    plan.update(changes)
    with pytest.raises(ValueError, match=error):
        service.prepare_local_exit(uid, drained, 5)
    if not drained or uid != "uid-4":
        discovery.assert_not_called()
    post.assert_not_called()


def test_prestop_does_not_resubmit_after_acknowledgement_is_lost(monkeypatch, scale_in_service):
    service, _, discovery = scale_in_service
    post = MagicMock(side_effect=requests.Timeout("no submission response"))
    monkeypatch.setattr(migration.requests, "post", post)
    with pytest.raises(requests.Timeout, match="no submission response"):
        service.prepare_local_exit("uid-4", True, 5)
    with pytest.raises(RuntimeError, match="no acknowledged UUID"):
        service.prepare_local_exit("uid-4", True, 5)
    post.assert_called_once()
    discovery.assert_called_once()


@pytest.mark.parametrize("journal", ["none", "accepted", "unknown", "corrupt"])
def test_exit_planning_rejection_rechecks_concurrent_receipt(monkeypatch, tmp_path, scale_in_service, journal):
    from motor.node_manager.core.services.memcache.migration import DrainNotStarted

    service, _, discovery = scale_in_service
    record = tmp_path / "motor-drain-uid-4" / "operation.json"

    def no_targets(*args, **kwargs):
        if journal != "none":
            record.parent.mkdir(exist_ok=True)
            content = {"response": {"drain_id": DRAIN_UUID}} if journal == "accepted" else {"intent": {}}
            record.write_text("broken" if journal == "corrupt" else json.dumps(content), encoding="utf-8")
        return {"source_uid": "uid-4", "src_ranks": [4], "targets": [], "dst_ranks": []}

    discovery.side_effect = no_targets
    post = MagicMock()
    monkeypatch.setattr(migration.requests, "post", post)
    monkeypatch.setattr(
        migration.requests, "get", lambda *_, **__: _response({"status": "done", "drain_id": DRAIN_UUID})
    )
    if journal == "accepted":
        assert service.prepare_local_exit("uid-4", True, 1)["state"] == "migration_completed"
    else:
        expected = {"none": DrainNotStarted, "unknown": RuntimeError, "corrupt": ValueError}[journal]
        with pytest.raises(expected) as error:
            service.prepare_local_exit("uid-4", True, 1)
        assert isinstance(error.value, DrainNotStarted) == (journal == "none")
        if journal == "none":
            with pytest.raises(DrainNotStarted):
                service.prepare_local_exit("uid-4", True, 1)
            with pytest.raises(RuntimeError, match="another operation"):
                service.submit_directed_drain("exit-uid-4", "uid-4", ["peer"], None)
    post.assert_not_called()
