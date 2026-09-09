# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
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
from motor.config.tls_config import TLSConfig

logger = get_logger(__name__)


class NativeEngineApiClient:
    """Read native engine operational endpoints over the inference channel."""

    @staticmethod
    def query_model_ids(address: str, tls_config: TLSConfig | None) -> list[str]:
        """Return the model IDs advertised by a native engine."""
        with SafeHTTPSClient(address=address, tls_config=tls_config, timeout=2) as client:
            response = client.do_get("/v1/models")
        if response.status_code != 200:
            raise RuntimeError(f"native engine /v1/models returned status={response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("native engine /v1/models returned an invalid response")
        return [
            model["id"]
            for model in payload["data"]
            if isinstance(model, dict) and isinstance(model.get("id"), str) and model["id"]
        ]

    @staticmethod
    def query_metrics(address: str, tls_config: TLSConfig | None) -> str:
        try:
            with SafeHTTPSClient(address=address, tls_config=tls_config, timeout=2) as client:
                response = client.do_get("/metrics")
            if response.status_code != 200:
                logger.warning(
                    "Coordinator native metrics request returned status=%s. address=%s",
                    response.status_code,
                    address,
                )
                return ""
            return response.text
        except Exception as err:
            logger.warning(
                "Coordinator native metrics request failed. address=%s, error=%s. "
                "Check native engine readiness, network reachability, and inference TLS configuration.",
                address,
                err,
            )
            return ""
