# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Instance source abstraction: read-only access to available instance list.
Policy layer depends on this interface, not InstanceManager, for tests and swappable impl.
"""

from __future__ import annotations

from typing import Mapping, Protocol

from motor.common.resources.instance import Instance, PDRole

from motor.coordinator.domain.scheduling import InstanceReadiness


class InstanceProvider(Protocol):
    """
    Instance source protocol.
    Implemented by InstanceManager; Scheduler injects it when creating policy; policy gets instances via provider.
    """

    def get_available_instances(
        self,
        role: PDRole | None = None,
    ) -> Mapping[int, Instance]:
        """Return read-only mapping of available instances for role (id -> Instance)."""
        ...

    def get_required_instances_status(self) -> InstanceReadiness:
        """Return detailed readiness inferred from available instance roles."""
        ...

    def has_required_instances(self) -> bool:
        """True if the current instance topology can serve requests."""
        ...
