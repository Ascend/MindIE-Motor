# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import pytest
from pydantic import ValidationError

from motor.common.resources.http_msg_spec import EventType, InsEventMsg
from motor.common.resources.instance import Instance, PDRole


def test_instance_event_rejects_duplicate_ids() -> None:
    """Duplicate request IDs must fail before downstream ID-keyed mappings are built."""
    first = Instance(job_name="prefill", model_name="test-model", id=1, role=PDRole.ROLE_P)
    second = Instance(job_name="decode", model_name="test-model", id=1, role=PDRole.ROLE_D)

    with pytest.raises(ValidationError, match="duplicate instance IDs"):
        InsEventMsg(event=EventType.SET, instances=[first, second])
