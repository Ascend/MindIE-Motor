# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""ReadOnlyInstance copy paths must carry the fields the controller pushes to the coordinator.

Regression guard: to_instance() / __deepcopy__ previously dropped dispatch_capabilities,
engine_type and enable_multi_endpoints, leaving the coordinator with empty capabilities so
readiness stayed instances_status=unknown forever.
"""

import copy

from motor.common.resources.instance import Instance, PDRole, ReadOnlyInstance


def _instance() -> Instance:
    return Instance(
        job_name="p0",
        model_name="m",
        id=1,
        role=PDRole.ROLE_P.value,
        engine_type="vllm",
        dispatch_capabilities=["concurrent_engine_sync"],
        enable_multi_endpoints=False,
    )


def test_to_instance_preserves_dispatch_fields():
    read_only = ReadOnlyInstance(_instance())

    copied = read_only.to_instance()

    assert copied.dispatch_capabilities == ["concurrent_engine_sync"]
    assert copied.engine_type == "vllm"
    assert copied.enable_multi_endpoints is False


def test_to_instance_dispatch_capabilities_is_independent_copy():
    read_only = ReadOnlyInstance(_instance())

    copied = read_only.to_instance()
    copied.dispatch_capabilities.append("prefill_handoff_decode")

    # Mutating the copy must not bleed back into the wrapped instance.
    assert read_only.to_instance().dispatch_capabilities == ["concurrent_engine_sync"]


def test_deepcopy_preserves_dispatch_fields():
    read_only = ReadOnlyInstance(_instance())

    duplicated = copy.deepcopy(read_only)

    assert duplicated.dispatch_capabilities == ["concurrent_engine_sync"]
    assert duplicated.engine_type == "vllm"
    assert duplicated.enable_multi_endpoints is False
