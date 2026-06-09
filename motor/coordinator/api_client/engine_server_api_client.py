# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from motor.common.http.http_client import SafeHTTPSClient
from motor.common.logger import get_logger
from motor.common.logger.rate_limited_logger import RateLimitedLogger
from motor.config.coordinator import CoordinatorConfig

logger = get_logger(__name__)
_rl = RateLimitedLogger(logger)


class EngineServerApiClient:
    tls_config = CoordinatorConfig.from_json().mgmt_tls_config

    @staticmethod
    def query_metrics(address: str):
        client_args = EngineServerApiClient._generate_client_args(address)
        try:
            client = SafeHTTPSClient(timeout=2, **client_args)
            response = client.do_get("/metrics")
            if response.status_code == 200:
                data = response.text
                return data
            else:
                logger.warning(
                    "Coordinator->EngineServer query_metrics non-2xx. "
                    "address=%s, status_code=%s. "
                    "Possible causes: 1) engine_server not ready 2) wrong endpoint 3) auth failure.",
                    address,
                    response.status_code,
                )
        except Exception as e:
            logger.warning(
                "Coordinator->EngineServer query_metrics failed. address=%s, error=%s. "
                "Possible causes: 1) engine_server down 2) network unreachable 3) tls mismatch. "
                "Check: ping %s, engine_server process status.",
                address,
                e,
                address,
            )

        return ""

    @classmethod
    def _generate_client_args(cls, address) -> dict[str, str]:
        client_ars = {
            "address": f"{address}",
            "tls_config": cls.tls_config,
        }
        return client_ars
