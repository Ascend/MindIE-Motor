# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Opt-in smoke tests against a real native vLLM or SGLang P/D pair.

Required environment:

* ``MOTOR_RUN_NATIVE_PD_SMOKE=1``
* ``MOTOR_NATIVE_SMOKE_ENGINE_TYPE=vllm|sglang``
* ``MOTOR_NATIVE_SMOKE_PREFILL_URL=http[s]://host:port``
* ``MOTOR_NATIVE_SMOKE_DECODE_URL=http[s]://host:port``
* ``MOTOR_NATIVE_SMOKE_MODEL=<served model name>``
* ``MOTOR_NATIVE_SMOKE_BOOTSTRAP_PORT=<port>`` for SGLang

HTTPS additionally requires ``MOTOR_NATIVE_SMOKE_CA_FILE``,
``MOTOR_NATIVE_SMOKE_CERT_FILE``, and ``MOTOR_NATIVE_SMOKE_KEY_FILE``.
"""

import json
import os
from urllib.parse import urlparse

import pytest

from motor.common.http import HTTPClientPool
from motor.common.resources.endpoint import Endpoint, EndpointStatus, Workload
from motor.common.resources.instance import Instance, InsStatus, ParallelConfig, PDRole
from motor.config.coordinator import CoordinatorConfig, ExceptionConfig
from motor.config.tls_config import TLSConfig
from motor.coordinator.domain.request_manager import RequestManager
from motor.coordinator.models.request import RequestInfo, ReqState
from motor.coordinator.router.strategies.unified_pd import UnifiedPDRouter


pytestmark = pytest.mark.integration


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        pytest.fail(f"{name} is required when MOTOR_RUN_NATIVE_PD_SMOKE=1")
    return value


def _parse_endpoint_url(name: str) -> tuple[str, int, str]:
    raw = _required_env(name)
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        pytest.fail(f"{name} must be an http[s]://host:port URL")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        pytest.fail(f"{name} must not contain a path, query, or fragment")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.hostname, port, parsed.scheme


def _instance(
    instance_id: int,
    role: PDRole,
    engine_type: str,
    host: str,
    port: int,
    *,
    bootstrap_port: int | None = None,
) -> Instance:
    endpoint = Endpoint(
        id=instance_id,
        ip=host,
        business_port=str(port),
        mgmt_port=str(port),
        bootstrap_port=bootstrap_port,
        status=EndpointStatus.NORMAL,
    )
    return Instance(
        job_name=f"smoke-{engine_type}-{role.value}",
        model_name=_required_env("MOTOR_NATIVE_SMOKE_MODEL"),
        engine_type=engine_type,
        id=instance_id,
        role=role,
        status=InsStatus.ACTIVE,
        parallel_config=ParallelConfig(dp_size=1, tp_size=1),
        endpoints={host: {instance_id: endpoint}},
    )


class _RealNativeScheduler:
    def __init__(
        self,
        engine_type: str,
        prefill: tuple[str, int],
        decode: tuple[str, int],
        bootstrap_port: int | None,
    ):
        self.p = _instance(
            1,
            PDRole.ROLE_P,
            engine_type,
            *prefill,
            bootstrap_port=bootstrap_port,
        )
        self.d = _instance(2, PDRole.ROLE_D, engine_type, *decode)

    async def select_and_allocate(self, role, _req_info, **_kwargs):
        instance = self.p if role == PDRole.ROLE_P else self.d
        endpoint = next(iter(next(iter(instance.endpoints.values())).values()))
        return instance, endpoint, Workload(active_tokens=1)

    async def update_workload(self, _params):
        return True

    async def report_cb_event(self, _instance_id: int, _event: str) -> None:
        return None

    async def get_unblocked_instances(self, role) -> list[int]:
        if role == PDRole.ROLE_P:
            return [self.p.id]
        if role == PDRole.ROLE_D:
            return [self.d.id]
        return []


def _tls_config(scheme: str) -> TLSConfig:
    if scheme == "http":
        return TLSConfig(enable_tls=False)
    return TLSConfig(
        enable_tls=True,
        ca_file=_required_env("MOTOR_NATIVE_SMOKE_CA_FILE"),
        cert_file=_required_env("MOTOR_NATIVE_SMOKE_CERT_FILE"),
        key_file=_required_env("MOTOR_NATIVE_SMOKE_KEY_FILE"),
        passwd_file=os.getenv("MOTOR_NATIVE_SMOKE_PASSWD_FILE", "").strip(),
        crl_file=os.getenv("MOTOR_NATIVE_SMOKE_CRL_FILE", "").strip(),
    )


async def _stream_body(response) -> bytes:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
    return b"".join(chunks)


@pytest.mark.parametrize(
    ("api", "request_payload", "stream"),
    [
        ("v1/completions", {"prompt": "Reply with OK."}, False),
        ("v1/completions", {"prompt": "Reply with OK."}, True),
        (
            "v1/chat/completions",
            {"messages": [{"role": "user", "content": "Reply with OK."}]},
            False,
        ),
        (
            "v1/chat/completions",
            {"messages": [{"role": "user", "content": "Reply with OK."}]},
            True,
        ),
    ],
)
@pytest.mark.asyncio
async def test_real_native_pd_openai_smoke(api, request_payload, stream):
    if os.getenv("MOTOR_RUN_NATIVE_PD_SMOKE", "").strip() != "1":
        pytest.skip("set MOTOR_RUN_NATIVE_PD_SMOKE=1 to run real native P/D smoke tests")

    engine_type = _required_env("MOTOR_NATIVE_SMOKE_ENGINE_TYPE").lower()
    if engine_type not in {"vllm", "sglang"}:
        pytest.fail("MOTOR_NATIVE_SMOKE_ENGINE_TYPE must be vllm or sglang")
    p_host, p_port, p_scheme = _parse_endpoint_url("MOTOR_NATIVE_SMOKE_PREFILL_URL")
    d_host, d_port, d_scheme = _parse_endpoint_url("MOTOR_NATIVE_SMOKE_DECODE_URL")
    if p_scheme != d_scheme:
        pytest.fail("Prefill and decode smoke endpoints must use the same HTTP scheme")
    bootstrap_port = None
    if engine_type == "sglang":
        bootstrap_port = int(_required_env("MOTOR_NATIVE_SMOKE_BOOTSTRAP_PORT"))
        if not 1 <= bootstrap_port <= 65535:
            pytest.fail("MOTOR_NATIVE_SMOKE_BOOTSTRAP_PORT must be in range 1..65535")

    scheduler = _RealNativeScheduler(
        engine_type,
        (p_host, p_port),
        (d_host, d_port),
        bootstrap_port,
    )
    config = CoordinatorConfig()
    config.exception_config = ExceptionConfig(max_retry=1, retry_delay=0)
    config.infer_tls_config = _tls_config(p_scheme)
    req_data = {
        "model": _required_env("MOTOR_NATIVE_SMOKE_MODEL"),
        **request_payload,
        "max_tokens": 2,
        "stream": stream,
    }
    if stream:
        req_data["stream_options"] = {"include_usage": True}
    req_info = RequestInfo(
        req_id=f"native-smoke-{engine_type}-{api.replace('/', '-')}-{stream}",
        req_data=req_data,
        api=api,
        entry_api=api,
        req_len=8,
    )
    router = UnifiedPDRouter(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=RequestManager(config),
    )

    try:
        response = await router.handle_request()
        if stream:
            body = await _stream_body(response)
            assert b"data:" in body
            assert b'"error"' not in body
        else:
            body = response.body
            payload = json.loads(body)
            assert payload.get("choices")
        assert b"kv_transfer_params" not in body
        assert b"bootstrap_host" not in body
        assert b"bootstrap_port" not in body
        assert b"bootstrap_room" not in body
        assert req_info.state == ReqState.DECODE_END
    finally:
        await HTTPClientPool().close_all()
