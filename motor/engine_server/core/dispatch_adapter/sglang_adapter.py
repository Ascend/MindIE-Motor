# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import hashlib
import os
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException, status

from motor.common.resources.dispatch import MotorDispatch
from motor.engine_server.core.dispatch_adapter.base import DispatchAdapter


class SGLangDispatchAdapter(DispatchAdapter):
    async def _adapt_engine_body(self, body: dict[str, Any], dispatch: MotorDispatch) -> dict[str, Any]:
        body["request_id"] = dispatch.engine_request_id
        prefill = dispatch.endpoints.prefill
        if prefill is None:
            return body

        parsed = urlparse(prefill.url)
        bootstrap_port = os.getenv("DISAGGREGATION_BOOTSTRAP_PORT", "").strip()
        if not bootstrap_port:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=("DISAGGREGATION_BOOTSTRAP_PORT must be set for SGLang dispatch requests."),
            )
        body.update(
            {
                "bootstrap_host": parsed.hostname or prefill.url,
                "bootstrap_port": bootstrap_port,
                "bootstrap_room": self._stable_bootstrap_room(dispatch),
            }
        )
        return body

    @staticmethod
    def _stable_bootstrap_room(dispatch: MotorDispatch) -> int:
        raw = f"{dispatch.pair_id}:{dispatch.attempt_seq}".encode("utf-8")
        digest = hashlib.blake2b(raw, digest_size=8).digest()
        return int.from_bytes(digest, "big") & ((1 << 63) - 1)
