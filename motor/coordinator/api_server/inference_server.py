# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Inference plane: Worker subprocess only; provides /v1/completions, /v1/chat/completions,
/v1/messages, /v1/messages/count_tokens, /v1/models, etc.
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status

from motor.common.resources.instance import PDRole
from motor.common.logger import get_logger
from motor.common.http.key_encryption import verify_api_key_against_valid_keys
from motor.config.coordinator import CoordinatorConfig, RateLimitConfig
from motor.coordinator.api_server.base_server import BaseCoordinatorServer
from motor.coordinator.middleware.fastapi_middleware import (
    SimpleRateLimitMiddleware,
    create_simple_rate_limit_middleware,
)
from motor.coordinator.scheduler.runtime import SchedulerConnectionManager
from motor.coordinator.api_server.app_builder import AppBuilder
from motor.common.http.http_client import HTTPClientPool
from motor.coordinator.models.constants import OpenAIField
from motor.coordinator.models.request import RequestType
from motor.coordinator.domain.request_manager import RequestManager
from motor.coordinator.router.dispatch import handle_request
from motor.coordinator.tracer.tracing import TracerManager

logger = get_logger(__name__)


def get_request_manager(request: Request) -> RequestManager:
    """FastAPI dependency: inject RequestManager from app.state."""
    return request.app.state.request_manager


def _validate_anthropic_request(body_json: dict[str, Any], *, require_max_tokens: bool = True) -> None:
    """Validate Anthropic-style request body. Raises HTTPException on invalid."""
    if not body_json.get("model"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing required field: model",
        )
    messages = body_json.get("messages")
    if not messages or not isinstance(messages, list) or len(messages) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing required field: messages (must be a non-empty array)",
        )
    if require_max_tokens:
        max_tokens = body_json.get("max_tokens")
        if max_tokens is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing required field: max_tokens",
            )
        if not isinstance(max_tokens, int) or max_tokens <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="max_tokens must be a positive integer",
            )


def _validate_openai_request(body_json: dict[str, Any], request_type: RequestType) -> None:
    """Validate OpenAI-style request body. Raises HTTPException on invalid."""
    if OpenAIField.MODEL not in body_json:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Missing required field: {OpenAIField.MODEL}",
        )
    if request_type != RequestType.OPENAI:
        return
    if OpenAIField.PROMPT not in body_json and OpenAIField.MESSAGES not in body_json:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Missing required field: {OpenAIField.PROMPT} or {OpenAIField.MESSAGES}",
        )
    if OpenAIField.MESSAGES not in body_json:
        return
    if not isinstance(body_json[OpenAIField.MESSAGES], list) or len(body_json[OpenAIField.MESSAGES]) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {OpenAIField.MESSAGES} field: must be a non-empty array",
        )
    for i, message in enumerate(body_json[OpenAIField.MESSAGES]):
        if not isinstance(message, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid message format at index {i}: must be an object",
            )
        if OpenAIField.ROLE not in message or OpenAIField.CONTENT not in message:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(f"Invalid message at index {i}: missing {OpenAIField.ROLE} or {OpenAIField.CONTENT}"),
            )
        if message[OpenAIField.ROLE] not in ["system", "user", "assistant", "tool"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Invalid {OpenAIField.ROLE} "
                    f"'{message[OpenAIField.ROLE]}' at index {i}: must be system, "
                    "user, or assistant"
                ),
            )


class InferenceServer(BaseCoordinatorServer):
    """
    Inference plane: inference_app only, runs in Worker process; started by
    InferenceProcessManager via run_inference_worker_proc with uvicorn.
    """

    def __init__(
        self,
        config: CoordinatorConfig | None = None,
        *,
        request_manager: RequestManager,
    ):
        super().__init__(config)
        self._request_manager = request_manager
        self._api_key_config = self.coordinator_config.api_key_config
        self._infer_ssl_config = self.coordinator_config.infer_tls_config
        self._log_configuration()
        self._app_builder = AppBuilder(self.coordinator_config)
        self._inference_app = self._app_builder.create_inference_app(lifespan=self._lifespan)
        self._scheduler_connection = SchedulerConnectionManager.from_config(
            self.coordinator_config,
            on_instance_refreshed=self._make_on_instance_refreshed(),
        )
        self._rate_limit_middleware: Any | None = None
        self._register_routes()

    @property
    def app(self) -> FastAPI:
        """Inference FastAPI app, run by process_worker with uvicorn."""
        return self._inference_app

    @asynccontextmanager
    async def _lifespan(self, app: FastAPI):
        logger.info("Inference server is starting...")
        app.state.request_manager = self._request_manager
        # Precision check: inject logprobs + token-id sampling only when precision_check_enabled
        from motor.coordinator.fault_tolerance.precision.sample_controller import (
            SampleController,
        )
        from motor.coordinator.fault_tolerance.precision import (
            build_precision_reporter,
        )

        sampling_cfg = self.coordinator_config.token_sampling_config
        TracerManager(self.coordinator_config)
        await self._scheduler_connection.connect()
        if sampling_cfg.precision_check_enabled:
            scheduler_client = self._scheduler_connection.get_client()
            if scheduler_client is None:
                logger.warning(
                    "Precision check enabled but scheduler client unavailable; "
                    "disabling precision sampling until scheduler connects"
                )
                app.state.sampling_manager = None
            else:
                precision = build_precision_reporter(
                    sampling_cfg,
                    self.coordinator_config.infer_tls_config,
                    config=self.coordinator_config,
                    scheduler=scheduler_client,
                    request_manager=self._request_manager,
                    scheduler_client=scheduler_client,
                )
                sampling_manager = SampleController(
                    sampling_cfg,
                    precision,
                    scheduler_client=scheduler_client,
                )
                logger.info(
                    "Precision check (token sampling): interval=%.1fs logprobs_count=%d "
                    "threshold=%d probe_attempts=%d probe_timeout=%.1fs "
                    "exit_gate=scheduler_zmq streak=scheduler_zmq probe=internal_router",
                    sampling_cfg.interval_seconds,
                    sampling_cfg.logprobs_count,
                    sampling_cfg.precision_issue_threshold,
                    sampling_cfg.probe_max_attempts,
                    sampling_cfg.probe_timeout_seconds,
                )
                app.state.sampling_manager = sampling_manager
        else:
            app.state.sampling_manager = None
        try:
            yield
        except asyncio.CancelledError:
            logger.info("Inference server startup was cancelled")
        except Exception as e:
            logger.error("Inference server startup failed: %s", e)
            raise
        finally:
            logger.info("Inference server is shutting down...")
            try:
                TracerManager().shutdown()
            except Exception as e:
                logger.warning("TracerManager shutdown during lifespan: %s", e)
            await self._scheduler_connection.disconnect()

    def verify_api_key(self, request: Request) -> None:
        if not self._api_key_config.enable_api_key:
            return
        if request.url.path in self._api_key_config.skip_paths:
            return
        authorization = request.headers.get(self._api_key_config.header_name)
        if not authorization:
            logger.warning("API Key validation failed: missing Authorization header")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing Authorization header",
                headers={"WWW-Authenticate": "Bearer"},
            )
        api_key = authorization
        if self._api_key_config.key_prefix and authorization.startswith(self._api_key_config.key_prefix):
            api_key = authorization[len(self._api_key_config.key_prefix) :]
        if api_key in self._api_key_config.valid_keys:
            return
        if verify_api_key_against_valid_keys(api_key, self._api_key_config.valid_keys):
            return
        logger.warning("API Key validation failed: invalid key")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid API Key")

    def setup_rate_limiting(self, rate_limit_config: RateLimitConfig | None = None) -> None:
        if rate_limit_config is None:
            rate_limit_config = self.coordinator_config.rate_limit_config
        if not rate_limit_config.enable_rate_limit:
            logger.info("Rate limiting is disabled")
            return

        if rate_limit_config.provider == "olc":
            try:
                path = rate_limit_config.olc_config_path
                os.environ['OLC_CONFIG_PATH'] = path
                from olc.adapters.fastapi import OlcFastAPIAdapter, OlcAdapterConfig

                adapter_config = OlcAdapterConfig(tag_extractor=self._extract_tags_from_request)
                self._inference_app.add_middleware(OlcFastAPIAdapter, adapter_config)
                logger.info("Olc limit setup succeeded, config path: %s", path)
            except Exception as e:
                logger.error("Using simple rate limit, Failed to create olc limit middleware: %s", e, exc_info=True)
                self.build_simple_rate_limit(rate_limit_config)
        else:
            self.build_simple_rate_limit(rate_limit_config)

    def build_simple_rate_limit(self, rate_limit_config: RateLimitConfig | None = None) -> None:
        try:
            middleware = create_simple_rate_limit_middleware(
                app=self._inference_app,
                max_requests=rate_limit_config.max_requests,
                window_size=rate_limit_config.window_size,
            )
            self._rate_limit_middleware = middleware
            self._inference_app.add_middleware(
                SimpleRateLimitMiddleware,
                rate_limiter=middleware.rate_limiter,
                skip_paths=rate_limit_config.skip_paths,
                error_message=rate_limit_config.error_message,
                error_status_code=rate_limit_config.error_status_code,
            )
            logger.info(
                "Create simple limit: max_requests=%s/%ss",
                rate_limit_config.max_requests,
                rate_limit_config.window_size,
            )
        except Exception as e:
            logger.error("Failed to create simple rate limit: %s", e, exc_info=True)
            raise

    @staticmethod
    def _extract_tags_from_request(request: Request) -> dict:
        # "Extract tags from the request; the tag names are defined in olc.bean.dimension."
        dimension_dict = {}
        dimension_dict["URL"] = request.url.path
        dimension_dict["Method"] = request.method
        dimension_dict["IP"] = request.client.host if request.client else "unknown"
        return dimension_dict

    def _make_on_instance_refreshed(self):
        """Create on_instance_refreshed callback: cleanup and warmup HTTP pool on instance change."""
        tls_config = self.coordinator_config.infer_tls_config
        pool = HTTPClientPool()

        async def _callback(active_endpoints: list[tuple[str, str]]) -> None:
            active_keys = pool.get_pool_keys_for_endpoints(active_endpoints, tls_config=tls_config)
            closed_count = await pool.cleanup_unused_clients(active_keys)
            if closed_count > 0:
                logger.info(
                    "HTTP pool cleanup on instance change: closed %d unused client(s), active=%d",
                    closed_count,
                    len(active_keys),
                )
            if active_endpoints:
                results = await pool.warmup_clients(
                    endpoints=active_endpoints,
                    tls_config=tls_config,
                )
                new_count = sum(1 for v in results.values() if v)
                if new_count > 0:
                    logger.info(
                        "HTTP pool warmup on instance change: created %d new client(s)",
                        new_count,
                    )

        return _callback

    def _apply_config_changes(self, new_config: CoordinatorConfig) -> None:
        """Apply Infer-specific config changes."""
        self._api_key_config = new_config.api_key_config
        self._infer_ssl_config = new_config.infer_tls_config
        rlc = new_config.rate_limit_config
        if self._rate_limit_middleware is not None:
            if isinstance(self._rate_limit_middleware, SimpleRateLimitMiddleware):
                self._rate_limit_middleware.update_config(
                    skip_paths=rlc.skip_paths,
                    error_message=rlc.error_message,
                    error_status_code=rlc.error_status_code,
                    enabled=rlc.enable_rate_limit,
                )

    def _get_scheduler_client(self):
        """Return SchedulerClient used for scheduling (select_and_allocate, get_available_instances, etc.)."""
        client = self._scheduler_connection.get_client()
        if client is None:
            raise RuntimeError("SchedulerClient is not set; Scheduler process is required")
        return client

    async def _is_available(self) -> bool:
        """Whether instances are available (Worker reads SchedulerClient cache).
        PD mode: available if has P or P+D.
        """
        client = self._scheduler_connection.get_client()
        if client is None:
            return False
        readiness = await client.has_required_instances()
        return readiness.is_run()

    def _register_routes(self) -> None:
        @self._inference_app.post("/v1/completions")
        @self.timeout_handler()
        async def openai_completions(
            request: Request,
            request_manager: RequestManager = Depends(get_request_manager),
        ):
            self.verify_api_key(request)
            return await self._handle_openai_request(request, RequestType.OPENAI, request_manager)

        @self._inference_app.post("/v1/chat/completions")
        @self.timeout_handler()
        async def openai_chat_completions(
            request: Request,
            request_manager: RequestManager = Depends(get_request_manager),
        ):
            self.verify_api_key(request)
            return await self._handle_openai_request(request, RequestType.OPENAI, request_manager)

        @self._inference_app.post("/v1/messages")
        @self.timeout_handler()
        async def anthropic_messages(
            request: Request,
            request_manager: RequestManager = Depends(get_request_manager),
        ):
            self.verify_api_key(request)
            return await self._handle_anthropic_request(
                request,
                RequestType.ANTHROPIC,
                request_manager,
                require_max_tokens=True,
            )

        @self._inference_app.post("/v1/messages/count_tokens")
        @self.timeout_handler()
        async def anthropic_count_tokens(
            request: Request,
            request_manager: RequestManager = Depends(get_request_manager),
        ):
            self.verify_api_key(request)
            return await self._handle_anthropic_request(
                request,
                RequestType.ANTHROPIC,
                request_manager,
                require_max_tokens=False,
            )

        @self._inference_app.get("/v1/models")
        async def list_models():
            models = await self._build_models_metadata()
            if not models:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="AIGW model configuration is not available. Please configure aigw in user_config.json.",
                )
            return {"object": "list", "data": models}

    async def _build_models_metadata(self) -> list[dict[str, Any]]:
        base_model = self.coordinator_config.get_aigw_models()
        if not base_model:
            logger.warning("No AIGW models configured")
            return []
        scheduler = self._get_scheduler_client()
        p_instances = len(await scheduler.get_available_instances(PDRole.ROLE_P))
        d_instances = len(await scheduler.get_available_instances(PDRole.ROLE_D))
        enriched = {
            **base_model,
            "p_instances_num": p_instances,
            "d_instances_num": d_instances,
            "created": self._service_start_timestamp,
        }
        return [enriched]

    async def _handle_anthropic_request(
        self,
        request: Request,
        request_type: RequestType,
        request_manager: RequestManager,
        *,
        require_max_tokens: bool = True,
    ):
        try:
            body = await request.body()
            body_json = json.loads(body.decode("utf-8"))
            _validate_anthropic_request(body_json, require_max_tokens=require_max_tokens)
            if not await self._is_available():
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Service is not available",
                )
            return await handle_request(
                request,
                self.coordinator_config,
                scheduler=self._get_scheduler_client(),
                request_manager=request_manager,
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to process Anthropic request: %s", e, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(e),
            ) from e

    async def _handle_openai_request(
        self,
        request: Request,
        request_type: RequestType,
        request_manager: RequestManager,
    ):
        try:
            body = await request.body()
            body_json = json.loads(body.decode("utf-8"))
            _validate_openai_request(body_json, request_type)
            if not await self._is_available():
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Service is not available",
                )
            return await handle_request(
                request,
                self.coordinator_config,
                scheduler=self._get_scheduler_client(),
                request_manager=request_manager,
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to process OpenAI request: %s", e, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(e),
            ) from e

    def _initialize_config(self, coordinator_config: CoordinatorConfig | None) -> None:
        if coordinator_config is None:
            try:
                coordinator_config = CoordinatorConfig.from_json(None)
                logger.info("CoordinatorConfig loaded from file/env")
            except Exception as e:
                logger.error("Failed to load CoordinatorConfig: %s", e)
                raise RuntimeError("Failed to initialize CoordinatorConfig") from e
        self.coordinator_config = coordinator_config

    def _log_configuration(self) -> None:
        logger.info(
            "Infer timeout: infer_timeout=%ss",
            self.coordinator_config.exception_config.infer_timeout,
        )
        logger.info(
            "API Key: enabled=%s, valid_keys_count=%s, header=%s",
            self._api_key_config.enable_api_key,
            len(self._api_key_config.valid_keys),
            self._api_key_config.header_name,
        )
        if self._infer_ssl_config.enable_tls:
            logger.info(
                "Infer SSL: cert_file=%s, key_file=%s",
                self._infer_ssl_config.cert_file,
                self._infer_ssl_config.key_file,
            )
