from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from motor.config.coordinator import CoordinatorConfig, DeployMode
from motor.common.resources.instance import Instance, PDRole
from motor.coordinator.domain import InstanceReadiness
from motor.coordinator.domain.request_manager import RequestManager
from motor.coordinator.router import dispatch


class _Scheduler:
    def __init__(self, instances: dict[int, Instance] | None = None):
        self._instances = instances

    async def get_available_instances(self, role=None):
        if self._instances is None:
            raise RuntimeError("instance shape unavailable")
        if role is None:
            return self._instances
        return {
            instance_id: instance for instance_id, instance in self._instances.items() if instance.role == role.value
        }

    async def has_required_instances(self):
        if self._instances is not None:
            has_p = any(instance.role == PDRole.ROLE_P.value for instance in self._instances.values())
            has_d = any(instance.role == PDRole.ROLE_D.value for instance in self._instances.values())
            if has_p and not has_d:
                return InstanceReadiness.ONLY_PREFILL
        return InstanceReadiness.REQUIRED_MET


def _app(config: CoordinatorConfig) -> FastAPI:
    app = FastAPI()
    request_manager = RequestManager(config)

    @app.post("/v1/completions")
    async def completions(request: Request):
        return await dispatch.handle_request(
            request,
            config,
            scheduler=_Scheduler(),
            request_manager=request_manager,
        )

    return app


def _config() -> CoordinatorConfig:
    config = CoordinatorConfig()
    config.scheduler_config.deploy_mode = DeployMode.PD_DUAL_DISPATCH
    return config


def test_dispatch_uses_unified_router_by_default(monkeypatch):
    calls = []

    class _FakeUnifiedRouter:
        def __init__(self, req_info, config, scheduler=None, request_manager=None, sampling_manager=None):
            calls.append(req_info.req_data)

        async def handle_request(self):
            return JSONResponse({"router": "unified"})

    monkeypatch.setitem(dispatch._ROUTER_MAP, DeployMode.PD_DUAL_DISPATCH, _FakeUnifiedRouter)

    client = TestClient(_app(_config()))
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
    monkeypatch.setitem(dispatch._ROUTER_MAP, DeployMode.SINGLE_NODE, _FakeHybridRouter)

    app = FastAPI()
    config = CoordinatorConfig()
    config.scheduler_config.deploy_mode = DeployMode.CDP_SEPARATE
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
