# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import inspect
import json
import multiprocessing
from abc import abstractmethod
from http import HTTPStatus
from typing import Any, AsyncGenerator, Awaitable, Callable

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import ValidationError

from motor.common.http.cert_util import CertUtil
from motor.common.logger import get_logger
from motor.common.resources.dispatch import DispatchStopState, MotorDispatch
from motor.engine_server.core.dispatch_adapter import create_dispatch_adapter
from motor.engine_server.core.config import IConfig
from motor.engine_server.core.dispatch_adapter.base import DispatchResponseContext
from motor.common.utils.net import format_address
from motor.engine_server.core.endpoint import Endpoint
from motor.engine_server.core.serving_error import map_serving_exception
from motor.engine_server.utils.cancellation import with_cancellation

logger = get_logger(__name__)

CONFIG_KEY = "_config"


class InferEndpoint(Endpoint):
    chat_completion_request: type[Any]
    completion_request: type[Any]

    def __init__(self, config: IConfig):
        self.config = config
        self.host = config.get_endpoint_config().host
        self.port = config.get_endpoint_config().port
        self.infer_tls_config = config.get_endpoint_config().deploy_config.infer_tls_config

        self.app = FastAPI(title="EngineServer InferEndpoint", lifespan=self.get_lifespan())

        self.app.extra[CONFIG_KEY] = self.config

        self._stop_event = multiprocessing.Event()
        self._server: uvicorn.Server | None = None
        self._server_process = multiprocessing.Process(
            target=self._run_server, name="infer_endpoint_process", daemon=False
        )
        self._run_http_in_process = True
        self.engine_type = config.get_endpoint_config().engine_type
        self.dispatch_adapter = create_dispatch_adapter(config)
        self.init_request_handlers()
        self._register_routes()

        self._register_snapshot_routes()

    @abstractmethod
    def get_lifespan(self) -> Callable[[FastAPI], AsyncGenerator[None, None]]:
        """Return lifespan async generator; state (openai_serving_chat etc.) must be set in lifespan."""
        pass

    @abstractmethod
    def init_request_handlers(self) -> None:
        """Set protocol classes (chat_completion_request, completion_request). State is set in lifespan."""
        pass

    def run(self):
        if getattr(self, "_run_http_in_process", False):
            logger.info("InferEndpoint running in same process (run_http_in_process=True).")
            self._run_server()
        elif self._server_process and not self._server_process.is_alive():
            self._server_process.start()
            logger.info("InferEndpoint started in process: http://%s", format_address(self.host, self.port))

    def join(self) -> None:
        self._server_process.join()
        logger.error("infer_endpoint process exited with code %s", self._server_process.exitcode)

    def wait(self) -> None:
        """Block until infer server exits. No-op when HTTP runs in-process (run() already blocks)."""
        if not getattr(self, "_run_http_in_process", True):
            self.join()

    def shutdown(self):
        if self._server:
            self._server.should_exit = True
            logger.info("InferEndpoint: Uvicorn server exit triggered")
        self._stop_event.set()
        logger.info("InferEndpoint stopped completely")

    async def _parse_openai_request(
        self, raw_request: Request, model: type[Any]
    ) -> tuple[Any | None, JSONResponse | None, DispatchResponseContext]:
        dispatch: MotorDispatch | None = None
        context: DispatchResponseContext | None = None
        try:
            original_body = await raw_request.json()
            body = original_body.copy()
            body, dispatch = await self.dispatch_adapter.adapt_request_body(body)
            context = DispatchResponseContext(
                api=raw_request.url.path.strip("/"),
                raw_path=raw_request.url.path,
                request_body=body,
                dispatch=dispatch,
                stream=bool(body.get("stream", False)),
                client_return_token_ids=bool(original_body.get("return_token_ids", False)),
                client_expects_chat_shape=("messages" in original_body or "chat/completions" in raw_request.url.path),
            )
            prepared = await self.dispatch_adapter.maybe_prepare_response(body, dispatch)
            if prepared is not None:
                if await self.dispatch_adapter.should_finish_prepared_response(prepared, dispatch):
                    await self.dispatch_adapter.finish_dispatch(dispatch)
                return None, JSONResponse(content=prepared), context
            # Keep raw_request consistent for engine serving code that reads
            # the request body again after protocol validation.
            self._replace_request_body(raw_request, body)
            return model.model_validate(body), None, context
        except (json.JSONDecodeError, ValidationError) as e:
            if context is not None:
                await self._handle_dispatch_failure(context)
            detail = e.errors() if isinstance(e, ValidationError) else f"Invalid JSON body: {e.msg}"
            raise HTTPException(status_code=HTTPStatus.BAD_REQUEST.value, detail=detail) from e
        except HTTPException:
            if context is not None:
                await self._handle_dispatch_failure(context)
            raise

    async def _call_openai_serving(
        self,
        call: Callable[[], Awaitable[Any]],
        context: DispatchResponseContext,
        *,
        normalize: bool = True,
    ) -> Any:
        try:
            response = await call()
            if await self.dispatch_adapter.is_dispatch_stopped(context.dispatch):
                raise HTTPException(
                    status_code=499,
                    detail="Dispatch stopped by peer.",
                )
            if (
                context.dispatch is not None
                and isinstance(response, Response)
                and response.status_code >= HTTPStatus.BAD_REQUEST.value
            ):
                await self._handle_dispatch_failure(context)
                return await self._normalize_openai_response(response, context) if normalize else response
        except Exception as e:
            mapped_error = map_serving_exception(
                e,
                map_unknown_to_http_500=context.dispatch is None,
            )
            if (mapped_error is e and not isinstance(e, HTTPException)) or (
                isinstance(mapped_error, HTTPException) and mapped_error.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR
            ):
                logger.exception(
                    "Engine serving request failed api=%s error_type=%s",
                    context.api,
                    type(e).__name__,
                )
            if context.dispatch is None:
                raise mapped_error from e
            await self._handle_dispatch_failure(context)
            mapped = self.dispatch_adapter.map_engine_error(mapped_error, context)
            if isinstance(mapped, HTTPException):
                raise mapped from e
            return mapped
        normalized = await self._normalize_openai_response(response, context) if normalize else response
        if not isinstance(normalized, StreamingResponse):
            await self.dispatch_adapter.finish_dispatch(context.dispatch)
        return normalized

    async def _normalize_openai_response(
        self,
        response: Any,
        context: DispatchResponseContext,
    ) -> Any:
        if isinstance(response, StreamingResponse):
            return self._wrap_streaming_response(response, context)
        if isinstance(response, Response):
            return await self.dispatch_adapter.normalize_response(response, context)
        return response

    def _wrap_streaming_response(
        self,
        response: StreamingResponse,
        context: DispatchResponseContext,
    ) -> StreamingResponse:
        async def _normalized_body():
            try:
                state: dict[str, Any] = {}
                async for chunk in response.body_iterator:
                    if await self.dispatch_adapter.is_dispatch_stopped(context.dispatch):
                        raise HTTPException(
                            status_code=499,
                            detail="Dispatch stopped by peer.",
                        )
                    normalized = await self.dispatch_adapter.normalize_stream_chunk(chunk, context, state)
                    if normalized:
                        yield normalized
            except Exception:
                await self._handle_dispatch_failure(context)
                raise
            finally:
                await self.dispatch_adapter.finish_dispatch(context.dispatch)

        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in ("content-length", "content-type")
        }
        return StreamingResponse(
            _normalized_body(),
            status_code=response.status_code,
            media_type=response.media_type,
            headers=headers,
            background=response.background,
        )

    @staticmethod
    def _replace_request_body(raw_request: Request, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        raw_request._body = encoded
        raw_request._json = body

    async def _handle_dispatch_failure(self, context: DispatchResponseContext) -> None:
        if context.dispatch is None:
            return
        if not await self.dispatch_adapter.is_dispatch_stopped(context.dispatch):
            await self.dispatch_adapter.stop_peer(context.dispatch)
        await self.dispatch_adapter.finish_dispatch(context.dispatch)

    async def _abort_engine_request(self, engine_request_id: str | None) -> None:
        if not engine_request_id:
            return
        candidates = [
            getattr(self.app.state, "engine_client", None),
            getattr(self.app.state, "tokenizer_manager", None),
        ]
        for candidate in candidates:
            if candidate is None:
                continue
            abort = getattr(candidate, "abort", None) or getattr(candidate, "abort_request", None)
            if abort is None:
                continue
            try:
                result = abort(engine_request_id)
                if inspect.isawaitable(result):
                    await result
                return
            except Exception as e:
                logger.warning(
                    "Dispatch engine abort failed engine_request_id=%s error=%s",
                    engine_request_id,
                    e,
                )
                return

    async def _chat_completion_body(self, raw_request: Request) -> Any:
        return await self._parse_openai_request(raw_request, self.chat_completion_request)

    async def _completion_body(self, raw_request: Request) -> Any:
        return await self._parse_openai_request(raw_request, self.completion_request)

    def _register_profile_routes_if_enabled(self) -> None:
        """Mirror vLLM profile API: POST /start_profile, /stop_profile when profiler is configured."""
        args = self.config.get_args()
        if args is None:
            return
        profiler_config = getattr(args, "profiler_config", None)
        profiler = getattr(profiler_config, "profiler", None) if profiler_config is not None else None
        if profiler is None:
            return
        logger.warning(
            "Profiler with mode '%s' is enabled on the infer API server. "
            "This should ONLY be used for local development!",
            profiler,
        )

        @self.app.post("/start_profile")
        async def start_profile(raw_request: Request):
            logger.info("Starting profiler...")
            engine_client = getattr(raw_request.app.state, "engine_client", None)
            if engine_client is None:
                raise HTTPException(
                    status_code=HTTPStatus.NOT_IMPLEMENTED.value,
                    detail="Profiling is not supported for this engine.",
                )
            await engine_client.start_profile()
            logger.info("Profiler started.")
            return Response(status_code=200)

        @self.app.post("/stop_profile")
        async def stop_profile(raw_request: Request):
            logger.info("Stopping profiler...")
            engine_client = getattr(raw_request.app.state, "engine_client", None)
            if engine_client is None:
                raise HTTPException(
                    status_code=HTTPStatus.NOT_IMPLEMENTED.value,
                    detail="Profiling is not supported for this engine.",
                )
            await engine_client.stop_profile()
            logger.info("Profiler stopped.")
            return Response(status_code=200)

    def _register_routes(self):
        @self.app.post("/v1/chat/completions")
        @with_cancellation
        async def create_chat_completion(
            raw_request: Request,
        ):
            request, prepared_response, context = await self._parse_openai_request(
                raw_request, self.chat_completion_request
            )
            if prepared_response is not None:
                return prepared_response
            return await self._call_openai_serving(
                lambda: self.app.state.openai_serving_chat.handle_request(request, raw_request),
                context,
            )

        @self.app.post("/v1/completions")
        @with_cancellation
        async def create_completion(
            raw_request: Request,
        ):
            request, prepared_response, context = await self._parse_openai_request(raw_request, self.completion_request)
            if prepared_response is not None:
                return prepared_response
            return await self._call_openai_serving(
                lambda: self.app.state.openai_serving_completion.handle_request(request, raw_request),
                context,
            )

        @self.app.post("/v1/metaserver")
        async def metaserver(raw_request: Request):
            try:
                body = await raw_request.json()
            except json.JSONDecodeError as e:
                raise HTTPException(
                    status_code=HTTPStatus.BAD_REQUEST.value,
                    detail=f"Invalid JSON body: {e.msg}",
                ) from e
            metaserver_request = await self.dispatch_adapter.prepare_metaserver_request(body)
            engine_body = metaserver_request.engine_body
            context = DispatchResponseContext(
                api="v1/metaserver",
                raw_path="/v1/metaserver",
                request_body=engine_body,
                dispatch=metaserver_request.dispatch,
                stream=False,
                client_return_token_ids=False,
                client_expects_chat_shape="messages" in engine_body,
            )
            self._replace_request_body(raw_request, engine_body)
            try:
                if "messages" in engine_body:
                    request = self.chat_completion_request.model_validate(engine_body)
                    return await self._call_openai_serving(
                        lambda: self.app.state.openai_serving_chat.handle_request(request, raw_request),
                        context,
                        normalize=False,
                    )
                request = self.completion_request.model_validate(engine_body)
            except ValidationError as e:
                await self._handle_dispatch_failure(context)
                raise HTTPException(status_code=HTTPStatus.BAD_REQUEST.value, detail=e.errors()) from e
            return await self._call_openai_serving(
                lambda: self.app.state.openai_serving_completion.handle_request(request, raw_request),
                context,
                normalize=False,
            )

        @self.app.get("/v1/models")
        async def list_models():
            models = getattr(self.app.state, "openai_serving_models", None)
            if models is None:
                raise HTTPException(
                    status_code=HTTPStatus.NOT_IMPLEMENTED.value,
                    detail="Model listing is not available for this engine.",
                )
            return await models.show_available_models()

        @self.app.get("/health")
        async def health(raw_request: Request):
            is_healthy = await self.app.state.health_checker()
            return is_healthy

        @self.app.post("/v1/dispatch/stop")
        async def dispatch_stop(raw_request: Request):
            try:
                body = await raw_request.json()
            except json.JSONDecodeError as e:
                raise HTTPException(
                    status_code=HTTPStatus.BAD_REQUEST.value,
                    detail=f"Invalid JSON body: {e.msg}",
                ) from e
            response = await self.dispatch_adapter.handle_stop(body)
            if response.state in (
                DispatchStopState.STOPPED,
                DispatchStopState.ALREADY_STOPPED,
            ):
                await self._abort_engine_request(body.get("engine_request_id"))
            return response.model_dump(mode="json")

        self._register_profile_routes_if_enabled()

    def _register_snapshot_routes(self):
        @self.app.post("/suspend")
        async def suspend_engine(raw_request: Request):
            model_save_path = raw_request.query_params.get("model_save_path")
            if model_save_path is None:
                raise HTTPException(
                    status_code=HTTPStatus.BAD_REQUEST.value,
                    detail="Missing required parameter: model_save_path",
                )
            if not all(
                callable(getattr(self.app.state.engine_client, method_name, None))
                for method_name in ["suspend", "resume"]
            ):
                raise HTTPException(
                    status_code=HTTPStatus.NOT_IMPLEMENTED.value,
                    detail="Snapshot suspend/resume is not supported for this engine.",
                )
            await self.app.state.engine_client.suspend(model_save_path=model_save_path)
            return Response(status_code=200)

        @self.app.post("/device_unlock")
        async def device_unlock_engine(raw_request: Request):
            if not callable(getattr(self.app.state.engine_client, "device_unlock", None)):
                raise HTTPException(
                    status_code=HTTPStatus.NOT_IMPLEMENTED.value,
                    detail="Snapshot device_unlock is not supported for this engine.",
                )
            await self.app.state.engine_client.device_unlock()
            return Response(status_code=200)

        @self.app.post("/resume")
        async def resume_engine(raw_request: Request):
            data_parallel_master_ip = raw_request.query_params.get("data_parallel_master_ip")
            model_path = raw_request.query_params.get("model_path")
            if data_parallel_master_ip is None or model_path is None:
                raise HTTPException(
                    status_code=HTTPStatus.BAD_REQUEST.value,
                    detail="Missing required parameter: data_parallel_master_ip and model_path",
                )
            if not all(
                callable(getattr(self.app.state.engine_client, method_name, None))
                for method_name in ["suspend", "resume"]
            ):
                raise HTTPException(
                    status_code=HTTPStatus.NOT_IMPLEMENTED.value,
                    detail="Snapshot suspend/resume is not supported for this engine.",
                )
            await self.app.state.engine_client.resume(
                data_parallel_master_ip=data_parallel_master_ip, model_path=model_path
            )
            return Response(status_code=200)

    def _run_server(self):
        config_kwargs = {
            "app": self.app,
            "host": self.host,
            "port": self.port,
            "log_level": "warning",
            "workers": 1,
            "loop": "uvloop",
            "http": "httptools",
        }
        config = uvicorn.Config(**config_kwargs)

        config.load()
        if self.infer_tls_config and self.infer_tls_config.enable_tls:
            ssl_context = CertUtil.create_ssl_context(self.infer_tls_config)
            if ssl_context:
                config.ssl = ssl_context
            else:
                raise RuntimeError("Failed to create ssl context")
            logger.info("InferEndpoint started: https://%s", format_address(self.host, self.port))
        else:
            logger.info("InferEndpoint started: http://%s", format_address(self.host, self.port))

        self._server = uvicorn.Server(config)
        if not self._stop_event.is_set():
            self._server.run()
