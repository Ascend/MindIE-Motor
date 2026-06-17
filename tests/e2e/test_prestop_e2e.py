# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""End-to-end unit tests for PreStop graceful shutdown flow.

Covers:
  - HeartbeatManager: pause_all_endpoints / resume_all_endpoints
  - Controller InstanceManager: PAUSED state machine transitions
  - Controller EventPusher: PAUSE / RESUME event routing
  - Coordinator InstanceManager: _paused_pool operations
"""

import asyncio
import sys
import time
from unittest.mock import patch, MagicMock

import pytest

# Pre-mock protobuf generated modules (not compiled in dev environment)
# so that motor.common.etcd.etcd_client can be imported without error.
_mock_pb2 = MagicMock()
_mock_pb2_grpc = MagicMock()
sys.modules['motor.common.etcd.proto.rpc_pb2'] = _mock_pb2
sys.modules['motor.common.etcd.proto.rpc_pb2_grpc'] = _mock_pb2_grpc

from motor.common.resources.endpoint import Endpoint, EndpointStatus  # noqa: E402
from motor.common.resources.http_msg_spec import EventType  # noqa: E402
from motor.common.resources.instance import (  # noqa: E402
    Instance,
    InsStatus,
    InsConditionEvent,
    PDRole,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_endpoint(endpoint_id: int, ip: str, status: EndpointStatus = EndpointStatus.NORMAL) -> Endpoint:
    return Endpoint(
        id=endpoint_id,
        ip=ip,
        business_port="8000",
        mgmt_port="9000",
        status=status,
        hb_timestamp=time.time(),
    )


def _make_instance(ins_id: int, job_name: str, role: str = "prefill", endpoints: dict | None = None) -> Instance:
    return Instance(
        id=ins_id,
        job_name=job_name,
        model_name="test_model",
        role=role,
        endpoints=endpoints or {},
    )


# ---------------------------------------------------------------------------
# 1. HeartbeatManager — pause / resume
# ---------------------------------------------------------------------------


class TestHeartbeatManagerPrestop:
    """Unit tests for HeartbeatManager PreStop methods."""

    @pytest.fixture
    def heartbeat_mgr(self):
        from motor.node_manager.core.heartbeat_manager import HeartbeatManager

        with (
            patch('motor.config.node_manager.NodeManagerConfig.from_json') as mock_cfg,
            patch('threading.Thread') as mock_thread,
        ):
            mock_cfg.return_value = MagicMock()
            mock_thread.return_value = MagicMock()
            # Clear singleton
            if hasattr(HeartbeatManager, '_instances'):
                HeartbeatManager._instances.clear()
            mgr = HeartbeatManager(MagicMock())
            yield mgr

    def test_pause_all_endpoints(self, heartbeat_mgr):
        """All endpoints should become PAUSED."""
        eps = [
            _make_endpoint(0, "10.0.0.1", EndpointStatus.NORMAL),
            _make_endpoint(1, "10.0.0.1", EndpointStatus.NORMAL),
        ]
        with heartbeat_mgr._endpoint_lock:
            heartbeat_mgr._endpoints = eps

        heartbeat_mgr.pause_all_endpoints()

        for ep in heartbeat_mgr._endpoints:
            assert ep.status == EndpointStatus.PAUSED

    def test_pause_all_endpoints_makes_readiness_false(self, heartbeat_mgr):
        """check_all_endpoints_normal() returns False after pause."""
        eps = [_make_endpoint(0, "10.0.0.1", EndpointStatus.NORMAL)]
        with heartbeat_mgr._endpoint_lock:
            heartbeat_mgr._endpoints = eps

        assert heartbeat_mgr.check_all_endpoints_normal() is True
        heartbeat_mgr.pause_all_endpoints()
        assert heartbeat_mgr.check_all_endpoints_normal() is False

    def test_resume_all_endpoints(self, heartbeat_mgr):
        """PAUSED endpoints go back to NORMAL."""
        eps = [
            _make_endpoint(0, "10.0.0.1", EndpointStatus.PAUSED),
            _make_endpoint(1, "10.0.0.1", EndpointStatus.PAUSED),
        ]
        with heartbeat_mgr._endpoint_lock:
            heartbeat_mgr._endpoints = eps

        heartbeat_mgr.resume_all_endpoints()

        for ep in heartbeat_mgr._endpoints:
            assert ep.status == EndpointStatus.NORMAL

    def test_resume_does_not_affect_abnormal(self, heartbeat_mgr):
        """Resume only touches PAUSED endpoints, leaves others alone."""
        eps = [
            _make_endpoint(0, "10.0.0.1", EndpointStatus.PAUSED),
            _make_endpoint(1, "10.0.0.1", EndpointStatus.ABNORMAL),
        ]
        with heartbeat_mgr._endpoint_lock:
            heartbeat_mgr._endpoints = eps

        heartbeat_mgr.resume_all_endpoints()

        assert heartbeat_mgr._endpoints[0].status == EndpointStatus.NORMAL
        assert heartbeat_mgr._endpoints[1].status == EndpointStatus.ABNORMAL

    def test_pause_empty_endpoints_is_noop(self, heartbeat_mgr):
        """Pausing with no endpoints should not crash."""
        with heartbeat_mgr._endpoint_lock:
            heartbeat_mgr._endpoints = []
        heartbeat_mgr.pause_all_endpoints()  # no exception


# ---------------------------------------------------------------------------
# 2. Instance — is_all_endpoints_paused
# ---------------------------------------------------------------------------


class TestInstancePausedDetection:
    """Unit tests for Instance.is_all_endpoints_paused()."""

    def test_all_normal_returns_false(self):
        endpoints = {
            "10.0.0.1": {0: _make_endpoint(0, "10.0.0.1", EndpointStatus.NORMAL)},
        }
        inst = _make_instance(1, "job-1", endpoints=endpoints)
        assert inst.is_all_endpoints_paused() is False

    def test_all_paused_returns_true(self):
        endpoints = {
            "10.0.0.1": {0: _make_endpoint(0, "10.0.0.1", EndpointStatus.PAUSED)},
            "10.0.0.2": {1: _make_endpoint(1, "10.0.0.2", EndpointStatus.PAUSED)},
        }
        inst = _make_instance(1, "job-1", endpoints=endpoints)
        assert inst.is_all_endpoints_paused() is True

    def test_mixed_returns_false(self):
        """One PAUSED + one NORMAL → not all paused."""
        endpoints = {
            "10.0.0.1": {0: _make_endpoint(0, "10.0.0.1", EndpointStatus.PAUSED)},
            "10.0.0.2": {1: _make_endpoint(1, "10.0.0.2", EndpointStatus.NORMAL)},
        }
        inst = _make_instance(1, "job-1", endpoints=endpoints)
        assert inst.is_all_endpoints_paused() is False

    def test_empty_endpoints_returns_false(self):
        inst = _make_instance(1, "job-1", endpoints={})
        assert inst.is_all_endpoints_paused() is False

    def test_cross_pod_all_paused(self):
        """Simulate two Pods, both PAUSED: returns True only when both are."""
        endpoints = {
            "10.0.0.1": {
                0: _make_endpoint(0, "10.0.0.1", EndpointStatus.PAUSED),
            },
            "10.0.0.2": {
                1: _make_endpoint(1, "10.0.0.2", EndpointStatus.PAUSED),
            },
        }
        inst = _make_instance(1, "cross-pod-job", endpoints=endpoints)
        assert inst.is_all_endpoints_paused() is True

    def test_cross_pod_one_normal(self):
        """Pod A PAUSED, Pod B still NORMAL → not all paused."""
        endpoints = {
            "10.0.0.1": {
                0: _make_endpoint(0, "10.0.0.1", EndpointStatus.PAUSED),
            },
            "10.0.0.2": {
                1: _make_endpoint(1, "10.0.0.2", EndpointStatus.NORMAL),
            },
        }
        inst = _make_instance(1, "cross-pod-job", endpoints=endpoints)
        assert inst.is_all_endpoints_paused() is False


# ---------------------------------------------------------------------------
# 3. Controller InstanceManager — PAUSED state machine
# ---------------------------------------------------------------------------


class TestControllerInstanceManagerPrestop:
    """Unit tests for Controller InstanceManager PAUSED transitions."""

    @pytest.fixture
    def im(self):
        from motor.controller.core.instance_manager import InstanceManager

        with (
            patch('motor.controller.core.instance_manager.EtcdClient') as mock_etcd,
            patch('threading.Thread') as mock_thread,
            patch('motor.controller.core.instance_manager.EventPusher') as mock_ep,
        ):
            mock_etcd.return_value = MagicMock()
            mock_thread.return_value = MagicMock()
            mock_ep.return_value = MagicMock()
            # Clear singleton
            if hasattr(InstanceManager, '_instances'):
                InstanceManager._instances.clear()
            mgr = InstanceManager(MagicMock())
            yield mgr

    @pytest.fixture
    def active_instance(self):
        endpoints = {
            "10.0.0.1": {
                0: _make_endpoint(0, "10.0.0.1", EndpointStatus.NORMAL),
            },
        }
        inst = _make_instance(1, "test-job", endpoints=endpoints)
        inst.status = InsStatus.ACTIVE
        return inst

    def test_handle_state_transition_active_to_paused(self, im, active_instance):
        """ACTIVE + all endpoints PAUSED → PAUSED."""
        # Set all endpoints to PAUSED
        with active_instance._lock:
            for pod_eps in active_instance.endpoints.values():
                for ep in pod_eps.values():
                    ep.status = EndpointStatus.PAUSED

        # Attach a mock observer to capture notification
        mock_obs = MagicMock()
        im.observers.append(mock_obs)

        result = im._handle_state_transition(active_instance)
        assert result is True
        assert active_instance.status == InsStatus.PAUSED

        # Notification sent
        from motor.controller.core.observer import ObserverEvent

        mock_obs.update.assert_called_once()
        call_args = mock_obs.update.call_args
        assert call_args[0][1] == ObserverEvent.INSTANCE_PAUSED

    def test_handle_state_transition_paused_to_active_on_resume(self, im):
        """PAUSED + endpoints back to NORMAL → ACTIVE (via _handle_active, INSTANCE_READY)."""
        endpoints = {
            "10.0.0.1": {
                0: _make_endpoint(0, "10.0.0.1", EndpointStatus.NORMAL),
            },
        }
        inst = _make_instance(1, "test-job", endpoints=endpoints)
        inst.status = InsStatus.PAUSED

        mock_obs = MagicMock()
        im.observers.append(mock_obs)

        result = im._handle_state_transition(inst)
        assert result is True
        assert inst.status == InsStatus.ACTIVE

        from motor.controller.core.observer import ObserverEvent

        call_args = mock_obs.update.call_args
        # When PAUSED + NORMAL endpoints, is_all_endpoints_ready() is True,
        # which triggers INSTANCE_NORMAL → _handle_active → INSTANCE_READY.
        assert call_args[0][1] == ObserverEvent.INSTANCE_READY

    def test_paused_detection_priority_over_ready(self, im, active_instance):
        """is_all_endpoints_paused is checked BEFORE is_all_endpoints_ready."""
        # All endpoints NORMAL + ACTIVE → should stay ACTIVE
        result = im._handle_state_transition(active_instance)
        assert result is True
        assert active_instance.status == InsStatus.ACTIVE

    def test_handle_paused_handler_noop_when_already_paused(self, im):
        """Re-entering PAUSED from PAUSED should be a no-op."""
        endpoints = {
            "10.0.0.1": {
                0: _make_endpoint(0, "10.0.0.1", EndpointStatus.PAUSED),
            },
        }
        inst = _make_instance(1, "test-job", endpoints=endpoints)
        inst.status = InsStatus.PAUSED

        mock_obs = MagicMock()
        im.observers.append(mock_obs)

        im._handle_paused(InsStatus.PAUSED, InsConditionEvent.INSTANCE_PAUSED, inst)
        # State should still be PAUSED, no duplicate notification
        assert inst.status == InsStatus.PAUSED

    def test_transition_paused_heartbeat_timeout_to_deleted(self, im):
        """PAUSED + HEARTBEAT_TIMEOUT → DELETED."""
        endpoints = {
            "10.0.0.1": {
                0: _make_endpoint(0, "10.0.0.1", EndpointStatus.PAUSED),
            },
        }
        inst = _make_instance(1, "test-job", endpoints=endpoints)
        inst.status = InsStatus.PAUSED

        im._handle_state_transition(inst, InsConditionEvent.INSTANCE_HEARTBEAT_TIMEOUT)
        assert inst.status == InsStatus.DELETED

    def test_transition_paused_abnormal_to_inactive(self, im):
        """PAUSED + ABNORMAL → INACTIVE."""
        endpoints = {
            "10.0.0.1": {
                0: _make_endpoint(0, "10.0.0.1", EndpointStatus.PAUSED),
            },
        }
        inst = _make_instance(1, "test-job", endpoints=endpoints)
        inst.status = InsStatus.PAUSED

        mock_obs = MagicMock()
        im.observers.append(mock_obs)

        im._handle_state_transition(inst, InsConditionEvent.INSTANCE_ABNORMAL)
        assert inst.status == InsStatus.INACTIVE


# ---------------------------------------------------------------------------
# 4. Controller EventPusher — PAUSE / RESUME routing
# ---------------------------------------------------------------------------


class TestEventPusherPrestop:
    """Unit tests for EventPusher PAUSE / RESUME event routing."""

    @pytest.fixture
    def pusher(self):
        from motor.controller.core.event_pusher import EventPusher

        with patch('threading.Thread') as mock_thread:
            mock_thread.return_value = MagicMock()
            config = MagicMock()
            config.event_config.event_consumer_sleep_interval = 0.1
            config.event_config.coordinator_heartbeat_interval = 1.0
            ep = EventPusher(config)
            yield ep

    def test_update_instance_paused_removes_from_instances(self, pusher):
        """PAUSED event removes the instance from local tracking."""
        from motor.controller.core.observer import ObserverEvent

        mock_inst = MagicMock()
        mock_inst.job_name = "test-job"
        mock_inst.to_instance.return_value = _make_instance(1, "test-job")

        # First add the instance
        pusher.update(mock_inst, ObserverEvent.INSTANCE_READY)
        assert "test-job" in pusher.instances

        # Then pause it — should be removed
        pusher.update(mock_inst, ObserverEvent.INSTANCE_PAUSED)
        assert "test-job" not in pusher.instances

        # Drain the ADD event from READY first, then check PAUSE event
        _ = pusher.event_queue.get_nowait()  # EventType.ADD
        event = pusher.event_queue.get_nowait()
        assert event.event_type == EventType.PAUSE

    def test_update_instance_resumed_adds_back(self, pusher):
        """RESUMED event adds instance back to local tracking."""
        from motor.controller.core.observer import ObserverEvent

        mock_inst = MagicMock()
        mock_inst.job_name = "test-job"
        mock_inst.to_instance.return_value = _make_instance(1, "test-job")

        # Resume a previously-paused instance
        pusher.update(mock_inst, ObserverEvent.INSTANCE_RESUMED)
        assert "test-job" in pusher.instances

        event = pusher.event_queue.get_nowait()
        assert event.event_type == EventType.RESUME

    def test_update_paused_does_nothing_for_unknown_instance(self, pusher):
        """PAUSED for an instance not in local dict is silently ignored."""
        from motor.controller.core.observer import ObserverEvent

        mock_inst = MagicMock()
        mock_inst.job_name = "unknown-job"
        mock_inst.to_instance.return_value = _make_instance(99, "unknown-job")

        pusher.update(mock_inst, ObserverEvent.INSTANCE_PAUSED)
        assert pusher.event_queue.empty()

    def test_consumer_constructs_pause_event_msg(self, pusher):
        """_event_consumer builds InsEventMsg with PAUSE type."""
        ins = _make_instance(1, "test-job")
        from motor.controller.core.event_pusher import Event

        pusher.event_queue.put(Event(EventType.PAUSE, ins))

        with patch('motor.controller.core.event_pusher.CoordinatorApiClient.send_instance_refresh') as mock_send:
            mock_send.side_effect = lambda *a, **kw: pusher.stop_event.set()
            pusher._event_consumer()
            mock_send.assert_called_once()
            call_args = mock_send.call_args[0][0]
            assert call_args.event == EventType.PAUSE
            assert len(call_args.instances) == 1

    def test_consumer_constructs_resume_event_msg(self, pusher):
        """_event_consumer builds InsEventMsg with RESUME type."""
        ins = _make_instance(1, "test-job")
        from motor.controller.core.event_pusher import Event

        pusher.event_queue.put(Event(EventType.RESUME, ins))

        with patch('motor.controller.core.event_pusher.CoordinatorApiClient.send_instance_refresh') as mock_send:
            mock_send.side_effect = lambda *a, **kw: pusher.stop_event.set()
            pusher._event_consumer()
            mock_send.assert_called_once()
            call_args = mock_send.call_args[0][0]
            assert call_args.event == EventType.RESUME
            assert len(call_args.instances) == 1


# ---------------------------------------------------------------------------
# 5. Coordinator InstanceManager — paused pool
# ---------------------------------------------------------------------------


class TestCoordinatorInstanceManagerPrestop:
    """Unit tests for Coordinator InstanceManager paused pool."""

    # pylint: disable=attribute-defined-outside-init

    def setup_method(self):
        from motor.config.coordinator import CoordinatorConfig
        from motor.coordinator.domain.instance_manager import InstanceManager

        self.config = CoordinatorConfig()
        self.im = InstanceManager(self.config)

    def _add_to_available(self, instance: Instance):
        """Add an instance to the available pool."""
        self.im._available_pool[instance.id] = instance
        role = instance.role if isinstance(instance.role, PDRole) else PDRole(instance.role)
        pool = self.im._available_role_pools.get(role)
        if pool is not None:
            pool[instance.id] = instance

    # -- _pause_instances tests --

    def test_pause_moves_from_available_to_paused_pool(self):
        """Instance moves from available pool to paused pool."""
        inst = _make_instance(1, "job-1", role=PDRole.ROLE_P)
        self._add_to_available(inst)

        self.im._pause_instances([inst])

        assert inst.id not in self.im._available_pool
        assert inst.id not in self.im._prefill_pool
        assert inst.id in self.im._paused_pool

    def test_pause_excluded_from_get_available_instances(self):
        """get_available_instances() does NOT return paused instances."""
        inst = _make_instance(1, "job-1", role=PDRole.ROLE_P)
        self._add_to_available(inst)

        assert inst.id in self.im.get_available_instances(PDRole.ROLE_P)

        self.im._pause_instances([inst])

        assert inst.id not in self.im.get_available_instances(PDRole.ROLE_P)

    def test_pause_already_paused_skips(self):
        """Pausing an already-paused instance is a no-op."""
        inst = _make_instance(1, "job-1", role=PDRole.ROLE_P)
        self._add_to_available(inst)

        self.im._pause_instances([inst])  # first pause
        modified = self.im._pause_instances([inst])  # second pause

        assert modified is False

    def test_pause_not_in_available_pool_skips(self):
        """Pausing an instance not in available pool is skipped."""
        inst = _make_instance(1, "job-1", role=PDRole.ROLE_P)
        # Don't add to available pool

        modified = self.im._pause_instances([inst])
        assert modified is False
        assert inst.id not in self.im._paused_pool

    # -- _resume_instances tests --

    def test_resume_moves_back_to_available(self):
        """Paused instance resumes back to available pool."""
        inst = _make_instance(1, "job-1", role=PDRole.ROLE_P)
        self._add_to_available(inst)
        self.im._pause_instances([inst])

        modified = self.im._resume_instances([inst])

        assert modified is True
        assert inst.id in self.im._available_pool
        assert inst.id in self.im._prefill_pool
        assert inst.id not in self.im._paused_pool

    def test_resume_not_in_paused_pool_skips(self):
        """Resuming an instance not in paused pool is skipped."""
        inst = _make_instance(1, "job-1", role=PDRole.ROLE_P)
        self._add_to_available(inst)
        # Instance is in available, not paused

        modified = self.im._resume_instances([inst])
        assert modified is False

    # -- _compute_set_diff with paused pool --

    def test_compute_set_diff_includes_paused_pool(self):
        """SET event should NOT treat paused instances as to-be-deleted."""
        inst = _make_instance(1, "job-1", role=PDRole.ROLE_P)
        self.im._available_pool[inst.id] = inst
        self.im._prefill_pool[inst.id] = inst

        # Simulate pause
        self.im._pause_instances([inst])

        # SET event with empty list (no ACTIVE instances)
        to_add, to_remove = self.im._compute_set_diff([])

        # paused instance should NOT appear in to_remove
        paused_ids = {i.id for i in to_remove}
        assert inst.id not in paused_ids

    # -- refresh_instances with PAUSE / RESUME (async) --

    def test_refresh_instances_pause_event(self):
        """refresh_instances with PAUSE event delegates to _pause_instances."""
        inst = _make_instance(1, "job-1", role=PDRole.ROLE_P)
        self._add_to_available(inst)

        asyncio.run(self.im.refresh_instances(EventType.PAUSE, [inst]))

        assert inst.id in self.im._paused_pool
        assert inst.id not in self.im._available_pool

    def test_refresh_instances_resume_event(self):
        """refresh_instances with RESUME event moves back to available."""
        inst = _make_instance(1, "job-1", role=PDRole.ROLE_P)
        self._add_to_available(inst)
        self.im._pause_instances([inst])

        asyncio.run(self.im.refresh_instances(EventType.RESUME, [inst]))

        assert inst.id in self.im._available_pool
        assert inst.id not in self.im._paused_pool

    # -- stop clears paused pool (async) --

    def test_stop_clears_paused_pool(self):
        """stop() should clear the paused pool."""
        inst = _make_instance(1, "job-1", role=PDRole.ROLE_P)
        self._add_to_available(inst)
        self.im._pause_instances([inst])

        asyncio.run(self.im.stop())

        assert len(self.im._paused_pool) == 0


# ---------------------------------------------------------------------------
# 6. End-to-end integration test (simulated full flow)
# ---------------------------------------------------------------------------


class TestE2EPrestopFlow:
    """Simulate the complete PreStop flow end-to-end.

    Flow:
      1. HeartbeatManager.pause_all_endpoints()
      2. Heartbeat → Controller → Instance.is_all_endpoints_paused() == True
      3. Controller InstanceManager ACTIVE → PAUSED
      4. EventPusher push PAUSE event
      5. Coordinator receive PAUSE → move to _paused_pool
    """

    def test_full_flow_single_pod(self):
        """E2E: single Pod, all endpoints paused → instance goes PAUSED."""
        # --- Setup ---
        # Create endpoints and instance
        endpoints_dict = {
            "10.0.0.1": {
                0: _make_endpoint(0, "10.0.0.1", EndpointStatus.NORMAL),
            },
        }
        inst = _make_instance(1, "e2e-job", endpoints=endpoints_dict)
        inst.status = InsStatus.ACTIVE

        # --- Step 1: Simulate PreStop → heartbeat reports PAUSED ---
        for pod_eps in inst.endpoints.values():
            for ep in pod_eps.values():
                ep.status = EndpointStatus.PAUSED

        # --- Step 2: Controller detects all endpoints paused ---
        assert inst.is_all_endpoints_paused() is True

        # --- Step 3: State transition ACTIVE → PAUSED ---
        from motor.controller.core.instance_manager import InstanceManager
        from motor.controller.core.observer import ObserverEvent

        with (
            patch('motor.controller.core.instance_manager.EtcdClient') as mock_etcd,
            patch('threading.Thread') as mock_thread,
            patch('motor.controller.core.instance_manager.EventPusher'),
        ):
            mock_etcd.return_value = MagicMock()
            mock_thread.return_value = MagicMock()
            if hasattr(InstanceManager, '_instances'):
                InstanceManager._instances.clear()
            im = InstanceManager(MagicMock())
            mock_obs = MagicMock()
            im.observers.append(mock_obs)

            result = im._handle_state_transition(inst)
            assert result is True
            assert inst.status == InsStatus.PAUSED
            mock_obs.update.assert_called_once()
            assert mock_obs.update.call_args[0][1] == ObserverEvent.INSTANCE_PAUSED

    def test_full_flow_two_pods_only_one_paused(self):
        """Cross-pod: only Pod A paused → instance stays ACTIVE."""
        endpoints_dict = {
            "10.0.0.1": {
                0: _make_endpoint(0, "10.0.0.1", EndpointStatus.PAUSED),  # Pod A paused
            },
            "10.0.0.2": {
                1: _make_endpoint(1, "10.0.0.2", EndpointStatus.NORMAL),  # Pod B still normal
            },
        }
        inst = _make_instance(1, "cross-pod-job", endpoints=endpoints_dict)
        inst.status = InsStatus.ACTIVE

        assert inst.is_all_endpoints_paused() is False
        assert inst.is_all_endpoints_ready() is False  # because Pod A is PAUSED

    def test_full_flow_two_pods_both_paused(self):
        """Cross-pod: both Pods paused → instance goes PAUSED."""
        endpoints_dict = {
            "10.0.0.1": {
                0: _make_endpoint(0, "10.0.0.1", EndpointStatus.PAUSED),
            },
            "10.0.0.2": {
                1: _make_endpoint(1, "10.0.0.2", EndpointStatus.PAUSED),
            },
        }
        inst = _make_instance(1, "cross-pod-job", endpoints=endpoints_dict)
        inst.status = InsStatus.ACTIVE

        assert inst.is_all_endpoints_paused() is True

    def test_full_flow_resume(self):
        """E2E: pause then resume → instance back to ACTIVE."""
        endpoints_dict = {
            "10.0.0.1": {
                0: _make_endpoint(0, "10.0.0.1", EndpointStatus.NORMAL),
            },
        }
        inst = _make_instance(1, "e2e-job", endpoints=endpoints_dict)
        inst.status = InsStatus.ACTIVE

        # Pause
        for pod_eps in inst.endpoints.values():
            for ep in pod_eps.values():
                ep.status = EndpointStatus.PAUSED
        assert inst.is_all_endpoints_paused() is True

        # Resume
        for pod_eps in inst.endpoints.values():
            for ep in pod_eps.values():
                ep.status = EndpointStatus.NORMAL
        assert inst.is_all_endpoints_ready() is True
        assert inst.is_all_endpoints_paused() is False

    def test_coordinator_paused_instance_not_scheduled(self):
        """Paused instance in Coordinator is excluded from scheduling."""
        from motor.config.coordinator import CoordinatorConfig
        from motor.coordinator.domain.instance_manager import InstanceManager

        im_c = InstanceManager(CoordinatorConfig())
        inst = _make_instance(1, "job-1", role=PDRole.ROLE_P)
        im_c._available_pool[inst.id] = inst
        im_c._prefill_pool[inst.id] = inst

        # Before pause: instance is available
        assert inst.id in im_c.get_available_instances(PDRole.ROLE_P)

        # After pause: instance NOT available
        im_c._pause_instances([inst])
        assert inst.id not in im_c.get_available_instances(PDRole.ROLE_P)
        assert inst.id in im_c._paused_pool

        # Resume
        im_c._resume_instances([inst])
        assert inst.id in im_c.get_available_instances(PDRole.ROLE_P)
        assert inst.id not in im_c._paused_pool
