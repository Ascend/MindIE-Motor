# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from motor.engine_server.core.config import IConfig
from motor.common.http.http_client import AsyncSafeHTTPSClient
from motor.common.logger import get_logger
from motor.common.utils.snapshot_utils import is_restored_from_host_side_snapshot, get_pod_ip
from motor.engine_server.utils.ip import build_endpoint

logger = get_logger(__name__)


class HealthCollector:
    def __init__(self, config: IConfig):
        endpoint_config = config.get_endpoint_config()
        self.host = endpoint_config.host
        self.port = endpoint_config.port
        self.infer_tls_config = endpoint_config.deploy_config.infer_tls_config
        self.timeout = endpoint_config.deploy_config.health_check_config.health_collector_timeout
        self.address = build_endpoint(self.host, self.port)
        self._has_connected = False
        self._has_refreshed_after_restored = False

    async def is_healthy(self) -> bool:
        try:
            if not self._has_refreshed_after_restored and is_restored_from_host_side_snapshot():
                self.address = build_endpoint(get_pod_ip(), self.port)
                self._has_refreshed_after_restored = True

            async with AsyncSafeHTTPSClient.create_client(
                address=self.address,
                tls_config=self.infer_tls_config,
                timeout=self.timeout,
            ) as client:
                response = await client.get("/health")
                response.raise_for_status()
                response_text = await response.aread()
                health_status = response_text.decode('utf-8').lower() != 'false'
                self._has_connected = True
                return health_status
        except Exception as e:
            logger.debug(f"Health check failed: {e}")
            if self._has_connected:
                return False
            else:
                raise e
