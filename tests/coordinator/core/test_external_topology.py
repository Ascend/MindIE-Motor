# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.

from types import SimpleNamespace
from threading import Barrier
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from motor.common.resources.http_msg_spec import ExternalInsEventMsg
from motor.coordinator.api_server.management_server import ManagementServer, _is_standalone_instance_refresh


def _external_instance(**overrides):
    instance = {
        "id": 1,
        "role": "prefill",
        "endpoints": [{"id": 0, "address": "127.0.0.1:8100"}],
    }
    instance.update(overrides)
    return instance


def _external_message(**overrides):
    message = {
        "event": "set",
        "model_name": "test-model",
        "dispatch_capabilities": "concurrent_engine_sync",
        "engine_type": "vllm",
        "instances": [_external_instance()],
    }
    message.update(overrides)
    return message


def test_external_set_builds_internal_instance_defaults():
    message = ExternalInsEventMsg.model_validate(_external_message(model_name="TEST-MODEL"))

    internal = message.to_internal("test-model")

    instance = internal.instances[0]
    endpoint = instance.endpoints["127.0.0.1"][0]
    assert instance.job_name == "external-prefill-1"
    assert instance.model_name == "test-model"
    assert instance.dispatch_capabilities == ["concurrent_engine_sync"]
    assert instance.parallel_config.dp_size == 1
    assert endpoint.business_port == "8100"
    assert endpoint.status.value == "normal"


def test_external_omitted_fields_default_to_handoff_and_vllm():
    body = {
        "event": "set",
        "model_name": "test-model",
        "instances": [_external_instance()],
    }
    message = ExternalInsEventMsg.model_validate(body)

    internal = message.to_internal("test-model")

    instance = internal.instances[0]
    assert message.dispatch_capabilities.value == "prefill_handoff_decode"
    assert message.engine_type == "vllm"
    assert instance.dispatch_capabilities == ["prefill_handoff_decode"]
    assert instance.engine_type == "vllm"


def test_external_omitted_model_name_uses_resolved_engine_model():
    body = {
        "event": "set",
        "instances": [_external_instance()],
    }
    message = ExternalInsEventMsg.model_validate(body)

    internal = message.to_internal(resolved_model_name="Qwen3-8B")

    assert internal.instances[0].model_name == "Qwen3-8B"


def test_external_set_assigns_endpoint_ids_by_array_order():
    message = ExternalInsEventMsg.model_validate(
        _external_message(
            instances=[
                _external_instance(
                    endpoints=[
                        {"id": 0, "address": "127.0.0.1:8100"},
                        {"id": 1, "address": "127.0.0.2:8101"},
                    ]
                )
            ]
        )
    )

    internal = message.to_internal("test-model")

    instance = internal.instances[0]
    assert instance.parallel_config.dp_size == 2
    assert instance.endpoints["127.0.0.1"][0].id == 0
    assert instance.endpoints["127.0.0.2"][1].id == 1


def test_external_set_parses_ipv6_address():
    message = ExternalInsEventMsg.model_validate(
        _external_message(
            instances=[
                _external_instance(
                    endpoints=[{"id": 0, "address": "[2001:db8::1]:8200"}],
                )
            ]
        )
    )

    internal = message.to_internal("test-model")

    endpoint = internal.instances[0].endpoints["2001:db8::1"][0]
    assert endpoint.ip == "2001:db8::1"
    assert endpoint.business_port == "8200"


@pytest.mark.parametrize("event", ["add", "del"])
def test_external_incremental_event_is_preserved(event):
    message = ExternalInsEventMsg.model_validate(_external_message(event=event))

    internal = message.to_internal("test-model")

    assert internal.event.value == event
    assert internal.instances[0].id == 1


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (_external_message(event="pause"), "event='set', 'add', or 'del'"),
        (_external_message(instances=[_external_instance(role="union")]), "unsupported external instance role"),
        (_external_message(engine_type="sglang"), "supports engine_type='vllm'"),
        (
            _external_message(
                instances=[
                    _external_instance(),
                    _external_instance(),
                ]
            ),
            "duplicate instance id",
        ),
        (
            _external_message(
                instances=[
                    _external_instance(
                        endpoints=[
                            {"id": 0, "address": "127.0.0.1:8100"},
                            {"id": 1, "address": "127.0.0.1:8100"},
                        ]
                    )
                ]
            ),
            "duplicate endpoint address",
        ),
        (
            _external_message(
                instances=[
                    _external_instance(
                        endpoints=[
                            {"id": 0, "address": "127.0.0.1:8100"},
                            {"id": 0, "address": "127.0.0.2:8101"},
                        ]
                    )
                ]
            ),
            "duplicate endpoint id",
        ),
        (
            _external_message(
                instances=[
                    _external_instance(
                        endpoints=[{"id": 0, "address": "127.0.0.1:70000"}],
                    )
                ]
            ),
            "range 1-65535",
        ),
        (
            _external_message(
                instances=[
                    _external_instance(
                        endpoints=[{"id": 0, "address": "127.0.0.1"}],
                    )
                ]
            ),
            "must contain a port",
        ),
    ],
)
def test_external_event_rejects_invalid_topology(body, message):
    external = ExternalInsEventMsg.model_validate(body)

    with pytest.raises(ValueError, match=message):
        external.to_internal("test-model")


def test_external_schema_forbids_control_plane_fields():
    body = _external_message(instances=[_external_instance(job_name="controller-owned")])

    with pytest.raises(ValidationError, match="job_name"):
        ExternalInsEventMsg.model_validate(body)


def test_external_schema_forbids_non_routing_endpoint_fields():
    body = _external_message(
        instances=[
            _external_instance(
                endpoints=[
                    {
                        "id": 0,
                        "address": "127.0.0.1:8100",
                        "bootstrap_port": 9000,
                    }
                ]
            )
        ]
    )

    with pytest.raises(ValidationError, match="bootstrap_port"):
        ExternalInsEventMsg.model_validate(body)


def test_external_schema_rejects_legacy_endpoint_fields():
    body = _external_message(
        instances=[
            _external_instance(
                endpoints=[
                    {
                        "id": 0,
                        "ip": "127.0.0.1",
                        "business_port": "8100",
                    }
                ]
            )
        ]
    )

    with pytest.raises(ValidationError, match="address"):
        ExternalInsEventMsg.model_validate(body)


def test_external_schema_rejects_unknown_dispatch_capability():
    body = _external_message(dispatch_capabilities="unknown_dispatch")

    with pytest.raises(ValidationError, match="dispatch_capabilities"):
        ExternalInsEventMsg.model_validate(body)


def test_external_set_rejects_model_mismatch():
    external = ExternalInsEventMsg.model_validate(_external_message(model_name="another-model"))

    with pytest.raises(ValueError, match="does not match configured single model"):
        external.to_internal("test-model")


def test_is_standalone_detects_minimal_payload_without_top_level_markers():
    body = {
        "event": "set",
        "instances": [
            {
                "id": 1,
                "role": "prefill",
                "endpoints": [{"id": 0, "address": "127.0.0.1:8100"}],
            }
        ],
    }

    assert _is_standalone_instance_refresh(body) is True


def test_is_standalone_detects_top_level_markers():
    body = _external_message(model_name="test-model")

    assert _is_standalone_instance_refresh(body) is True


def test_is_standalone_rejects_controller_endpoint_dict():
    body = {
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

    assert _is_standalone_instance_refresh(body) is False


def test_is_standalone_rejects_mixed_controller_and_standalone_shapes():
    body = {
        "event": "set",
        "instances": [
            {
                "id": 1,
                "role": "prefill",
                "endpoints": [{"id": 0, "address": "127.0.0.1:8100"}],
            },
            {
                "job_name": "test-job",
                "model_name": "test-model",
                "id": 2,
                "role": "decode",
                "endpoints": {
                    "192.168.1.2": {
                        "0": {
                            "id": 0,
                            "ip": "192.168.1.2",
                            "business_port": "8200",
                        }
                    }
                },
            },
        ],
    }

    with pytest.raises(ValueError, match="mixed controller and coordinator-standalone"):
        _is_standalone_instance_refresh(body)


def _resolver():
    return SimpleNamespace(coordinator_config=SimpleNamespace(infer_tls_config=None))


def _multi_dp_message(**overrides):
    message = {
        "event": "set",
        "instances": [
            _external_instance(
                endpoints=[
                    {"id": 0, "address": "127.0.0.1:8100"},
                    {"id": 1, "address": "127.0.0.2:8100"},
                ]
            ),
            _external_instance(
                id=2,
                role="decode",
                endpoints=[{"id": 0, "address": "127.0.0.3:8200"}],
            ),
        ],
    }
    message.update(overrides)
    return ExternalInsEventMsg.model_validate(message)


def _model_ids_by_address(mapping):
    def query(address, _tls_config):
        result = mapping[address]
        if isinstance(result, Exception):
            raise result
        return result

    return query


def test_resolve_omitted_model_name_requires_all_dps_to_match():
    event_msg = _multi_dp_message()

    with patch(
        "motor.coordinator.api_server.management_server.NativeEngineApiClient.query_model_ids",
        side_effect=_model_ids_by_address(
            {
                "127.0.0.1:8100": ["Qwen3-8B"],
                "127.0.0.2:8100": ["Qwen3-8B"],
                "127.0.0.3:8200": ["wrong-model"],
            }
        ),
    ):
        with pytest.raises(ValueError, match="does not match previously probed"):
            ManagementServer._resolve_external_model_name(_resolver(), event_msg)


def test_resolve_omitted_model_name_uses_shared_single_model():
    event_msg = _multi_dp_message()

    with patch(
        "motor.coordinator.api_server.management_server.NativeEngineApiClient.query_model_ids",
        side_effect=_model_ids_by_address(
            {
                "127.0.0.1:8100": ["Qwen3-8B"],
                "127.0.0.2:8100": ["qwen3-8b"],
                "127.0.0.3:8200": ["Qwen3-8B"],
            }
        ),
    ) as query_model_ids:
        resolved, rejected_indexes = ManagementServer._resolve_external_model_name(_resolver(), event_msg)

    assert resolved == "Qwen3-8B"
    assert rejected_indexes == set()
    assert query_model_ids.call_count == 3


def test_resolve_provided_model_name_rejects_wrong_later_dp():
    event_msg = _multi_dp_message(event="add", model_name="Qwen3-8B")

    with patch(
        "motor.coordinator.api_server.management_server.NativeEngineApiClient.query_model_ids",
        side_effect=_model_ids_by_address(
            {
                "127.0.0.1:8100": ["Qwen3-8B"],
                "127.0.0.2:8100": ["other-model"],
                "127.0.0.3:8200": ["Qwen3-8B"],
            }
        ),
    ):
        with pytest.raises(ValueError, match="expected 'Qwen3-8B'"):
            ManagementServer._resolve_external_model_name(_resolver(), event_msg)


def test_resolve_provided_model_name_accepts_all_matching_dps():
    event_msg = _multi_dp_message(event="add", model_name="Qwen3-8B")

    with patch(
        "motor.coordinator.api_server.management_server.NativeEngineApiClient.query_model_ids",
        side_effect=_model_ids_by_address(
            {
                "127.0.0.1:8100": ["Qwen3-8B"],
                "127.0.0.2:8100": ["qwen3-8b"],
                "127.0.0.3:8200": ["Qwen3-8B", "extra-model"],
            }
        ),
    ) as query_model_ids:
        resolved, rejected_indexes = ManagementServer._resolve_external_model_name(_resolver(), event_msg)

    assert resolved == ""
    assert rejected_indexes == set()
    assert query_model_ids.call_count == 3


def test_resolve_del_with_provided_model_name_skips_probe():
    event_msg = _multi_dp_message(event="del", model_name="Qwen3-8B")

    with patch(
        "motor.coordinator.api_server.management_server.NativeEngineApiClient.query_model_ids",
    ) as query_model_ids:
        resolved, rejected_indexes = ManagementServer._resolve_external_model_name(_resolver(), event_msg)

    assert resolved == ""
    assert rejected_indexes == set()
    query_model_ids.assert_not_called()


def test_resolve_set_probes_dps_concurrently():
    event_msg = _multi_dp_message()
    all_workers_started = Barrier(3, timeout=1)

    def query_model_ids(_address, _tls_config):
        all_workers_started.wait()
        return ["Qwen3-8B"]

    with patch(
        "motor.coordinator.api_server.management_server.NativeEngineApiClient.query_model_ids",
        side_effect=query_model_ids,
    ):
        resolved, rejected_indexes = ManagementServer._resolve_external_model_name(_resolver(), event_msg)

    assert resolved == "Qwen3-8B"
    assert rejected_indexes == set()


@pytest.mark.parametrize("event", ["set", "add"])
def test_resolve_rejects_only_instance_with_unreachable_dp(event):
    event_msg = _multi_dp_message(event=event)

    with patch(
        "motor.coordinator.api_server.management_server.NativeEngineApiClient.query_model_ids",
        side_effect=_model_ids_by_address(
            {
                "127.0.0.1:8100": ["Qwen3-8B"],
                "127.0.0.2:8100": OSError("connection refused"),
                "127.0.0.3:8200": ["Qwen3-8B"],
            }
        ),
    ):
        resolved, rejected_indexes = ManagementServer._resolve_external_model_name(_resolver(), event_msg)

    assert resolved == "Qwen3-8B"
    assert rejected_indexes == {0}


def test_resolve_fails_when_every_instance_has_unreachable_dp():
    event_msg = _multi_dp_message()

    with patch(
        "motor.coordinator.api_server.management_server.NativeEngineApiClient.query_model_ids",
        side_effect=_model_ids_by_address(
            {
                "127.0.0.1:8100": ["Qwen3-8B"],
                "127.0.0.2:8100": OSError("connection refused"),
                "127.0.0.3:8200": OSError("connection refused"),
            }
        ),
    ):
        with pytest.raises(ValueError, match="no fully reachable instances remain"):
            ManagementServer._resolve_external_model_name(_resolver(), event_msg)
