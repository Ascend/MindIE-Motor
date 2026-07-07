# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import json
from typing import Any

from fastapi import HTTPException, status
from fastapi.responses import JSONResponse, Response

from motor.common.constants import (
    CHAT_COMPLETION_PREFIX,
    COMPLETION_PREFIX,
    COMPLETION_SUFFIX,
)
from motor.common.resources.dispatch import (
    DispatchProfile,
    MotorDispatch,
    PrefillResult,
    infer_vllm_dispatch_profile_from_config,
)
from motor.engine_server.core.dispatch_adapter.base import (
    DispatchAdapter,
    DispatchMetaserverRequest,
    DispatchResponseContext,
)
from motor.engine_server.core.dispatch_adapter.normalization import (
    normalize_nonstream_body,
    normalize_stream_chunk,
)


class VLLMDispatchAdapter(DispatchAdapter):
    _METASERVER_PREFILL_WAIT_SECONDS = 30.0
    _LEGACY_CPCD_DISPATCH_MODE = "cpcd_separate"

    _METASERVER_REQUIRED_FIELDS = (
        "request_id",
        "do_remote_decode",
        "remote_block_ids",
        "remote_block_size",
        "remote_engine_id",
        "remote_host",
        "remote_port",
        "remote_cached_tokens",
    )

    def __init__(self, config) -> None:
        super().__init__(config)
        self._dispatch_profile = self._infer_dispatch_profile(config)

    async def _adapt_engine_body(self, body: dict[str, Any], dispatch: MotorDispatch) -> dict[str, Any]:
        body["request_id"] = dispatch.engine_request_id
        if dispatch.role == "decode":
            # In handoff the decode kv_transfer_params is the KV bootstrap threaded
            # in from the prefill result (_consume_prefill_result). The metaserver
            # callback is exclusive to the trigger profile, so a handoff connector
            # (e.g. MooncakeConnectorV1) must never be handed a metaserver URL -- it
            # expects remote_block_ids/remote_host/remote_port instead.
            if not self._uses_handoff(dispatch):
                prefill = dispatch.endpoints.prefill
                if prefill is not None and "kv_transfer_params" not in body:
                    body["kv_transfer_params"] = {
                        "do_remote_decode": False,
                        "do_remote_prefill": True,
                        "metaserver": f"{prefill.url.rstrip('/')}/v1/metaserver",
                    }
        elif dispatch.role == "prefill":
            body.setdefault("return_token_ids", True)
            self._apply_prefill_generation_params(body)
            if self._uses_handoff(dispatch):
                # Tell the producer engine to generate KV for a remote decode so its
                # response carries the kv_transfer_params bootstrap. Without this the
                # connector's request_finished returns no bootstrap and PD silently
                # degrades. Mirrors the vLLM-ascend native proxy build_prefill_request.
                body.setdefault(
                    "kv_transfer_params",
                    {"do_remote_decode": True, "do_remote_prefill": False},
                )
        return body

    async def maybe_prepare_response(
        self, body: dict[str, Any], dispatch: MotorDispatch | None
    ) -> dict[str, Any] | None:
        if dispatch is None or dispatch.role != "prefill":
            return None
        if self._uses_handoff(dispatch):
            return None
        await self._registry.cache_prefill_body(dispatch, body)
        return PrefillResult(
            root_request_id=dispatch.root_request_id,
            engine_request_id=dispatch.engine_request_id,
            pair_id=dispatch.pair_id,
            attempt_seq=dispatch.attempt_seq,
            status="prepared",
            handoff_mode="trigger",
        ).model_dump(mode="json")

    async def should_finish_prepared_response(
        self,
        prepared: dict[str, Any],
        dispatch: MotorDispatch | None,
    ) -> bool:
        if dispatch is None:
            return True
        return not (
            dispatch.role == "prefill"
            and prepared.get("status") == "prepared"
            and prepared.get("handoff_mode") == "trigger"
        )

    async def _consume_prefill_result(
        self,
        body: dict[str, Any],
        dispatch: MotorDispatch,
        prefill_result: PrefillResult,
    ) -> dict[str, Any]:
        if not self._uses_handoff(dispatch):
            return body
        if prefill_result.status != "completed":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="vLLM CPCD decode requires completed prefill result.",
            )
        if prefill_result.handoff_mode != "handoff":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="vLLM CPCD decode requires handoff prefill result.",
            )
        if prefill_result.payload:
            body["kv_transfer_params"] = prefill_result.payload.copy()
        return body

    async def prepare_metaserver_body(self, body: dict[str, Any]) -> dict[str, Any]:
        metaserver_request = await self.prepare_metaserver_request(body)
        return metaserver_request.engine_body

    async def prepare_metaserver_request(self, body: dict[str, Any]) -> DispatchMetaserverRequest:
        kv_transfer_params = self._extract_metaserver_kv_params(body)
        engine_request_id = self._extract_engine_request_id(kv_transfer_params)
        if not engine_request_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Metaserver request missing request_id.",
            )
        cached_entry = await self._registry.wait_prefill_entry(engine_request_id, self._METASERVER_PREFILL_WAIT_SECONDS)
        if cached_entry is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Prefill context not found for request_id {engine_request_id}.",
            )
        try:
            self._validate_metaserver_kv_params(kv_transfer_params)
        except HTTPException:
            await self.stop_peer(cached_entry.dispatch)
            await self.finish_dispatch(cached_entry.dispatch)
            raise
        cached = cached_entry.body.copy()
        self._apply_prefill_generation_params(cached)
        # Preserve trigger-provided KV metadata while keeping the original
        # client request body cached by the prefill leg.
        cached["kv_transfer_params"] = kv_transfer_params
        return DispatchMetaserverRequest(
            engine_body=cached,
            dispatch=cached_entry.dispatch,
        )

    async def normalize_response(self, response: Response, context: DispatchResponseContext) -> Response:
        if context.dispatch is None:
            return response
        raw_body = getattr(response, "body", None)
        if not raw_body:
            return response
        try:
            body = json.loads(raw_body)
        except (TypeError, json.JSONDecodeError, UnicodeDecodeError):
            return response
        if not isinstance(body, dict):
            return response
        if context.dispatch is not None and context.dispatch.role == "prefill" and self._uses_handoff(context.dispatch):
            # The decode leg consumes ``payload`` directly as its ``kv_transfer_params``
            # (see ``_consume_prefill_result``), and the engine connector reads the KV
            # bootstrap fields (do_remote_prefill, remote_block_ids, remote_host, ...) at
            # the top level of ``kv_transfer_params``.
            kv_transfer_params = body.get("kv_transfer_params")
            usage = body.get("usage")
            prefill_result = PrefillResult(
                root_request_id=context.dispatch.root_request_id,
                engine_request_id=context.dispatch.engine_request_id,
                pair_id=context.dispatch.pair_id,
                attempt_seq=context.dispatch.attempt_seq,
                status="completed",
                handoff_mode="handoff",
                payload=kv_transfer_params if isinstance(kv_transfer_params, dict) else {},
                # Preserve the prefill usage separately so the coordinator can still
                # capture prompt_tokens_details (cached tokens) -- payload now carries
                # only the KV bootstrap and no longer the full response body.
                usage=usage if isinstance(usage, dict) else None,
            )
            return JSONResponse(
                content=prefill_result.model_dump(mode="json"),
                status_code=response.status_code,
            )
        normalize_nonstream_body(
            body,
            client_expects_chat_shape=context.client_expects_chat_shape,
            req_id=context.dispatch.root_request_id,
            client_return_token_ids=context.client_return_token_ids,
        )
        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in ("content-length", "content-type")
        }
        return JSONResponse(
            content=body,
            status_code=response.status_code,
            headers=headers,
        )

    async def normalize_stream_chunk(
        self,
        chunk: bytes | str,
        context: DispatchResponseContext,
        state: dict[str, Any],
    ) -> bytes | str | None:
        if context.dispatch is None:
            return chunk
        return normalize_stream_chunk(
            chunk,
            client_expects_chat_shape=context.client_expects_chat_shape,
            req_id=context.dispatch.root_request_id,
            stream_state=state,
            client_return_token_ids=context.client_return_token_ids,
        )

    @staticmethod
    def _extract_engine_request_id(body: dict[str, Any]) -> str | None:
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            return None
        if request_id.startswith(CHAT_COMPLETION_PREFIX):
            return request_id.removeprefix(CHAT_COMPLETION_PREFIX)
        if request_id.startswith(COMPLETION_PREFIX) and request_id.endswith(COMPLETION_SUFFIX):
            return request_id.removeprefix(COMPLETION_PREFIX).removesuffix(COMPLETION_SUFFIX)
        return request_id

    @staticmethod
    def _extract_metaserver_kv_params(body: dict[str, Any]) -> dict[str, Any]:
        nested = body.get("kv_transfer_params")
        if isinstance(nested, dict):
            return nested.copy()
        return body.copy()

    @classmethod
    def _validate_metaserver_kv_params(cls, kv_transfer_params: dict[str, Any]) -> None:
        missing = [field for field in cls._METASERVER_REQUIRED_FIELDS if field not in kv_transfer_params]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Metaserver request missing KV fields: {', '.join(missing)}.",
            )
        if kv_transfer_params.get("do_remote_decode") is not True:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Metaserver request must set do_remote_decode=true.",
            )

    @staticmethod
    def _apply_prefill_generation_params(body: dict[str, Any]) -> None:
        body["stream"] = False
        body["max_tokens"] = 1
        if "max_completion_tokens" in body:
            body["max_completion_tokens"] = 1
        body["min_tokens"] = 1
        body.pop("stream_options", None)

    def _uses_handoff(self, dispatch: MotorDispatch) -> bool:
        return (
            self._dispatch_profile == DispatchProfile.HANDOFF
            or dispatch.dispatch_mode == self._LEGACY_CPCD_DISPATCH_MODE
        )

    @classmethod
    def _infer_dispatch_profile(cls, config) -> DispatchProfile:
        return infer_vllm_dispatch_profile_from_config(config)
