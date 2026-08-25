# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Decorator-based service registry for the NodeManager daemon.

Usage::

    from motor.node_manager.core.services.registry import register_service

    @register_service("engine", backend="engine")
    class NativeEngineService:
        ...

    @register_service("kv_store", backend="memcache", prepare_priority=10)
    class LocalService:
        ...

Set ``KV_STORE_BACKEND`` to a comma-separated list of backend tags, e.g.
``"engine,memcache"`` (both), ``"engine"`` (inference only), or ``"memcache"``
(KV only).  Empty / unset defaults to ``"engine"`` for backward compatibility.

The Daemon discovers active services at init time via :func:`registry.get_active`
and instantiates them.  Non-matching backends are never instantiated.
"""

import importlib
import threading
from collections.abc import Callable
from typing import cast

from motor.common.logger import get_logger
from motor.node_manager.core.services.protocols import DaemonService

logger = get_logger(__name__)

# --- Well-known service names ---
SERVICE_ENGINE: str = "engine"
SERVICE_KV_STORE: str = "kv_store"

# --- Module discovery: backend → modules to import for @register_service ---
# Each key is a backend tag; ``discover()`` imports the listed modules when
# that backend appears in the ``Env.kv_store_backend`` list (comma-separated).
# ``None`` key = always-active (imported unconditionally).
_DEFAULT_MODULE_MAP: dict[str | None, list[str]] = {
    "engine": ["motor.node_manager.core.services.native_engine.service"],
    "memcache": ["motor.node_manager.core.services.memcache.lifecycle"],
    "mooncake": ["motor.node_manager.core.services.mooncake.lifecycle"],
}


# ---------------------------------------------------------------------------
# Registration record
# ---------------------------------------------------------------------------


class _ServiceRegistration:
    """Internal record for one registered service."""

    def __init__(
        self,
        name: str,
        service_class: type,
        *,
        backend: str | None = None,
        prepare_priority: int | None = None,
        factory: Callable[..., DaemonService] | None = None,
    ):
        self.name = name
        self.service_class = service_class
        self.backend = backend  # None = always active
        self.prepare_priority = prepare_priority  # None = no prepare phase
        self.factory = factory  # None = use service_class(**kwargs)
        self.instance: DaemonService | None = None

    def _is_active(self, active_backends: list[str]) -> bool:
        """True when this service's *backend* appears in *active_backends*.

        ``backend=None`` means always active regardless of config.
        """
        if self.backend is None:
            return True
        return self.backend in active_backends

    def instantiate(self, **kwargs) -> DaemonService:
        """Create and store the service instance.

        Uses *factory* when provided, otherwise calls ``service_class(**kwargs)``.

        Note: this is NOT idempotent by design — each call creates a fresh
        instance.  The Daemon's own singleton pattern (``_initialized`` guard)
        is the sole protection against double-instantiation.  This allows
        tests to construct multiple Daemon instances with different configs.
        """
        if self.factory is not None:
            self.instance = self.factory(**kwargs)
        else:
            self.instance = self.service_class(**kwargs)
        logger.info("Instantiated service %r (backend=%s)", self.name, self.backend)
        return self.instance


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class _ServiceRegistry:
    """Thread-safe registry of daemon-managed services.

    Services declare themselves via the ``@register_service`` decorator.
    The Daemon queries active services at init time via :meth:`get_active`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._registrations: dict[str, _ServiceRegistration] = {}
        self._module_map: dict[str | None, list[str]] = dict(_DEFAULT_MODULE_MAP)
        self._active_backends: list[str] = ["engine"]  # resolved by discover()

    # ------------------------------------------------------------------
    # Module discovery
    # ------------------------------------------------------------------

    def add_discovery_path(self, backend: str | None, *module_paths: str) -> None:
        """Add module paths for *backend* into the discovery map.

        ``backend=None`` means always-active (imported unconditionally).
        Call before :meth:`discover`.
        """
        with self._lock:
            self._module_map.setdefault(backend, []).extend(module_paths)

    @staticmethod
    def _parse_backends(raw: str) -> list[str]:
        """Parse a comma-separated services string into a list of backend tags.

        Supports comma-separated values (e.g. ``"engine,memcache"``).
        Defaults to ``["engine"]`` when empty, preserving backward compatibility.
        """
        if not raw or not raw.strip():
            return ["engine"]
        return [b.strip() for b in raw.split(",") if b.strip()]

    def discover(self, services: str) -> None:
        """Import modules registered via :meth:`add_discovery_path`.

        *services* is a comma-separated list of backend tags (e.g.
        ``"engine,memcache"``), derived from user_config.

        Always-active modules (``backend=None``) are imported unconditionally;
        backend-specific modules are imported for each matching tag.
        Importing the module causes ``@register_service`` decorators to fire,
        populating the registry.

        Called once by the Daemon before :meth:`get_active`.
        """
        backends = self._parse_backends(services)
        self._active_backends = backends
        for mod_path in self._module_map.get(None, []):
            importlib.import_module(mod_path)
        for backend in backends:
            for mod_path in self._module_map.get(backend, []):
                importlib.import_module(mod_path)
        logger.debug(
            "Service discovery complete (backends=%s, registered=%d)",
            backends,
            len(self._registrations),
        )

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        *,
        backend: str | None = None,
        prepare_priority: int | None = None,
        factory: Callable[..., DaemonService] | None = None,
    ):
        """Decorator: register *cls* as a daemon-managed service.

        Parameters
        ----------
        name:
            Unique service identifier (e.g. ``SERVICE_ENGINE``, ``SERVICE_KV_STORE``).
        backend:
            If set, the service is only active when
            ``Env.kv_store_backend == backend``.  ``None`` means always active.
        prepare_priority:
            If set, the Daemon calls ``svc.prepare()`` on this service before
            starting the engine.  Lower values run first.  ``None`` means the
            service does not participate in the prepare phase.
        factory:
            Optional callable ``(hardware_type, config) -> DaemonService``.
            When provided, :meth:`_ServiceRegistration.instantiate` delegates to
            *factory* instead of calling ``cls(**kwargs)`` directly.  This keeps
            constructor specifics out of the daemon.
        """

        def decorator(cls: type) -> type:
            with self._lock:
                existing = self._registrations.get(name)
                if existing is not None:
                    logger.warning(
                        "Service %r (from %s) is replacing %s",
                        name,
                        cls.__module__,
                        existing.service_class.__module__,
                    )
                self._registrations[name] = _ServiceRegistration(
                    name,
                    cls,
                    backend=backend,
                    prepare_priority=prepare_priority,
                    factory=factory,
                )
                logger.debug(
                    "Registered service %r from %s (backend=%s, prepare=%s, factory=%s)",
                    name,
                    cls.__module__,
                    backend,
                    prepare_priority,
                    factory is not None,
                )
            return cls

        return decorator

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_active(self) -> dict[str, _ServiceRegistration]:
        """Return registrations whose backend matches the active backends."""
        with self._lock:
            active_backends = self._active_backends
            return {name: reg for name, reg in self._registrations.items() if reg._is_active(active_backends)}

    def get_preparable(self) -> list[_ServiceRegistration]:
        """Active, preparable registrations sorted by priority (ascending)."""
        active = self.get_active().values()
        preparable = [r for r in active if r.prepare_priority is not None]
        preparable.sort(key=lambda r: cast(int, r.prepare_priority))
        return preparable

    def get_active_sorted(self) -> list[tuple[str, _ServiceRegistration]]:
        """Active registrations sorted for instantiation.

        PreparableServices come first (by priority, ascending), followed by
        non-preparable services in insertion order.
        """
        active = self.get_active()

        def _sort_key(item: tuple[str, _ServiceRegistration]) -> tuple[int, int]:
            _name, reg = item
            if reg.prepare_priority is not None:
                return (0, cast(int, reg.prepare_priority))
            return (1, 0)

        return sorted(active.items(), key=_sort_key)

    def get_instance(self, name: str) -> DaemonService | None:
        """Return the instantiated service by name, or *None*."""
        reg = self._registrations.get(name)
        return reg.instance if reg is not None else None


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------

registry = _ServiceRegistry()
register_service = registry.register
