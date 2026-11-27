# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Retiring instance membership and Kubernetes survivor selection for voluntary KV migration."""

import json
import os
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from motor.common.resources.instance import Instance, NodeManagerInfo


@dataclass
class ExitingInstance:
    instance: Instance
    members: dict[str, NodeManagerInfo]
    ingress: set[str]
    expires: float
    drained: set[str] = field(default_factory=set)


class ScaleInIngress:
    """Keep ingress acknowledgements for this retiring instance generation across Pod exit order."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_uid: dict[str, ExitingInstance] = {}

    def resolve(
        self,
        instance: Instance | None,
        ip: str,
        uid: str,
        headless: bool,
        deadline: float,
    ) -> tuple[ExitingInstance | None, bool]:
        now = time.monotonic()
        with self._lock:
            self._by_uid = {key: group for key, group in self._by_uid.items() if group.expires > now}
            group = self._by_uid.get(uid)
            if instance is not None:
                members = {node.pod_ip: node for node in instance.get_node_managers()}
                source = members.get(ip)
                if source is None or (source.pod_uid and source.pod_uid != uid):
                    raise ValueError("Registered source Pod UID changed")
                if group is not None and (
                    instance.id != group.instance.id
                    or any(
                        host not in group.members or node.pod_uid != group.members[host].pod_uid
                        for host, node in members.items()
                    )
                ):
                    if not headless:
                        return None, True
                    raise ValueError("Retiring instance generation changed")
                if group is None:
                    endpoints = {host: tuple(instance.get_endpoints(host).values()) for host in members}
                    if not any(ep.headless for local in endpoints.values() for ep in local):
                        if headless:
                            raise ValueError("Source is not a registered headless worker")
                        return None, True
                    if not all(endpoints.values()) or not all(node.pod_uid for node in members.values()):
                        if not headless:
                            return None, True
                        raise ValueError("Incomplete membership for cross-node scale-in")
                    ingress = {
                        members[host].pod_uid
                        for host, local in endpoints.items()
                        if any(not ep.headless for ep in local)
                    }
                    if not ingress:
                        raise ValueError("No registered inference ingress for cross-node scale-in")
                    # A later Pod can enter its hook after the first Pod finishes draining requests.
                    group = ExitingInstance(instance, members, ingress, deadline + 600)
                    self._by_uid.update({member.pod_uid: group for member in members.values()})
            if group is None or ip not in group.members or group.members[ip].pod_uid != uid:
                raise ValueError("Source is not registered in this controller")
            if headless == (uid in group.ingress):
                raise ValueError("Source ingress role changed")
        share_ack = not headless
        if share_ack and os.environ.get("KUBERNETES_SERVICE_HOST"):
            # A probe-triggered container restart retains its Pod UID and is not an instance scale-in.
            with _cluster_reader(deadline) as (read, namespace):
                pods = read(
                    f"/api/v1/namespaces/{namespace}/pods",
                    [("fieldSelector", f"status.podIP={ip}")],
                )
                share_ack = any(
                    p["metadata"]["uid"] == uid and p["metadata"].get("deletionTimestamp") for p in pods["items"]
                )
        with self._lock:
            if share_ack:
                group.drained.add(uid)
            return group, group.drained == group.ingress


_ROLE_COUNTS = {
    "prefill": "p_instances_num",
    "decode": "d_instances_num",
    "union": "hybrid_instances_num",
    "encode": "e_instances_num",
}


def _motor_config(pod_spec: dict) -> str | None:
    return next(
        (v.get("configMap", {}).get("name") for v in pod_spec.get("volumes", []) if v["name"] == "motor-config"),
        None,
    )


def _controller_ref(resource: dict) -> dict:
    return next(
        (ref for ref in resource["metadata"].get("ownerReferences", []) if ref.get("controller")),
        {},
    )


def _live_pod(pod: dict, workloads: dict[str, dict]) -> bool:
    resource = pod
    visited = set()
    while True:
        metadata = resource["metadata"]
        if metadata.get("deletionTimestamp"):
            return False
        reference = _controller_ref(resource)
        owner = reference.get("uid")
        if owner not in workloads:
            return reference.get("kind") not in ("Deployment", "StatefulSet", "ReplicaSet")
        if owner in visited:
            return False
        visited.add(owner)
        resource = workloads[owner]


def live_migration_targets(candidates: list[NodeManagerInfo], deadline: float) -> list[NodeManagerInfo]:
    """Exclude deleted or replaced Kubernetes providers without waiting for scale-in convergence."""
    if not candidates or not os.environ.get("KUBERNETES_SERVICE_HOST"):
        return candidates
    with _cluster_reader(deadline) as (read, namespace):
        apps = f"/apis/apps/v1/namespaces/{namespace}"
        workloads = {
            obj["metadata"]["uid"]: obj
            for kind in ("deployments", "statefulsets", "replicasets")
            for obj in read(f"{apps}/{kind}")["items"]
        }
        pods = read(f"/api/v1/namespaces/{namespace}/pods")["items"]
        live = {
            (pod["metadata"]["uid"], pod.get("status", {}).get("podIP")) for pod in pods if _live_pod(pod, workloads)
        }
        return [node for node in candidates if (node.pod_uid, node.pod_ip) in live]


def _survivors(read: Callable, namespace: str, uid: str, *, headless: bool = False) -> tuple[set[str], bool]:
    """Wait for actual deletions to identify retained workloads, including Pending and sparse indices."""
    core = f"/api/v1/namespaces/{namespace}"
    apps = f"/apis/apps/v1/namespaces/{namespace}"
    pods = read(core + "/pods")["items"]
    source = next((p for p in pods if p["metadata"]["uid"] == uid), None)
    if source is None or not (cm := _motor_config(source["spec"])):
        raise ValueError("Cannot identify the source Motor ConfigMap")
    if headless and not source["metadata"].get("deletionTimestamp"):
        raise ValueError("Headless Pod is not terminating")
    config = json.loads(read(f"{core}/configmaps/{cm}")["data"]["user_config.json"])
    deploy = config["motor_deploy_config"]
    desired = {}
    workloads = {}
    resources = {}
    for resource in ("deployments", "statefulsets"):
        for obj in read(f"{apps}/{resource}")["items"]:
            template = obj["spec"]["template"]["spec"]
            meta = obj["metadata"]
            resources[meta["uid"]] = obj
            if _motor_config(template) != cm or meta.get("deletionTimestamp"):
                continue
            role = next(
                (e.get("value") for c in template["containers"] for e in c.get("env", []) if e["name"] == "ROLE"),
                None,
            )
            if role not in _ROLE_COUNTS:
                continue
            owner = _controller_ref(obj)
            parent = owner["name"] if owner.get("kind") == "InstanceSet" else None
            key = (role, parent)
            if key not in desired:
                if parent and role in (config.get("scaling_policy") or {}):
                    path = f"/apis/mindcluster.huawei.com/v1/namespaces/{namespace}/instancesets/{parent}"
                    desired[key] = int(read(path)["spec"]["replicas"])
                else:
                    desired[key] = int(deploy[_ROLE_COUNTS[role]])
            workloads[meta["uid"]] = key
    counts = Counter(workloads.values())
    unsettled = {key for key, count in counts.items() if count > desired[key]}
    # A Deployment Pod is owned by a ReplicaSet; a StatefulSet owns its Pods directly.
    replicasets = read(apps + "/replicasets")["items"]
    resources.update({rs["metadata"]["uid"]: rs for rs in replicasets})
    owners = {rs["metadata"]["uid"]: _controller_ref(rs).get("uid") for rs in replicasets}
    retained = set()
    for pod in pods:
        owner_uid = _controller_ref(pod).get("uid")
        key = workloads.get(owners.get(owner_uid, owner_uid))
        if key is not None and key not in unsettled and _live_pod(pod, resources):
            retained.add(pod["metadata"]["uid"])
    return retained, bool(unsettled)


@contextmanager
def _cluster_reader(deadline: float) -> Iterator[tuple[Callable, str]]:
    from kubernetes import client, config
    from kubernetes.client.exceptions import ApiException
    from kubernetes.config.config_exception import ConfigException
    from urllib3.exceptions import HTTPError

    namespace = os.environ.get("POD_NAMESPACE")
    if not namespace:
        raise ValueError("Missing Controller namespace for Kubernetes scale-in discovery")
    configuration = client.Configuration()
    try:
        config.load_incluster_config(client_configuration=configuration)
    except ConfigException as exc:
        raise RuntimeError("Cannot initialize Kubernetes scale-in discovery") from exc
    configuration.retries = 0
    with client.ApiClient(configuration) as api:

        def read(path: str, query: list | None = None) -> dict:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("KV migration deadline reached while identifying retained workloads")
            timeout = min(2.0, remaining / 2)
            try:
                return api.call_api(
                    path,
                    "GET",
                    query_params=query,
                    response_type="object",
                    auth_settings=["BearerToken"],
                    _return_http_data_only=True,
                    _request_timeout=(timeout, timeout),
                )
            except (ApiException, HTTPError) as exc:
                raise RuntimeError(
                    f"Cannot verify Kubernetes migration membership at {path}: "
                    f"status={getattr(exc, 'status', 'unavailable')} reason={getattr(exc, 'reason', str(exc))}"
                ) from exc

        yield read, namespace


def retained_scale_in_targets(
    candidates: list[NodeManagerInfo], uid: str, deadline: float, *, headless: bool = False
) -> list[NodeManagerInfo]:
    if not candidates or not os.environ.get("KUBERNETES_SERVICE_HOST"):
        return candidates
    with _cluster_reader(deadline) as (read, namespace):
        while True:
            retained, unsettled = _survivors(read, namespace, uid, headless=headless)
            targets = [node for node in candidates if node.pod_uid in retained]
            if targets or not unsettled:
                return targets
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
