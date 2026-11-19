# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from motor.common.http.http_client import HttpStatusError, SafeHTTPSClient
from motor.common.logger import get_logger
from motor.config.tls_config import TLSConfig

logger = get_logger(__name__)

_PROFILE_START_ENDPOINT = "/start_profile"
_PROFILE_STOP_ENDPOINT = "/stop_profile"


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

    @staticmethod
    def start_profile(
        address: str,
        tls_config: TLSConfig | None,
        timeout: float = 10.0,
        body: dict | None = None,
    ) -> None:
        """Start the native engine profiler (vLLM ``POST /start_profile``)."""
        NativeEngineApiClient._post_profile_control(_PROFILE_START_ENDPOINT, address, tls_config, timeout, body)

    @staticmethod
    def stop_profile(
        address: str,
        tls_config: TLSConfig | None,
        timeout: float = 10.0,
        body: dict | None = None,
    ) -> None:
        """Stop the native engine profiler (vLLM ``POST /stop_profile``)."""
        NativeEngineApiClient._post_profile_control(_PROFILE_STOP_ENDPOINT, address, tls_config, timeout, body)

    @staticmethod
    def _post_profile_control(
        endpoint: str,
        address: str,
        tls_config: TLSConfig | None,
        timeout: float,
        body: dict | None,
    ) -> None:
        with SafeHTTPSClient(address=address, tls_config=tls_config, timeout=timeout) as client:
            response = client.do_post(endpoint, data=body)
        if response.status_code not in (200, 202, 204):
            raise HttpStatusError(
                response.status_code,
                f"native engine {endpoint} returned status={response.status_code}",
            )
