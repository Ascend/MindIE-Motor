from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from motor.common.resources.dispatch import DispatchPlan, has_compatible_dispatch_pair
from motor.config.coordinator import CoordinatorConfig
from motor.common.resources.instance import Instance, PDRole
from motor.coordinator.domain import InstanceReadiness
from motor.coordinator.domain.request_manager import RequestManager
from motor.coordinator.router import dispatch
from motor.coordinator.router.upstream_error import UpstreamHTTPError


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

    async def has_compatible_pd_pair(self):
        if self._instances is None:
            return False
        prefill = [instance for instance in self._instances.values() if instance.role == PDRole.ROLE_P.value]
        decode = [instance for instance in self._instances.values() if instance.role == PDRole.ROLE_D.value]
        return has_compatible_dispatch_pair(prefill, decode)


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
            dispatch_capabilities=[DispatchPlan.CONCURRENT_ENGINE_SYNC.value],
        ),
        2: Instance(
            job_name="d",
            model_name="m",
            id=2,
            role=PDRole.ROLE_D.value,
            dispatch_capabilities=[DispatchPlan.CONCURRENT_ENGINE_SYNC.value],
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


def test_dispatch_rejects_incompatible_pd_topology():
    instances = {
        1: Instance(
            job_name="p",
            model_name="m",
            id=1,
            role=PDRole.ROLE_P.value,
            dispatch_capabilities=[DispatchPlan.CONCURRENT_ENGINE_SYNC.value],
        ),
        2: Instance(
            job_name="d",
            model_name="m",
            id=2,
            role=PDRole.ROLE_D.value,
            dispatch_capabilities=[DispatchPlan.PREFILL_HANDOFF_DECODE.value],
        ),
    }

    response = TestClient(_app(_config(), _Scheduler(instances))).post(
        "/v1/completions",
        json={"model": "m", "prompt": "hi"},
    )

    assert response.status_code == 503


def test_dispatch_falls_back_to_union_for_incompatible_pd(monkeypatch):
    class _FakeHybridRouter:
        def __init__(self, req_info, config, scheduler=None, request_manager=None, sampling_manager=None):
            pass

        async def handle_request(self):
            return JSONResponse({"router": "hybrid"})

    monkeypatch.setattr(dispatch, "PDHybridRouter", _FakeHybridRouter)
    instances = {
        1: Instance(
            job_name="p",
            model_name="m",
            id=1,
            role=PDRole.ROLE_P.value,
            dispatch_capabilities=[DispatchPlan.CONCURRENT_ENGINE_SYNC.value],
        ),
        2: Instance(
            job_name="d",
            model_name="m",
            id=2,
            role=PDRole.ROLE_D.value,
            dispatch_capabilities=[DispatchPlan.PREFILL_HANDOFF_DECODE.value],
        ),
        3: Instance(job_name="u", model_name="m", id=3, role=PDRole.ROLE_U.value),
    }

    response = TestClient(_app(_config(), _Scheduler(instances))).post(
        "/v1/completions",
        json={"model": "m", "prompt": "hi"},
    )

    assert response.status_code == 200
    assert response.json() == {"router": "hybrid"}


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
            dispatch_capabilities=[DispatchPlan.CONCURRENT_ENGINE_SYNC.value],
        ),
        2: Instance(
            job_name="d",
            model_name="m",
            id=2,
            role=PDRole.ROLE_D.value,
            dispatch_capabilities=[DispatchPlan.CONCURRENT_ENGINE_SYNC.value],
        ),
    }

    response = TestClient(_app(_config(), _Scheduler(instances))).post(
        "/v1/completions",
        json={"model": "m", "prompt": "hi"},
    )

    assert response.status_code == 400
    assert response.content == error_body
    assert response.headers["retry-after"] == "3"
