# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Entry Point discovery and validation for scheduling policy plugins."""

from __future__ import annotations

import inspect
from importlib.metadata import entry_points
from typing import Any

from motor.common.logger import get_logger
from motor.config.coordinator import PolicyPluginConfig, SchedulerType
from motor.coordinator.scheduler.policy.api import (
    SUPPORTED_POLICY_API_VERSION,
    LoadBalancingPolicy,
)
from motor.coordinator.scheduler.policy.factory import create_load_balancing_policy

logger = get_logger(__name__)

SCHEDULING_POLICIES_GROUP = "mindie_motor.scheduling_policies"
IN_TREE_POLICY_NAMES = PolicyPluginConfig.IN_TREE_NAMES
RESERVED_POLICY_NAMES = IN_TREE_POLICY_NAMES
FALLBACK_POLICY_NAMES = PolicyPluginConfig.FALLBACK_NAMES


class PolicyLoadError(Exception):
    """Raised when a configured policy plugin cannot be loaded."""


def _entry_point_sources(entries: list[Any]) -> str:
    parts: list[str] = []
    for entry in entries:
        dist = getattr(entry, "dist", None)
        package = dist.metadata.get("Name", "?") if dist is not None else "?"
        version = dist.version if dist is not None else "?"
        parts.append("%s@%s -> %s" % (package, version, entry.value))
    return "; ".join(parts)


def _validate_policy_class(policy_cls: type, *, entry_name: str, entry_value: str) -> None:
    if not isinstance(policy_cls, type) or not issubclass(policy_cls, LoadBalancingPolicy):
        raise PolicyLoadError(
            "Entry point %r (%s) is not a LoadBalancingPolicy subclass, got %r" % (entry_name, entry_value, policy_cls)
        )
    api_version = getattr(policy_cls, "api_version", None)
    if api_version != SUPPORTED_POLICY_API_VERSION:
        raise PolicyLoadError(
            "Entry point %r (%s) api_version=%r is incompatible with supported version %s"
            % (entry_name, entry_value, api_version, SUPPORTED_POLICY_API_VERSION)
        )
    rank_fn = getattr(policy_cls, "rank", None)
    if rank_fn is None or not callable(rank_fn):
        raise PolicyLoadError("Entry point %r (%s) has no rank() method" % (entry_name, entry_value))
    if inspect.iscoroutinefunction(rank_fn):
        raise PolicyLoadError("Entry point %r (%s) rank() must be synchronous" % (entry_name, entry_value))
    if policy_cls.rank is LoadBalancingPolicy.rank:
        raise PolicyLoadError("Entry point %r (%s) does not override rank()" % (entry_name, entry_value))
    requires_kv = getattr(policy_cls, "requires_kv_match", None)
    if not isinstance(requires_kv, bool):
        raise PolicyLoadError(
            "Entry point %r (%s) requires_kv_match must be bool, got %r" % (entry_name, entry_value, requires_kv)
        )


def validate_policy_plugin_config(spec: PolicyPluginConfig | None) -> None:
    if spec is None or not (spec.name or "").strip():
        return
    fallback = (spec.fallback or "load_balance").strip()
    if fallback not in FALLBACK_POLICY_NAMES:
        raise PolicyLoadError(
            "Policy plugin fallback must be one of %s, got %r" % (sorted(FALLBACK_POLICY_NAMES), fallback)
        )
    if spec.options is not None and not isinstance(spec.options, dict):
        raise PolicyLoadError("Policy plugin options must be a JSON object")


def is_in_tree_policy(name: str) -> bool:
    """Return True when ``name`` is a built-in Worker strategy."""
    return (name or "").strip() in IN_TREE_POLICY_NAMES


def effective_policy_name(
    scheduler_type: SchedulerType | str | None,
    plugin: PolicyPluginConfig | None,
) -> str:
    """Resolve the active policy name.

    ``policy_plugin.name`` wins when non-empty; otherwise ``scheduler_type`` is used.
    Empty scheduler type follows the Worker client default of ``round_robin``.
    """
    if plugin is not None and (plugin.name or "").strip():
        return plugin.name.strip()
    if isinstance(scheduler_type, SchedulerType):
        return scheduler_type.value
    return str(scheduler_type or "round_robin").strip() or "round_robin"


def resolve_load_balancing_policy(
    name: str,
    *,
    builtin_options: dict[str, Any] | None = None,
    plugin_spec: PolicyPluginConfig | None = None,
) -> LoadBalancingPolicy:
    """Create the policy for ``name``: in-tree factory, otherwise Entry Point load."""
    normalized = (name or "").strip()
    if not normalized:
        raise ValueError("Scheduling policy name is empty")
    if is_in_tree_policy(normalized):
        opts = dict(builtin_options or {})
        plugin_name = (plugin_spec.name or "").strip() if plugin_spec is not None else ""
        if plugin_spec is not None and plugin_name == normalized and plugin_spec.options:
            opts.update(dict(plugin_spec.options))
        return create_load_balancing_policy(normalized, opts)
    if plugin_spec is None or (plugin_spec.name or "").strip() != normalized:
        raise PolicyLoadError("Scheduling policy %r is not in-tree and policy_plugin.name does not match" % normalized)
    return PolicyLoader().load(plugin_spec)


class PolicyLoader:
    """Discover and initialize a LoadBalancingPolicy from Entry Point metadata."""

    _cache: dict[str, LoadBalancingPolicy] = {}

    def load(self, spec: PolicyPluginConfig) -> LoadBalancingPolicy:
        validate_policy_plugin_config(spec)
        name = spec.name.strip()
        if name in IN_TREE_POLICY_NAMES:
            return create_load_balancing_policy(name, dict(spec.options or {}))
        cached = self._cache.get(name)
        if cached is not None:
            return cached
        entries = [entry for entry in entry_points(group=SCHEDULING_POLICIES_GROUP) if entry.name == name]
        if not entries:
            raise PolicyLoadError(
                "Scheduling policy %r not found in group %r; install a plugin wheel in this Python environment"
                % (name, SCHEDULING_POLICIES_GROUP)
            )
        if len(entries) > 1:
            raise PolicyLoadError(
                "Scheduling policy %r matches multiple entry points: %s" % (name, _entry_point_sources(entries))
            )
        entry = entries[0]
        try:
            policy_cls = entry.load()
        except Exception as exc:
            raise PolicyLoadError("Failed to import scheduling policy %r (%s): %s" % (name, entry.value, exc)) from exc
        _validate_policy_class(policy_cls, entry_name=name, entry_value=entry.value)
        options = dict(spec.options or {})
        try:
            policy = policy_cls(options=options)
        except Exception as exc:
            raise PolicyLoadError(
                "Failed to construct scheduling policy %r (%s): %s" % (name, entry.value, exc)
            ) from exc
        dist = getattr(entry, "dist", None)
        package = dist.metadata.get("Name", "?") if dist is not None else "?"
        version = dist.version if dist is not None else "?"
        logger.info(
            "Loaded scheduling policy name=%s package=%s version=%s entry=%s api_version=%s requires_kv_match=%s",
            name,
            package,
            version,
            entry.value,
            policy_cls.api_version,
            policy_cls.requires_kv_match,
        )
        self._cache[name] = policy
        return policy

    @classmethod
    def reset_cache(cls) -> None:
        """Drop cached policy instances (tests and process shutdown)."""
        cls._cache.clear()

    @staticmethod
    def load_at_startup(spec: PolicyPluginConfig | None) -> LoadBalancingPolicy | None:
        """Load plugin policy; raise PolicyLoadError on failure (fail-fast at worker startup)."""
        if spec is None or not (spec.name or "").strip():
            return None
        name = spec.name.strip()
        if name in IN_TREE_POLICY_NAMES:
            validate_policy_plugin_config(spec)
            return None
        return PolicyLoader().load(spec)
