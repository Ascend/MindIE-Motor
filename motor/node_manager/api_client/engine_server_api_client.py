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
from motor.config.node_manager import NodeManagerConfig

logger = get_logger(__name__)
_rl = RateLimitedLogger(logger)


class EngineServerApiClient:
    tls_config = NodeManagerConfig.from_json().mgmt_tls_config

    @staticmethod
    def query_status(address: str):
        client_args = EngineServerApiClient._generate_client_args(address)
        client = SafeHTTPSClient(**client_args, timeout=5)
        response = client.get("/status")
        _rl.record_success(f"node_manager.engine_server.query_status.{address}")
        _rl.emit_info_periodic(
            f"node_manager.engine_server.query_status.{address}",
            "NodeManager->EngineServer query_status periodic summary: succeeded {count} times in last 60s",
        )
        logger.debug(f"Query engine server status success, "
                    f"response: {response}, "
                    f"address: {client_args['address']}")
        return response

    @classmethod
    def _generate_client_args(cls, address: str) -> dict[str, str]:
        client_ars = {
            "address": f"{address}",
            "tls_config": cls.tls_config
        }
        return client_ars
