# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Tests for the service registry."""

import threading

from motor.node_manager.core.services.registry import (
    _ServiceRegistry,
    _ServiceRegistration,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _FakeDaemonService:
    """Service that satisfies DaemonService protocol."""

    def stop(self) -> None:
        pass

    def health_check(self) -> None:
        pass


class _FakePreparableService(_FakeDaemonService):
    """Service that also satisfies PreparableService protocol."""

    def prepare(self, **kwargs) -> None:
        pass


# ---------------------------------------------------------------------------
# _ServiceRegistration
# ---------------------------------------------------------------------------


class TestServiceRegistration:
    def test_basic_attributes(self):
        reg = _ServiceRegistration("test", _FakeDaemonService, backend=None)
        assert reg.name == "test"
        assert reg.service_class is _FakeDaemonService
        assert reg.backend is None
        assert reg.prepare_priority is None
        assert reg.factory is None
        assert reg.instance is None

    def test_with_backend_and_priority(self):
        reg = _ServiceRegistration(
            "kv",
            _FakePreparableService,
            backend="memcache",
            prepare_priority=10,
        )
        assert reg.backend == "memcache"
        assert reg.prepare_priority == 10

    def test_is_active_no_backend(self):
        """backend=None → always active regardless of configured backends."""
        reg = _ServiceRegistration("engine", _FakeDaemonService, backend=None)
        assert reg._is_active(["engine"]) is True
        assert reg._is_active(["memcache"]) is True
        assert reg._is_active([]) is True

    def test_is_active_matching_backend(self):
        reg = _ServiceRegistration("kv", _FakeDaemonService, backend="memcache")
        assert reg._is_active(["engine", "memcache"]) is True
        assert reg._is_active(["memcache"]) is True

    def test_is_active_non_matching_backend(self):
        reg = _ServiceRegistration("kv", _FakeDaemonService, backend="memcache")
        assert reg._is_active(["engine"]) is False
        assert reg._is_active(["mooncake"]) is False
        assert reg._is_active([]) is False

    def test_instantiate_no_factory(self):
        reg = _ServiceRegistration("test", _FakeDaemonService)
        inst = reg.instantiate()
        assert inst is not None
        assert isinstance(inst, _FakeDaemonService)
        assert reg.instance is inst

    def test_instantiate_with_factory(self):
        def _factory(**kwargs):
            return _FakeDaemonService()

        reg = _ServiceRegistration("test", _FakeDaemonService, factory=_factory)
        inst = reg.instantiate()
        assert inst is not None
        assert reg.instance is inst

    def test_instantiate_creates_new_instance_each_call(self):
        """Each call creates a fresh instance (not idempotent).

        The Daemon's singleton pattern guards against double-instantiation;
        :meth:`_ServiceRegistration.instantiate` itself is not idempotent
        so that tests can construct multiple Daemon instances with different
        configs.
        """
        reg = _ServiceRegistration("test", _FakeDaemonService)
        first = reg.instantiate()
        second = reg.instantiate()
        assert first is not second
        # reg.instance always points to the latest created instance
        assert reg.instance is second


# ---------------------------------------------------------------------------
# _ServiceRegistry: register / get_active / get_preparable
# ---------------------------------------------------------------------------


class TestServiceRegistry:
    def test_register_and_get_active(self):
        registry = _ServiceRegistry()
        registry.register("engine")(_FakeDaemonService)

        active = registry.get_active()
        assert "engine" in active
        assert active["engine"].name == "engine"

    def test_get_active_filters_by_backend(self):
        registry = _ServiceRegistry()
        registry.register("engine", backend=None)(_FakeDaemonService)
        registry.register("kv", backend="memcache")(_FakeDaemonService)

        # Simulate engine-only mode
        registry._active_backends = ["engine"]
        active = registry.get_active()
        assert "engine" in active
        assert "kv" not in active

        # Simulate engine + memcache mode
        registry._active_backends = ["engine", "memcache"]
        active = registry.get_active()
        assert "engine" in active
        assert "kv" in active

    def test_get_preparable_only_returns_with_priority(self):
        registry = _ServiceRegistry()
        registry.register("engine", backend=None)(_FakeDaemonService)
        registry.register("kv", backend=None, prepare_priority=10)(_FakePreparableService)

        active_regs = registry.get_active()
        assert "kv" in active_regs

        preparable = registry.get_preparable()
        assert len(preparable) == 1
        assert preparable[0].name == "kv"

    def test_get_preparable_sorted_by_priority(self):
        registry = _ServiceRegistry()
        registry.register("c", backend=None, prepare_priority=30)(_FakePreparableService)
        registry.register("a", backend=None, prepare_priority=10)(_FakePreparableService)
        registry.register("b", backend=None, prepare_priority=20)(_FakePreparableService)

        preparable = registry.get_preparable()
        names = [r.name for r in preparable]
        assert names == ["a", "b", "c"]

    def test_get_active_sorted_preparable_first(self):
        registry = _ServiceRegistry()
        registry.register("engine", backend=None)(_FakeDaemonService)
        registry.register("kv", backend=None, prepare_priority=10)(_FakePreparableService)

        sorted_list = registry.get_active_sorted()
        names = [name for name, _reg in sorted_list]
        # preparable service (kv) should come before non-preparable (engine)
        assert names == ["kv", "engine"]

    def test_get_active_sorted_preparable_by_priority(self):
        registry = _ServiceRegistry()
        registry.register("engine", backend=None)(_FakeDaemonService)
        registry.register("kv2", backend=None, prepare_priority=20)(_FakePreparableService)
        registry.register("kv1", backend=None, prepare_priority=10)(_FakePreparableService)

        sorted_list = registry.get_active_sorted()
        names = [name for name, _reg in sorted_list]
        assert names == ["kv1", "kv2", "engine"]

    def test_duplicate_register(self):
        registry = _ServiceRegistry()
        registry.register("svc")(_FakeDaemonService)

        class _Replacement:
            def stop(self) -> None:
                pass

            def health_check(self) -> None:
                pass

        registry.register("svc")(_Replacement)
        active = registry.get_active()
        assert active["svc"].service_class is _Replacement

    def test_get_instance(self):
        registry = _ServiceRegistry()
        registry.register("engine")(_FakeDaemonService)
        # Need to go through get_active_sorted + instantiate to populate instance
        for _name, reg in registry.get_active_sorted():
            reg.instantiate()

        inst = registry.get_instance("engine")
        assert inst is not None
        assert isinstance(inst, _FakeDaemonService)

    def test_get_instance_nonexistent(self):
        registry = _ServiceRegistry()
        assert registry.get_instance("nonexistent") is None

    def test_add_discovery_path(self):
        registry = _ServiceRegistry()
        registry.add_discovery_path("mooncake", "tests.node_manager.test_registry")
        assert "mooncake" in registry._module_map
        assert "tests.node_manager.test_registry" in registry._module_map["mooncake"]

    def test_add_discovery_path_multiple(self):
        registry = _ServiceRegistry()
        registry.add_discovery_path("mooncake", "mod.a", "mod.b")
        assert registry._module_map["mooncake"] == ["mod.a", "mod.b"]


# ---------------------------------------------------------------------------
# thread safety
# ---------------------------------------------------------------------------


def test_concurrent_register_and_read():
    """Concurrent register and get_active should not lose registrations."""
    registry = _ServiceRegistry()
    n_services = 50
    errors = []

    def register_svc(i):
        try:
            registry.register(f"svc_{i}")(_FakeDaemonService)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=register_svc, args=(i,)) for i in range(n_services)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    active = registry.get_active()
    assert len(active) == n_services
