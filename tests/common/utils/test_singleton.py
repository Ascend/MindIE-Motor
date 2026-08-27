# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Unit tests for ThreadSafeSingleton.

Guards against regressions in instance uniqueness, subclass isolation, and the
_instances/_lock reset protocol used by other test suites.
"""

from __future__ import annotations

import threading

import pytest

from motor.common.utils.singleton import ThreadSafeSingleton

_CONCURRENT_WORKERS = 32


def _make_singleton_cls(name: str) -> type[ThreadSafeSingleton]:
    """Create an isolated subclass so tests do not share cached instances."""
    return type(name, (ThreadSafeSingleton,), {})


def _create_concurrently(
    cls: type[ThreadSafeSingleton], workers: int = _CONCURRENT_WORKERS
) -> list[ThreadSafeSingleton]:
    """Instantiate *cls* from many threads that start at the same barrier."""
    barrier = threading.Barrier(workers)
    created: list[ThreadSafeSingleton | None] = [None] * workers
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def _record_error(exc: BaseException) -> None:
        with errors_lock:
            errors.append(exc)

    def worker(index: int) -> None:
        try:
            barrier.wait()
        except threading.BrokenBarrierError as exc:
            _record_error(exc)
            return
        try:
            created[index] = cls()
        except Exception as exc:
            _record_error(exc)

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    if any(thread.is_alive() for thread in threads):
        barrier.abort()
        for thread in threads:
            thread.join(timeout=1)
        pytest.fail("concurrent singleton workers did not finish")

    assert errors == []
    instances = [instance for instance in created if instance is not None]
    assert len(instances) == workers
    return instances


@pytest.fixture
def singleton_cls():
    cls = _make_singleton_cls("DummySingleton")
    yield cls
    ThreadSafeSingleton._instances.pop(cls, None)


class TestThreadSafeSingleton:
    def test_same_class_returns_same_instance(self, singleton_cls):
        """Guards against creating a second object for the same subclass."""
        first = singleton_cls()
        second = singleton_cls()

        assert first is second
        assert ThreadSafeSingleton._instances[singleton_cls] is first

    def test_concurrent_creation_returns_single_instance(self, singleton_cls):
        """Guards against a race that would mint multiple instances."""
        created = _create_concurrently(singleton_cls)
        assert all(instance is created[0] for instance in created)
        assert len({id(instance) for instance in created}) == 1

    def test_constructor_arguments_still_return_same_instance(self):
        """Guards against args/kwargs (used by Observability, InstanceManager) minting a new object."""

        class WithInit(ThreadSafeSingleton):
            def __init__(self, *args, **kwargs) -> None:
                pass

        try:
            first = WithInit("config", flag=True)
            second = WithInit("other", flag=False)
            assert first is second
        finally:
            ThreadSafeSingleton._instances.pop(WithInit, None)

    def test_init_runs_on_every_constructor_call(self):
        """Documents why subclasses such as HTTPClientPool guard with _initialized."""

        class Counted(ThreadSafeSingleton):
            inits = 0

            def __init__(self) -> None:
                type(self).inits += 1

        try:
            first = Counted()
            second = Counted()
            assert first is second
            assert Counted.inits == 2
        finally:
            ThreadSafeSingleton._instances.pop(Counted, None)

    def test_subclass_instances_are_isolated(self):
        """Guards against two subclasses sharing one cached instance."""
        first_cls = _make_singleton_cls("FirstSingleton")
        second_cls = _make_singleton_cls("SecondSingleton")
        try:
            first = first_cls()
            second = second_cls()

            assert first is first_cls()
            assert second is second_cls()
            assert first is not second
            assert ThreadSafeSingleton._instances[first_cls] is first
            assert ThreadSafeSingleton._instances[second_cls] is second
        finally:
            ThreadSafeSingleton._instances.pop(first_cls, None)
            ThreadSafeSingleton._instances.pop(second_cls, None)

    def test_grandchild_is_isolated_from_parent(self):
        """Guards against a subclass sharing its parent's cached instance."""
        parent_cls = _make_singleton_cls("ParentSingleton")
        child_cls = type("ChildSingleton", (parent_cls,), {})
        try:
            parent = parent_cls()
            child = child_cls()
            assert parent is not child
            assert parent_cls() is parent
            assert child_cls() is child
        finally:
            ThreadSafeSingleton._instances.pop(parent_cls, None)
            ThreadSafeSingleton._instances.pop(child_cls, None)

    def test_mixin_subclass_is_isolated(self):
        """Guards against multiple-inheritance singletons colliding (FaultManager pattern)."""

        class DummyMixin:
            pass

        mixed_cls = type("MixedSingleton", (DummyMixin, ThreadSafeSingleton), {})
        sibling_cls = _make_singleton_cls("SiblingSingleton")
        try:
            mixed = mixed_cls()
            sibling = sibling_cls()
            assert mixed is mixed_cls()
            assert mixed is not sibling
        finally:
            ThreadSafeSingleton._instances.pop(mixed_cls, None)
            ThreadSafeSingleton._instances.pop(sibling_cls, None)

    def test_reset_instances_allows_new_object(self, singleton_cls):
        """Guards against leftover cache after _instances is cleared for a class."""
        original = singleton_cls()

        del ThreadSafeSingleton._instances[singleton_cls]

        replacement = singleton_cls()
        assert replacement is not original
        assert singleton_cls() is replacement
        assert ThreadSafeSingleton._instances[singleton_cls] is replacement

    def test_reset_one_class_does_not_affect_another(self):
        """Guards against popping one key wiping a sibling singleton from the shared dict."""
        first_cls = _make_singleton_cls("ResetFirst")
        second_cls = _make_singleton_cls("ResetSecond")
        try:
            first = first_cls()
            second = second_cls()

            del ThreadSafeSingleton._instances[first_cls]

            assert first_cls() is not first
            assert second_cls() is second
        finally:
            ThreadSafeSingleton._instances.pop(first_cls, None)
            ThreadSafeSingleton._instances.pop(second_cls, None)

    def test_reset_lock_keeps_cached_instance(self, singleton_cls):
        """Guards against replacing _lock accidentally dropping an already cached object."""
        original = singleton_cls()
        original_lock = ThreadSafeSingleton._lock
        ThreadSafeSingleton._lock = threading.Lock()
        try:
            assert singleton_cls() is original
        finally:
            ThreadSafeSingleton._lock = original_lock

    def test_reset_lock_still_enforces_uniqueness(self, singleton_cls):
        """Guards against uniqueness breaking after _lock is replaced."""
        original_lock = ThreadSafeSingleton._lock
        ThreadSafeSingleton._lock = threading.Lock()
        try:
            created = _create_concurrently(singleton_cls)
            assert all(instance is created[0] for instance in created)
            assert len({id(instance) for instance in created}) == 1
        finally:
            ThreadSafeSingleton._lock = original_lock

    def test_reset_instances_and_lock_then_concurrent_uniqueness(self, singleton_cls):
        """Guards against the combined reset protocol used by other test suites."""
        original = singleton_cls()
        original_lock = ThreadSafeSingleton._lock
        del ThreadSafeSingleton._instances[singleton_cls]
        ThreadSafeSingleton._lock = threading.Lock()
        try:
            created = _create_concurrently(singleton_cls)
            assert created[0] is not original
            assert all(instance is created[0] for instance in created)
            assert len({id(instance) for instance in created}) == 1
        finally:
            ThreadSafeSingleton._lock = original_lock
