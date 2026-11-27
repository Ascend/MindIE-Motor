# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from threading import Event
from types import SimpleNamespace

import pytest

from motor.common.resources.instance import InsStatus
from motor.controller.fault_tolerance import kv_migration_service as migration
from motor.controller.fault_tolerance.kv_migration_service import evacuate_provider, plan_provider_exit


def _node(ip):
    return SimpleNamespace(pod_ip=ip, port="1026", pod_uid=None)


@pytest.fixture(autouse=True)
def isolate_kubernetes_environment(monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)


def _inventory(rank):
    return {
        "uid": "uid-%s" % rank,
        "pool": "pool",
        "label": "node-%s" % rank,
        "ranks": [rank],
    }


@pytest.mark.parametrize("moved", [1, 0])
def test_recovery_submits_once_and_consumes_memcache_capacity_outcome(monkeypatch, moved):
    calls = []
    mib = 1024 * 1024

    def rpc(node, action, payload=None, *, timeout):
        calls.append(action)
        if action == "inventory":
            return _inventory(1) if node.pod_ip == "a" else _inventory(2)
        if action == "trigger":
            assert payload == {
                "source_uid": "uid-1",
                "operation_id": "task",
                "target_node_labels": ["node-2"],
                "timeout_sec": timeout,
            }
            return {"drain_id": "job", "src_ranks": [1], "dst_ranks": [2]}
        assert action == "status" and payload == {"drain_id": "job"}
        return {
            "status": "done",
            "state": "migration_failed",
            "moved_bytes": moved * 12 * mib,
            "successful_keys": moved,
            "oom_keys": 2 - moved,
        }

    monkeypatch.setattr(
        "motor.controller.fault_tolerance.kv_migration_service.NodeManagerApiClient.migration_request", rpc
    )
    report = evacuate_provider(_node("a"), [_node("b")], "task", migration.time.monotonic() + 1)
    assert report["drain_id"] == "job" and report["status"] == "done"
    assert report["successful_keys"] == moved and report["oom_keys"] == 2 - moved
    assert report["moved_bytes"] == moved * 12 * mib
    assert calls == ["inventory", "inventory", "trigger", "status"]


def test_recovery_without_an_eligible_peer_never_submits_an_undirected_job(monkeypatch):
    calls = []

    def rpc(node, action, payload=None, *, timeout):
        calls.append(action)
        return _inventory(1)

    monkeypatch.setattr(
        "motor.controller.fault_tolerance.kv_migration_service.NodeManagerApiClient.migration_request", rpc
    )
    with pytest.raises(ValueError, match="No eligible target"):
        evacuate_provider(_node("a"), [_node("a")], "task", migration.time.monotonic() + 1)
    assert calls == ["inventory"]


@pytest.mark.parametrize(
    ("status", "state", "counts"),
    [
        ("done", "migration_completed", {"total_keys": 5, "successful_keys": 5, "moved_bytes": 60}),
        ("failed", "migration_failed", {"total_keys": 2, "failed_keys": 2, "moved_bytes": 0}),
        ("cancelled", "migration_cancelled", {"successful_keys": 1, "cancelled_keys": 2, "moved_bytes": 12}),
    ],
)
def test_recovery_records_native_query_without_per_key_verification(monkeypatch, status, state, counts):
    calls = []
    terminal = {"drain_id": "job", "status": status, "state": state, **counts}
    replies = iter([{"state": "migrating"}, terminal])

    def rpc(node, action, payload=None, *, timeout):
        calls.append(action)
        if action == "inventory":
            return _inventory(1) if node.pod_ip == "a" else _inventory(2)
        if action == "trigger":
            return {"drain_id": "job"}
        assert action == "status" and payload == {"drain_id": "job"}
        return next(replies)

    monkeypatch.setattr(
        "motor.controller.fault_tolerance.kv_migration_service.NodeManagerApiClient.migration_request", rpc
    )
    result = evacuate_provider(_node("a"), [_node("b")], "task", migration.time.monotonic() + 3)
    assert result == {**terminal, "source_uid": "uid-1"}
    assert calls == ["inventory", "inventory", "trigger", "status", "status"]


def _instance(i, ip):
    return SimpleNamespace(
        id=i,
        job_name=ip,
        model_name="model",
        engine_type="vllm",
        parallel_config=None,
        gathered_workload=SimpleNamespace(active_tokens=i),
        get_node_managers=lambda: [_node(ip)],
    )


def test_exit_plan_excludes_replacement_and_a_target_after_its_failure(monkeypatch):
    source = _instance(1, "a")
    same_job = _instance(4, "replacement")
    same_job.job_name = source.job_name
    target = _instance(2, "b")
    target.status, same_job.status = InsStatus.ACTIVE, InsStatus.ACTIVE

    def active_peers(statuses):
        return [instance for instance in (target, same_job) if instance.status in statuses]

    manager = SimpleNamespace(get_instance_by_podip=lambda _: source, get_instances=active_peers)

    def rpc(node, action, *, timeout):
        assert action == "inventory"
        assert node.pod_ip != "replacement"
        return _inventory(1) if node.pod_ip == "a" else _inventory(2)

    monkeypatch.setattr(
        "motor.controller.fault_tolerance.kv_migration_service.NodeManagerApiClient.migration_request", rpc
    )
    assert plan_provider_exit(manager, "a", "uid-1") == {
        "source_uid": "uid-1",
        "src_ranks": [1],
        "targets": ["node-2"],
        "dst_ranks": [2],
    }
    target.status = InsStatus.INACTIVE
    assert plan_provider_exit(manager, "a", "uid-1")["targets"] == []


@pytest.mark.parametrize("key,bad", [("uid", None), ("pool", ""), ("label", []), ("ranks", [True])])
def test_exit_plan_skips_malformed_peer_but_rejects_malformed_source(monkeypatch, key, bad):
    source = _instance(1, "src")
    peers = [_instance(2, "bad"), _instance(3, "good")]
    manager = SimpleNamespace(get_instance_by_podip=lambda _: source, get_instances=lambda _: peers)
    inventories = {ip: _inventory(rank) for rank, ip in enumerate(("src", "bad", "good"))}
    del inventories["bad"][key]
    monkeypatch.setattr(
        migration.NodeManagerApiClient, "migration_request", lambda node, _, **kw: inventories[node.pod_ip]
    )
    assert plan_provider_exit(manager, "src", "uid-0")["targets"] == ["node-2"]
    inventories["src"][key] = bad
    with pytest.raises(ValueError, match=key):
        plan_provider_exit(manager, "src", "uid-0")


def test_exit_plan_rejects_source_missing_from_node_managers():
    manager = SimpleNamespace(get_instance_by_podip=lambda _: _instance(1, "peer"))
    with pytest.raises(ValueError, match="Source pod src has no registered NodeManager"):
        plan_provider_exit(manager, "src", "uid-1")


@pytest.mark.parametrize("budget", [3, 30])
def test_exit_plan_keeps_time_for_a_healthy_peer_after_a_slow_one(monkeypatch, budget):
    source = _instance(1, "src")
    peers = [_instance(2, "slow"), _instance(3, "healthy")]
    manager = SimpleNamespace(get_instance_by_podip=lambda _: source, get_instances=lambda _: peers)
    now = [100.0]
    deadline = now[0] + budget
    monkeypatch.setattr(migration, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def inventory(node, action, *, timeout):
        assert action == "inventory" and 0 < timeout <= deadline - now[0]
        if node.pod_ip == "slow":
            now[0] += timeout
            raise TimeoutError("inventory timed out")
        now[0] += 0.2
        return _inventory(1 if node.pod_ip == "src" else 3)

    monkeypatch.setattr(migration.NodeManagerApiClient, "migration_request", inventory)
    plan = plan_provider_exit(manager, "src", "uid-1", deadline=deadline)
    assert plan["targets"] == ["node-3"] and plan["dst_ranks"] == [3]
    assert now[0] < deadline
    if budget == 30:
        assert now[0] > 110


@pytest.mark.parametrize("pool", ["pool", "other"])
def test_exit_plan_requires_the_same_storage_pool(monkeypatch, pool):
    source = _instance(1, "source")
    target = _instance(2, "target")
    manager = SimpleNamespace(get_instance_by_podip=lambda _: source, get_instances=lambda _: [target])
    inventories = {"source": _inventory(1), "target": {**_inventory(2), "pool": pool}}
    monkeypatch.setattr(
        migration.NodeManagerApiClient, "migration_request", lambda node, _, **kw: inventories[node.pod_ip]
    )

    plan = plan_provider_exit(manager, "source", "uid-1")
    assert plan["targets"] == (["node-2"] if pool == "pool" else [])
    assert plan["dst_ranks"] == ([2] if pool == "pool" else [])


def test_instance_migrates_sources_concurrently_with_one_deadline(monkeypatch):
    monkeypatch.setattr(migration, "time", SimpleNamespace(monotonic=lambda: 10.0))
    instance = _instance(1, "source")
    slow, fast = _node("a"), _node("b")
    slow.pod_uid, fast.pod_uid = "uid-a", "uid-b"
    instance.get_node_managers = lambda: [_node("unregistered"), slow, fast]
    manager = SimpleNamespace(get_instances=lambda _: [_instance(2, "target")], is_instance_separated=lambda _: False)
    fast_finished = Event()
    slow_saw_fast_finish = Event()
    attempts = {}

    def submit(source, targets, operation, deadline, uid, recheck):
        attempts[source.pod_ip] = (deadline, operation, uid, [dest.pod_ip for dest in targets])
        if source is slow:
            if fast_finished.wait(timeout=2):
                slow_saw_fast_finish.set()
            raise ConnectionError("source unavailable")
        fast_finished.set()
        return {"state": "migration_completed"}

    monkeypatch.setattr(migration, "evacuate_provider", submit)
    migration.evacuate_instance(manager, instance, timeout=30)

    assert slow_saw_fast_finish.is_set()
    assert attempts == {
        "a": (40, "exit-uid-a", "uid-a", ["target"]),
        "b": (40, "exit-uid-b", "uid-b", ["target"]),
    }


def test_instance_returns_at_its_deadline_when_a_source_rpc_is_still_blocked(monkeypatch, caplog):
    instance = _instance(1, "source")
    member = _node("source")
    member.pod_uid = "uid-source"
    instance.get_node_managers = lambda: [member]
    manager = SimpleNamespace(get_instances=lambda _: [_instance(2, "target")], is_instance_separated=lambda _: False)
    started, release, finished = Event(), Event(), Event()

    def submit(*args):
        started.set()
        release.wait(timeout=3)
        finished.set()
        return {"state": "migration_completed"}

    monkeypatch.setattr(migration, "evacuate_provider", submit)
    try:
        migration.evacuate_instance(manager, instance, timeout=0.25)
        assert started.is_set() and not finished.is_set()
        assert "1 sources pending" in caplog.text
    finally:
        release.set()
        assert finished.wait(timeout=2)


def test_expired_poll_cancels_only_acknowledged_uuid_without_blocking_exit(monkeypatch):
    from motor.common.utils import kv_migration

    now = [0.0]
    clock = SimpleNamespace(monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(migration, "time", clock)
    monkeypatch.setattr(kv_migration, "time", clock)
    calls = []
    cancelled = Event()

    def rpc(node, action, payload, *, timeout):
        if action == "cancel":
            assert now[0] == 2 and timeout == 1 and payload == {"drain_id": "job"}
            cancelled.set()
            return {"state": "migrating"}
        assert 0 < timeout <= 2 - now[0]
        calls.append(action)
        if action == "inventory":
            return _inventory(1 if node.pod_ip == "src" else 2)
        if action == "trigger":
            return {"drain_id": "job"}
        assert payload == {"drain_id": "job"}
        return {"state": "migrating"}

    monkeypatch.setattr(migration.NodeManagerApiClient, "migration_request", rpc)
    with pytest.raises(TimeoutError):
        evacuate_provider(_node("src"), [_node("dest")], "exit-uid-1", 2, "uid-1")
    assert cancelled.wait(timeout=2) and now[0] == 2
    assert calls.count("trigger") == 1 and calls.count("status") > 0


def test_reused_target_ip_is_excluded_and_reused_source_ip_rejects_planning(monkeypatch):
    source, target = _node("src"), _node("target")
    source.pod_uid, target.pod_uid = "uid-1", "gone"
    instance, peer = _instance(1, "source"), _instance(2, "target")
    instance.get_node_managers, peer.get_node_managers = lambda: [source], lambda: [target]
    manager = SimpleNamespace(get_instance_by_podip=lambda _: instance, get_instances=lambda _: [peer])
    monkeypatch.setattr(
        migration.NodeManagerApiClient,
        "migration_request",
        lambda node, _, **kw: _inventory(1 if node is source else 2),
    )
    assert plan_provider_exit(manager, "src", "uid-1")["targets"] == []
    with pytest.raises(ValueError, match="UID changed"):
        plan_provider_exit(manager, "src", "new")


@pytest.mark.parametrize("deleting", ["none", "pod", "replicaset", "deployment", "replaced", "missing"])
def test_recovery_filters_terminating_kubernetes_targets(monkeypatch, deleting):
    from contextlib import nullcontext
    from motor.controller.fault_tolerance import scale_in_migration as scaling

    node = SimpleNamespace(pod_ip="target", pod_uid="uid-target")

    def resource(uid, kind=None, parent=None):
        metadata = {"uid": uid}
        if parent:
            metadata["ownerReferences"] = [{"controller": True, "uid": parent, "kind": kind}]
        return {"metadata": metadata}

    deployment = resource("deployment")
    replicaset = resource("replicaset", "Deployment", "deployment")
    pod = resource("uid-target", "ReplicaSet", "replicaset")
    pod["status"] = {"podIP": "target"}
    objects = {"pod": pod, "replicaset": replicaset, "deployment": deployment}
    if deleting in objects:
        objects[deleting]["metadata"]["deletionTimestamp"] = "now"
    if deleting == "replaced":
        pod["metadata"]["uid"] = "new-uid"
    data = {
        "pods": [pod],
        "replicasets": [replicaset],
        "deployments": [] if deleting == "missing" else [deployment],
        "statefulsets": [],
    }

    def read(path):
        return {"items": data[path.rsplit("/", 1)[1]]}

    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "test")
    monkeypatch.setattr(scaling, "_cluster_reader", lambda _: nullcontext((read, "ns")))
    assert scaling.live_migration_targets([node], migration.time.monotonic() + 10) == (
        [node] if deleting == "none" else []
    )


@pytest.mark.parametrize("status", [403, 500])
def test_kubernetes_migration_error_preserves_status_and_path(monkeypatch, status):
    from unittest.mock import MagicMock
    from kubernetes import client, config
    from kubernetes.client.exceptions import ApiException
    from motor.controller.fault_tolerance import scale_in_migration as scaling

    monkeypatch.setenv("POD_NAMESPACE", "ns")
    monkeypatch.setattr(config, "load_incluster_config", lambda **_: None)
    api = MagicMock()
    api.call_api.side_effect = ApiException(status=status, reason="test failure")
    context = MagicMock()
    context.__enter__.return_value = api
    monkeypatch.setattr(client, "ApiClient", lambda *_: context)
    with scaling._cluster_reader(migration.time.monotonic() + 10) as (read, _):
        with pytest.raises(RuntimeError, match=f"/test.*status={status}.*test failure"):
            read("/test")


def test_recovery_skips_submission_when_kubernetes_verification_fails(monkeypatch):
    calls = []
    monkeypatch.setattr(
        migration,
        "live_migration_targets",
        lambda *_: (_ for _ in ()).throw(RuntimeError("status=403 reason=Forbidden")),
    )
    monkeypatch.setattr(migration.NodeManagerApiClient, "migration_request", lambda *a, **k: calls.append(a))
    with pytest.raises(RuntimeError, match="status=403"):
        evacuate_provider(_node("source"), [_node("peer")], "exit-uid", migration.time.monotonic() + 10)
    assert calls == []
