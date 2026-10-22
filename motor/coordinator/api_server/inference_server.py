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
/v1/responses, /v1/messages, /v1/messages/count_tokens, /v1/models, etc.
Dedicated per-worker metaserver (POST /v1/metaserver) is served on worker_metaserver_port
when inference_workers_config.worker_metaserver_base_port > 0.
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import TypeAdapter, ValidationError

from motor.common.http.http_client import HTTPClientPool
from motor.common.http.key_encryption import verify_api_key_against_valid_keys
from motor.common.logger import get_logger
from motor.common.resources.instance import PDRole
from motor.config.coordinator import CoordinatorConfig, RateLimitConfig
from motor.coordinator.api_server.app_builder import AppBuilder
from motor.coordinator.api_server.base_server import BaseCoordinatorServer
from motor.coordinator.domain.agent_hint import (
    AgentStatusHint,
    agent_hint_implies_manage_request,
)
from motor.coordinator.domain.agent_session_status import (
    AgentSessionStateMachine,
)
from motor.coordinator.domain.request_manager import RequestManager
from motor.coordinator.domain.scheduling import (
    InstanceReadiness,
    has_decode_colocation_candidate,
)
from motor.coordinator.middleware.fastapi_middleware import (
    RateLimitConfigHolder,
    SimpleRateLimitMiddleware,
)
from motor.coordinator.middleware.rate_limiter import SimpleRateLimiter
from motor.coordinator.models.constants import OpenAIField
from motor.coordinator.models.request import RequestType
from motor.coordinator.render.image_obfuscation_service import (
    ImageObfuscationService,
    resolve_image_obfuscation_config,
)
from motor.coordinator.render.token_obfuscation_service import (
    TokenObfuscationService,
    resolve_token_obfuscation_config,
)
from motor.coordinator.render.tokenization_service import TokenizationService
from motor.coordinator.render.vllm_render_client import VLLMRenderClient
from motor.coordinator.router.dispatch import handle_metaserver_request, handle_request
from motor.coordinator.scheduler.policy.kv_cache_affinity import TokenizerManager
from motor.coordinator.scheduler.runtime import SchedulerConnectionManager
from motor.coordinator.tracer.tracing import TracerManager

logger = get_logger(__name__)

_OPENAI_INT_ADAPTER = TypeAdapter(int)
_OPENAI_BOOL_ADAPTER = TypeAdapter(bool)


def get_request_manager(request: Request) -> RequestManager:
    """FastAPI dependency: inject RequestManager from app.state."""
    return request.app.state.request_manager


def _validate_positive_int_field(body_json: dict[str, Any], field_name: str) -> None:
    """Validate an optional positive integer field.

    If the field is present but not a positive integer, remove it from the
    request body and log a warning with the body content before removal.
    """
    value = body_json.get(field_name)
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        logger.warning(
            "Invalid %s=%r in request body, removing it.",
            field_name,
            value,
        )
        body_json.pop(field_name, None)


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


def _is_assistant_tool_call_message(message: dict[str, Any]) -> bool:
    """Return whether an assistant message has a non-empty OpenAI tool_calls list."""
    tool_calls = message.get(OpenAIField.TOOLS_CALLS)
    return message.get(OpenAIField.ROLE) == "assistant" and isinstance(tool_calls, list) and bool(tool_calls)


def _validate_message_array(messages: list[Any], field_name: str) -> None:
    """Validate and normalize messages in place, adding content=None for eligible assistant tool calls.

    Raises HTTPException when a message is invalid.
    """
    if len(messages) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {field_name} field: must be a non-empty array",
        )
    for i, message in enumerate(messages):
        if not isinstance(message, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid message format at index {i}: must be an object",
            )
        if OpenAIField.ROLE not in message:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid message at index {i}: missing {OpenAIField.ROLE}",
            )
        role = message[OpenAIField.ROLE]
        if role not in ["system", "developer", "user", "assistant", "tool"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Invalid {OpenAIField.ROLE} "
                    f"'{role}' at index {i}: must be system, "
                    "developer, user, assistant, or tool"
                ),
            )
        if OpenAIField.CONTENT not in message:
            if _is_assistant_tool_call_message(message):
                message[OpenAIField.CONTENT] = None
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid message at index {i}: missing {OpenAIField.CONTENT}",
                )


def _validate_responses_input_items(input_items: list[Any]) -> None:
    """Validate Responses message items while deferring other typed items to the engine."""
    if not input_items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid input field: must be a non-empty array",
        )
    for i, item in enumerate(input_items):
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid input item at index {i}: must be an object",
            )

        item_type = item.get("type")
        if item_type is None and OpenAIField.ROLE not in item and OpenAIField.CONTENT not in item:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid input item at index {i}: missing type or message fields",
            )
        if item_type is not None and (not isinstance(item_type, str) or not item_type):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid input item type at index {i}: must be a non-empty string",
            )
        if item_type not in (None, "message"):
            continue

        if OpenAIField.ROLE not in item or OpenAIField.CONTENT not in item:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(f"Invalid Responses message at index {i}: missing {OpenAIField.ROLE} or {OpenAIField.CONTENT}"),
            )
        if item[OpenAIField.ROLE] not in ["system", "developer", "user", "assistant"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Invalid {OpenAIField.ROLE} '{item[OpenAIField.ROLE]}' at index {i}: "
                    "must be system, developer, user, or assistant"
                ),
            )


def _validate_openai_request(body_json: dict[str, Any], request_type: RequestType) -> None:
    """Validate OpenAI-style request body. Raises HTTPException on invalid."""
    if OpenAIField.MODEL not in body_json:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Missing required field: {OpenAIField.MODEL}",
        )
    _validate_positive_int_field(body_json, OpenAIField.MAX_TOKENS)
    _validate_positive_int_field(body_json, OpenAIField.MAX_COMPLETION_TOKENS)
    for field_name, adapter in (
        (OpenAIField.STREAM, _OPENAI_BOOL_ADAPTER),
        (OpenAIField.MIN_TOKENS, _OPENAI_INT_ADAPTER),
    ):
        if field_name not in body_json or (field_name == OpenAIField.STREAM and body_json[field_name] is None):
            continue
        try:
            adapter.validate_python(body_json[field_name])
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid {field_name} field.",
            ) from exc

    min_tokens = (
        0
        if OpenAIField.MIN_TOKENS not in body_json
        else _OPENAI_INT_ADAPTER.validate_python(body_json[OpenAIField.MIN_TOKENS])
    )

    # Deliberately inspect the raw stream value, matching the engine protocol's
    # mode="before" validator. String "false" is truthy here and later coerced
    # to False, so Prefill and Decode consistently accept it.
    if body_json.get(OpenAIField.STREAM_OPTIONS) and not body_json.get(OpenAIField.STREAM):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Stream options can only be defined when `stream=True`.",
        )
    max_field = (
        OpenAIField.MAX_COMPLETION_TOKENS
        if OpenAIField.MESSAGES in body_json and OpenAIField.MAX_COMPLETION_TOKENS in body_json
        else OpenAIField.MAX_TOKENS
    )
    max_tokens = body_json.get(max_field)
    if min_tokens < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{OpenAIField.MIN_TOKENS} must be greater than or equal to 0.",
        )
    if max_tokens is not None and min_tokens > max_tokens:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{OpenAIField.MIN_TOKENS} must be less than or equal to {max_field}.",
        )
    if request_type != RequestType.OPENAI:
        return
    if OpenAIField.PROMPT not in body_json and OpenAIField.MESSAGES not in body_json:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Missing required field: {OpenAIField.PROMPT} or {OpenAIField.MESSAGES}",
        )
    if OpenAIField.MESSAGES in body_json:
        if not isinstance(body_json[OpenAIField.MESSAGES], list):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid {OpenAIField.MESSAGES} field: must be an array",
            )
        # Allow empty messages for manage requests (context_management or session_control)
        if len(body_json[OpenAIField.MESSAGES]) == 0 and agent_hint_implies_manage_request(body_json.get("agent_hint")):
            pass
        else:
            _validate_message_array(body_json[OpenAIField.MESSAGES], OpenAIField.MESSAGES)


def _validate_responses_request(body_json: dict[str, Any]) -> None:
    """Validate the required fields of a Responses create request."""
    model = body_json.get(OpenAIField.MODEL)
    if not isinstance(model, str) or not model:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Missing or invalid required field: {OpenAIField.MODEL}",
        )
    if "input" not in body_json:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing required field: input",
        )
    input_value = body_json["input"]
    if isinstance(input_value, str):
        if not input_value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid input field: must not be empty",
            )
        return
    if not isinstance(input_value, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid input field: must be a string or array",
        )
    _validate_responses_input_items(input_value)


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
        self._rate_limit_config: RateLimitConfigHolder | None = None
        self._agent_status_listener_wired = False
        status_cfg = self.coordinator_config.agent_status_config
        self._agent_status_machine = (
            AgentSessionStateMachine(
                session_ttl_s=status_cfg.session_ttl_s,
                max_sessions=status_cfg.max_sessions,
            )
            if status_cfg.enabled
            else None
        )
        self._register_routes()

    @property
    def app(self) -> FastAPI:
        """Inference FastAPI app, run by process_worker with uvicorn."""
        return self._inference_app

    def create_metaserver_app(self) -> FastAPI:
        """Dedicated per-worker app for Decode layerwise callbacks. No API key / TLS."""
        app = FastAPI(title="Inference Worker Metaserver")

        @app.post("/v1/metaserver")
        async def metaserver(request: Request):
            return await handle_metaserver_request(
                request,
                self.coordinator_config,
                scheduler=self._get_scheduler_client(),
                request_manager=self._request_manager,
            )

        return app

    @asynccontextmanager
    async def _lifespan(self, app: FastAPI):
        logger.info("Inference server is starting...")
        app.state.request_manager = self._request_manager
        # Precision check: inject logprobs + token-id sampling only when precision_check_enabled
        from motor.coordinator.fault_tolerance.precision import (
            build_precision_reporter,
        )
        from motor.coordinator.fault_tolerance.precision.sample_controller import (
            SampleController,
        )

        sampling_cfg = self.coordinator_config.precision_detection_config
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
                    "entry_admission=scheduler_zmq streak=scheduler_zmq probe=internal_router",
                    sampling_cfg.interval_seconds,
                    sampling_cfg.logprobs_count,
                    sampling_cfg.precision_issue_threshold,
                    sampling_cfg.probe_max_attempts,
                    sampling_cfg.probe_timeout_seconds,
                )
                app.state.sampling_manager = sampling_manager
        else:
            app.state.sampling_manager = None
        render_client = None
        app.state.tokenization_service = None
        app.state.token_obfuscation_service = None
        app.state.image_obfuscation_service = None
        try:
            obfuscation_config = self.coordinator_config.token_obfuscation_config
            render_config = self.coordinator_config.render_config
            if render_config.enable:
                if obfuscation_config.enable:
                    token_config, vocab_source = resolve_token_obfuscation_config(
                        obfuscation_config,
                        self.coordinator_config.engine_model_paths,
                    )
                    app.state.token_obfuscation_service = TokenObfuscationService(token_config)
                    logger.info("Token data obfuscation is enabled vocab_size_source=%s", vocab_source)
                image_obfuscation_service = None
                if obfuscation_config.enable and obfuscation_config.image_config.enable:
                    image_config, geometry_source = resolve_image_obfuscation_config(
                        obfuscation_config.image_config,
                        self.coordinator_config.engine_model_paths,
                    )
                    image_obfuscation_service = ImageObfuscationService(
                        image_config,
                        obfuscation_config.seed_content,
                    )
                    logger.info(
                        "Vision data obfuscation is enabled for Render image items geometry_source=%s",
                        geometry_source,
                    )
                app.state.image_obfuscation_service = image_obfuscation_service
                render_client = VLLMRenderClient(render_config)
                app.state.tokenization_service = TokenizationService(
                    render_config,
                    render_client=render_client,
                    local_tokenizer=TokenizerManager(self.coordinator_config),
                    context_budget_mode=self.coordinator_config.context_budget_mode,
                    obfuscation_service=app.state.token_obfuscation_service,
                    image_obfuscation_service=image_obfuscation_service,
                )
            yield
        except asyncio.CancelledError:
            logger.info("Inference server startup was cancelled")
        except Exception as e:
            logger.error("Inference server startup failed: %s", e)
            raise
        finally:
            logger.info("Inference server is shutting down...")
            sampling_manager = getattr(app.state, "sampling_manager", None)
            if sampling_manager is not None:
                await sampling_manager.shutdown()
            if render_client is not None:
                await render_client.aclose()
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
                from olc.adapters.fastapi import OlcAdapterConfig, OlcFastAPIAdapter

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
            holder = RateLimitConfigHolder(
                skip_paths=rate_limit_config.skip_paths,
                error_message=rate_limit_config.error_message,
                error_status_code=rate_limit_config.error_status_code,
                max_request_body_size=rate_limit_config.max_request_body_size,
                strict_priority_enabled=self.coordinator_config.strict_priority_config.enabled,
                strict_priority_tokens=list(self.coordinator_config.strict_priority_config.tokens),
            )

            rate_limiter = SimpleRateLimiter(
                max_requests=rate_limit_config.max_requests,
                window_size=rate_limit_config.window_size,
                priority_reserve_ratio=rate_limit_config.priority_reserve_ratio,
                priority_admission_level=self.coordinator_config.scheduler_config.priority_admission_level,
            )

            self._inference_app.add_middleware(
                SimpleRateLimitMiddleware,
                rate_limiter=rate_limiter,
                config_holder=holder,
            )

            holder.rate_limiter = rate_limiter
            self._rate_limit_config = holder

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
        dimension_dict = {
            "URL": request.url.path,
            "Method": request.method,
            "IP": request.client.host if request.client else "unknown",
        }
        return dimension_dict

    def _make_on_instance_refreshed(self):
        """Create on_instance_refreshed callback: cleanup and warmup HTTP pool on instance change."""
        tls_config = self.coordinator_config.infer_tls_config
        keepalive_expiry = self.coordinator_config.timeout_config.engine_client_keepalive_expiry
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
                    keepalive_expiry=keepalive_expiry,
                )
                new_count = sum(1 for v in results.values() if v)
                if new_count > 0:
                    logger.info(
                        "HTTP pool warmup on instance change: created %d new client(s)",
                        new_count,
                    )

        return _callback

    def _register_agent_status_routes(self) -> None:
        """Register the out-of-band agent_status routes (Phase 4, start-time).

        Wire contract (DSH emit seam, agent-hints plugin ``statusAuto``):
        ``POST <origin>/agent-status`` with ``{"session": {"session_id"},
        "agent_status": {...}}``; DSH sends no auth header and treats any
        non-2xx as retry-then-drop, so: no API-key check by default (and
        ``/agent-status`` is in the api-key + rate-limit default skip sets;
        ``agent_status_config.require_infer_auth`` inherits both gates for
        hardened deployments), structural errors answer 400 (client-visible),
        field-level malformations answer 200 with ``applied=false`` + WARN
        (never fails the sender).

        The scheduler client's PUB fan-out listener is wired to the local
        state machine here: POST finalize + PUB finalize must land on every
        worker, or a session could re-enter ``running`` on a worker that never
        saw the POST (review 194528015).
        """
        logger.info("agent_status out-of-band channel enabled (Phase 4)")

        @self._inference_app.post("/agent-status")
        @self.timeout_handler()
        async def agent_status_report(request: Request):
            return await self._handle_agent_status_report(request)

        @self._inference_app.get("/agent-status")
        @self.timeout_handler()
        async def agent_status_query(request: Request):
            return await self._handle_agent_status_query(request)

    def _wire_agent_status_listener(self) -> None:
        """Attach the local state machine to the scheduler PUB fan-out (best-effort)."""
        if self._agent_status_machine is None or getattr(self, "_agent_status_listener_wired", False):
            return
        try:
            client = self._get_scheduler_client()
        except RuntimeError:
            return
        machine = self._agent_status_machine

        def _apply_state_event(session_id: str, state: str) -> None:
            machine.apply(
                session_id,
                state=state,
            )

        client.agent_status_state_listener = _apply_state_event
        self._agent_status_listener_wired = True

    async def _handle_agent_status_report(self, request: Request) -> dict[str, Any]:
        """Apply one out-of-band status event; never 5xx (spec Phase 4 §2.2)."""
        if self._agent_status_machine is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="agent status channel is not enabled",
            )
        # Lazy wiring: the scheduler client connects asynchronously at startup;
        # once wired, PUB fan-out drives the local state machine too (review 194528015).
        self._wire_agent_status_listener()
        try:
            raw = await request.body()
            if not raw:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Request body cannot be empty")
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            # A non-UTF-8 body raises UnicodeDecodeError before json.loads ever
            # runs; it is a client-visible structural error (400), not a 500
            # (PR #1196 review).
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid JSON format: {str(e)}",
            ) from e
        if not isinstance(body, dict):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Request body must be a JSON object")
        session = body.get("session")
        status_data = body.get("agent_status")
        if not isinstance(session, dict) or not isinstance(status_data, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Both 'session' and 'agent_status' must be JSON objects",
            )
        session_id = session.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="session.session_id is required",
            )
        try:
            agent_status = AgentStatusHint.model_validate(status_data)
        except ValidationError as e:
            logger.warning(
                "agent_status event rejected (field-level invalid, WARN not 4xx): session_id=%s error=%s",
                session_id,
                e,
            )
            return {
                "status": "ok",
                "applied": False,
                "reason": "invalid_agent_status",
            }
        state = agent_status.state
        transition = self._agent_status_machine.apply(
            session_id,
            state,
            reason=agent_status.reason,
            tools=[tool.model_dump(exclude_none=True) for tool in agent_status.tools],
            state_since_ms=agent_status.state_since_ms,
        )
        logger.info(
            "agent_status event session_id=%s state=%s applied=%s reason=%s previous=%s",
            session_id,
            state,
            transition.applied,
            transition.reason,
            transition.previous_state,
        )
        if transition.applied and state == "finalize":
            await self._release_session_pins(session_id)
            logger.warning(
                "agent_status finalize needs engine KV action (offload); "
                "engine consumption capability unconfirmed (B1) — degraded to "
                "state tracking + pin release only: session_id=%s",
                session_id,
            )
        return {
            "status": "ok",
            "applied": transition.applied,
            "reason": transition.reason,
            "state": transition.state,
            "previous_state": transition.previous_state,
        }

    async def _handle_agent_status_query(self, request: Request) -> dict[str, Any]:
        """Serve the current lifecycle record for one session (observability)."""
        if self._agent_status_machine is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="agent status channel is not enabled",
            )
        session_id = request.query_params.get("session_id")
        if not session_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="session_id query parameter is required",
            )
        record = self._agent_status_machine.get(session_id)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No agent status record for session_id={session_id}",
            )
        return record.to_dict()

    async def _release_session_pins(self, session_id: str) -> None:
        """Release every role pin for session_id (local + fleet-wide PUB).

        Local drop first (the endpoint worker owns its own affinity table),
        then fan out via SchedulerServer PUB so every worker forgets the
        session (multi-worker pins). PUB is lossy by design (ZMQ does not
        queue): a missed event only leaves the pin for the normal
        stale/epoch/LRU fallback — affinity never fails a request.
        """
        client = self._scheduler_connection.get_client()
        if client is None:
            logger.warning(
                "agent_status finalize: scheduler client unavailable, pins kept: session_id=%s",
                session_id,
            )
            return
        dropped = client.drop_session_pins(session_id)
        logger.info(
            "agent_status finalize released session pins: session_id=%s dropped=%d",
            session_id,
            dropped,
        )
        await client.report_agent_status(session_id, "finalize")

    def _apply_config_changes(self, new_config: CoordinatorConfig) -> None:
        """Apply Infer-specific config changes."""
        self._api_key_config = new_config.api_key_config
        # agent_status enabled gates the route at process start (route
        # registration is not hot-reloaded); TTL / capacity hot-reload here.
        if self._agent_status_machine is not None:
            self._agent_status_machine.update_limits(
                session_ttl_s=new_config.agent_status_config.session_ttl_s,
                max_sessions=new_config.agent_status_config.max_sessions,
            )
        self._infer_ssl_config = new_config.infer_tls_config
        rlc = new_config.rate_limit_config
        if self._rate_limit_config is not None:
            holder = self._rate_limit_config
            holder.skip_paths = rlc.skip_paths
            holder.error_message = rlc.error_message
            holder.error_status_code = rlc.error_status_code
            holder.enabled = rlc.enable_rate_limit
            holder.max_request_body_size = rlc.max_request_body_size
            # strict trust gate must follow hot-reloaded config: a rotated or
            # disabled token set must take effect immediately (middleware
            # live-reads the holder).
            holder.strict_priority_enabled = new_config.strict_priority_config.enabled
            holder.strict_priority_tokens = list(new_config.strict_priority_config.tokens)
            if holder.rate_limiter is not None:
                holder.rate_limiter.update_config(
                    max_requests=rlc.max_requests,
                    window_size=rlc.window_size,
                    priority_reserve_ratio=rlc.priority_reserve_ratio,
                )

    def _get_scheduler_client(self):
        """Return SchedulerClient used for scheduling (select_and_allocate, get_available_instances, etc.)."""
        client = self._scheduler_connection.get_client()
        if client is None:
            raise RuntimeError("SchedulerClient is not set; Scheduler process is required")
        return client

    async def _is_available(self) -> bool:
        """Whether instances are available (Worker reads SchedulerClient cache).
        PD mode: decode-only is available when hybrid fallback is enabled.
        """
        client = self._scheduler_connection.get_client()
        if client is None:
            return False
        readiness = await client.has_required_instances()
        if readiness == InstanceReadiness.ONLY_DECODE:
            fallback_enabled = self.coordinator_config.scheduler_config.enable_pd_separation_fallback_to_hybrid
            return fallback_enabled and await has_decode_colocation_candidate(client)
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

        @self._inference_app.post("/v1/responses")
        @self.timeout_handler()
        async def openai_responses(
            request: Request,
            request_manager: RequestManager = Depends(get_request_manager),
        ):
            self.verify_api_key(request)
            return await self._handle_responses_request(request, request_manager)

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

        if self.coordinator_config.agent_status_config.enabled:
            self._register_agent_status_routes()

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
                request_json=body_json,
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
                request_json=body_json,
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to process OpenAI request: %s", e, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(e),
            ) from e

    async def _handle_responses_request(
        self,
        request: Request,
        request_manager: RequestManager,
    ):
        """Validate and transparently route a native Responses create request."""
        try:
            body_json = json.loads((await request.body()).decode("utf-8"))
            _validate_responses_request(body_json)
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
                request_json=body_json,
            )
        except HTTPException:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid JSON format",
            ) from error
        except Exception as error:
            logger.error("Failed to process Responses request: %s", error, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(error),
            ) from error

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
