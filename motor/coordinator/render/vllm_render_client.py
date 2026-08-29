# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""HTTP client for the vLLM Render sidecar."""

import time
from http import HTTPStatus
from typing import Any

import httpx
from pydantic import ValidationError

from motor.common.utils.net import format_address
from motor.config.coordinator import RenderConfig
from motor.coordinator.render.api_spec import get_render_api_spec
from motor.coordinator.render.models import TokenizedRequest, TokenizerSource

_UNSUPPORTED_STATUS_CODES = {
    HTTPStatus.BAD_REQUEST,
    HTTPStatus.NOT_FOUND,
    HTTPStatus.METHOD_NOT_ALLOWED,
    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
    HTTPStatus.UNPROCESSABLE_ENTITY,
    HTTPStatus.NOT_IMPLEMENTED,
}
_CIRCUIT_COOLDOWN_SECONDS = 5.0


class RenderClientError(RuntimeError):
    """Base error for a failed Render operation."""


class RenderUnavailableError(RenderClientError):
    """Render endpoint cannot be reached or returned a transient failure."""


class RenderTimeoutError(RenderUnavailableError):
    """Render endpoint exceeded its configured request timeout."""


class RenderUnsupportedError(RenderClientError):
    """Render cannot process the request API or payload."""


class RenderInvalidResponseError(RenderClientError):
    """Render returned a response that cannot become a TokenizedRequest."""


class VLLMRenderClient:
    """Call vLLM Render endpoints without exposing vLLM response types."""

    def __init__(self, config: RenderConfig, http_client: httpx.AsyncClient | None = None) -> None:
        self._timeout = config.timeout_ms / 1000
        self._circuit_open_until = 0.0
        self._circuit_probe_in_flight = False
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=f"http://{format_address(config.endpoint.host, config.endpoint.port)}",
            timeout=httpx.Timeout(self._timeout),
        )

    async def health(self) -> bool:
        try:
            response = await self._client.get("/health", timeout=self._timeout)
            if response.is_success:
                self._close_circuit()
                return True
            self._open_circuit()
            return False
        except httpx.HTTPError:
            self._open_circuit()
            return False

    async def render(self, api: str, request_data: dict[str, Any]) -> list[TokenizedRequest]:
        spec = get_render_api_spec(api)
        if spec is None:
            raise RenderUnsupportedError(f"unsupported Render API: {api}")
        payload = await self._post_json(spec.render_path, request_data, "Render")

        render_items = payload if isinstance(payload, list) else [payload]
        if not render_items:
            self._open_circuit()
            raise RenderInvalidResponseError("Render response must not be empty")

        results = []
        try:
            for item in render_items:
                if not isinstance(item, dict):
                    raise RenderInvalidResponseError("Render response items must be JSON objects")
                if not isinstance(item.get("sampling_params"), dict):
                    raise RenderInvalidResponseError("Render response contains invalid sampling_params")
                results.append(
                    TokenizedRequest(
                        prompt_token_ids=item.get("token_ids"),
                        tokenizer_source=TokenizerSource.RENDER,
                        metadata={key: value for key, value in item.items() if key != "token_ids"},
                    )
                )
        except (ValidationError, RenderInvalidResponseError) as e:
            self._open_circuit()
            raise RenderInvalidResponseError("Render response contains invalid GenerateRequest items") from e
        return results

    async def derender(self, api: str, request_data: dict[str, Any]) -> dict[str, Any]:
        spec = get_render_api_spec(api)
        if spec is None:
            raise RenderUnsupportedError(f"unsupported Derender API: {api}")
        payload = await self._post_json(spec.derender_path, request_data, "Derender")
        if not isinstance(payload, dict) or not isinstance(payload.get("choices"), list):
            self._open_circuit()
            raise RenderInvalidResponseError("Derender response contains invalid choices")
        return payload

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _post_json(
        self,
        path: str,
        request_data: dict[str, Any],
        operation: str,
    ) -> Any:
        circuit_probe = self._enter_circuit(operation)
        try:
            return await self._post_json_unchecked(path, request_data, operation)
        finally:
            if circuit_probe:
                self._circuit_probe_in_flight = False

    async def _post_json_unchecked(
        self,
        path: str,
        request_data: dict[str, Any],
        operation: str,
    ) -> Any:
        try:
            response = await self._client.post(path, json=request_data, timeout=self._timeout)
        except httpx.TimeoutException as e:
            self._open_circuit()
            raise RenderTimeoutError(f"{operation} request timed out") from e
        except httpx.RequestError as e:
            self._open_circuit()
            raise RenderUnavailableError(f"{operation} request failed: {type(e).__name__}") from e

        if not response.is_success:
            error_type = (
                RenderUnsupportedError if response.status_code in _UNSUPPORTED_STATUS_CODES else RenderUnavailableError
            )
            if response.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
                self._open_circuit()
            raise error_type(f"{operation} returned HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as e:
            self._open_circuit()
            raise RenderInvalidResponseError(f"{operation} response is not valid JSON") from e
        if not isinstance(payload, (dict, list)):
            self._open_circuit()
            raise RenderInvalidResponseError(f"{operation} response must be a JSON object or array")
        self._close_circuit()
        return payload

    def _enter_circuit(self, operation: str) -> bool:
        if self._circuit_open_until <= 0:
            return False
        if time.monotonic() < self._circuit_open_until or self._circuit_probe_in_flight:
            raise RenderUnavailableError(f"{operation} skipped while Render circuit is open")
        self._circuit_probe_in_flight = True
        return True

    def _open_circuit(self) -> None:
        self._circuit_open_until = time.monotonic() + _CIRCUIT_COOLDOWN_SECONDS

    def _close_circuit(self) -> None:
        self._circuit_open_until = 0.0
