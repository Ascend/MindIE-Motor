# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from motor.common.resources.instance import PDRole
from motor.config.endpoint import EndpointConfig
from motor.config.tls_config import TLSConfig
from motor.node_manager.core.services.native_engine.backends.base import build_endpoint_config
from motor.node_manager.core.services.native_engine.backends.sglang.backend import SGLangBackend
from motor.node_manager.core.services.native_engine.backends.vllm.backend import VllmBackend
from motor.node_manager.core.services.native_engine.factory import get_backend
from motor.node_manager.core.services.native_engine.models import LaunchContext


def _context(
    *,
    role: PDRole = PDRole.ROLE_P,
    headless: bool = False,
    dp_rank: int = 2,
    environment: dict | None = None,
) -> LaunchContext:
    return LaunchContext(
        role=role,
        instance_id=7,
        dp_rank=dp_rank,
        node_rank=1,
        host="10.0.0.2",
        business_port=8002,
        mgmt_port=9002,
        config_path="/config/user_config.json",
        master_dp_ip="10.0.0.1",
        kv_port=5002,
        lookup_rpc_port=6002,
        dp_rpc_port=7002,
        d2d_peer_ips=("10.0.0.3",),
        environment=environment or {"VLLM_HOST_IP": "10.0.0.2"},
        headless=headless,
    )


def _endpoint(
    *,
    engine_type: str,
    role: str,
    connector: str | None = None,
    enable_virtual_inference: bool = False,
    npu_usage_threshold: int = 3,
):
    engine_config = {}
    if connector is not None:
        engine_config["kv_transfer_config"] = {"kv_connector": connector}
    return SimpleNamespace(
        engine_type=engine_type,
        role=role,
        deploy_config=SimpleNamespace(
            engine_config=engine_config,
            dispatch_profile=None,
            health_check_config=SimpleNamespace(
                health_collector_timeout=5,
                health_collector_timeout_retry_attempts=3,
                startup_timeout=1800,
                enable_virtual_inference=enable_virtual_inference,
                npu_usage_threshold=npu_usage_threshold,
            ),
            infer_tls_config=None,
        ),
    )


def _prepare_with_config(backend, context, endpoint):
    native_config = MagicMock()
    native_config.get_cli_args.return_value = ["--model", "/models/glm-test", "--port", "8002"]
    with (
        patch(
            "motor.node_manager.core.services.native_engine.backends.base.build_endpoint_config", return_value=endpoint
        ),
        patch("motor.node_manager.core.services.native_engine.config_factory.ConfigFactory") as factory,
    ):
        factory.return_value.build_cli_config.return_value = native_config
        return backend.prepare(context)


def _build_with_config(backend, context, endpoint):
    return _prepare_with_config(backend, context, endpoint).command


def test_vllm_backend_builds_native_command_and_preserves_environment():
    context = _context()
    spec = _build_with_config(
        VllmBackend(),
        context,
        _endpoint(engine_type="vllm", role="prefill", connector="MooncakeConnectorV1"),
    )

    assert spec.argv == ("vllm", "serve", "--model", "/models/glm-test", "--port", "8002")
    assert dict(spec.env) == {"VLLM_HOST_IP": "10.0.0.2"}
    assert "engine_server" not in spec.argv


def test_backend_prepares_command_and_probe_from_one_endpoint_config():
    context = _context()
    endpoint = _endpoint(engine_type="vllm", role="prefill", connector="MooncakeConnectorV1")
    native_config = MagicMock()
    native_config.get_cli_args.return_value = ["--model", "/models/glm-test"]

    with (
        patch(
            "motor.node_manager.core.services.native_engine.backends.base.build_endpoint_config",
            return_value=endpoint,
        ) as build_endpoint,
        patch("motor.node_manager.core.services.native_engine.config_factory.ConfigFactory") as factory,
    ):
        factory.return_value.build_cli_config.return_value = native_config
        launch_spec = VllmBackend().prepare(context)

    build_endpoint.assert_called_once_with(context, "vllm")
    assert launch_spec.command.argv == ("vllm", "serve", "--model", "/models/glm-test")
    assert launch_spec.probe.path == "/health"
    native_config.get_cli_args.assert_called_once_with()


@pytest.mark.parametrize(
    "connector",
    ["MooncakeConnectorV1", "MooncakeHybridConnector", "NixlConnector"],
)
def test_vllm_backend_accepts_frozen_handoff_connector_whitelist(connector):
    context = _context()

    spec = _build_with_config(
        VllmBackend(),
        context,
        _endpoint(engine_type="vllm", role="prefill", connector=connector),
    )

    assert spec.argv[:2] == ("vllm", "serve")


def test_vllm_backend_accepts_multi_connector_with_handoff_transport():
    context = _context()
    endpoint = _endpoint(engine_type="vllm", role="prefill", connector="MooncakeConnectorV1")
    endpoint.deploy_config.engine_config = {
        "kv_transfer_config": {
            "kv_connector": "MultiConnector",
            "kv_connector_extra_config": {
                "connectors": [
                    {"kv_connector": "NixlConnector"},
                    {"kv_connector": "AscendStoreConnector"},
                ]
            },
        }
    }

    spec = _build_with_config(VllmBackend(), context, endpoint)

    assert spec.argv[:2] == ("vllm", "serve")


@pytest.mark.parametrize(
    "connector,profile",
    [
        ("UnknownConnector", "unknown"),
    ],
)
def test_vllm_backend_rejects_non_handoff_pd_connector(connector, profile):
    context = _context()
    endpoint = _endpoint(engine_type="vllm", role="prefill", connector=connector)

    with (
        patch(
            "motor.node_manager.core.services.native_engine.backends.base.build_endpoint_config", return_value=endpoint
        ),
        pytest.raises(ValueError, match=rf"resolved dispatch profile is {profile}"),
    ):
        VllmBackend().prepare(context)


def test_vllm_backend_accepts_layerwise_connector():
    context = _context()
    spec = _build_with_config(
        VllmBackend(),
        context,
        _endpoint(engine_type="vllm", role="prefill", connector="MooncakeLayerwiseConnector"),
    )
    assert spec.argv[:2] == ("vllm", "serve")


def test_vllm_backend_accepts_explicit_trigger_profile():
    context = _context()
    endpoint = _endpoint(
        engine_type="vllm",
        role="prefill",
        connector="MooncakeConnectorV1",
    )
    endpoint.deploy_config.dispatch_profile = "trigger"

    spec = _build_with_config(VllmBackend(), context, endpoint)
    assert spec.argv[:2] == ("vllm", "serve")


def test_vllm_backend_allows_union_without_kv_connector():
    context = _context(role=PDRole.ROLE_U)

    spec = _build_with_config(
        VllmBackend(),
        context,
        _endpoint(engine_type="vllm", role="union"),
    )

    assert spec.argv[:2] == ("vllm", "serve")


def test_sglang_backend_builds_native_module_command():
    context = _context(role=PDRole.ROLE_D)

    spec = _build_with_config(
        SGLangBackend(),
        context,
        _endpoint(engine_type="sglang", role="decode"),
    )

    assert spec.argv[:3] == ("python3", "-m", "sglang.launch_server")
    assert "engine_server" not in spec.argv


def test_sglang_backend_rejects_encode_before_loading_config():
    context = _context(role=PDRole.ROLE_E)

    with (
        patch("motor.node_manager.core.services.native_engine.backends.base.build_endpoint_config") as build_endpoint,
        pytest.raises(ValueError, match="SGLang encode role is not supported"),
    ):
        SGLangBackend().prepare(context)

    build_endpoint.assert_not_called()


# ------------------------------------------------------------------
# SGLang generative health endpoint switch
# ------------------------------------------------------------------


def _sglang_prepare(*, dp_rank: int = 0, headless: bool = False, env=None):
    context = _context(role=PDRole.ROLE_D, dp_rank=dp_rank, headless=headless, environment=env)
    endpoint = _endpoint(engine_type="sglang", role="decode")
    return _prepare_with_config(SGLangBackend(), context, endpoint)


@pytest.mark.parametrize(
    "dp_rank, headless",
    [
        (0, False),
        (1, False),
        (0, True),
    ],
    ids=["dp0", "non_dp0", "headless"],
)
def test_sglang_backend_generation_env_always_true(dp_rank, headless):
    """Motor never runs virtual inference for SGLang: the generative /health
    switch is always pinned to true regardless of DP rank or headless.
    """
    spec = _sglang_prepare(dp_rank=dp_rank, headless=headless)

    assert spec.command.env["SGLANG_ENABLE_HEALTH_ENDPOINT_GENERATION"] == "true"


def test_sglang_backend_overrides_external_generation_env():
    """The generative /health switch is pinned true, overriding any external value."""
    external_env = {"VLLM_HOST_IP": "10.0.0.2", "SGLANG_ENABLE_HEALTH_ENDPOINT_GENERATION": "false"}
    spec = _sglang_prepare(env=external_env)

    assert spec.command.env["SGLANG_ENABLE_HEALTH_ENDPOINT_GENERATION"] == "true"
    assert spec.command.env["VLLM_HOST_IP"] == "10.0.0.2"


def test_sglang_generation_env_true_even_with_non_error_log_level():
    """Log-level gate is vLLM-only; SGLang generative /health stays forced on."""
    spec = _sglang_prepare(env={"ASCEND_GLOBAL_LOG_LEVEL": "1"})

    assert spec.command.env["SGLANG_ENABLE_HEALTH_ENDPOINT_GENERATION"] == "true"
    assert spec.probe.path == "/health"


def test_sglang_backend_probe_stays_lightweight_health():
    spec = _sglang_prepare()

    assert spec.probe.path == "/health"


def test_sglang_backend_pins_generation_env_when_deploy_config_missing():
    """Generative /health must stay pinned even if a stub/base path omits deploy_config."""
    from motor.node_manager.core.services.native_engine.models import CommandSpec, LaunchSpec, ProbeSpec

    context = _context(role=PDRole.ROLE_D, dp_rank=0)
    base_spec = LaunchSpec(
        command=CommandSpec(
            argv=("python3", "-m", "sglang.launch_server"),
            env={"SGLANG_ENABLE_HEALTH_ENDPOINT_GENERATION": "false", "KEEP": "1"},
        ),
        probe=ProbeSpec(path="/health", timeout_seconds=5.0, startup_timeout_seconds=1800.0),
        deploy_config=None,
    )
    backend = SGLangBackend()
    with patch(
        "motor.node_manager.core.services.native_engine.backends.base.BaseNativeEngineBackend.prepare",
        return_value=base_spec,
    ):
        spec = backend.prepare(context)

    assert spec.deploy_config is None
    assert spec.command.env["SGLANG_ENABLE_HEALTH_ENDPOINT_GENERATION"] == "true"
    assert spec.command.env["KEEP"] == "1"
    assert spec.probe.path == "/health"


def test_get_backend_rejects_unknown_engine():
    with pytest.raises(ValueError, match="Unsupported engine type"):
        get_backend("unknown")


def testbuild_endpoint_config_maps_launch_context_without_cli_globals():
    context = _context()

    with (
        patch.object(EndpointConfig, "validate") as validate,
        patch.object(EndpointConfig, "load_deploy_config") as load_deploy_config,
    ):
        endpoint_config = build_endpoint_config(context, "vllm")

    validate.assert_called_once_with()
    load_deploy_config.assert_called_once_with()
    assert endpoint_config.role == "prefill"
    assert endpoint_config.dp_rank == 2
    assert endpoint_config.node_rank == 1
    assert endpoint_config.d2d_peer_ips == "10.0.0.3"


def test_backend_builds_native_health_probe_from_engine_config():
    context = _context()
    tls_config = TLSConfig(enable_tls=True, ca_file="/certs/ca.crt")
    endpoint = _endpoint(engine_type="vllm", role="prefill", connector="MooncakeConnectorV1")
    endpoint.deploy_config.health_check_config = SimpleNamespace(
        health_collector_timeout=7,
        health_collector_timeout_retry_attempts=4,
        startup_timeout=900,
    )
    endpoint.deploy_config.infer_tls_config = tls_config

    probe = _prepare_with_config(VllmBackend(), context, endpoint).probe

    assert probe.path == "/health"
    assert probe.timeout_seconds == 7
    assert probe.max_attempts == 4
    assert probe.startup_timeout_seconds == 900
    assert probe.tls_config is tls_config
    assert probe.process_only is False


def test_headless_backend_uses_process_only_probe():
    context = _context(headless=True)
    endpoint = _endpoint(engine_type="vllm", role="prefill", connector="MooncakeConnectorV1")
    endpoint.deploy_config.health_check_config = SimpleNamespace(
        health_collector_timeout=5,
        health_collector_timeout_retry_attempts=3,
        startup_timeout=1800,
    )
    endpoint.deploy_config.infer_tls_config = None

    probe = _prepare_with_config(VllmBackend(), context, endpoint).probe

    assert probe.process_only is True
