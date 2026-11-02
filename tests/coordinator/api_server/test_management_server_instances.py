# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Instance registration/deregistration over the management API (Controller-shaped payload).

These tests drive the real HTTP surface against a real ``InstanceManager`` (nothing inside the
request path is stubbed), so they pin the contract of ``POST /instances/refresh`` for the payload
shape that the Controller -- and any client posting full ``InsEventMsg`` instances -- uses:

* ``event=add`` puts the instance in the available pool of the requested role;
* repeating the identical ``add`` (periodic re-registration) is a no-op;
* ``event=del`` removes it, and deleting an already-absent instance is idempotent;
* ``event=set`` applies the snapshot as a diff and rejects an unmappable role atomically;
* ``event=resume`` moves the instance back and, when rejected, leaves it paused (never lost);
* ``GET /instances`` reports the role and endpoints that were registered;
* a payload Motor cannot map is rejected with 400 and **no pool mutation** (all-or-nothing),
  and the error names the offending instance id;
* the management API key, when enabled, gates both routes.

The coordinator-standalone *External Deployer* protocol (minimal ``id``/``role``/``endpoints[]``
events) is a separate, deliberately reduced schema owned by ``ExternalInsEventMsg``; it validates
and converts its own payload in ``to_internal()`` and is covered by
``tests/coordinator/core/test_external_topology.py``.
"""

import pytest
from fastapi.testclient import TestClient

from motor.coordinator.api_server.management_server import ManagementServer
from motor.coordinator.domain.instance_manager import InstanceManager, TYPE_MGMT
from motor.config.coordinator import CoordinatorConfig
from motor.common.resources.http_msg_spec import EventType
from motor.common.resources.instance import Instance
from motor.coordinator import domain as domain_pkg

# Provider-assigned instance ids, as the Controller mints them (low sequential integers).
PREFILL_INSTANCE_ID = 4097
DECODE_INSTANCE_ID = 4098


def _managed_instance(role: str, ip: str, port: str, instance_id: int, model_name: str = "Qwen3-8B") -> dict:
    """Instance payload in the Controller (``InsEventMsg``) shape.

    Full identity fields plus the nested ``{pod_ip: {endpoint_id: Endpoint}}`` endpoint map, in
    contrast to the flat ``address`` list of the External Deployer schema.
    """
    ep_id = 0
    return {
        "job_name": f"{model_name}-{role}-{ip}-{port}",
        "model_name": model_name,
        "engine_type": "vllm",
        "dispatch_capabilities": ["prefill_handoff_decode"],
        "id": instance_id,
        "role": role,
        "status": "active",
        "enable_multi_endpoints": True,
        "endpoints": {
            ip: {
                str(ep_id): {
                    "id": ep_id,
                    "ip": ip,
                    "business_port": port,
                    "status": "normal",
                    "headless": False,
                }
            }
        },
    }


class _StubControlPlaneRefresh:
    """Route /instances/refresh into the real InstanceManager without the scheduler/SHM plane."""

    def __init__(self, instance_manager: InstanceManager):
        self._instance_manager = instance_manager

    async def apply_refresh(self, event, instances):
        return await self._instance_manager.refresh_instances(event, instances)


@pytest.fixture(name="client_and_instances")
def fixture_client_and_instances(monkeypatch):
    """A management client wired to a real InstanceManager, with the KV conductor disabled."""
    monkeypatch.setattr(
        domain_pkg.instance_manager.ConductorApiClient, "register_kv_instance", staticmethod(lambda *a, **k: None)
    )
    monkeypatch.setattr(
        domain_pkg.instance_manager.ConductorApiClient, "unregister_kv_instance", staticmethod(lambda *a, **k: None)
    )

    config = CoordinatorConfig()
    mgmt = ManagementServer(config)
    instance_manager = InstanceManager(config, typename=TYPE_MGMT)
    mgmt.instance_manager = instance_manager
    mgmt._control_plane.apply_refresh = _StubControlPlaneRefresh(instance_manager).apply_refresh
    return TestClient(mgmt.management_app), mgmt, instance_manager


def _registered_ids(client) -> list[int]:
    response = client.get("/instances")
    assert response.status_code == 200
    return sorted(item["id"] for item in response.json()["instances"])


def test_add_heartbeat_deregister_round_trip(client_and_instances):
    client, _, _ = client_and_instances
    body = {
        "event": "add",
        "instances": [
            _managed_instance("prefill", "10.10.0.11", "8000", PREFILL_INSTANCE_ID),
            _managed_instance("decode", "10.10.0.12", "8000", DECODE_INSTANCE_ID),
        ],
    }

    response = client.post("/instances/refresh", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "success"
    assert response.json()["data"]["event_type"] == "add"
    assert _registered_ids(client) == [PREFILL_INSTANCE_ID, DECODE_INSTANCE_ID]

    # Periodic re-registration with the identical payload must be a no-op.
    response = client.post("/instances/refresh", json=body)
    assert response.status_code == 200, response.text
    assert _registered_ids(client) == [PREFILL_INSTANCE_ID, DECODE_INSTANCE_ID]

    # Deregistration.
    response = client.post(
        "/instances/refresh",
        json={"event": "del", "instances": [_managed_instance("prefill", "10.10.0.11", "8000", PREFILL_INSTANCE_ID)]},
    )
    assert response.status_code == 200, response.text
    assert _registered_ids(client) == [DECODE_INSTANCE_ID]

    # Deleting an already-absent instance is idempotent (safe to retry).
    response = client.post(
        "/instances/refresh",
        json={"event": "del", "instances": [_managed_instance("prefill", "10.10.0.11", "8000", PREFILL_INSTANCE_ID)]},
    )
    assert response.status_code == 200, response.text
    assert _registered_ids(client) == [DECODE_INSTANCE_ID]


def test_added_instance_is_immediately_routable(client_and_instances):
    """A refreshed instance lands in the available pool of the requested role."""
    client, _, instance_manager = client_and_instances
    client.post(
        "/instances/refresh",
        json={"event": "add", "instances": [_managed_instance("prefill", "10.10.0.11", "8000", PREFILL_INSTANCE_ID)]},
    )

    available = instance_manager.get_available_instances()
    assert list(available) == [PREFILL_INSTANCE_ID]
    instance = available[PREFILL_INSTANCE_ID]
    assert instance.role == "prefill"
    assert instance.engine_type == "vllm"
    assert instance.dispatch_capabilities == ["prefill_handoff_decode"]
    endpoints = [ep for pod in instance.endpoints.values() for ep in pod.values()]
    assert [(ep.ip, ep.business_port) for ep in endpoints] == [("10.10.0.11", "8000")]


def test_get_instances_reports_role_and_endpoints(client_and_instances):
    client, _, _ = client_and_instances
    client.post(
        "/instances/refresh",
        json={"event": "add", "instances": [_managed_instance("decode", "10.10.0.12", "8001", DECODE_INSTANCE_ID)]},
    )

    payload = client.get("/instances").json()
    assert payload["count"] == 1
    summary = payload["instances"][0]
    assert summary["id"] == DECODE_INSTANCE_ID
    assert summary["role"] == "decode"
    assert summary["model_name"] == "Qwen3-8B"
    assert summary["status"] == "active"
    assert summary["endpoints"] == [
        {"id": 0, "ip": "10.10.0.12", "business_port": "8001", "headless": False},
    ]


def test_rejected_payload_is_not_partially_applied(client_and_instances):
    """A payload Motor cannot map (unknown role) is rejected before it reaches the pools.

    Regression: the role used to be resolved while inserting into the role pool, so the valid
    entry of the same request was already registered when the invalid one raised -- the caller
    received a bare 400 while the instance was live.
    """
    client, _, instance_manager = client_and_instances
    valid = _managed_instance("prefill", "10.10.0.11", "8000", PREFILL_INSTANCE_ID)
    invalid = _managed_instance("router", "10.10.0.99", "8000", DECODE_INSTANCE_ID)

    response = client.post("/instances/refresh", json={"event": "add", "instances": [valid, invalid]})
    assert response.status_code == 400
    assert instance_manager.get_available_instances() == {}
    assert _registered_ids(client) == []


def test_set_with_invalid_role_is_not_partially_applied(client_and_instances):
    """``event=set`` resolves roles before the diff, exactly like ``add``.

    SET does not share ADD's code path: it computes a diff and then calls ``_add_instances`` on
    the added part. An unmappable role there must still reject the whole snapshot, otherwise the
    Controller's periodic snapshot leaves instances registered while the caller sees a 400.
    """
    client, _, instance_manager = client_and_instances
    valid = _managed_instance("prefill", "10.10.0.11", "8000", PREFILL_INSTANCE_ID)
    invalid = _managed_instance("router", "10.10.0.99", "8000", DECODE_INSTANCE_ID)

    response = client.post("/instances/refresh", json={"event": "set", "instances": [valid, invalid]})
    assert response.status_code == 400
    assert instance_manager.get_available_instances() == {}
    assert _registered_ids(client) == []


def test_resume_with_invalid_role_keeps_instance_paused(client_and_instances):
    """A rejected RESUME must not resume the earlier entries nor drop the offending one.

    RESUME also resolves the role (it re-inserts into a role pool), so it takes part in the same
    all-or-nothing check. Before that check existed, the valid entry was already back in the
    available pool when the invalid one raised, and the invalid one had been removed from the
    paused pool *before* its role was resolved, i.e. it was in no pool at all afterwards.
    """
    client, _, instance_manager = client_and_instances
    prefill = _managed_instance("prefill", "10.10.0.11", "8000", PREFILL_INSTANCE_ID)
    decode = _managed_instance("decode", "10.10.0.12", "8000", DECODE_INSTANCE_ID)
    assert client.post("/instances/refresh", json={"event": "add", "instances": [prefill, decode]}).status_code == 200
    assert client.post("/instances/refresh", json={"event": "pause", "instances": [prefill, decode]}).status_code == 200
    assert instance_manager.get_available_instances() == {}
    assert instance_manager.get_tracked_instance_pool(PREFILL_INSTANCE_ID) == "paused"
    assert instance_manager.get_tracked_instance_pool(DECODE_INSTANCE_ID) == "paused"

    invalid = _managed_instance("router", "10.10.0.12", "8000", DECODE_INSTANCE_ID)
    response = client.post("/instances/refresh", json={"event": "resume", "instances": [prefill, invalid]})
    assert response.status_code == 400
    assert instance_manager.get_available_instances() == {}
    assert instance_manager.get_tracked_instance_pool(PREFILL_INSTANCE_ID) == "paused"
    assert instance_manager.get_tracked_instance_pool(DECODE_INSTANCE_ID) == "paused"
    # Paused instances are still reported by GET /instances: nothing may be lost by the reject.
    assert sorted(_registered_ids(client)) == [PREFILL_INSTANCE_ID, DECODE_INSTANCE_ID]


def test_resume_returns_paused_instance_to_available_pool(client_and_instances):
    """The happy path still works: RESUME moves the instance back into its role pool."""
    client, _, instance_manager = client_and_instances
    prefill = _managed_instance("prefill", "10.10.0.11", "8000", PREFILL_INSTANCE_ID)
    assert client.post("/instances/refresh", json={"event": "add", "instances": [prefill]}).status_code == 200
    assert client.post("/instances/refresh", json={"event": "pause", "instances": [prefill]}).status_code == 200

    assert client.post("/instances/refresh", json={"event": "resume", "instances": [prefill]}).status_code == 200
    assert list(instance_manager.get_available_instances()) == [PREFILL_INSTANCE_ID]
    assert instance_manager.get_tracked_instance_pool(PREFILL_INSTANCE_ID) == "available"


def test_rejection_names_the_offending_instance(client_and_instances):
    """The 400 must identify which entry is wrong; a batch caller cannot guess the instance."""
    client, _, _ = client_and_instances
    valid = _managed_instance("prefill", "10.10.0.11", "8000", PREFILL_INSTANCE_ID)
    invalid = _managed_instance("router", "10.10.0.99", "8000", DECODE_INSTANCE_ID)

    response = client.post("/instances/refresh", json={"event": "add", "instances": [valid, invalid]})
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert str(DECODE_INSTANCE_ID) in detail
    assert "router" in detail


async def test_unsupported_role_value_is_rejected_before_apply():
    """Role values that bypass ``PDRole`` (non-strings) must fail validation, not a later 200.

    ``_role_to_pdrole`` returns anything that is not a ``str`` unchanged, so without a membership
    check such a value used to reach ``_add_instance_to_available_pool``, which only logged
    ``Unknown role`` and returned ``False`` -- the HTTP surface still answered 200.
    """
    instance_manager = InstanceManager(CoordinatorConfig(), typename=TYPE_MGMT)
    instance = Instance.model_construct(id=PREFILL_INSTANCE_ID, role=None, job_name="broken")

    with pytest.raises(ValueError, match=str(PREFILL_INSTANCE_ID)):
        await instance_manager.refresh_instances(EventType.ADD, [instance])
    assert instance_manager.get_available_instances() == {}


def test_malformed_endpoint_is_rejected(client_and_instances):
    """``business_port`` is required on an Endpoint, so a payload without it is a 400."""
    client, _, _ = client_and_instances
    instance = _managed_instance("prefill", "10.10.0.11", "8000", PREFILL_INSTANCE_ID)
    del instance["endpoints"]["10.10.0.11"]["0"]["business_port"]

    response = client.post("/instances/refresh", json={"event": "add", "instances": [instance]})
    assert response.status_code == 400
    assert _registered_ids(client) == []


def test_management_api_key_gates_registration(client_and_instances, monkeypatch):
    """When the management API key is enabled, clients must present the header."""
    client, mgmt, _ = client_and_instances
    monkeypatch.setattr(mgmt._mgmt_api_key_config, "enable_api_key", True)
    monkeypatch.setattr(mgmt, "_mgmt_api_key", "mgmt-secret")

    body = {"event": "add", "instances": [_managed_instance("prefill", "10.10.0.11", "8000", PREFILL_INSTANCE_ID)]}
    assert client.post("/instances/refresh", json=body).status_code == 401
    assert client.get("/instances").status_code == 401

    response = client.post("/instances/refresh", json=body, headers={"X-Motor-Management-Key": "mgmt-secret"})
    assert response.status_code == 200, response.text
    assert client.get("/instances", headers={"X-Motor-Management-Key": "mgmt-secret"}).json()["count"] == 1

    assert client.post("/instances/refresh", json=body, headers={"X-Motor-Management-Key": "wrong"}).status_code == 403
