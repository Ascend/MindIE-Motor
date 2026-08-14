# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import asyncio
import json

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import motor.common.utils.error as cancel_error
from motor.common.logger.logger import _resolve_logger_name
from motor.config.coordinator import CoordinatorConfig
from motor.common.resources.instance import Instance, PDRole
from motor.coordinator.domain import InstanceReadiness
from motor.coordinator.domain.request_manager import RequestManager
from motor.coordinator.router import dispatch
from motor.coordinator.router.upstream_error import UpstreamHTTPError
from motor.common.utils.error import RequestCancelledError


class _Scheduler:
    def __init__(self, instances: dict[int, Instance] | None = None):
        self._instances = instances

    async def get_available_instance_roles(self):
        if self._instances is None:
            raise RuntimeError("instance shape unavailable")
        return {PDRole(instance.role) for instance in self._instances.values()}

    async def has_required_instances(self):
        if self._instances is not None:
            has_p = any(instance.role == PDRole.ROLE_P.value for instance in self._instances.values())
            has_d = any(instance.role == PDRole.ROLE_D.value for instance in self._instances.values())
            if has_p and not has_d:
                return InstanceReadiness.ONLY_PREFILL
        return InstanceReadiness.REQUIRED_MET


def _app(config: CoordinatorConfig, scheduler: _Scheduler) -> FastAPI:
    app = FastAPI()
    request_manager = RequestManager(config)

    @app.post("/v1/completions")
    async def completions(request: Request):
        return await dispatch.handle_request(
            request,
            config,
            scheduler=scheduler,
            request_manager=request_manager,
        )

    return app


def _config() -> CoordinatorConfig:
    return CoordinatorConfig()


def test_dispatch_uses_unified_router_by_default(monkeypatch):
    calls = []

    class _FakeUnifiedRouter:
        def __init__(self, req_info, config, scheduler=None, request_manager=None, sampling_manager=None):
            calls.append(req_info.req_data)

        async def handle_request(self):
            return JSONResponse({"router": "unified"})

    monkeypatch.setattr(dispatch, "UnifiedPDRouter", _FakeUnifiedRouter)

    instances = {
        1: Instance(
            job_name="p",
            model_name="m",
            id=1,
            role=PDRole.ROLE_P.value,
        ),
        2: Instance(
            job_name="d",
            model_name="m",
            id=2,
            role=PDRole.ROLE_D.value,
        ),
    }
    client = TestClient(_app(_config(), _Scheduler(instances)))
    response = client.post("/v1/completions", json={"model": "m", "prompt": "hi"})

    assert response.status_code == 200
    assert response.json() == {"router": "unified"}
    assert calls and calls[0]["prompt"] == "hi"


def test_dispatch_uses_single_node_fallback_when_only_prefill(monkeypatch):
    calls = []

    class _FakeHybridRouter:
        def __init__(self, req_info, config, scheduler=None, request_manager=None, sampling_manager=None):
            calls.append(req_info.req_data)

        async def handle_request(self):
            return JSONResponse({"router": "hybrid"})

    instances = {
        1: Instance(job_name="p", model_name="m", id=1, role=PDRole.ROLE_P.value),
    }
    monkeypatch.setattr(dispatch, "PDHybridRouter", _FakeHybridRouter)

    app = FastAPI()
    config = CoordinatorConfig()
    request_manager = RequestManager(config)

    @app.post("/v1/completions")
    async def completions(request: Request):
        return await dispatch.handle_request(
            request,
            config,
            scheduler=_Scheduler(instances),
            request_manager=request_manager,
        )

    response = TestClient(app).post("/v1/completions", json={"model": "m", "prompt": "hi"})

    assert response.status_code == 200
    assert response.json() == {"router": "hybrid"}
    assert calls and calls[0]["prompt"] == "hi"


def test_dispatch_rejects_only_prefill_when_hybrid_fallback_disabled(monkeypatch):
    class _FakeHybridRouter:
        def __init__(self, req_info, config, scheduler=None, request_manager=None, sampling_manager=None):
            raise AssertionError("PDHybridRouter should not be used when fallback is disabled")

    instances = {
        1: Instance(job_name="p", model_name="m", id=1, role=PDRole.ROLE_P.value),
    }
    monkeypatch.setattr(dispatch, "PDHybridRouter", _FakeHybridRouter)

    config = CoordinatorConfig()
    config.scheduler_config.enable_pd_separation_fallback_to_hybrid = False

    response = TestClient(_app(config, _Scheduler(instances))).post(
        "/v1/completions",
        json={"model": "m", "prompt": "hi"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "PD separate service is unavailable and fallback to hybrid is disabled"


def test_dispatch_reuses_request_json_parsed_at_ingress(monkeypatch):
    """A body parsed by the API layer is not parsed again while building RequestInfo."""
    calls = []

    class _FakeHybridRouter:
        def __init__(self, req_info, config, scheduler=None, request_manager=None, sampling_manager=None):
            calls.append(req_info.req_data)

        async def handle_request(self):
            return JSONResponse({"router": "hybrid"})

    monkeypatch.setattr(dispatch, "PDHybridRouter", _FakeHybridRouter)
    app = FastAPI()
    config = CoordinatorConfig()
    request_manager = RequestManager(config)
    scheduler = _Scheduler({1: Instance(job_name="p", model_name="m", id=1, role=PDRole.ROLE_P.value)})

    @app.post("/v1/completions")
    async def completions(request: Request):
        request_json = json.loads((await request.body()).decode("utf-8"))

        async def unexpected_second_parse():
            raise AssertionError("request.json() must not be called after ingress parsing")

        monkeypatch.setattr(request, "json", unexpected_second_parse)
        return await dispatch.handle_request(
            request,
            config,
            scheduler=scheduler,
            request_manager=request_manager,
            request_json=request_json,
        )

    response = TestClient(app).post("/v1/completions", json={"model": "m", "prompt": "hi"})

    assert response.status_code == 200
    assert calls == [{"model": "m", "prompt": "hi"}]


def test_dispatch_preserves_upstream_http_error(monkeypatch):
    error_body = b'{"error":{"message":"prompt is too long","code":400}}'

    class _RejectingUnifiedRouter:
        def __init__(self, req_info, config, scheduler=None, request_manager=None, sampling_manager=None):
            pass

        async def handle_request(self):
            raise UpstreamHTTPError(
                status_code=400,
                body=error_body,
                headers={"content-type": "application/json", "retry-after": "3"},
                phase="non-stream",
            )

    monkeypatch.setattr(dispatch, "UnifiedPDRouter", _RejectingUnifiedRouter)
    instances = {
        1: Instance(
            job_name="p",
            model_name="m",
            id=1,
            role=PDRole.ROLE_P.value,
        ),
        2: Instance(
            job_name="d",
            model_name="m",
            id=2,
            role=PDRole.ROLE_D.value,
        ),
    }

    response = TestClient(_app(_config(), _Scheduler(instances))).post(
        "/v1/completions",
        json={"model": "m", "prompt": "hi"},
    )

    assert response.status_code == 400
    assert response.content == error_body
    assert response.headers["retry-after"] == "3"


def test_dispatch_request_cancelled_does_not_log_error(monkeypatch, caplog):
    import logging

    caplog.set_level(
        logging.DEBUG,
        logger=_resolve_logger_name("motor.coordinator.router.dispatch"),
    )

    class _CancellingUnifiedRouter:
        def __init__(self, req_info, config, scheduler=None, request_manager=None, sampling_manager=None):
            pass

        async def handle_request(self):
            raise RequestCancelledError(cancel_error.CLIENT_DISCONNECT)

    monkeypatch.setattr(dispatch, "UnifiedPDRouter", _CancellingUnifiedRouter)
    instances = {
        1: Instance(
            job_name="p",
            model_name="m",
            id=1,
            role=PDRole.ROLE_P.value,
        ),
        2: Instance(
            job_name="d",
            model_name="m",
            id=2,
            role=PDRole.ROLE_D.value,
        ),
    }

    response = TestClient(_app(_config(), _Scheduler(instances))).post(
        "/v1/completions",
        json={"model": "m", "prompt": "hi"},
    )

    assert response.status_code == 499
    assert cancel_error.CLIENT_DISCONNECT in response.json()["detail"]
    assert "Error occurred in proxy server endpoint" not in caplog.text
    assert "Request cancelled api=v1/completions" in caplog.text


def test_dispatch_dispatch_abort_still_returns_500(monkeypatch, caplog):
    import logging

    caplog.set_level(
        logging.DEBUG,
        logger=_resolve_logger_name("motor.coordinator.router.dispatch"),
    )

    class _AbortingUnifiedRouter:
        def __init__(self, req_info, config, scheduler=None, request_manager=None, sampling_manager=None):
            pass

        async def handle_request(self):
            raise RequestCancelledError(cancel_error.DISPATCH_ABORT)

    monkeypatch.setattr(dispatch, "UnifiedPDRouter", _AbortingUnifiedRouter)
    instances = {
        1: Instance(
            job_name="p",
            model_name="m",
            id=1,
            role=PDRole.ROLE_P.value,
        ),
        2: Instance(
            job_name="d",
            model_name="m",
            id=2,
            role=PDRole.ROLE_D.value,
        ),
    }

    response = TestClient(_app(_config(), _Scheduler(instances))).post(
        "/v1/completions",
        json={"model": "m", "prompt": "hi"},
    )

    assert response.status_code == 500
    assert cancel_error.DISPATCH_ABORT in response.json()["detail"]
    assert "Error occurred in proxy server endpoint" not in caplog.text
    assert "Request cancelled api=v1/completions" in caplog.text


@pytest.mark.asyncio
async def test_with_cancellation_disconnect_raises_http_499(caplog):
    import logging

    caplog.set_level(
        logging.INFO,
        logger=_resolve_logger_name("motor.coordinator.router.dispatch"),
    )

    async def receive():
        return {"type": "http.disconnect"}

    @dispatch.with_cancellation
    async def handler(raw_request: Request):
        await asyncio.Future()

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/completions",
            "headers": [],
        },
        receive=receive,
    )

    with pytest.raises(HTTPException) as exc_info:
        await handler(raw_request=request)

    assert exc_info.value.status_code == 499
    assert cancel_error.CLIENT_DISCONNECT in str(exc_info.value.detail)
    assert "Client disconnected; cancelling in-flight request with HTTP 499" in caplog.text


def test_dispatch_unexpected_exception_still_logs_error(monkeypatch, caplog):
    import logging

    caplog.set_level(
        logging.ERROR,
        logger=_resolve_logger_name("motor.coordinator.router.dispatch"),
    )

    class _FailingUnifiedRouter:
        def __init__(self, req_info, config, scheduler=None, request_manager=None, sampling_manager=None):
            pass

        async def handle_request(self):
            raise RuntimeError("unexpected upstream failure")

    monkeypatch.setattr(dispatch, "UnifiedPDRouter", _FailingUnifiedRouter)
    instances = {
        1: Instance(
            job_name="p",
            model_name="m",
            id=1,
            role=PDRole.ROLE_P.value,
        ),
        2: Instance(
            job_name="d",
            model_name="m",
            id=2,
            role=PDRole.ROLE_D.value,
        ),
    }

    response = TestClient(_app(_config(), _Scheduler(instances))).post(
        "/v1/completions",
        json={"model": "m", "prompt": "hi"},
    )

    assert response.status_code == 500
    assert "Error occurred in proxy server endpoint" in caplog.text
