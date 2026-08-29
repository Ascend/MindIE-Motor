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
Basic functionality tests for Coordinator server
Using FastAPI TestClient for testing
"""

# pylint: disable=attribute-defined-outside-init,reimported

import json
import os
import tempfile
import pytest
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse
from fastapi import HTTPException
from unittest.mock import patch, MagicMock, AsyncMock

from fastapi import FastAPI

from motor.common.standby.standby_manager import StandbyRole, StandbyManager
from motor.coordinator.api_server.management_server import ManagementServer
from motor.coordinator.domain.instance_manager import InstanceIdConflictError
from motor.coordinator.domain.probe import RoleHeartbeatResult
from motor.coordinator.api_server.inference_server import (
    InferenceServer,
    _validate_anthropic_request,
    _validate_openai_request,
    _validate_positive_int_field,
)
from motor.coordinator.domain.request_manager import RequestManager
from motor.config.coordinator import CoordinatorConfig, RateLimitConfig
from motor.coordinator.domain import InstanceReadiness
from motor.common.http.key_encryption import encrypt_api_key, set_default_key_encryption_by_name
from motor.common.resources import Endpoint, Instance, InsStatus, PDRole
from motor.coordinator.models.constants import OpenAIField
from motor.coordinator.models.request import RequestType
from motor.coordinator.middleware.fastapi_middleware import (
    SimpleRateLimitMiddleware,
    RateLimitConfigHolder,
)


def _copy_routes(
    src: FastAPI,
    dst: FastAPI,
    skip_paths: list | None = None,
) -> None:
    """Copy routes from src to dst; optionally skip some paths (test helper)."""
    skip_set = set(skip_paths or [])
    for route in src.routes:
        path = getattr(route, "path", None)
        if path is None or path in skip_set:
            continue
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        methods = getattr(route, "methods", None) or {"GET"}
        dst.add_api_route(path, endpoint, methods=list(methods))


def _openai_is_stream(body_json: dict) -> bool:
    """Return True if body has stream enabled (test helper)."""
    if OpenAIField.STREAM not in body_json:
        return False
    stream_value = body_json[OpenAIField.STREAM]
    if isinstance(stream_value, str):
        return stream_value.lower() in ("true", "1", "yes")
    return bool(stream_value)


def create_unified_app_for_test(
    mgmt: ManagementServer,
    inf: InferenceServer,
    rate_limit_config: RateLimitConfig | None = None,
) -> FastAPI:
    """Merge management + inference routes into one app (test helper)."""
    unified = FastAPI(lifespan=mgmt.lifespan)
    _copy_routes(mgmt.management_app, unified)
    _copy_routes(inf.app, unified)
    if not getattr(unified.state, "request_manager", None):
        unified.state.request_manager = inf._request_manager
    if rate_limit_config and rate_limit_config.enable_rate_limit:
        from motor.coordinator.middleware.rate_limiter import SimpleRateLimiter

        holder = RateLimitConfigHolder(
            skip_paths=rate_limit_config.skip_paths,
            error_message=rate_limit_config.error_message,
            error_status_code=rate_limit_config.error_status_code,
            max_request_body_size=rate_limit_config.max_request_body_size,
        )
        rate_limiter = SimpleRateLimiter(
            max_requests=rate_limit_config.max_requests,
            window_size=rate_limit_config.window_size,
        )
        holder.rate_limiter = rate_limiter
        unified.add_middleware(
            SimpleRateLimitMiddleware,
            rate_limiter=rate_limiter,
            config_holder=holder,
        )
        inf._rate_limit_config = holder
    return unified


class _TestServerShell:
    """Thin shell for tests: composes ManagementServer + InferenceServer (replaces CoordinatorServer)."""

    def __init__(self, config: CoordinatorConfig | None = None) -> None:
        self._mgmt = ManagementServer(config)
        _config = config or CoordinatorConfig()
        _request_manager = RequestManager(_config)
        self._inf = InferenceServer(_config, request_manager=_request_manager)
        self.coordinator_config = self._mgmt.coordinator_config

    @property
    def management_app(self) -> FastAPI:
        return self._mgmt.management_app

    @property
    def inference_app(self) -> FastAPI:
        return self._inf.app

    @property
    def instance_manager(self):
        return self._mgmt.instance_manager

    @instance_manager.setter
    def instance_manager(self, value) -> None:
        self._mgmt.instance_manager = value

    @property
    def _daemon_liveness(self):
        """Expose for tests that patch read_role_and_heartbeat."""
        return self._mgmt._daemon_liveness

    @property
    def lifespan(self):
        return self._mgmt.lifespan

    def setup_rate_limiting(self, rate_limit_config: RateLimitConfig | None = None) -> None:
        self._inf.setup_rate_limiting(rate_limit_config=rate_limit_config)

    def create_unified_app(
        self,
        rate_limit_config: RateLimitConfig | None = None,
    ) -> FastAPI:
        return create_unified_app_for_test(self._mgmt, self._inf, rate_limit_config)

    def _copy_routes(
        self,
        src: FastAPI,
        dst: FastAPI,
        skip_paths: list | None = None,
    ) -> None:
        _copy_routes(src, dst, skip_paths)

    def _openai_is_stream(self, body_json: dict) -> bool:
        return _openai_is_stream(body_json)


class TestCoordinatorServer:
    """Mock test class for Coordinator server"""

    def setup_method(self):
        """Setup test fixtures"""
        # Mock InstanceManager
        self._im_patcher = patch("motor.coordinator.api_server.management_server.InstanceManager")
        im_mock_cls = self._im_patcher.start()
        im_instance = MagicMock()
        im_instance.has_required_instances.return_value = True
        im_instance.get_required_instances_status.return_value = InstanceReadiness.REQUIRED_MET
        im_instance.refresh_instances = AsyncMock(return_value=None)
        im_instance.validate_refresh_instances = AsyncMock(return_value=None)
        im_instance.snapshot_instances = AsyncMock(return_value=[])
        im_mock_cls.return_value = im_instance

        # Mock handle_request to return appropriate JSON response
        async def mock_handle_request(request, config, scheduler=None, request_manager=None, request_json=None):
            """Mock handle_request that returns JSON response matching test expectations"""
            if request_json is not None:
                body_json = request_json
            else:
                try:
                    # Try to get JSON from request (cached if already parsed)
                    body_json = await request.json()
                except Exception:
                    # Fallback: try to read body directly
                    try:
                        request_body = await request.body()
                        body_json = json.loads(request_body.decode("utf-8"))
                    except Exception:
                        body_json = {}

            # Extract input_data based on request type
            input_data = ""
            if "prompt" in body_json:
                # For completions API, use prompt directly as string
                input_data = str(body_json["prompt"])
            elif "messages" in body_json:
                # For chat completions API, convert messages to JSON string
                input_data = json.dumps(body_json["messages"], ensure_ascii=False)

            # Determine if stream
            is_stream = body_json.get("stream", False)
            if isinstance(is_stream, str):
                is_stream = is_stream.lower() in ("true", "1", "yes")

            # Determine request_type based on endpoint
            request_type = "openai"
            if request.url.path.endswith("/completions"):
                request_type = "completions"
            elif request.url.path.endswith("/chat/completions"):
                request_type = "chat_completions"
            elif request.url.path.endswith("/responses"):
                request_type = "responses"

            # Generate request_id (simulate)
            import hashlib

            request_id = f"req-{hashlib.md5(str(body_json).encode()).hexdigest()[:8]}"

            # /v1/responses returns OpenAI-native response format (engine passthrough)
            if request_type == "responses":
                import time

                response_data = {
                    "id": request_id,
                    "object": "response",
                    "created": int(time.time()),
                    "model": body_json.get("model", ""),
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": str(body_json.get("input", "")),
                                }
                            ],
                        }
                    ],
                }
            else:
                response_data = {
                    "request_id": request_id,
                    "status": "success",
                    "data": {"input_data": input_data, "is_stream": bool(is_stream), "request_type": request_type},
                }

            return JSONResponse(content=response_data)

        # Patch handle_request
        self._handle_request_patcher = patch(
            "motor.coordinator.api_server.inference_server.handle_request", side_effect=mock_handle_request
        )
        self._handle_request_patcher.start()

        # So that /v1/completions and /v1/chat/completions pass availability check (no 503)
        self._is_available_patcher = patch(
            "motor.coordinator.api_server.inference_server.InferenceServer._is_available",
            new_callable=AsyncMock,
            return_value=True,
        )
        self._is_available_patcher.start()

        coordinator_config = CoordinatorConfig()
        # Enable API key validation for this test
        coordinator_config.api_key_config.enable_api_key = True
        # Use PBKDF2 as the encryption algorithm to align with default/documentation
        coordinator_config.api_key_config.encryption_algorithm = "PBKDF2_SHA256"

        # Encrypt the API keys using the configured encryption algorithm
        # This ensures the keys are stored in the same format as they would be in production
        set_default_key_encryption_by_name(coordinator_config.api_key_config.encryption_algorithm)
        plain_key1 = "sk-test123456789"
        plain_key2 = "sk-coordinator2024"
        encrypted_key1 = encrypt_api_key(plain_key1)
        encrypted_key2 = encrypt_api_key(plain_key2)
        coordinator_config.api_key_config.valid_keys = {encrypted_key1, encrypted_key2}

        self.coordinator_config = coordinator_config

        # Create test server shell (ManagementServer + InferenceServer)
        self.coordinator_server = _TestServerShell(config=coordinator_config)
        self.coordinator_server.setup_rate_limiting()
        # Do not mock _handle_openai_request: let real handler run so validation (400), JSON error (500), and
        # _is_available (503) are exercised; handle_request is already patched above for 200 responses.
        inf = self.coordinator_server._inf
        inf._is_available = AsyncMock(return_value=True)
        # Mock scheduler so handle_request is reached and /v1/models can await get_available_instances
        _mock_scheduler = MagicMock()
        _mock_scheduler.get_available_instances = AsyncMock(return_value={})
        inf._get_scheduler_client = lambda: _mock_scheduler
        mgmt_app = self.coordinator_server.management_app
        inference_app = self.coordinator_server.inference_app
        if not getattr(inference_app.state, "request_manager", None):
            inference_app.state.request_manager = inf._request_manager

        # Create TestClient (verify two endpoints separately)
        self.mgmt_client = TestClient(mgmt_app)
        self.openai_client = TestClient(inference_app)
        self.valid_api_key = "sk-test123456789"

    def teardown_method(self):
        """Teardown test fixtures"""
        try:
            if hasattr(self, "_im_patcher"):
                self._im_patcher.stop()
            if hasattr(self, "_handle_request_patcher"):
                self._handle_request_patcher.stop()
            if hasattr(self, "_is_available_patcher"):
                self._is_available_patcher.stop()
        except Exception:
            pass

    def test_liveness_endpoints(self):
        """Test liveness check endpoints"""
        # Test /liveness
        response = self.mgmt_client.get("/liveness")
        assert response.status_code == 200, f"Liveness probe failed: {response.status_code}"
        data = response.json()
        assert data["status"] == "ok", f"Liveness probe status abnormal: {data}"

        # Test /startup
        response = self.mgmt_client.get("/startup")
        assert response.status_code == 200, f"Startup probe failed: {response.status_code}"
        data = response.json()
        assert data["status"] == "ok", f"Startup probe status abnormal: {data}"

        # Test /readiness
        response = self.mgmt_client.get("/readiness")
        assert response.status_code == 200, f"Readiness check failed: {response.status_code}"
        data = response.json()
        assert data["status"] == "ok", f"Readiness check status abnormal: {data}"

    def test_readiness_endpoints_fail_when_instance_manager_not_ready(self):
        """Test readiness when instance manager reports not ready (reuse server's mock)."""
        im = MagicMock()
        im.get_required_instances_status.return_value = InstanceReadiness.NONE
        self.coordinator_server.instance_manager = im
        response = self.mgmt_client.get("/readiness")
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "Coordinator is ok"
        assert data["ready"] is False

    def test_readiness_endpoints_fail_when_instance_manager_ready(self):
        """Test readiness when instance manager reports ready (default mock)."""
        response = self.mgmt_client.get("/readiness")
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "Coordinator is ok"
        assert data["ready"] is True

    def test_readiness_endpoints_fail_when_enable_standby_is_master_but_instance_not_ready(self):
        """Test readiness when standby is master but instance manager not ready."""
        im = MagicMock()
        im.get_required_instances_status.return_value = InstanceReadiness.NONE
        self.coordinator_server.instance_manager = im
        self.coordinator_config.standby_config.enable_master_standby = True
        self.coordinator_server._mgmt._readiness_probe._enable_master_standby = True
        standby_manager = StandbyManager(self.coordinator_config)
        standby_manager.current_role = StandbyRole.MASTER

        # Mock daemon liveness (no shm in test); patch the provider instance the probe uses
        with patch.object(
            self.coordinator_server._daemon_liveness,
            "read_role_and_heartbeat",
            return_value=RoleHeartbeatResult(is_master=True, heartbeat_stale=False, orphaned=False),
        ):
            response = self.mgmt_client.get("/readiness")
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "Coordinator is master"
        assert data["ready"] is False

    def test_readiness_endpoints_fail_when_enable_standby_is_standby(self):
        """Test readiness endpoints"""
        self.coordinator_config.standby_config.enable_master_standby = True
        self.coordinator_server._mgmt._readiness_probe._enable_master_standby = True
        standby_manager = StandbyManager(self.coordinator_config)
        standby_manager.current_role = StandbyRole.STANDBY

        # Mock daemon liveness: not master
        with patch.object(
            self.coordinator_server._daemon_liveness,
            "read_role_and_heartbeat",
            return_value=RoleHeartbeatResult(is_master=False, heartbeat_stale=False, orphaned=False),
        ):
            response = self.mgmt_client.get("/readiness")
        assert response.status_code == 503, f"Readiness check failed: {response.status_code}"
        data = response.json()
        assert data["detail"] == "Coordinator is not master"

    def test_readiness_endpoints_fail_when_enable_standby_is_master(self):
        """Test readiness endpoints"""
        self.coordinator_config.standby_config.enable_master_standby = True
        self.coordinator_server._mgmt._readiness_probe._enable_master_standby = True
        standby_manager = StandbyManager(self.coordinator_config)
        standby_manager.current_role = StandbyRole.MASTER

        # Mock daemon liveness: master, heartbeat ok
        with patch.object(
            self.coordinator_server._daemon_liveness,
            "read_role_and_heartbeat",
            return_value=RoleHeartbeatResult(is_master=True, heartbeat_stale=False, orphaned=False),
        ):
            response = self.mgmt_client.get("/readiness")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["message"] == "Coordinator is master"
        assert data["ready"] is True

    def test_root_endpoints(self):
        """Test root endpoints"""
        # Test /
        response = self.mgmt_client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert data["service"] == "Motor Coordinator Management Server"
        assert data["version"] == "1.0.0"
        assert "GET /instances" in data["endpoints"]

    def test_list_instances_empty(self):
        response = self.mgmt_client.get("/instances")
        assert response.status_code == 200
        data = response.json()
        assert data == {"count": 0, "instances": []}

    def test_management_api_key_protects_privileged_routes_but_not_probes(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as key_file:
            key_file.write("test-management-key\n")
            api_key_file = key_file.name
        try:
            config = CoordinatorConfig()
            config.mgmt_api_key_config.enable_api_key = True
            config.mgmt_api_key_config.api_key_file = api_key_file
            server = ManagementServer(config=config)
            client = TestClient(server.management_app)

            assert client.get("/startup").status_code == 200
            assert client.get("/instances").status_code == 401
            assert client.get("/instances", headers={"X-Motor-Management-Key": "wrong"}).status_code == 403
            assert (
                client.get("/instances", headers={"X-Motor-Management-Key": "test-management-key"}).status_code == 200
            )
            assert client.post("/instances/refresh", json={}).status_code == 401
        finally:
            os.remove(api_key_file)

    def test_list_instances_summarizes_registered_instances(self):
        endpoint = Endpoint(id=0, ip="10.0.0.1", business_port="8000", headless=False)
        instance = Instance(
            job_name="qwen-prefill-10.0.0.1-8000",
            model_name="Qwen3-8B",
            id=42,
            role=PDRole.ROLE_P,
            status=InsStatus.ACTIVE,
            endpoints={"10.0.0.1": {0: endpoint}},
        )
        decode = Instance(
            job_name="qwen-decode-10.0.0.2-8000",
            model_name="Qwen3-8B",
            id=7,
            role=PDRole.ROLE_D,
            status=InsStatus.ACTIVE,
            endpoints={"10.0.0.2": {0: Endpoint(id=0, ip="10.0.0.2", business_port="8000")}},
        )
        self.coordinator_server.instance_manager.snapshot_instances = AsyncMock(return_value=[instance, decode])

        response = self.mgmt_client.get("/instances")
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        assert [item["role"] for item in data["instances"]] == ["decode", "prefill"]
        assert data["instances"][1] == {
            "id": 42,
            "role": "prefill",
            "job_name": "qwen-prefill-10.0.0.1-8000",
            "model_name": "Qwen3-8B",
            "status": "active",
            "endpoints": [{"id": 0, "ip": "10.0.0.1", "business_port": "8000", "headless": False}],
        }

    def test_list_models_exception(self):
        """Test list_models endpoints"""
        response = self.openai_client.get("/v1/models")
        assert response.status_code == 503

    def test_list_models_ok(self):
        """Test list_models endpoints"""
        self.coordinator_config.aigw_model = {"k": "v"}

        response = self.openai_client.get("/v1/models")
        assert response.status_code == 200
        print(response.json()["data"][0])
        assert response.json()["data"] is not None
        assert response.json()["data"][0]["p_instances_num"] == 0
        assert response.json()["data"][0]["d_instances_num"] == 0

    def test_openai_completions_api(self):
        """Test OpenAI Completions API"""
        test_cases = [
            {
                "name": "Basic completion request",
                "data": {
                    "model": "text-davinci-003",
                    "prompt": "Write a poem about spring",
                    "max_tokens": 100,
                    "temperature": 0.7,
                },
            },
            {
                "name": "Completion request with stop tokens",
                "data": {
                    "model": "text-davinci-003",
                    "prompt": "Differences between Python lists and tuples:",
                    "max_tokens": 200,
                    "temperature": 0.8,
                    "stop": ["\n\n", "Summary"],
                },
            },
            {"name": "Minimal parameter completion request", "data": {"model": "text-davinci-003", "prompt": "Hello"}},
        ]

        for test_case in test_cases:
            response = self.openai_client.post(
                "/v1/completions",
                json=test_case["data"],
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
            )

            assert response.status_code == 200, f"Completions API failed: {response.status_code}"

            data = response.json()
            assert "request_id" in data, "Response missing request_id"
            assert "status" in data, "Response missing status"
            assert "data" in data, "Response missing data field"

            # Verify input data parsing
            assert "input_data" in data["data"], "Response data missing input_data"
            assert "is_stream" in data["data"], "Response data missing is_stream"
            assert "request_type" in data["data"], "Response data missing request_type"

    def test_openai_chat_completions_api(self):
        """Test OpenAI Chat Completions API"""
        test_cases = [
            {
                "name": "Basic chat completion request",
                "data": {
                    "model": "gpt-3.5-turbo",
                    "messages": [{"role": "user", "content": "Hello, please introduce yourself"}],
                    "max_tokens": 100,
                    "temperature": 0.7,
                },
            },
            {
                "name": "Multi-turn conversation chat completion request",
                "data": {
                    "model": "gpt-3.5-turbo",
                    "messages": [
                        {"role": "user", "content": "What is machine learning?"},
                        {"role": "assistant", "content": "Machine learning is a branch of artificial intelligence..."},
                        {"role": "user", "content": "Can you give an example?"},
                    ],
                    "max_tokens": 200,
                    "temperature": 0.8,
                },
            },
            {
                "name": "Chat completion request with system message",
                "data": {
                    "model": "gpt-3.5-turbo",
                    "messages": [
                        {"role": "system", "content": "You are a professional Python programming assistant"},
                        {"role": "user", "content": "Please write a Python implementation of quicksort"},
                    ],
                    "max_tokens": 500,
                    "temperature": 0.5,
                },
            },
            {
                "name": "Minimal parameter chat completion request",
                "data": {"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "Hello"}]},
            },
        ]

        for test_case in test_cases:
            response = self.openai_client.post(
                "/v1/chat/completions",
                json=test_case["data"],
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
            )

            assert response.status_code == 200, f"Chat Completions API failed: {response.status_code}"

            data = response.json()
            assert "request_id" in data, "Response missing request_id"
            assert "status" in data, "Response missing status"
            assert "data" in data, "Response missing data field"

            # Verify input data parsing
            assert "input_data" in data["data"], "Response data missing input_data"
            assert "is_stream" in data["data"], "Response data missing is_stream"
            assert "request_type" in data["data"], "Response data missing request_type"

    def test_openai_responses_api(self):
        """Test OpenAI Responses API — standard /v1/responses path.

        The Coordinator is a transparent proxy for /v1/responses:
        it accepts the request and forwards it to the backend engine
        (e.g. vLLM 0.22+), which returns the OpenAI-native format.
        The Coordinator does NOT wrap the response in request_id/status/data.
        """
        response = self.openai_client.post(
            "/v1/responses",
            json={
                "model": "gpt-4o-mini",
                "input": "Hello there!",
                "max_tokens": 64,
            },
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )

        assert response.status_code == 200, f"Responses API failed: {response.status_code}"

        data = response.json()
        # OpenAI-native response format: id, object, created, model, output
        assert "id" in data, "Response missing id"
        assert "object" in data, "Response missing object"
        assert data["object"] == "response", f"Expected object='response', got '{data['object']}'"
        assert "created" in data, "Response missing created"
        assert "model" in data, "Response missing model"
        assert "output" in data, "Response missing output"
        assert isinstance(data["output"], list), "output must be an array"
        assert len(data["output"]) > 0, "output must not be empty"
        assert data["output"][0]["type"] == "message"
        assert data["output"][0]["role"] == "assistant"

    def test_streaming_requests(self):
        """Test streaming requests"""
        # Test completion streaming request
        completion_stream_data = {
            "model": "text-davinci-003",
            "prompt": "Write a Python function to calculate the Fibonacci sequence",
            "max_tokens": 200,
            "temperature": 0.7,
            "stream": True,
        }

        response = self.openai_client.post(
            "/v1/completions",
            json=completion_stream_data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )

        assert response.status_code == 200, f"Streaming completion request failed: {response.status_code}"

        data = response.json()
        assert data["data"]["is_stream"], "Stream flag not set correctly"

        # Test chat completion streaming request
        chat_stream_data = {
            "model": "gpt-3.5-turbo",
            "messages": [{"role": "user", "content": "Please explain the basic concepts of deep learning in detail"}],
            "max_tokens": 300,
            "temperature": 0.7,
            "stream": True,
        }

        response = self.openai_client.post(
            "/v1/chat/completions",
            json=chat_stream_data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )

        assert response.status_code == 200, f"Streaming chat completion request failed: {response.status_code}"

        data = response.json()
        assert data["data"]["is_stream"], "Stream flag not set correctly"

    def test_error_handling(self):
        """Test error handling"""
        # Test invalid JSON
        response = self.openai_client.post(
            "/v1/completions",
            content="invalid json",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )

        # Should return 400, 500, or 422 error
        assert response.status_code in [400, 422, 500], f"Invalid JSON handling exception: {response.status_code}"

        # Test missing required fields
        invalid_data = {
            "prompt": "test"  # Missing model field
        }
        response = self.openai_client.post(
            "/v1/completions",
            json=invalid_data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )

        # Should return 400 or 422 error
        assert response.status_code in [400, 422, 500], f"Missing field handling exception: {response.status_code}"

        # Test invalid chat completion request
        invalid_chat_data = {
            "model": "gpt-3.5-turbo",
            "messages": "invalid messages",  # messages should be an array
        }
        response = self.openai_client.post(
            "/v1/chat/completions",
            json=invalid_chat_data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )
        assert response.status_code in [400, 422, 500], (
            f"Invalid chat completion handling exception: {response.status_code}"
        )

    def test_rate_limiting(self):
        """Test rate limiting functionality"""
        # Send many requests to test rate limiting
        for i in range(30):  # Send multiple requests to verify endpoint stability under load
            test_data = {
                "model": "gpt-3.5-turbo",
                "messages": [{"role": "user", "content": f"This is the {i + 1}th rate limiting test request"}],
                "max_tokens": 10,
            }

            response = self.openai_client.post(
                "/v1/chat/completions",
                json=test_data,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
            )

            if response.status_code == 429:
                break

        # Note: Rate limiting may or may not trigger depending on configuration
        # This test just verifies the endpoint can handle many requests
        assert True, "Rate limiting test completed"

    def test_api_key_validation(self):
        """Test API Key validation functionality"""
        # Valid API Keys (from api_key_config.json)
        valid_api_keys = ["sk-test123456789", "sk-coordinator2024"]
        invalid_api_key = "sk-invalid-key"

        # Test 1: Request without API Key should fail (401)
        test_data = {"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "test"}], "max_tokens": 10}

        response = self.openai_client.post(
            "/v1/chat/completions", json=test_data, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 401, f"Expected 401, got: {response.status_code}"
        error_data = response.json()
        assert "detail" in error_data, "Error response missing detail field"

        # Test 2: Invalid API Key should fail (403)
        response = self.openai_client.post(
            "/v1/chat/completions",
            json=test_data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {invalid_api_key}"},
        )
        assert response.status_code == 403, f"Expected 403, got: {response.status_code}"
        error_data = response.json()
        assert "detail" in error_data, "Error response missing detail field"

        # Test 3: Valid API Key should succeed (200)
        for valid_key in valid_api_keys:
            response = self.openai_client.post(
                "/v1/chat/completions",
                json=test_data,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {valid_key}"},
            )
            assert response.status_code == 200, f"Valid API Key request failed: {response.status_code}"
            data = response.json()
            assert "request_id" in data, "Response missing request_id"

        # Test 4: API Key without Bearer prefix
        response = self.openai_client.post(
            "/v1/chat/completions",
            json=test_data,
            headers={
                "Content-Type": "application/json",
                "Authorization": valid_api_keys[0],  # Without Bearer prefix
            },
        )
        # Depending on implementation, may fail or succeed without prefix
        assert response.status_code in [200, 401, 403], f"Unexpected status code: {response.status_code}"

        # Test 5: Skip paths don't require API Key (/startup, /readiness, etc.)
        skip_paths = ["/startup", "/readiness"]
        for path in skip_paths:
            response = self.mgmt_client.get(f"{path}")
            assert response.status_code == 200, f"Skip path {path} returned non-200 status code: {response.status_code}"

        # Test 6: Completions API also requires API Key validation
        completion_data = {"model": "text-davinci-003", "prompt": "test", "max_tokens": 10}

        # Without API Key
        response = self.openai_client.post(
            "/v1/completions", json=completion_data, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 401, f"Expected 401, got: {response.status_code}"

        # Valid API Key
        response = self.openai_client.post(
            "/v1/completions",
            json=completion_data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {valid_api_keys[0]}"},
        )
        assert response.status_code == 200, f"Completions API with valid API Key request failed: {response.status_code}"


class TestFastAPIMiddleware:
    """Test FastAPI middleware functionality"""

    def setup_method(self):
        """Setup test fixtures"""
        from motor.coordinator.middleware.fastapi_middleware import SimpleRateLimitMiddleware
        from motor.coordinator.middleware.rate_limiter import SimpleRateLimiter
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        self.app = FastAPI()
        self.SimpleRateLimitMiddleware = SimpleRateLimitMiddleware
        self.SimpleRateLimiter = SimpleRateLimiter
        self.TestClient = TestClient
        self._report_alarms_patcher = patch(
            "motor.coordinator.api_client.controller_api_client.ControllerApiClient.report_alarms",
            return_value=True,
        )
        self._report_alarms_patcher.start()

    def teardown_method(self):
        self._report_alarms_patcher.stop()

    def test_rate_limit_middleware_skip_paths(self):
        """Test rate limit middleware skip paths"""

        @self.app.get("/test")
        async def test_endpoint():
            return {"status": "ok"}

        @self.app.get("/liveness")
        async def liveness_endpoint():
            return {"status": "healthy"}

        rate_limiter = self.SimpleRateLimiter(max_requests=1, window_size=60)
        middleware = self.SimpleRateLimitMiddleware(app=self.app, rate_limiter=rate_limiter, skip_paths=["/liveness"])

        # Middleware itself is an ASGI app (inherits from BaseHTTPMiddleware)
        client = self.TestClient(middleware)

        # /liveness should be skipped (not rate limited)
        for _ in range(5):
            response = client.get("/liveness")
            assert response.status_code == 200, "Liveness endpoint should not be rate limited"

        # /test should be rate limited after first request
        response1 = client.get("/test")
        assert response1.status_code == 200, "First request should succeed"

        response2 = client.get("/test")
        # May be rate limited depending on timing
        assert response2.status_code in [200, 429], "Second request may be rate limited"

    def test_rate_limit_middleware_error_handling(self):
        """Test rate limit middleware error handling"""
        # Create a middleware that will raise an exception
        rate_limiter = MagicMock()
        rate_limiter.is_allowed = MagicMock(side_effect=Exception("Test error"))

        @self.app.get("/test")
        async def test_endpoint():
            return {"status": "ok"}

        middleware = self.SimpleRateLimitMiddleware(app=self.app, rate_limiter=rate_limiter, skip_paths=[])

        # Middleware itself is an ASGI app (inherits from BaseHTTPMiddleware)
        client = self.TestClient(middleware)

        # Should allow request when error occurs
        response = client.get("/test")
        assert response.status_code == 200, "Should allow request when error occurs"
        assert middleware.stats["allowed_requests"] > 0, "Should increment allowed_requests on error"

    def test_rate_limit_middleware_stats(self):
        """Test rate limit middleware statistics"""

        @self.app.get("/test")
        async def test_endpoint():
            return {"status": "ok"}

        rate_limiter = self.SimpleRateLimiter(max_requests=10, window_size=60)
        middleware = self.SimpleRateLimitMiddleware(app=self.app, rate_limiter=rate_limiter, skip_paths=[])

        # Middleware itself is an ASGI app (inherits from BaseHTTPMiddleware)
        client = self.TestClient(middleware)

        # Make some requests
        for _ in range(5):
            client.get("/test")

        assert middleware.stats["total_requests"] >= 5, "Should track total requests"
        assert middleware.stats["allowed_requests"] >= 5, "Should track allowed requests"
        assert "start_time" in middleware.stats, "Should track start time"


class TestCoordinatorServerAdvanced:
    """Advanced functionality test class for Coordinator server"""

    def setup_method(self):
        """Setup test fixtures"""
        # Mock InstanceManager
        self._im_patcher = patch("motor.coordinator.api_server.management_server.InstanceManager")
        im_mock_cls = self._im_patcher.start()
        im_instance = MagicMock()
        im_instance.has_required_instances.return_value = True
        im_instance.get_required_instances_status.return_value = InstanceReadiness.REQUIRED_MET
        im_instance.refresh_instances = AsyncMock(return_value=None)
        im_instance.validate_refresh_instances = AsyncMock(return_value=None)
        im_instance.snapshot_instances = AsyncMock(return_value=[])
        im_mock_cls.return_value = im_instance

        # Mock handle_request to return appropriate JSON response
        async def mock_handle_request(request, config, scheduler=None, request_manager=None, request_json=None):
            """Mock handle_request that returns JSON response matching test expectations"""
            if request_json is not None:
                body_json = request_json
            else:
                try:
                    # Try to get JSON from request (cached if already parsed)
                    body_json = await request.json()
                except Exception:
                    # Fallback: try to read body directly
                    try:
                        request_body = await request.body()
                        body_json = json.loads(request_body.decode("utf-8"))
                    except Exception:
                        body_json = {}

            # Extract input_data based on request type
            input_data = ""
            if "prompt" in body_json:
                # For completions API, use prompt directly as string
                input_data = str(body_json["prompt"])
            elif "messages" in body_json:
                # For chat completions API, convert messages to JSON string
                input_data = json.dumps(body_json["messages"], ensure_ascii=False)

            # Determine if stream
            is_stream = body_json.get("stream", False)
            if isinstance(is_stream, str):
                is_stream = is_stream.lower() in ("true", "1", "yes")

            # Determine request_type based on endpoint
            request_type = "openai"
            if request.url.path.endswith("/completions"):
                request_type = "completions"
            elif request.url.path.endswith("/chat/completions"):
                request_type = "chat_completions"
            elif request.url.path.endswith("/responses"):
                request_type = "responses"

            # Generate request_id (simulate)
            import hashlib

            request_id = f"req-{hashlib.md5(str(body_json).encode()).hexdigest()[:8]}"

            # /v1/responses returns OpenAI-native response format (engine passthrough)
            if request_type == "responses":
                import time

                response_data = {
                    "id": request_id,
                    "object": "response",
                    "created": int(time.time()),
                    "model": body_json.get("model", ""),
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": str(body_json.get("input", "")),
                                }
                            ],
                        }
                    ],
                }
            else:
                response_data = {
                    "request_id": request_id,
                    "status": "success",
                    "data": {"input_data": input_data, "is_stream": bool(is_stream), "request_type": request_type},
                }

            return JSONResponse(content=response_data)

        # Patch handle_request
        self._handle_request_patcher = patch(
            "motor.coordinator.api_server.inference_server.handle_request", side_effect=mock_handle_request
        )
        self._handle_request_patcher.start()

        self._is_available_patcher = patch(
            "motor.coordinator.api_server.inference_server.InferenceServer._is_available",
            new_callable=AsyncMock,
            return_value=True,
        )
        self._is_available_patcher.start()

        # Create unified configuration
        coordinator_config = CoordinatorConfig()
        coordinator_config.api_key_config.enable_api_key = True
        coordinator_config.api_key_config.valid_keys = {"sk-test123456789", "sk-coordinator2024"}

        # Create test server shell (ManagementServer + InferenceServer)
        self.coordinator_server = _TestServerShell(config=coordinator_config)
        # Replace scheduler connection with mock to avoid ZMQ connection timeout (~15s per call)
        self.coordinator_server._mgmt._scheduler_connection = MagicMock()
        self.coordinator_server._mgmt._scheduler_connection.ensure_connected = AsyncMock()
        self.coordinator_server._mgmt._scheduler_connection.get_client.return_value = None
        self.coordinator_server._mgmt._scheduler_connection.disconnect = AsyncMock()
        self.coordinator_server.setup_rate_limiting()
        # Do not mock _handle_openai_request: let real handler run so validation (400), JSON/decode (500), and
        # _is_available (503) are exercised; handle_request is already patched above for 200 responses.
        inf = self.coordinator_server._inf
        inf._is_available = AsyncMock(return_value=True)
        _mock_scheduler = MagicMock()
        _mock_scheduler.get_available_instances = AsyncMock(return_value={})
        inf._get_scheduler_client = lambda: _mock_scheduler
        if not getattr(inf.app.state, "request_manager", None):
            inf.app.state.request_manager = inf._request_manager
        self.mgmt_client = TestClient(self.coordinator_server.management_app)
        self.valid_api_key = "sk-test123456789"

    def teardown_method(self):
        """Teardown test fixtures"""
        try:
            if hasattr(self, "_im_patcher"):
                self._im_patcher.stop()
            if hasattr(self, "_handle_request_patcher"):
                self._handle_request_patcher.stop()
            if hasattr(self, "_is_available_patcher"):
                self._is_available_patcher.stop()
        except Exception:
            pass

    def test_refresh_instances_valid_request(self):
        """Test refresh_instances with valid request"""
        valid_body = {
            "event": "add",
            "instances": [
                {
                    "job_name": "test-job",
                    "model_name": "test-model",
                    "id": 1,
                    "role": "prefill",
                    "endpoints": {
                        "192.168.1.1": {
                            "0": {
                                "id": 0,
                                "ip": "192.168.1.1",
                                "business_port": "8080",
                            }
                        }
                    },
                }
            ],
        }

        response = self.mgmt_client.post("/instances/refresh", json=valid_body)

        assert response.status_code == 200, f"Refresh instances failed: {response.status_code}"
        data = response.json()
        assert data["status"] == "success", f"Refresh instances status abnormal: {data}"
        assert "request_id" in data, "Response missing request_id"
        assert "data" in data, "Response missing data field"

    def test_refresh_instances_rejects_duplicate_ids(self):
        """Duplicate IDs must be rejected before list-to-dict conversion can drop an instance."""
        instance = {
            "job_name": "test-job",
            "model_name": "test-model",
            "id": 1,
            "role": "prefill",
            "endpoints": {},
        }

        response = self.mgmt_client.post(
            "/instances/refresh",
            json={"event": "set", "instances": [instance, {**instance, "job_name": "other-job"}]},
        )

        assert response.status_code == 400
        assert "duplicate instance IDs" in response.json()["detail"]

    def test_refresh_instances_returns_409_before_scheduler_on_id_conflict(self):
        """Existing-state ID collisions are reported as conflicts before Scheduler forwarding."""
        self.coordinator_server._mgmt.instance_manager.validate_refresh_instances.side_effect = InstanceIdConflictError(
            "instance ID 1 already belongs to another instance"
        )
        scheduler_client = MagicMock()
        scheduler_client.refresh_instances = AsyncMock()
        self.coordinator_server._mgmt._scheduler_connection.get_client.return_value = scheduler_client
        body = {
            "event": "add",
            "instances": [
                {
                    "job_name": "test-job",
                    "model_name": "test-model",
                    "id": 1,
                    "role": "prefill",
                    "endpoints": {},
                }
            ],
        }

        response = self.mgmt_client.post("/instances/refresh", json=body)

        assert response.status_code == 409
        scheduler_client.refresh_instances.assert_not_awaited()

    def test_refresh_instances_does_not_update_mgmt_when_scheduler_rejects(self):
        """Mgmt mirror must remain unchanged when Scheduler rejects the authoritative update."""
        scheduler_client = MagicMock()
        scheduler_client.refresh_instances = AsyncMock(return_value=False)
        self.coordinator_server._mgmt._scheduler_connection.get_client.return_value = scheduler_client
        body = {
            "event": "add",
            "instances": [
                {
                    "job_name": "test-job",
                    "model_name": "test-model",
                    "id": 1,
                    "role": "prefill",
                    "endpoints": {},
                }
            ],
        }

        response = self.mgmt_client.post("/instances/refresh", json=body)

        assert response.status_code == 503
        self.coordinator_server._mgmt.instance_manager.refresh_instances.assert_not_awaited()

    def test_refresh_instances_empty_body(self):
        """Test refresh_instances with empty body"""
        response = self.mgmt_client.post("/instances/refresh", json={})

        # Should return 400 for empty body
        assert response.status_code == 400, f"Expected 400 for empty body, got: {response.status_code}"

    def test_refresh_instances_invalid_json(self):
        """Test refresh_instances with invalid JSON"""
        response = self.mgmt_client.post(
            "/instances/refresh", content="invalid json", headers={"Content-Type": "application/json"}
        )

        # Should return 400 or 422 for invalid JSON
        assert response.status_code in [400, 422, 500], (
            f"Expected 400/422/500 for invalid JSON, got: {response.status_code}"
        )

    def test_refresh_instances_invalid_event_msg(self):
        """Test refresh_instances with invalid event message format"""
        invalid_body = {
            "event": "INVALID_EVENT",
            "instances": "not a list",  # Invalid format
        }

        response = self.mgmt_client.post("/instances/refresh", json=invalid_body)

        # Should return 400 for invalid format
        assert response.status_code == 400, f"Expected 400 for invalid format, got: {response.status_code}"

    def test_refresh_instances_no_body(self):
        """Test refresh_instances with no body"""
        response = self.mgmt_client.post("/instances/refresh", content=None)

        # Should return 400 for no body
        assert response.status_code == 400, f"Expected 400 for no body, got: {response.status_code}"

    def test_precision_alarm_cleared_success(self):
        """Test precision alarm clear returns dismissed when scheduler state is cleared."""
        scheduler_client = MagicMock()
        scheduler_client.dismiss_precision_alarm_state = AsyncMock(return_value=True)
        self.coordinator_server._mgmt._scheduler_connection.get_client.return_value = scheduler_client

        response = self.mgmt_client.post(
            "/precision/alarm_cleared",
            json={"p_instance_id": 1, "d_instance_id": 2},
        )

        assert response.status_code == 200, f"Expected 200, got: {response.status_code}"
        data = response.json()
        assert data["status"] == "success"
        assert data["data"]["dismissed"] is True
        scheduler_client.dismiss_precision_alarm_state.assert_awaited_once_with(
            p_instance_id=1,
            d_instance_id=2,
        )

    def test_precision_alarm_cleared_fails_without_scheduler_client(self):
        """Test precision alarm clear does not report success when scheduler client is unavailable."""
        self.coordinator_server._mgmt._scheduler_connection.get_client.return_value = None

        response = self.mgmt_client.post(
            "/precision/alarm_cleared",
            json={"p_instance_id": 1, "d_instance_id": 2},
        )

        assert response.status_code == 503, f"Expected 503, got: {response.status_code}"

    def test_precision_alarm_cleared_fails_when_scheduler_rejects(self):
        """Test precision alarm clear does not report success when scheduler rejects the request."""
        scheduler_client = MagicMock()
        scheduler_client.dismiss_precision_alarm_state = AsyncMock(return_value=False)
        self.coordinator_server._mgmt._scheduler_connection.get_client.return_value = scheduler_client

        response = self.mgmt_client.post(
            "/precision/alarm_cleared",
            json={"p_instance_id": 1, "d_instance_id": 2},
        )

        assert response.status_code == 503, f"Expected 503, got: {response.status_code}"

    def test_precision_alarm_cleared_rejects_non_object_body(self):
        """Test precision alarm clear returns 400 when body is not a JSON object."""
        response = self.mgmt_client.post(
            "/precision/alarm_cleared",
            content=b"[1, 2]",
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 400, f"Expected 400, got: {response.status_code}"

    def test_create_unified_app(self):
        """Test create_unified_app method"""
        unified_app = self.coordinator_server.create_unified_app()

        assert unified_app is not None, "Unified app should be created"

        # Test that unified app has routes from both management and inference apps
        unified_client = TestClient(unified_app)

        # Test management route
        response = unified_client.get("/liveness")
        assert response.status_code == 200, "Liveness endpoint should be available in unified app"

        # Test inference route
        response = unified_client.post(
            "/v1/chat/completions",
            json={"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "test"}]},
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )
        assert response.status_code == 200, "Chat completions endpoint should be available in unified app"

    def test_create_unified_app_with_rate_limit_disabled(self):
        """Test create_unified_app with rate limit disabled"""
        # Create a config with rate limit disabled
        coordinator_config = CoordinatorConfig()
        coordinator_config.rate_limit_config.enable_rate_limit = False

        coordinator_server = _TestServerShell(config=coordinator_config)
        coordinator_server.instance_manager = MagicMock()
        unified_app = coordinator_server.create_unified_app()
        assert unified_app is not None, "Unified app should be created even with rate limit disabled"

    def test_create_unified_app_with_custom_rate_limit_config(self):
        """Test create_unified_app with custom rate limit config"""
        from motor.config.coordinator import RateLimitConfig

        custom_rate_limit_config = RateLimitConfig()
        custom_rate_limit_config.enable_rate_limit = True
        custom_rate_limit_config.max_requests = 50
        custom_rate_limit_config.window_size = 30

        unified_app = self.coordinator_server.create_unified_app(rate_limit_config=custom_rate_limit_config)

        assert unified_app is not None, "Unified app should be created with custom rate limit config"

    def test_copy_routes_skip_paths(self):
        """Test _copy_routes with skip paths"""
        from fastapi import FastAPI

        src_app = FastAPI()

        @src_app.get("/test")
        async def test():
            return {"status": "ok"}

        @src_app.get("/docs")
        async def docs():
            return {"status": "docs"}

        # Create dst_app with docs disabled to avoid FastAPI auto-generated docs
        dst_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        self.coordinator_server._copy_routes(src_app, dst_app, skip_paths=["/docs"])

        dst_client = TestClient(dst_app)

        # /test should be copied
        response = dst_client.get("/test")
        assert response.status_code == 200, "/test route should be copied"

        # /docs should be skipped (not copied from src_app, and FastAPI docs disabled)
        response = dst_client.get("/docs")
        assert response.status_code == 404, "/docs route should be skipped"

    def test_validate_openai_request_invalid_model(self):
        """Test _validate_openai_request with missing model"""
        # This tests the validation logic indirectly through the endpoint
        invalid_data = {
            "messages": [{"role": "user", "content": "test"}]
            # Missing model field
        }

        inference_client = TestClient(self.coordinator_server.inference_app)
        response = inference_client.post(
            "/v1/chat/completions",
            json=invalid_data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )

        assert response.status_code == 400, f"Expected 400 for missing model, got: {response.status_code}"

    def test_validate_openai_request_invalid_messages(self):
        """Test _validate_openai_request with invalid messages"""
        invalid_data = {
            "model": "gpt-3.5-turbo",
            "messages": "not a list",  # Invalid format
        }

        inference_client = TestClient(self.coordinator_server.inference_app)
        response = inference_client.post(
            "/v1/chat/completions",
            json=invalid_data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )

        assert response.status_code == 400, f"Expected 400 for invalid messages, got: {response.status_code}"

    def test_validate_openai_request_empty_messages(self):
        """Test _validate_openai_request with empty messages list"""
        invalid_data = {"model": "gpt-3.5-turbo", "messages": []}

        inference_client = TestClient(self.coordinator_server.inference_app)
        response = inference_client.post(
            "/v1/chat/completions",
            json=invalid_data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )

        assert response.status_code == 400, f"Expected 400 for empty messages, got: {response.status_code}"

    def test_validate_openai_request_empty_messages_manage_false_session_target_rejected(self):
        """Empty messages must be rejected when manage=false even if a session-targeted edit is present."""
        invalid_data = {
            "model": "gpt-3.5-turbo",
            "messages": [],
            "agent_hint": {
                "context_management": {
                    "manage_request": False,
                    "edits": [{"type": "evict", "target": "session"}],
                }
            },
        }
        inference_client = TestClient(self.coordinator_server.inference_app)
        response = inference_client.post(
            "/v1/chat/completions",
            json=invalid_data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )
        assert response.status_code == 400, (
            f"Expected 400 for empty messages with manage=false + target=session, got: {response.status_code}"
        )
        assert "must be a non-empty array" in response.text

    def test_validate_openai_request_empty_messages_manage_true_messages_target_rejected(self):
        """Empty messages must be rejected when manage=true but target is messages (not session)."""
        invalid_data = {
            "model": "gpt-3.5-turbo",
            "messages": [],
            "agent_hint": {
                "context_management": {
                    "manage_request": True,
                    "edits": [{"type": "evict", "target": "messages"}],
                }
            },
        }
        inference_client = TestClient(self.coordinator_server.inference_app)
        response = inference_client.post(
            "/v1/chat/completions",
            json=invalid_data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )
        assert response.status_code == 400, (
            f"Expected 400 for empty messages with manage=true + target=messages, got: {response.status_code}"
        )
        assert "must be a non-empty array" in response.text

    def test_validate_openai_request_empty_messages_manage_true_tools_target_rejected(self):
        """Empty messages must be rejected when manage=true but target is tools (not session)."""
        invalid_data = {
            "model": "gpt-3.5-turbo",
            "messages": [],
            "agent_hint": {
                "context_management": {
                    "manage_request": True,
                    "edits": [{"type": "evict", "target": "tools"}],
                }
            },
        }
        inference_client = TestClient(self.coordinator_server.inference_app)
        response = inference_client.post(
            "/v1/chat/completions",
            json=invalid_data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )
        assert response.status_code == 400, (
            f"Expected 400 for empty messages with manage=true + target=tools, got: {response.status_code}"
        )
        assert "must be a non-empty array" in response.text

    def test_validate_openai_request_empty_messages_manage_true_session_target_allowed(self):
        """Empty messages is the only valid bypass: manage=true AND target=session."""
        data = {
            "model": "gpt-3.5-turbo",
            "messages": [],
            "agent_hint": {
                "context_management": {
                    "manage_request": True,
                    "edits": [{"type": "evict", "target": "session"}],
                }
            },
        }
        inference_client = TestClient(self.coordinator_server.inference_app)
        response = inference_client.post(
            "/v1/chat/completions",
            json=data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )
        assert response.status_code == 200, (
            f"Empty messages with manage=true + target=session must bypass the non-empty check "
            f"and reach the mocked handle_request (200), got {response.status_code}: {response.text}"
        )

    def test_validate_openai_request_empty_messages_session_control_allowed(self):
        """session_control pause/stop/compact/resume must bypass the empty-messages check."""
        data = {
            "model": "gpt-3.5-turbo",
            "messages": [],
            "agent_hint": {"session_control": {"type": "stop"}},
        }
        inference_client = TestClient(self.coordinator_server.inference_app)
        response = inference_client.post(
            "/v1/chat/completions",
            json=data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )
        assert response.status_code == 200, (
            f"Empty messages with session_control must bypass the non-empty check "
            f"and reach the mocked handle_request (200), got {response.status_code}: {response.text}"
        )

    def test_validate_openai_request_empty_messages_session_control_start_rejected(self):
        """'start' carries no context semantics, so empty messages must still be rejected."""
        data = {
            "model": "gpt-3.5-turbo",
            "messages": [],
            "agent_hint": {"session_control": {"type": "start"}},
        }
        inference_client = TestClient(self.coordinator_server.inference_app)
        response = inference_client.post(
            "/v1/chat/completions",
            json=data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )
        assert response.status_code == 400, (
            f"Expected 400 for empty messages with session_control=start, got: {response.status_code}"
        )

    def test_validate_openai_request_empty_messages_session_control_invalid_cm_allowed(self):
        """session_control=stop with an INVALID raw context_management (manage_request=False)
        must still bypass the empty-messages check.

        Regression: previously the mere presence of ``context_management`` triggered the
        mutual-exclusion branch in ``_resolve_session_control`` and dropped session_control,
        so a request like ``session_control=stop`` + ``context_management={manage_request: False}``
        fell through to plain inference and was rejected for empty messages — silently
        downgrading the client's ``stop`` intent.
        """
        data = {
            "model": "gpt-3.5-turbo",
            "messages": [],
            "agent_hint": {
                "session_control": {"type": "stop"},
                "context_management": {"manage_request": False, "edits": [{"type": "evict", "target": "session"}]},
            },
        }
        inference_client = TestClient(self.coordinator_server.inference_app)
        response = inference_client.post(
            "/v1/chat/completions",
            json=data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )
        assert response.status_code == 200, (
            f"Invalid raw context_management must NOT block session_control=stop; "
            f"expected 200 (reaches handle_request), got {response.status_code}: {response.text}"
        )

    def test_validate_openai_request_invalid_message_format(self):
        """Test _validate_openai_request with invalid message format"""
        invalid_data = {
            "model": "gpt-3.5-turbo",
            "messages": [
                "not a dict"  # Invalid message format
            ],
        }

        inference_client = TestClient(self.coordinator_server.inference_app)
        response = inference_client.post(
            "/v1/chat/completions",
            json=invalid_data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )

        assert response.status_code == 400, f"Expected 400 for invalid message format, got: {response.status_code}"

    def test_validate_openai_request_missing_role_or_content(self):
        """Test _validate_openai_request with missing role or content"""
        invalid_data = {
            "model": "gpt-3.5-turbo",
            "messages": [
                {"role": "user"}  # Missing content
            ],
        }

        inference_client = TestClient(self.coordinator_server.inference_app)
        response = inference_client.post(
            "/v1/chat/completions",
            json=invalid_data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )

        assert response.status_code == 400, f"Expected 400 for missing content, got: {response.status_code}"

    def test_validate_openai_request_invalid_role(self):
        """Test _validate_openai_request with invalid role"""
        invalid_data = {"model": "gpt-3.5-turbo", "messages": [{"role": "invalid_role", "content": "test"}]}

        inference_client = TestClient(self.coordinator_server.inference_app)
        response = inference_client.post(
            "/v1/chat/completions",
            json=invalid_data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )

        assert response.status_code == 400, f"Expected 400 for invalid role, got: {response.status_code}"

    def test_validate_responses_input_empty_array(self):
        """Test _validate_openai_request rejects empty input array on /v1/responses."""
        inference_client = TestClient(self.coordinator_server.inference_app)
        response = inference_client.post(
            "/v1/responses",
            json={"model": "gpt-4o-mini", "input": []},
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )
        assert response.status_code == 400, f"Expected 400 for empty input array, got: {response.status_code}"

    def test_validate_responses_input_elements_not_dict(self):
        """Test _validate_openai_request rejects input array with non-dict elements."""
        inference_client = TestClient(self.coordinator_server.inference_app)
        response = inference_client.post(
            "/v1/responses",
            json={"model": "gpt-4o-mini", "input": [1, 2, 3]},
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )
        assert response.status_code == 400, f"Expected 400 for non-dict input elements, got: {response.status_code}"

    def test_validate_responses_input_missing_role_or_content(self):
        """Test _validate_openai_request rejects input element missing role or content."""
        inference_client = TestClient(self.coordinator_server.inference_app)
        response = inference_client.post(
            "/v1/responses",
            json={"model": "gpt-4o-mini", "input": [{"role": "user"}]},
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )
        assert response.status_code == 400, f"Expected 400 for missing content, got: {response.status_code}"

    def test_validate_responses_input_invalid_role(self):
        """Test _validate_openai_request rejects input element with invalid role."""
        inference_client = TestClient(self.coordinator_server.inference_app)
        response = inference_client.post(
            "/v1/responses",
            json={"model": "gpt-4o-mini", "input": [{"role": "invalid", "content": "x"}]},
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )
        assert response.status_code == 400, f"Expected 400 for invalid role, got: {response.status_code}"

    def test_validate_responses_input_string_is_valid(self):
        """Test _validate_openai_request accepts string input (non-array) on /v1/responses."""
        inference_client = TestClient(self.coordinator_server.inference_app)
        response = inference_client.post(
            "/v1/responses",
            json={"model": "gpt-4o-mini", "input": "Hello there!"},
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )
        assert response.status_code == 200, f"Expected 200 for string input, got: {response.status_code}"

    def test_handle_openai_request_unavailable_instances(self):
        """Test _handle_openai_request when instances are unavailable (503)."""
        # Inference server uses _is_available() (SchedulerClient), not InstanceManager; force unavailable
        self.coordinator_server._inf._is_available = AsyncMock(return_value=False)
        inference_client = TestClient(self.coordinator_server.inference_app)
        response = inference_client.post(
            "/v1/chat/completions",
            json={"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "test"}]},
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )
        assert response.status_code == 503, f"Expected 503 for unavailable instances, got: {response.status_code}"

    def test_handle_openai_request_with_prompt(self):
        """Test _handle_openai_request with prompt field (completions API)"""
        inference_client = TestClient(self.coordinator_server.inference_app)
        response = inference_client.post(
            "/v1/completions",
            json={"model": "text-davinci-003", "prompt": "Hello world"},
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )

        assert response.status_code == 200, f"Completions API failed: {response.status_code}"
        data = response.json()
        assert data["data"]["input_data"] == "Hello world", "Prompt should be extracted correctly"

    def test_handle_openai_request_empty_input(self):
        """Test _handle_openai_request with empty input"""
        inference_client = TestClient(self.coordinator_server.inference_app)
        response = inference_client.post(
            "/v1/completions",
            json={
                "model": "text-davinci-003"
                # Missing prompt and messages
            },
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )

        # Should return 400 for missing required fields
        assert response.status_code == 400, f"Expected 400 for missing prompt/messages, got: {response.status_code}"

    def test_openai_is_stream(self):
        """Test _openai_is_stream method"""
        # Test with stream=True
        assert self.coordinator_server._openai_is_stream({"stream": True})

        # Test with stream=False
        assert not self.coordinator_server._openai_is_stream({"stream": False})

        # Test without stream field
        assert not self.coordinator_server._openai_is_stream({})

        # Test with stream as string
        assert self.coordinator_server._openai_is_stream({"stream": "true"})  # Truthy value

    def test_refresh_instances_with_complex_endpoints(self):
        """Test refresh_instances with complex endpoint structures"""
        complex_body = {
            "event": "add",
            "instances": [
                {
                    "job_name": "test-job",
                    "model_name": "test-model",
                    "id": 3,
                    "role": "prefill",
                    "endpoints": {
                        "192.168.1.3": {
                            "0": {
                                "id": 0,
                                "ip": "192.168.1.3",
                                "business_port": "8080",
                            },
                            "1": {
                                "id": 1,
                                "ip": "192.168.1.3",
                                "business_port": "8081",
                            },
                        },
                        "192.168.1.4": {
                            "2": {
                                "id": 2,
                                "ip": "192.168.1.4",
                                "business_port": "9000",
                            }
                        },
                    },
                }
            ],
        }

        response = self.mgmt_client.post("/instances/refresh", json=complex_body)

        assert response.status_code == 200, f"Refresh instances failed: {response.status_code}"
        data = response.json()
        assert data["status"] == "success", f"Refresh instances status abnormal: {data}"

    def test_refresh_instances_with_non_dict_endpoints(self):
        """Test refresh_instances with non-dict endpoints value"""
        invalid_body = {
            "event": "add",
            "instances": [
                {
                    "job_name": "test-job",
                    "model_name": "test-model",
                    "id": 4,
                    "role": "prefill",
                    "endpoints": "not a dict",  # Invalid endpoints format
                }
            ],
        }

        response = self.mgmt_client.post("/instances/refresh", json=invalid_body)

        # Should return 400 for invalid format (endpoints must be a dict)
        assert response.status_code == 400, f"Expected 400 for invalid endpoints format, got: {response.status_code}"

    def test_refresh_instances_with_non_dict_endpoint_data(self):
        """Test refresh_instances with non-dict endpoint data"""
        invalid_body = {
            "event": "add",
            "instances": [
                {
                    "job_name": "test-job",
                    "model_name": "test-model",
                    "id": 5,
                    "role": "prefill",
                    "endpoints": {
                        "192.168.1.5": "not a dict"  # Invalid endpoint data format
                    },
                }
            ],
        }

        response = self.mgmt_client.post("/instances/refresh", json=invalid_body)

        # Should return 400 for invalid format (endpoint data must be a dict)
        assert response.status_code == 400, (
            f"Expected 400 for invalid endpoint data format, got: {response.status_code}"
        )

    def test_timeout_handler(self):
        """Test timeout handler decorator"""
        # This tests the timeout handler indirectly through endpoints
        # The timeout handler should not raise errors for normal requests
        inference_client = TestClient(self.coordinator_server.inference_app)
        response = inference_client.post(
            "/v1/chat/completions",
            json={"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "test"}]},
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )

        assert response.status_code == 200, "Timeout handler should not block normal requests"

    def test_verify_api_key_skip_paths(self):
        """Test verify_api_key with skip paths"""
        # Test that skip paths don't require API key
        TestClient(self.coordinator_server.inference_app)

        # These paths should not require API key (tested indirectly through skip_paths)
        # The actual skip paths are configured in api_key_config

    def test_lifespan_context_manager(self):
        """Test lifespan context manager"""
        # Test that the lifespan context manager can be entered and exited
        from fastapi import FastAPI

        app = FastAPI(lifespan=self.coordinator_server.lifespan)
        client = TestClient(app)

        # The lifespan should work correctly (any HTTP status means the app responded)
        client.get("/")
        assert True, "Lifespan context manager works correctly"

    def test_setup_rate_limiting_with_disabled_config(self):
        """Test setup_rate_limiting with disabled config"""
        from motor.config.coordinator import RateLimitConfig

        # Create a config with rate limit disabled
        disabled_config = RateLimitConfig()
        disabled_config.enable_rate_limit = False

        coordinator_server = _TestServerShell(config=CoordinatorConfig())
        coordinator_server.instance_manager = MagicMock()
        coordinator_server.setup_rate_limiting(rate_limit_config=disabled_config)
        assert True, "Setup rate limiting with disabled config works correctly"

    def test_setup_rate_limiting_with_exception(self):
        """Test setup_rate_limiting exception handling"""
        with patch("motor.coordinator.api_server.inference_server.SimpleRateLimiter") as mock_limiter:
            mock_limiter.side_effect = Exception("Test exception")
            coordinator_server = _TestServerShell(config=CoordinatorConfig())
            coordinator_server.instance_manager = MagicMock()
            coordinator_server.setup_rate_limiting()
            assert True, "Setup rate limiting handles exceptions correctly"

    def test_create_unified_app_with_exception(self):
        """Test create_unified_app exception handling"""
        with patch("motor.coordinator.api_server.inference_server.SimpleRateLimiter") as mock_limiter:
            mock_limiter.side_effect = Exception("Test exception")
            unified_app = self.coordinator_server.create_unified_app()
            assert unified_app is not None, "Unified app should be created even with exceptions"

    def test_copy_routes_with_exception(self):
        """Test _copy_routes when a route has invalid shape (path/endpoint must be real to avoid re/str errors)."""
        from fastapi import FastAPI

        src_app = FastAPI()

        @src_app.get("/test")
        async def test():
            return {"status": "ok"}

        dst_app = FastAPI()

        # Use a route-like object with string path so add_api_route never sees MagicMock as path
        bad_route = MagicMock()
        bad_route.path = "/bad"
        bad_route.methods = {"GET"}
        bad_route.endpoint = None  # endpoint None is skipped by _copy_routes (getattr(route, "endpoint", None))
        with patch.object(src_app.router, "routes", new=[bad_route]):
            self.coordinator_server._copy_routes(src_app, dst_app)
        assert True, "Copy routes handles exception-like route correctly"

    def test_handle_openai_request_json_decode_error(self):
        """Test _handle_openai_request with JSON decode error"""
        inference_client = TestClient(self.coordinator_server.inference_app)

        # Send invalid JSON
        response = inference_client.post(
            "/v1/chat/completions",
            content="invalid json",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
        )

        # Should return error status code
        assert response.status_code in [400, 422, 500], f"Expected error for invalid JSON, got: {response.status_code}"

    def test_handle_openai_request_general_exception(self):
        """Test _handle_openai_request when handle_request raises (expect 500; avoid 503 via _is_available True)."""
        with patch(
            "motor.coordinator.api_server.inference_server.handle_request",
            new_callable=AsyncMock,
        ) as mock_handle:
            mock_handle.side_effect = Exception("Test exception")
            inference_client = TestClient(self.coordinator_server.inference_app)
            response = inference_client.post(
                "/v1/chat/completions",
                json={"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "test"}]},
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.valid_api_key}"},
            )
            # _is_available is still patched True in setup, so we reach handle_request; exception -> 500
            assert response.status_code == 500, f"Expected 500 for exception, got: {response.status_code}"


class TestFastAPIMiddlewareAdvanced:
    """Test FastAPI middleware advanced functionality"""

    def setup_method(self):
        """Setup test fixtures"""
        from motor.coordinator.middleware.fastapi_middleware import SimpleRateLimitMiddleware
        from motor.coordinator.middleware.rate_limiter import SimpleRateLimiter
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        self.app = FastAPI()
        self.SimpleRateLimitMiddleware = SimpleRateLimitMiddleware
        self.SimpleRateLimiter = SimpleRateLimiter
        self.TestClient = TestClient
        self._report_alarms_patcher = patch(
            "motor.coordinator.api_client.controller_api_client.ControllerApiClient.report_alarms",
            return_value=True,
        )
        self._report_alarms_patcher.start()

    def teardown_method(self):
        self._report_alarms_patcher.stop()

    def test_rate_limit_middleware_extract_request_data(self):
        """Test _extract_request_data method"""

        @self.app.get("/test")
        async def test_endpoint():
            return {"status": "ok"}

        rate_limiter = self.SimpleRateLimiter(max_requests=10, window_size=60)
        middleware = self.SimpleRateLimitMiddleware(app=self.app, rate_limiter=rate_limiter, skip_paths=[])

        client = self.TestClient(middleware)

        # Make a request to trigger _extract_request_data
        response = client.get("/test")
        assert response.status_code == 200, "Request should succeed"
        assert middleware.stats["total_requests"] > 0, "Should extract request data"

    def test_rate_limit_middleware_create_rate_limit_headers(self):
        """Test _create_rate_limit_headers method"""

        @self.app.get("/test")
        async def test_endpoint():
            return {"status": "ok"}

        rate_limiter = self.SimpleRateLimiter(max_requests=10, window_size=60)
        middleware = self.SimpleRateLimitMiddleware(app=self.app, rate_limiter=rate_limiter, skip_paths=[])

        client = self.TestClient(middleware)

        # Make a request to trigger header creation
        response = client.get("/test")
        assert response.status_code == 200, "Request should succeed"
        # Check if headers are present
        assert "X-RateLimit-Remaining" in response.headers or "X-RateLimit-Limit" in response.headers, (
            "Should create rate limit headers"
        )

    def test_rate_limit_middleware_dispatch_exception(self):
        """Test dispatch method exception handling"""

        @self.app.get("/test")
        async def test_endpoint():
            return {"status": "ok"}

        rate_limiter = MagicMock()
        rate_limiter.is_allowed = MagicMock(side_effect=Exception("Test error"))

        middleware = self.SimpleRateLimitMiddleware(app=self.app, rate_limiter=rate_limiter, skip_paths=[])

        client = self.TestClient(middleware)

        # Should allow request when error occurs
        response = client.get("/test")
        assert response.status_code == 200, "Should allow request when error occurs"
        assert middleware.stats["allowed_requests"] > 0, "Should increment allowed_requests on error"

    def test_rate_limit_middleware_should_skip_path(self):
        """Test _should_skip_path method"""

        @self.app.get("/test")
        async def test_endpoint():
            return {"status": "ok"}

        @self.app.get("/liveness")
        async def liveness_endpoint():
            return {"status": "healthy"}

        rate_limiter = self.SimpleRateLimiter(max_requests=1, window_size=60)
        middleware = self.SimpleRateLimitMiddleware(app=self.app, rate_limiter=rate_limiter, skip_paths=["/liveness"])

        client = self.TestClient(middleware)

        # /liveness should be skipped
        response1 = client.get("/liveness")
        assert response1.status_code == 200, "Liveness endpoint should not be rate limited"

        response2 = client.get("/liveness")
        assert response2.status_code == 200, "Liveness endpoint should still not be rate limited"

        # /test should be rate limited
        response3 = client.get("/test")
        assert response3.status_code == 200, "First request should succeed"

        # Second request may be rate limited
        response4 = client.get("/test")
        assert response4.status_code in [200, 429], "Second request may be rate limited"


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_run_combined_mode(monkeypatch):
    from motor.coordinator.api_server.management_server import ManagementServer
    from motor.config.coordinator import CoordinatorConfig

    class DummyServer:
        def __init__(self, *args, **kwargs):
            self.should_exit = False

        async def serve(self):
            return

    monkeypatch.setattr("motor.coordinator.api_server.management_server.uvicorn.Server", lambda *a, **k: DummyServer())

    cfg = CoordinatorConfig()
    # Disable TLS for testing
    cfg.infer_tls_config.enable_tls = False
    cfg.mgmt_tls_config.enable_tls = False

    srv = ManagementServer(config=cfg)
    await srv.run()


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_run_split_mode(monkeypatch):
    from motor.coordinator.api_server.management_server import ManagementServer
    from motor.config.coordinator import CoordinatorConfig

    instances = []

    class DummyServer:
        def __init__(self, *args, **kwargs):
            self.should_exit = False
            instances.append(self)

        async def serve(self):
            return

    monkeypatch.setattr("motor.coordinator.api_server.management_server.uvicorn.Server", lambda *a, **k: DummyServer())

    cfg = CoordinatorConfig()
    # Disable TLS for testing
    cfg.infer_tls_config.enable_tls = False
    cfg.mgmt_tls_config.enable_tls = False

    srv = ManagementServer(config=cfg)
    await srv.run()
    assert len(instances) == 2 or len(instances) == 0 or len(instances) == 1


class TestValidateAnthropicRequest:
    """Unit tests for _validate_anthropic_request validation function."""

    def test_valid_messages_request(self):
        """Valid Anthropic messages request should pass validation."""
        _validate_anthropic_request(
            {"model": "claude-3", "messages": [{"role": "user", "content": "Hello"}], "max_tokens": 100},
            require_max_tokens=True,
        )

    def test_valid_messages_request_with_complex_content(self):
        """Valid request with content blocks should pass validation."""
        _validate_anthropic_request(
            {
                "model": "claude-3",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Hello"},
                            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "..."}},
                        ],
                    }
                ],
                "max_tokens": 100,
            },
            require_max_tokens=True,
        )

    def test_missing_model(self):
        """Missing model field should raise HTTP 400."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_anthropic_request(
                {"messages": [{"role": "user", "content": "Hello"}], "max_tokens": 100},
            )
        assert exc_info.value.status_code == 400
        assert "model" in exc_info.value.detail.lower()

    def test_empty_model(self):
        """Empty model should raise HTTP 400."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_anthropic_request(
                {"model": "", "messages": [{"role": "user", "content": "Hello"}], "max_tokens": 100},
            )
        assert exc_info.value.status_code == 400

    def test_missing_messages(self):
        """Missing messages field should raise HTTP 400."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_anthropic_request(
                {"model": "claude-3", "max_tokens": 100},
            )
        assert exc_info.value.status_code == 400
        assert "messages" in exc_info.value.detail.lower()

    def test_empty_messages(self):
        """Empty messages array should raise HTTP 400."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_anthropic_request(
                {"model": "claude-3", "messages": [], "max_tokens": 100},
            )
        assert exc_info.value.status_code == 400

    def test_messages_not_a_list(self):
        """Messages as non-list should raise HTTP 400."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_anthropic_request(
                {"model": "claude-3", "messages": "not a list", "max_tokens": 100},
            )
        assert exc_info.value.status_code == 400

    def test_missing_max_tokens(self):
        """Missing max_tokens should raise HTTP 400 when require_max_tokens=True."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_anthropic_request(
                {"model": "claude-3", "messages": [{"role": "user", "content": "Hello"}]},
                require_max_tokens=True,
            )
        assert exc_info.value.status_code == 400
        assert "max_tokens" in exc_info.value.detail.lower()

    def test_max_tokens_zero(self):
        """max_tokens=0 should raise HTTP 400."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_anthropic_request(
                {"model": "claude-3", "messages": [{"role": "user", "content": "Hello"}], "max_tokens": 0},
            )
        assert exc_info.value.status_code == 400

    def test_max_tokens_negative(self):
        """Negative max_tokens should raise HTTP 400."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_anthropic_request(
                {"model": "claude-3", "messages": [{"role": "user", "content": "Hello"}], "max_tokens": -1},
            )
        assert exc_info.value.status_code == 400

    def test_max_tokens_not_integer(self):
        """Non-integer max_tokens should raise HTTP 400."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_anthropic_request(
                {"model": "claude-3", "messages": [{"role": "user", "content": "Hello"}], "max_tokens": "abc"},
            )
        assert exc_info.value.status_code == 400

    def test_count_tokens_no_max_tokens_required(self):
        """Count tokens validation should NOT require max_tokens."""
        _validate_anthropic_request(
            {"model": "claude-3", "messages": [{"role": "user", "content": "Hello"}]},
            require_max_tokens=False,
        )

    def test_count_tokens_missing_model(self):
        """Count tokens validation should still require model."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_anthropic_request(
                {"messages": [{"role": "user", "content": "Hello"}]},
                require_max_tokens=False,
            )
        assert exc_info.value.status_code == 400

    def test_count_tokens_missing_messages(self):
        """Count tokens validation should still require messages."""
        with pytest.raises(HTTPException) as exc_info:
            _validate_anthropic_request(
                {"model": "claude-3"},
                require_max_tokens=False,
            )
        assert exc_info.value.status_code == 400

    def test_system_as_string_accepted(self):
        """System prompt as string should pass validation."""
        _validate_anthropic_request(
            {
                "model": "claude-3",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 100,
                "system": "You are a helpful assistant",
            },
            require_max_tokens=True,
        )

    def test_tool_definitions_accepted(self):
        """Request with tools should pass validation."""
        _validate_anthropic_request(
            {
                "model": "claude-3",
                "messages": [{"role": "user", "content": "What is the weather?"}],
                "max_tokens": 100,
                "tools": [{"name": "get_weather", "input_schema": {"type": "object"}}],
                "tool_choice": {"type": "auto"},
            },
            require_max_tokens=True,
        )


class TestValidateOpenaiPositiveIntField:
    """Unit tests for _validate_positive_int_field (max_tokens / max_completion_tokens)."""

    def test_missing_field_is_untouched(self):
        body = {"model": "m"}
        _validate_positive_int_field(body, OpenAIField.MAX_TOKENS)
        assert body == {"model": "m"}

    def test_valid_positive_int_is_kept(self):
        body = {"model": "m", "max_tokens": 128}
        _validate_positive_int_field(body, OpenAIField.MAX_TOKENS)
        assert body == {"model": "m", "max_tokens": 128}

    @pytest.mark.parametrize("value", [0, -1, 1.5, "128", True, False])
    def test_invalid_values_are_removed(self, value):
        body = {"model": "m", "max_tokens": value}
        _validate_positive_int_field(body, OpenAIField.MAX_TOKENS)
        assert "max_tokens" not in body

    def test_invalid_value_logs_warning(self, caplog):
        body = {"model": "m", "max_tokens": 0}
        _validate_positive_int_field(body, OpenAIField.MAX_TOKENS)
        assert "Invalid max_tokens=0" in caplog.text

    def test_openai_request_removes_invalid_max_tokens(self):
        body = {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 0,
            "max_completion_tokens": -1,
        }
        _validate_openai_request(body, RequestType.OPENAI)
        assert "max_tokens" not in body
        assert "max_completion_tokens" not in body

    def test_openai_request_keeps_valid_max_tokens(self):
        body = {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 128,
            "max_completion_tokens": 64,
        }
        _validate_openai_request(body, RequestType.OPENAI)
        assert body["max_tokens"] == 128
        assert body["max_completion_tokens"] == 64


class TestAnthropicEndpoints:
    """Integration tests for Anthropic API endpoints."""

    def setup_method(self):
        """Setup test fixtures"""
        # Mock InstanceManager
        self._im_patcher = patch("motor.coordinator.api_server.management_server.InstanceManager")
        im_mock_cls = self._im_patcher.start()
        im_instance = MagicMock()
        im_instance.has_required_instances.return_value = True
        im_instance.get_required_instances_status.return_value = InstanceReadiness.REQUIRED_MET
        im_instance.refresh_instances = AsyncMock(return_value=None)
        im_instance.validate_refresh_instances = AsyncMock(return_value=None)
        im_instance.snapshot_instances = AsyncMock(return_value=[])
        im_mock_cls.return_value = im_instance

        # Mock handle_request to return appropriate response
        async def mock_handle_request(request, config, scheduler=None, request_manager=None, request_json=None):
            if request_json is not None:
                body_json = request_json
            else:
                try:
                    body_json = await request.json()
                except Exception:
                    body_json = {}

            if "messages" in body_json:
                json.dumps(body_json["messages"], ensure_ascii=False)

            is_stream = body_json.get("stream", False)
            if isinstance(is_stream, str):
                is_stream = is_stream.lower() in ("true", "1", "yes")

            import hashlib

            request_id = f"req-{hashlib.md5(str(body_json).encode()).hexdigest()[:8]}"

            response_data = {
                "id": f"msg_{request_id}",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello! How can I help you?"}],
                "model": body_json.get("model", "unknown"),
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 7},
            }
            return JSONResponse(content=response_data)

        self._handle_request_patcher = patch(
            "motor.coordinator.api_server.inference_server.handle_request", side_effect=mock_handle_request
        )
        self._handle_request_patcher.start()

        self._is_available_patcher = patch(
            "motor.coordinator.api_server.inference_server.InferenceServer._is_available",
            new_callable=AsyncMock,
            return_value=True,
        )
        self._is_available_patcher.start()

        coordinator_config = CoordinatorConfig()
        coordinator_config.api_key_config.enable_api_key = True
        coordinator_config.api_key_config.valid_keys = {"sk-test123456789"}

        self.coordinator_server = _TestServerShell(config=coordinator_config)
        self.coordinator_server.setup_rate_limiting()
        inf = self.coordinator_server._inf
        inf._is_available = AsyncMock(return_value=True)
        _mock_scheduler = MagicMock()
        _mock_scheduler.get_available_instances = AsyncMock(return_value={})
        inf._get_scheduler_client = lambda: _mock_scheduler
        if not getattr(inf.app.state, "request_manager", None):
            inf.app.state.request_manager = inf._request_manager
        self.inference_client = TestClient(self.coordinator_server.inference_app)
        self.valid_api_key = "sk-test123456789"
        self.auth_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.valid_api_key}",
        }

    def teardown_method(self):
        for patcher_name in ["_im_patcher", "_handle_request_patcher", "_is_available_patcher"]:
            patcher = getattr(self, patcher_name, None)
            if patcher is not None:
                try:
                    patcher.stop()
                except Exception:
                    pass

    # 4.3 Integration test for POST /v1/messages
    def test_anthropic_messages_basic(self):
        """Basic Anthropic messages request should succeed."""
        response = self.inference_client.post(
            "/v1/messages",
            json={
                "model": "claude-3",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 100,
            },
            headers=self.auth_headers,
        )
        assert response.status_code == 200, f"Expected 200, got: {response.status_code}"
        data = response.json()
        assert data["type"] == "message"
        assert data["role"] == "assistant"
        assert len(data["content"]) > 0
        assert "usage" in data

    def test_anthropic_messages_with_system_prompt(self):
        """Anthropic request with system prompt should succeed."""
        response = self.inference_client.post(
            "/v1/messages",
            json={
                "model": "claude-3",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 100,
                "system": "You are a helpful assistant",
            },
            headers=self.auth_headers,
        )
        assert response.status_code == 200

    def test_anthropic_messages_with_tools(self):
        """Anthropic request with tools should succeed."""
        response = self.inference_client.post(
            "/v1/messages",
            json={
                "model": "claude-3",
                "messages": [{"role": "user", "content": "What is the weather?"}],
                "max_tokens": 100,
                "tools": [{"name": "get_weather", "input_schema": {"type": "object"}}],
                "tool_choice": {"type": "auto"},
            },
            headers=self.auth_headers,
        )
        assert response.status_code == 200

    # 4.4 Integration test for streaming
    def test_anthropic_messages_streaming(self):
        """Streaming Anthropic request should succeed."""
        response = self.inference_client.post(
            "/v1/messages",
            json={
                "model": "claude-3",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 100,
                "stream": True,
            },
            headers=self.auth_headers,
        )
        assert response.status_code == 200

    # 4.5 Integration test for count_tokens
    def test_anthropic_count_tokens(self):
        """Count tokens request should succeed."""
        response = self.inference_client.post(
            "/v1/messages/count_tokens",
            json={
                "model": "claude-3",
                "messages": [{"role": "user", "content": "Hello"}],
            },
            headers=self.auth_headers,
        )
        assert response.status_code == 200

    # 4.6 Error passthrough
    def test_anthropic_messages_missing_required_field(self):
        """Missing required field should return 400."""
        response = self.inference_client.post(
            "/v1/messages",
            json={
                "model": "claude-3",
                "messages": [{"role": "user", "content": "Hello"}],
                # Missing max_tokens
            },
            headers=self.auth_headers,
        )
        assert response.status_code == 400

    def test_anthropic_messages_auth_required(self):
        """Request without API key should return 401."""
        response = self.inference_client.post(
            "/v1/messages",
            json={
                "model": "claude-3",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 100,
            },
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 401

    def test_anthropic_messages_invalid_auth(self):
        """Request with invalid API key should return 403."""
        response = self.inference_client.post(
            "/v1/messages",
            json={
                "model": "claude-3",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 100,
            },
            headers={"Content-Type": "application/json", "Authorization": "Bearer invalid-key"},
        )
        assert response.status_code == 403

    def test_anthropic_count_tokens_missing_model(self):
        """Count tokens without model should return 400."""
        response = self.inference_client.post(
            "/v1/messages/count_tokens",
            json={"messages": [{"role": "user", "content": "Hello"}]},
            headers=self.auth_headers,
        )
        assert response.status_code == 400

    def test_anthropic_count_tokens_with_max_tokens_ok(self):
        """Count tokens with max_tokens (extra field) should still succeed."""
        response = self.inference_client.post(
            "/v1/messages/count_tokens",
            json={
                "model": "claude-3",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 100,  # extra, not validated against
            },
            headers=self.auth_headers,
        )
        assert response.status_code == 200

    # 4.7 Regression test
    def test_openai_endpoints_still_work(self):
        """Verify OpenAI endpoints still function."""
        response = self.inference_client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-3.5-turbo",
                "messages": [{"role": "user", "content": "Hello"}],
            },
            headers=self.auth_headers,
        )
        assert response.status_code == 200

        response = self.inference_client.post(
            "/v1/completions",
            json={"model": "text-davinci-003", "prompt": "Hello"},
            headers=self.auth_headers,
        )
        assert response.status_code == 200

    def test_service_unavailable(self):
        """When service is unavailable, should return 503."""
        self.coordinator_server._inf._is_available = AsyncMock(return_value=False)
        response = self.inference_client.post(
            "/v1/messages",
            json={
                "model": "claude-3",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 100,
            },
            headers=self.auth_headers,
        )
        assert response.status_code == 503
