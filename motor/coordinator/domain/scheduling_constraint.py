# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Internal scheduling constraints (pin instances); not part of client API."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from motor.common.resources.instance import PDRole


class SchedulingConstraint(BaseModel):
    """
    Pin scheduling to specific instance IDs for one or more roles.
    Used by internal probes; normal client requests leave this unset.
    """

    model_config = ConfigDict(frozen=True)

    pinned_instances: dict[str, int] = Field(
        default_factory=dict,
        description="PDRole.value -> instance id",
    )
    reason: str = Field(default="internal", description="Audit tag, e.g. precision_probe")

    @classmethod
    def for_precision_probe(
        cls,
        *,
        p_instance_id: int | None,
        d_instance_id: int,
    ) -> SchedulingConstraint:
        pinned: dict[str, int] = {PDRole.ROLE_D.value: d_instance_id}
        if p_instance_id is not None:
            pinned[PDRole.ROLE_P.value] = p_instance_id
        return cls(pinned_instances=pinned, reason="precision_probe")

    def target_for_role(self, role: PDRole) -> int | None:
        key = role.value if hasattr(role, "value") else str(role)
        return self.pinned_instances.get(key)
